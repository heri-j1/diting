# -*- coding: utf-8 -*-
"""谛听 Web UI：FastAPI 后端 + WebSocket 实时字幕 + 浏览器界面

启动: .venv/Scripts/python webapp.py   → 自动打开浏览器 http://127.0.0.1:8321
复用现有模块: capture(采集) / live_subtitles(实时字幕) / transcribe(精转) /
             hotwords(热词) / summarize(纪要)。本文件只做会话编排与通信。
"""
import asyncio
import datetime
import os
import threading
import time
import webbrowser
from pathlib import Path

import pyaudiowpatch as pyaudio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from capture import Track, find_loopback_device

ROOT = Path(__file__).parent
RECORDINGS = ROOT / "recordings"
HOST, PORT = "127.0.0.1", 8321

app = FastAPI(title="谛听 Diting")

# ---------- 全局会话状态（单用户本地应用，一把锁够用） ----------
lock = threading.Lock()
state = {
    "phase": "idle",  # idle | loading | recording | transcribing
    "detail": "",
    "started_at": None,
    "error": None,
    "last_md": None,
}
session = {}  # 录音会话资源: tracks/live/paudio/loopback_wav/mic_wav
clients = set()  # 已连接的 WebSocket
_loop = None  # asyncio 主循环（线程安全广播用）


def broadcast(msg: dict):
    """从任意线程向所有 WS 客户端广播（线程安全）"""
    if not clients or _loop is None:
        return
    def _send():
        for ws in list(clients):
            asyncio.ensure_future(_safe_send(ws, msg))
    try:
        _loop.call_soon_threadsafe(_send)
    except RuntimeError:
        pass


async def _safe_send(ws: WebSocket, msg: dict):
    try:
        await ws.send_json(msg)
    except Exception:
        clients.discard(ws)


def set_phase(phase, detail=""):
    with lock:
        state["phase"] = phase
        state["detail"] = detail
    broadcast({"type": "status", "phase": phase, "detail": detail,
               "elapsed": time.time() - state["started_at"] if state["started_at"] else 0})


# ---------- 录音会话（编排 capture + live_subtitles） ----------
def _level_broadcaster():
    while state["phase"] == "recording":
        lv = {t.name: round(min(t.level * 8, 1.0), 2) for t in session.get("tracks", [])}
        broadcast({"type": "level", **lv})
        time.sleep(0.5)


def start_session():
    """开新录音会话：加载模型 → 开双轨 → 实时字幕入 WebSocket"""
    from live_subtitles import LiveTranscriber

    if state["phase"] != "idle":
        raise RuntimeError("已有会话进行中")
    set_phase("loading", "加载模型中...")

    RECORDINGS.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    lb_wav = str(RECORDINGS / f"{ts}_loopback.wav")
    mic_wav = str(RECORDINGS / f"{ts}_mic.wav")

    p = pyaudio.PyAudio()
    lb_dev = find_loopback_device(p)
    if lb_dev is None:
        p.terminate()
        set_phase("idle")
        raise RuntimeError("未找到 WASAPI loopback 设备")
    live = LiveTranscriber.create(display=_WsDisplay())
    tracks = [Track(p, int(lb_dev["index"]), "系统", True, lb_wav,
                    on_audio=live.callback("系统", int(lb_dev["defaultSampleRate"]),
                                           min(2, int(lb_dev["maxInputChannels"]))))]
    mic_name = None
    try:
        mic = p.get_default_input_device_info()
        mic_name = mic["name"]
        tracks.append(Track(p, int(mic["index"]), "麦克风", False, mic_wav,
                            on_audio=live.callback("麦克风", int(mic["defaultSampleRate"]), 1)))
    except OSError:
        pass
    session.update(p=p, live=live, tracks=tracks, lb_wav=lb_wav, mic_wav=mic_wav)
    for t in tracks:
        t.start()
    with lock:
        state["started_at"] = time.time()
        state["error"] = None
    set_phase("recording", f"录制中：{lb_dev['name']}")
    threading.Thread(target=_level_broadcaster, daemon=True).start()
    return {"devices": {"loopback": lb_dev["name"], "mic": mic_name}}


def stop_session():
    """停止 → 后台线程执行精转+热词+纪要，进度经 WS 推送"""
    if state["phase"] != "recording":
        raise RuntimeError("当前没有录音会话")
    for t in session["tracks"]:
        t.stop()
    session["live"].flush_all()
    session["p"].terminate()
    lb_wav, mic_wav = session["lb_wav"], session["mic_wav"]
    session.clear()
    set_phase("transcribing", "离线精转中...")

    def _run():
        def progress(msg):
            broadcast({"type": "progress", "msg": msg})
        try:
            from transcribe import transcribe_pair
            md = transcribe_pair(lb_wav, mic_wav, summarize=True, progress=progress)
            with lock:
                state["last_md"] = md
            broadcast({"type": "transcript", "name": Path(md).name, "md": Path(md).read_text(encoding="utf-8")})
            broadcast({"type": "recordings", "list": list_recordings()})
        except Exception as e:
            with lock:
                state["error"] = str(e)
            broadcast({"type": "status", "phase": "idle", "detail": f"出错: {e}", "elapsed": 0})
            set_phase("idle", f"出错: {e}")
            return
        set_phase("idle", "完成")

    threading.Thread(target=_run, daemon=True).start()


class _WsDisplay:
    """LiveTranscriber 显示端：字幕事件 → WebSocket 广播"""

    def on_final(self, track_name, ts_text, text):
        broadcast({"type": "final", "track": track_name, "ts": ts_text, "text": text})

    def render_partial(self, track_name, partial):
        broadcast({"type": "partial", "track": track_name, "text": partial})

    def clear_partial(self, track_name):
        broadcast({"type": "partial", "track": track_name, "text": ""})


# ---------- REST ----------
def list_recordings():
    out = []
    for md in sorted(RECORDINGS.glob("*_transcript.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        base = md.name[: -len("_transcript.md")]
        out.append({"name": base, "md": md.name,
                    "mtime": datetime.datetime.fromtimestamp(md.stat().st_mtime).strftime("%m-%d %H:%M")})
    return out


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
def api_status():
    with lock:
        s = dict(state)
    s["elapsed"] = time.time() - s["started_at"] if s["started_at"] and s["phase"] == "recording" else 0
    return s


@app.post("/api/start")
def api_start():
    try:
        info = start_session()
        return {"ok": True, **info}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@app.post("/api/stop")
def api_stop():
    try:
        stop_session()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@app.get("/api/recordings")
def api_recordings():
    return list_recordings()


@app.get("/api/transcript")
def api_transcript(name: str):
    md = RECORDINGS / name
    if not name.endswith("_transcript.md") or not md.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": name, "md": md.read_text(encoding="utf-8")}


@app.get("/api/export")
def api_export(name: str, fmt: str = "srt"):
    """文稿导出：fmt=srt 字幕 / fmt=docx Word 文档"""
    from fastapi import Response
    import export as _export

    md = RECORDINGS / name
    if not name.endswith("_transcript.md") or not md.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    base = name[: -len("_transcript.md")]
    md_text = md.read_text(encoding="utf-8")
    if fmt == "srt":
        return Response(
            content=_export.md_to_srt_text(md_text),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base}.srt"'},
        )
    if fmt == "docx":
        docx = RECORDINGS / f"{base}.docx"
        _export.md_to_docx(str(md), str(docx))
        return FileResponse(docx, filename=f"{base}.docx",
                            headers={"Content-Disposition": f'attachment; filename="{base}.docx"'})
    return JSONResponse({"error": "fmt must be srt or docx"}, status_code=400)


@app.get("/api/hotwords")
def api_hotwords():
    p = ROOT / "hotwords.txt"
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.post("/api/hotwords")
def api_hotwords_save(body: dict):
    (ROOT / "hotwords.txt").write_text(body.get("text", ""), encoding="utf-8")
    return {"ok": True}


@app.get("/api/config")
def api_config():
    import json as _json
    p = ROOT / "config.json"
    cfg = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg["api_key"] = "******" if cfg.get("api_key") else ""
    return cfg


@app.post("/api/config")
def api_config_save(body: dict):
    import json as _json
    cfg = _json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
    for k in ("api_base", "model", "temperature", "max_chars"):
        if k in body and body[k] != "":
            cfg[k] = body[k]
    key = body.get("api_key", "")
    if key and key != "******":
        cfg["api_key"] = key
    (ROOT / "config.json").write_text(_json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


# ---------- WebSocket ----------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global _loop
    _loop = asyncio.get_running_loop()
    await ws.accept()
    clients.add(ws)
    try:
        with lock:
            s = dict(state)
        await ws.send_json({"type": "status", "phase": s["phase"], "detail": s["detail"],
                            "elapsed": time.time() - s["started_at"] if s["started_at"] else 0})
        while True:
            await ws.receive_text()  # 保活；客户端不发数据
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


def _open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
