# -*- coding: utf-8 -*-
"""本地转写（CPU 即可，无需 torch）：FunASR onnx 组件

  Paraformer-large  中文离线大模型（int8 量化）
  FSMN-VAD          语音活动检测，把长音频切成句
  CT-Transformer    标点恢复

用法:
  python transcribe.py <音频文件或recordings目录>
  或作为模块: from transcribe import transcribe_pair
"""
import glob
import os
import sys
import time

import librosa
import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
ASR_MODEL = os.path.join(_ROOT, "models", "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx")
VAD_MODEL = os.path.join(_ROOT, "models", "speech_fsmn_vad_zh-cn-16k-common-onnx")
PUNC_MODEL = os.path.join(_ROOT, "models", "punc_ct-transformer_cn-en-common-vocab471067-large-onnx")

_MODELS = None
MIN_SEG_MS = 200      # 短于 200ms 的切分丢弃
GAP_MS = 700          # 段间隔小于 700ms 合并为同一句话
MAX_BLOCK_MS = 30000  # 单个时间戳块最长 30s


def _fmt_ts(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def get_models():
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    from funasr_onnx import CT_Transformer, Fsmn_vad, Paraformer

    print("加载模型中（首次运行自动从 ModelScope 下载约 1.2GB）...")
    t0 = time.time()
    asr = Paraformer(model_dir=ASR_MODEL, quantize=True, batch_size=1, device_id="-1")
    vad = Fsmn_vad(model_dir=VAD_MODEL, quantize=True, device_id="-1")
    punc = CT_Transformer(model_dir=PUNC_MODEL, quantize=True, device_id="-1")
    print(f"模型就绪 ({time.time() - t0:.0f}s)")
    _MODELS = (asr, vad, punc)
    return _MODELS


def _flatten_segments(x, out):
    """VAD 输出可能是 [[b,e],...] 或更深嵌套，统一展开成 (beg_ms, end_ms) 列表"""
    if isinstance(x, str):
        return
    if (
        isinstance(x, (list, tuple))
        and len(x) == 2
        and all(isinstance(v, (int, float)) for v in x)
    ):
        out.append((float(x[0]), float(x[1])))
        return
    if isinstance(x, (list, tuple)):
        for y in x:
            _flatten_segments(y, out)


def _extract_text(res):
    """兼容 funasr_onnx 不同模型的返回结构，取出纯文本
    ASR: [{'preds': (text, [chars])}]  VAD: 段落  标点: 字符串或 list"""
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        if "text" in res:
            return res["text"]
        if "preds" in res:
            return _extract_text(res["preds"])
        return ""
    if isinstance(res, (list, tuple)) and res:
        return _extract_text(res[0])
    return str(res) if res is not None else ""


def _group_segments(segments):
    """把 VAD 段落合并成适合逐条输出的语句块：间隔近的合一段，块长有上限"""
    blocks = []
    cur_b, cur_e = segments[0]
    for b, e in segments[1:]:
        if b - cur_e <= GAP_MS and e - cur_b <= MAX_BLOCK_MS:
            cur_e = e
        else:
            blocks.append((cur_b, cur_e))
            cur_b, cur_e = b, e
    blocks.append((cur_b, cur_e))
    return blocks


def transcribe_file(models, path, progress=None):
    """转写单个音频，返回带时间戳的语句块 [(beg_ms, end_ms, text), ...]
    流程：librosa 统一加载(支持wav/mp3/flac等) → VAD 切句 → 逐块识别 → 逐块加标点"""
    asr, vad, punc = models
    t0 = time.time()
    speech, _ = librosa.load(path, sr=16000, mono=True)
    segments = []
    _flatten_segments(vad(audio_in=speech), segments)
    segments = [(b, e) for b, e in segments if e - b >= MIN_SEG_MS]
    if not segments:
        print(f"  未检测到语音: {os.path.basename(path)}")
        if progress:
            progress(f"{os.path.basename(path)}: 未检测到语音")
        return []

    blocks = []
    total = _group_segments(segments)
    for i, (beg_ms, end_ms) in enumerate(total, 1):
        chunk = speech[int(beg_ms / 1000 * 16000) : int(end_ms / 1000 * 16000)]
        if len(chunk) < 1600:
            continue
        try:
            text = _extract_text(asr(chunk)).strip()
        except Exception as e:
            print(f"  [跳过一段识别失败] {e}")
            continue
        if not text:
            continue
        try:
            text = _extract_text(punc(text)).strip() or text
        except Exception as e:
            print(f"  [标点恢复失败，用原文] {e}")
        blocks.append((beg_ms, end_ms, text))
        msg = f"[{i}/{len(total)}] {_fmt_ts(beg_ms)} {text[:30]}{'...' if len(text) > 30 else ''}"
        print(f"  {msg}")
        if progress:
            progress(msg)
    print(f"  {os.path.basename(path)}: {len(blocks)} 块, 耗时 {time.time() - t0:.0f}s")
    return blocks


def _apply_hotwords(blocks, hotwords):
    """对转写块应用热词校正：本地拼音校正 + LLM 校正（配置接口时）"""
    import hotwords as _hw

    fixed_blocks = []
    total = []
    for beg_ms, end_ms, text in blocks:
        fixed, reps = _hw.local_correct(text, hotwords)
        total.extend(reps)
        fixed_llm = _hw.llm_correct(fixed, hotwords)
        if fixed_llm:
            fixed = fixed_llm.strip()
        fixed_blocks.append((beg_ms, end_ms, fixed))
    if total:
        uniq = "、".join(f"{a}→{b}" for a, b in dict(total).items())
        print(f"  热词校正: {uniq}")
    return fixed_blocks


def transcribe_pair(loopback_wav, mic_wav=None, summarize=True, progress=None):
    """转写双轨录音 → 带时间戳的 Markdown 文稿 → 热词校正 → 生成会议纪要；返回 md 路径"""
    models = get_models()
    import hotwords as _hw

    hotwords = _hw.load_hotwords()
    if hotwords:
        print(f"热词: {len(hotwords)} 个（{'、'.join(hotwords[:5])}{'...' if len(hotwords) > 5 else ''}）")
        if progress:
            progress(f"热词: {len(hotwords)} 个")
    sections = []

    for wav, meeting_title in (
        (loopback_wav, "对方 / 会议内容（系统声音）"),
        (mic_wav, "我（麦克风）"),
    ):
        if not wav or not os.path.exists(wav):
            continue
        stem = os.path.splitext(os.path.basename(wav))[0]
        title = meeting_title if stem.endswith(("_loopback", "_mic")) else "转写内容"
        print(f"转写: {os.path.basename(wav)} ...")
        if progress:
            progress(f"转写 {title} ...")
        blocks = transcribe_file(models, wav, progress=progress)
        if hotwords and blocks:
            blocks = _apply_hotwords(blocks, hotwords)
        if blocks:
            sections.append((title, blocks))

    stem = os.path.splitext(os.path.basename(loopback_wav))[0]
    base = stem.rsplit("_loopback", 1)[0] if stem.endswith("_loopback") else stem
    md_path = os.path.join(os.path.dirname(os.path.abspath(loopback_wav)), f"{base}_transcript.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 会议转写 {base}\n\n")
        for title, blocks in sections:
            f.write(f"## {title}\n\n")
            for beg_ms, _, text in blocks:
                f.write(f"[{_fmt_ts(beg_ms)}] {text}\n\n")

    # 自动生成精确时间轴的 SRT 字幕（双轨合并按时间排序），剪辑软件可直接使用
    try:
        import export as _export
        all_blocks = sorted((b for _, blks in sections for b in blks), key=lambda b: b[0])
        srt_path = os.path.splitext(md_path)[0] + ".srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(_export.blocks_to_srt(all_blocks))
    except Exception as e:
        print(f"[SRT 生成失败，不影响文稿] {e}")
    if summarize:
        if progress:
            progress("生成会议纪要...")
        try:
            import summarize
            summarize.append_minutes(md_path)
        except Exception as e:
            print(f"[纪要生成失败，不影响文稿] {e}")
    if progress:
        progress(f"完成: {os.path.basename(md_path)}")
    return md_path


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    target = sys.argv[1]
    if os.path.isdir(target):
        pairs = {}
        for wav in sorted(glob.glob(os.path.join(target, "*_loopback.wav"))):
            base = os.path.basename(wav).rsplit("_loopback", 1)[0]
            mic = os.path.join(target, f"{base}_mic.wav")
            pairs[base] = (wav, mic if os.path.exists(mic) else None)
        if not pairs:
            sys.exit(f"{target} 下没有 *_loopback.wav")
        for base, (lb, mic) in pairs.items():
            print(f"\n=== {base} ===")
            transcribe_pair(lb, mic)
    else:
        transcribe_pair(target, None)


if __name__ == "__main__":
    main()
