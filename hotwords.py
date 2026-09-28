# -*- coding: utf-8 -*-
"""热词定制：提升专有名词（人名/产品名/术语）的转写准确率

词表: 项目根目录 hotwords.txt，一行一个热词，# 开头为注释。文件存在且有条目即启用。

两层校正（按顺序应用）:
  1. 本地拼音模糊校正(离线、默认生效): 文本中等长汉字片段与热词的
     无声调拼音完全一致(2-3字)或仅差一个音节(≥4字)时，替换为热词。
  2. LLM 语义校正(配置 qwen 接口后自动启用): 交由大模型按热词表纠错，
     覆盖拼音校正照顾不到的情况(同音不同字数、方言口音等)。

注: FunASR 的 SeACo 热词模型无公开 onnx 版(本机 torch 被智能应用控制拦截
无法自行导出)，故用上述后处理方案达到同样目的。
"""
import difflib
import os

from pypinyin import lazy_pinyin

_ROOT = os.path.dirname(os.path.abspath(__file__))
HOTWORDS_PATH = os.path.join(_ROOT, "hotwords.txt")
_PUNC = "，。！？、；：…—·“”‘’（）()[]{}《》<> \t\n"
_PY_CACHE = {}


def load_hotwords():
    """读词表，返回热词列表；无文件/无条目返回空"""
    if not os.path.exists(HOTWORDS_PATH):
        return []
    words = []
    with open(HOTWORDS_PATH, encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w and not w.startswith("#") and w not in words:
                words.append(w)
    return words


def _py(text):
    """无声调拼音串（带缓存）"""
    if text not in _PY_CACHE:
        _PY_CACHE[text] = " ".join(lazy_pinyin(text))
    return _PY_CACHE[text]


def _is_hanzi(s):
    return s and all("\u4e00" <= c <= "\u9fff" for c in s)


def _syllable_diff(a, b):
    """按音节比较差异个数（"di ting" vs "di ting" → 0）"""
    sa, sb = a.split(), b.split()
    if len(sa) != len(sb):
        return 99
    return sum(1 for x, y in zip(sa, sb) if x != y)


def _letter_match(seg, syllables):
    """seg 含一段连续 ascii 字母、其余为汉字：字母段 = 对应音节(或其首字母)，
    其余汉字音节全对 —— 处理 ASR 把某个字识别成拼音字母的情况（谛听→d听/di听）"""
    runs = [(i, c) for i, c in enumerate(seg) if c.isascii() and c.isalpha()]
    if not runs:
        return False
    first_i, last_i = runs[0][0], runs[-1][0]
    if not all(seg[i].isascii() and seg[i].isalpha() for i in range(first_i, last_i + 1)):
        return False
    hanzi = seg[:first_i] + seg[last_i + 1 :]
    if not _is_hanzi(hanzi) or len(hanzi) + 1 != len(syllables):
        return False
    k = first_i  # 字母段占据的音节下标 = 其前的汉字个数
    token = seg[first_i : last_i + 1].lower()
    expect = " ".join(syllables[:k] + syllables[k + 1 :])
    if " ".join(lazy_pinyin(hanzi)) != expect:
        return False
    return token == syllables[k] or (len(token) == 1 and token == syllables[k][0])


def local_correct(text, hotwords):
    """对文本做拼音模糊校正，返回 (新文本, [(错词, 热词), ...])"""
    if not text or not hotwords:
        return text, []
    # 预排序: 长词优先，避免短词先替换挡住长词
    hotwords = sorted(hotwords, key=len, reverse=True)
    py_cache = {w: _py(w) for w in hotwords}
    syl_cache = {w: lazy_pinyin(w) for w in hotwords}
    out = []
    replacements = []
    i, n = 0, len(text)
    while i < n:
        matched = False
        for w in hotwords:
            wl = len(w)
            # 窗口长度 = 词长（全汉字/单字母替代）或 +1（多字母替代，如 di听）
            for L in (wl, wl + 1):
                seg = text[i : i + L]
                if L < 2 or len(seg) < L or seg == w:
                    continue
                if _is_hanzi(seg):
                    if L != wl:
                        continue
                    seg_py = _py(seg)
                    ok = seg_py == py_cache[w] or (
                        wl >= 4 and _syllable_diff(seg_py, py_cache[w]) <= 1
                    )
                else:
                    ok = _letter_match(seg, syl_cache[w])
                if ok:
                    out.append(w)
                    replacements.append((seg, w))
                    i += L
                    matched = True
                    break
            if matched:
                break
        if not matched:
            out.append(text[i])
            i += 1
    return "".join(out), replacements


def llm_correct(text, hotwords):
    """LLM 按热词表纠错；未配置接口或失败返回 None（保留原文本）"""
    if not text or not hotwords:
        return None
    try:
        import summarize

        cfg = summarize.load_config()
        if not cfg.get("api_base"):
            return None
        system = (
            "你是转写校对助手。只把转写文本中与热词表读音相近但写错的词，"
            "替换为热词表中正确的写法；不要改动其他任何内容，不要增删字句，"
            "不要添加标点或解释。直接输出纠正后的完整文本。"
        )
        user = f"热词表：{'、'.join(hotwords)}\n\n转写文本：\n{text}"
        return summarize.chat(system, user, cfg)
    except Exception as e:
        print(f"[热词LLM校正失败，保留原文] {e}")
        return None


if __name__ == "__main__":
    # 自测: python hotwords.py
    demo = "帝听是一个本地会议记录工具，帝听的实时字幕很好用。"
    fixed, reps = local_correct(demo, ["谛听"])
    print("原文:", demo)
    print("校正:", fixed)
    print("替换:", reps)
