# -*- coding: utf-8 -*-
"""双说话人分离验证：真人配音(3.mp3) + 系统语音(asr_example) 交替拼接 → 声纹聚类校验"""
import sys

sys.path.insert(0, ".")

import librosa
import numpy as np
import soundfile as sf

N = 5  # 每个说话人段数


def main():
    from transcribe import get_models, _flatten_segments
    import diarize

    asr, vad, punc = get_models()

    # 说话人0：真人配音（VAD 切段取前 N 段）
    full_a, _ = librosa.load("E:/钝太狼/如何成为三条龙/配音/3.mp3", sr=16000, mono=True)
    segs = []
    _flatten_segments(vad(audio_in=full_a), segs)
    segs = [(b, e) for b, e in segs if e - b >= 1500][:N]
    a = [full_a[int(b * 16): int(e * 16)] for b, e in segs]

    # 说话人1：系统语音，固定等分切片
    full_b, _ = librosa.load("testdata/asr_example_zh.wav", sr=16000, mono=True)
    step = len(full_b) // N
    b = [full_b[i * step: (i + 1) * step] for i in range(N)]
    use = min(len(a), len(b))  # 实际可用段数
    a, b = a[:use], b[:use]
    print(f"说话人0 段数: {len(a)}, 说话人1 段数: {len(b)}")

    parts, truth = [], []
    for i in range(use):
        parts.append(a[i]); truth.append(0)
        parts.append(np.zeros(4800, dtype=np.float32))
        parts.append(b[i]); truth.append(1)
        parts.append(np.zeros(14400, dtype=np.float32))  # 0.9s 停顿，避免 VAD 跨说话人合并
    wav = np.concatenate(parts)
    sf.write("testdata/two_speakers.wav", wav, 16000)
    print(f"合成: {len(wav)/16000:.1f}s, 段真值: {truth}")

    segs2 = []
    _flatten_segments(vad(audio_in="testdata/two_speakers.wav"), segs2)
    segs2 = [(x, y) for x, y in segs2 if y - x >= 800]
    print(f"重切 VAD 段数: {len(segs2)}")

    speech, _ = librosa.load("testdata/two_speakers.wav", sr=16000, mono=True)
    vectors, keep = [], []
    for beg, end in segs2:
        chunk = speech[int(beg * 16): int(end * 16)]
        v = diarize.embed(chunk)
        if v is not None:
            vectors.append(v)
            keep.append((beg, end))

    # 拼接段的真实时间范围（用于给重切段定真值）
    spans, t = [], 0.0
    for i, p in enumerate(parts):
        dur = len(p) / 16000
        if i % 4 == 0:
            spans.append((t * 1000, (t + dur) * 1000, 0))
        elif i % 4 == 2:
            spans.append((t * 1000, (t + dur) * 1000, 1))
        t += dur
    truth_of = []
    for beg, end in keep:
        best, bestov = 0, 0
        for s, e, sp in spans:
            ov = min(end, e) - max(beg, s)
            if ov > bestov:
                bestov, best = ov, sp
        truth_of.append(best)

    V = np.array(vectors)
    S = V @ V.T
    same = [S[i][j] for i in range(len(S)) for j in range(i + 1, len(S)) if truth_of[i] == truth_of[j]]
    diff = [S[i][j] for i in range(len(S)) for j in range(i + 1, len(S)) if truth_of[i] != truth_of[j]]
    if same:
        print(f"同类相似度: mean={np.mean(same):.3f} min={np.min(same):.3f}")
    if diff:
        print(f"异类相似度: mean={np.mean(diff):.3f} max={np.max(diff):.3f}")

    labels = diarize._cluster(vectors, 0.4)
    n = len(labels)
    good = sum(1 for i in range(n) for j in range(i + 1, n)
               if (labels[i] == labels[j]) == (truth_of[i] == truth_of[j]))
    total = n * (n - 1) // 2
    print(f"簇号: {labels}")
    print(f"真值 : {truth_of}")
    print(f"成对准确率: {good}/{total} = {good / total * 100:.0f}%" if total else "n/a")


if __name__ == "__main__":
    main()
