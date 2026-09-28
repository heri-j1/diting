# -*- coding: utf-8 -*-
"""直接从 modelscope.cn HTTP API 下载模型（绕开 modelscope python 包的 torch 依赖）"""
import json
import os
import sys
import time

import requests

BASE = "https://modelscope.cn/api/v1/models"
REPOS = [
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx",
    "iic/speech_fsmn_vad_zh-cn-16k-common-onnx",
    "iic/punc_ct-transformer_cn-en-common-vocab471067-large-onnx",
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx",
]
OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def list_files(repo):
    r = requests.get(f"{BASE}/{repo}/repo/files", params={"Revision": "master"}, timeout=60)
    r.raise_for_status()
    files = []
    for f in r.json()["Data"]["Files"]:
        if f.get("Type") == "blob":
            fpath = f.get("Path") or f["Name"]  # Path 已是完整路径
            files.append((fpath, f.get("Size", 0)))
    return files


def download(repo, fpath, size, dest):
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        print(f"  SKIP {fpath}", flush=True)
        return True
    url = f"{BASE}/{repo}/repo"
    for attempt in (1, 2, 3):
        try:
            t0 = time.time()
            with requests.get(url, params={"Revision": "master", "FilePath": fpath}, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            os.replace(tmp, dest)
            dt = max(time.time() - t0, 0.1)
            print(f"  OK   {fpath} ({size / 1048576:.1f}MB, {size / 1048576 / dt:.1f}MB/s)", flush=True)
            return True
        except Exception as e:
            print(f"  RETRY{attempt} {fpath}: {e}", flush=True)
            time.sleep(5)
    return False


def main():
    ok_all = True
    for repo in REPOS:
        name = repo.split("/")[-1]
        out_dir = os.path.join(OUT_ROOT, name)
        os.makedirs(out_dir, exist_ok=True)
        print(f"== {repo}", flush=True)
        files = list_files(repo)
        for fpath, size in files:
            if not download(repo, fpath, size, os.path.join(out_dir, fpath.replace("/", os.sep))):
                ok_all = False
    print("DOWNLOAD-DONE" if ok_all else "DOWNLOAD-FAILED", flush=True)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
