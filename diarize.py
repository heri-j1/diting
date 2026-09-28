# -*- coding: utf-8 -*-
"""说话人分离：VAD 切段 → CAM++ 声纹(onnx) → 层次聚类 → 按时间段贴"说话人N"

只用于离线精转阶段（对"对方轨"），麦克风轨固定为"我"。
模型: models/campplus_sv.onnx (192 维声纹, 3D-Speaker CAM++, 16k)
配置(config.json): diarize=true 启用; diarize_threshold 余弦距离阈值(默认 0.4, 越小越容易合并);
                   num_speakers=数字 则强制人数, 缺省自动。
"""
import os

import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_ROOT, "models", "campplus_sv.onnx")

_sessions = {}


def _get_models():
    if "embed" in _sessions:
        return _sessions["embed"], _sessions["vad"]
    import onnxruntime as ort
    from funasr_onnx import Fsmn_vad
    from transcribe import VAD_MODEL

    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    _sessions["embed"] = ort.InferenceSession(MODEL_PATH, so, providers=["CPUExecutionProvider"])
    _sessions["vad"] = Fsmn_vad(model_dir=os.path.join(_ROOT, "models",
                                "speech_fsmn_vad_zh-cn-16k-common-onnx"), quantize=True, device_id="-1")
    return _sessions["embed"], _sessions["vad"]


def _fbank(speech: np.ndarray) -> np.ndarray:
    """16k float32 → [T, 80] log-mel fbank（CAM++ 训练口径: 25ms/10ms, 80 mel, 无 dither）"""
    import kaldi_native_fbank as knf

    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = 16000
    opts.frame_opts.frame_length_ms = 25.0
    opts.frame_opts.frame_shift_ms = 10.0
    opts.frame_opts.dither = 0.0
    opts.frame_opts.preemph_coeff = 0.97
    opts.mel_opts.num_bins = 80
    fbank = knf.OnlineFbank(opts)
    fbank.accept_waveform(16000, speech.copy())
    frames = fbank.num_frames_ready
    return np.array([fbank.get_frame(i) for i in range(frames)], dtype=np.float32)


def embed(speech: np.ndarray) -> np.ndarray:
    """单段语音 → L2 归一化的 192 维声纹"""
    embed_sess, _ = _get_models()
    feats = _fbank(speech)
    if feats.shape[0] < 10:  # <100ms 特征帧太少，不可靠
        return None
    out = embed_sess.run(None, {"x": feats[None, :, :]})[0][0]
    v = np.asarray(out, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def _flatten_vad(x, out):
    if isinstance(x, str):
        return
    if (isinstance(x, (list, tuple)) and len(x) == 2
            and all(isinstance(v, (int, float)) for v in x)):
        out.append((float(x[0]), float(x[1])))
        return
    if isinstance(x, (list, tuple)):
        for y in x:
            _flatten_vad(y, out)


def _cluster(vectors, threshold, num_speakers=None):
    """层次聚类(余弦)。num_speakers 给定则强制人数，否则按距离阈值切。返回 [簇号...]"""
    from sklearn.cluster import AgglomerativeClustering

    if num_speakers and num_speakers >= 1:
        model = AgglomerativeClustering(n_clusters=int(num_speakers), metric="cosine", linkage="average")
    else:
        model = AgglomerativeClustering(n_clusters=None, distance_threshold=threshold,
                                        metric="cosine", linkage="average")
    return model.fit_predict(np.array(vectors)).tolist()


def label_track(speech_16k: np.ndarray, blocks, cfg):
    """给一条音轨的转写块贴说话人标签。

    speech_16k: librosa 加载的 16k float32 全轨音频
    blocks:     [(beg_ms, end_ms, text), ...]
    返回:       [(beg_ms, end_ms, text, spk_id), ...]  spk_id 按首次出现顺序从 1 起
    """
    if not blocks:
        return [(b, e, t, 1) for b, e, t in blocks]
    embed_fn, vad = _get_models()

    segs = []
    from transcribe import _flatten_segments as _fl  # 复用健壮展开
    raw = vad(audio_in=speech_16k)
    _fl(raw, segs)
    segs = [(b, e) for b, e in segs if e - b >= 300][:2000]  # ≥300ms，上限 2000 段
    if not segs:
        return [(b, e, t, 1) for b, e, t in blocks]

    vectors, keep = [], []
    for b, e in segs:
        if e - b < 600:  # <600ms 的碎片不参与声纹（暂停边界残渣）
            continue
        chunk = speech_16k[int(b / 1000 * 16000): int(e / 1000 * 16000)]
        if len(chunk) < 4800:  # <300ms
            continue
        v = embed(chunk)
        if v is not None:
            vectors.append(v)
            keep.append((b, e))
    if len(vectors) < 2:  # 语音太少，不分离
        return [(b, e, t, 1) for b, e, t in blocks]

    threshold = float(cfg.get("diarize_threshold", 0.4))
    num = cfg.get("num_speakers")
    labels = _cluster(vectors, threshold, int(num) if num else None)

    # 单段簇 → 就近合并（避免停顿碎片自成一个"说话人"）
    from collections import Counter
    counts = Counter(labels)
    for i, lb in enumerate(labels):
        if counts[lb] == 1:
            dists = [(1 - float(np.dot(vectors[i], vectors[j])), labels[j])
                     for j in range(len(labels)) if j != i and counts[labels[j]] > 1]
            if dists:
                d, tgt = min(dists)
                if d <= max(threshold + 0.15, 0.55):
                    labels[i] = tgt

    # 段→簇 的发声占比；块→按时间重叠最大的簇归属
    def seg_speaker(beg_ms, end_ms):
        scores = {}
        for (b, e), lb in zip(keep, labels):
            ov = min(end_ms, e) - max(beg_ms, b)
            if ov > 0:
                scores[lb] = scores.get(lb, 0) + ov
        return max(scores, key=scores.get) if scores else None

    order, mapping = [], {}
    out = []
    for beg, end, text in blocks:
        lb = seg_speaker(beg, end)
        if lb is None:
            lb = labels[0]
        if lb not in mapping:
            mapping[lb] = len(order) + 1
            order.append(lb)
        out.append((beg, end, text, mapping[lb]))
    return out
