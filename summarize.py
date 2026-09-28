# -*- coding: utf-8 -*-
"""会议纪要生成：调用 qwen（OpenAI 兼容接口）

配置 config.json（项目根目录）：
  {
    "api_base": "https://xx/v1",   # OpenAI 兼容地址；留空 = 模拟模式
    "api_key":  "sk-...",
    "model":    "qwen-plus",
    "temperature": 0.3
  }

用法:
  python summarize.py recordings/xxx_transcript.md   # 单独为文稿生成纪要
  或由 transcribe.py 自动调用（转写完自动追加纪要）
"""
import json
import os
import re
import sys

import requests

_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_ROOT, "config.json")

DEFAULT_CONFIG = {
    "api_base": "",     # 留空走模拟模式
    "api_key": "",
    "model": "qwen-plus",
    "api_format": "openai",  # openai(/v1/chat/completions) | anthropic(/v1/messages)
    "temperature": 0.3,
    "max_chars": 12000,  # 送入模型的转写字数上限
    "max_tokens": 4096,  # 回复长度上限（Anthropic 格式必填）
}

SYSTEM_PROMPT = (
    "你是专业的会议纪要助手。根据给定的会议转写文本，输出 Markdown 格式的会议纪要，"
    "依次包含以下小节：\n"
    "## 会议主题（一句话概括）\n"
    "## 会议摘要（3-5 句）\n"
    "## 关键讨论点（分条列出，每条注明发言内容和出处时间戳，如 [03:12]；"
    "若行首有 [说话人N] 标签，请在讨论点中注明是哪位说话人）\n"
    "## 结论（达成的共识或决定）\n"
    "## 待办事项（用 - [ ] 列表，尽量注明负责人和时间）\n"
    "只依据转写内容归纳，不要编造转写中不存在的信息；内容确实缺失的小节写“（转写中未体现）”。"
)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[纪要] config.json 解析失败，使用默认配置: {e}")
    return cfg


def _clean_body(md_text):
    """去掉文稿的标题行，保留 [mm:ss] 时间戳（便于纪要引用出处）"""
    lines = md_text.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def chat(system, user, cfg):
    """按 api_format 调用大模型接口（OpenAI 兼容 / Anthropic Messages），返回回复文本"""
    fmt = (cfg.get("api_format") or "openai").lower()
    if fmt == "anthropic":
        url = cfg["api_base"].rstrip("/") + "/messages"
        headers = {
            "x-api-key": cfg.get("api_key", ""),
            # 部分网关（如 new-api）按 Bearer 鉴权，双头兼容标准 Anthropic API
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cfg["model"],
            "max_tokens": int(cfg.get("max_tokens", 4096)),
            "temperature": cfg.get("temperature", 0.3),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts).strip()

    # OpenAI 兼容（默认）
    url = cfg["api_base"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    payload = {
        "model": cfg["model"],
        "temperature": cfg.get("temperature", 0.3),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _call_qwen(transcript, cfg):
    return chat(SYSTEM_PROMPT, f"会议转写文本如下：\n\n{transcript}", cfg)


def _mock_minutes(transcript):
    """未配置接口时的模拟纪要：用简单启发式拼一个格式示例，便于预览"""
    text = re.sub(r"^\[\d{2}:\d{2}\]\s*", "", transcript, flags=re.M)
    text = re.sub(r"^#+ .*$", "", text, flags=re.M)
    sents = [s.strip() for s in re.split(r"[。！？\n]", text) if len(s.strip()) >= 6]
    key = sents[:5] or ["（转写内容为空）"]

    lines = [
        "## 会议主题",
        "（模拟）本次会议围绕转写内容所述事项进行了讨论。",
        "",
        "## 会议摘要",
        "（模拟）以下为按转写顺序提取的前几条内容，仅用于预览纪要格式：",
    ]
    lines += [f"- 第{i + 1}条：{s}" for i, s in enumerate(key)]
    lines += [
        "",
        "## 关键讨论点",
        "（模拟）待接入真实 qwen 接口后自动归纳。",
        "",
        "## 结论",
        "（转写中未体现）",
        "",
        "## 待办事项",
        "- [ ] （模拟）配置 config.json 的 api_base 后，此处将生成真实待办清单。",
    ]
    return "\n".join(lines)


def generate_minutes(transcript_md):
    """输入带时间戳的文稿文本，返回纪要 Markdown；失败返回 None"""
    cfg = load_config()
    body = _clean_body(transcript_md)
    if not body:
        print("[纪要] 文稿为空，跳过")
        return None
    max_chars = int(cfg.get("max_chars", 12000))
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n（注：转写过长，已截断至前 {max_chars} 字）"

    if not cfg.get("api_base"):
        print("[纪要] 未配置 api_base，输出模拟纪要（config.json 填入地址后自动调用真实接口）")
        return _mock_minutes(body)
    print(f"[纪要] 调用 {cfg['model']} @ {cfg['api_base']} ...")
    try:
        return _call_qwen(body, cfg)
    except Exception as e:
        print(f"[纪要] 接口调用失败: {e}")
        return None


def append_minutes(md_path):
    """为文稿追加会议纪要（写入同一 md 文件）"""
    with open(md_path, encoding="utf-8") as f:
        content = f.read()
    minutes = generate_minutes(content)
    if not minutes:
        return False
    if "## 会议主题" in content.split("---")[-1]:  # 已有纪要，避免重复追加
        return True
    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\n---\n\n# 会议纪要\n\n" + minutes + "\n")
    print(f"[纪要] 已追加到 {md_path}")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if not append_minutes(sys.argv[1]):
        sys.exit(1)


if __name__ == "__main__":
    main()
