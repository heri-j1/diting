# -*- coding: utf-8 -*-
"""谛听 Web UI：FastAPI 后端 + WebSocket 实时字幕 + 浏览器界面

启动: .venv/Scripts/python webapp.py   → 自动打开浏览器 http://127.0.0.1:8321
复用现有模块: capture(采集) / live_subtitles(实时字幕) / transcribe(精转) /
             hotwords(热词) / summarize(纪要)。本文件只做会话编排与通信。
"""
import asyncio
import datetime
import json
import re
import shutil
import socket
import threading
import time
import webbrowser
from pathlib import Path

import pyaudiowpatch as pyaudio
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from capture import Track, find_loopback_device

ROOT = Path(__file__).parent
RECORDINGS = ROOT / "recordings"
ARCHIVE = RECORDINGS / "archive"
PORT = 8321
CONFIG_PATH = ROOT / "config.json"


def _load_web_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _lan_ips() -> set:
    """本机所有网卡的 IPv4（这些来源视为可信的本机）"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return ips


def _bind_host() -> str:
    return "0.0.0.0" if _load_web_config().get("lan_access") else "127.0.0.1"


HOST = _bind_host()  # 兼容旧引用；实际监听以启动时为准

app = FastAPI(title="谛听 Diting")

# ---------- 信任栅栏（借鉴 DSH 小鲸鱼 widget） ----------
# 读操作(看字幕/文稿)可开放局域网；所有写操作仅限本机来源(loopback/本机网卡)或 admin_hosts 白名单；
# 带 Origin 的请求校验同源，拦截恶意网页驱动浏览器打内网接口(含 DNS rebinding)。

SAFE_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _trust_fence(request: Request):
    """写操作依赖：来源必须是本机(loopback/本机网卡 IP/admin_hosts)且 Origin 同源"""
    cfg = _load_web_config()
    client = request.client.host if request.client else ""
    trusted = set(SAFE_HOSTS) | _lan_ips() | set(cfg.get("admin_hosts", []))
    if client not in trusted:
        raise HTTPException(status_code=403, detail=f"写操作仅限本机（来源 {client} 不在白名单）")
    _check_origin(request, cfg)


def _origin_host(origin: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(origin).hostname or "").lower()
    except ValueError:
        return ""


def _check_origin(request: Request, cfg: dict):
    """浏览器会带 Origin 头；必须与 Host 同源或来自本机/白名单，否则拒绝"""
    origin = request.headers.get("origin")
    if not origin:
        return  # 非浏览器客户端（curl/CLI）不带 Origin，放行
    host = (request.headers.get("host", "") or "").rsplit(":", 1)[0].lower()
    oh = _origin_host(origin)
    allowed = oh in ({h.lower() for h in SAFE_HOSTS} | _lan_ips() | {h.lower() for h in cfg.get("admin_hosts", [])}) \
        or oh == host
    if not allowed:
        raise HTTPException(status_code=403, detail=f"跨站 Origin 被拒绝: {origin}")


require_local = Depends(_trust_fence)


@app.middleware("http")
async def fence_origin_middleware(request: Request, call_next):
    """全站 Origin 校验（含读请求，防恶意网页探测内网）；WebSocket 握手另行校验"""
    if request.url.path.startswith(("/api", "/ws")):
        try:
            _check_origin(request, _load_web_config())
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=403)
    return await call_next(request)  # starlette 1.x 需显式传 request

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
            broadcast({"type": "recordings"})
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
_BASE_RE = re.compile(r"^[\w\-]+$")


def _safe_base(name: str) -> str:
    """从会话名或文稿文件名提取并校验 base（如 20260928-211349），防路径穿越"""
    base = name[: -len("_transcript.md")] if name.endswith("_transcript.md") else name
    if not _BASE_RE.match(base):
        raise ValueError(f"非法名称: {name!r}")
    return base


def _session_files(directory: Path, base: str):
    """某次会话在目录下的全部产物：文稿 / 双轨录音 wav / 导出的 docx"""
    return sorted(directory.glob(f"{base}_*")) + sorted(directory.glob(f"{base}.docx"))


def _find_md(name: str) -> Path:
    md_name = _safe_base(name) + "_transcript.md"
    for d in (RECORDINGS, ARCHIVE):
        p = d / md_name
        if p.exists():
            return p
    raise FileNotFoundError(name)


def list_recordings(view: str = "active"):
    folder = ARCHIVE if view == "archive" else RECORDINGS
    out = []
    for md in sorted(folder.glob("*_transcript.md"), key=lambda p: p.stat().st_mtime, reverse=True):
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


@app.post("/api/start", dependencies=[require_local])
def api_start():
    try:
        info = start_session()
        return {"ok": True, **info}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@app.post("/api/stop", dependencies=[require_local])
def api_stop():
    try:
        stop_session()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)


@app.get("/api/recordings")
def api_recordings(view: str = "active"):
    return list_recordings(view)


@app.post("/api/archive", dependencies=[require_local])
def api_archive(body: dict):
    """归档/还原：会话全部产物（文稿+录音+docx）在 recordings/ 与 recordings/archive/ 间整体搬移"""
    try:
        base = _safe_base(body.get("name", ""))
        undo = bool(body.get("undo"))
        src, dst = (ARCHIVE, RECORDINGS) if undo else (RECORDINGS, ARCHIVE)
        files = _session_files(src, base)
        if not files:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        dst.mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.move(str(p), str(dst / p.name))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    broadcast({"type": "recordings"})
    return {"ok": True, "moved": len(files)}


@app.post("/api/delete", dependencies=[require_local])
def api_delete(body: dict):
    """永久删除某次会话的全部产物（录音含隐私，不可恢复）"""
    try:
        base = _safe_base(body.get("name", ""))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    n = 0
    for d in (RECORDINGS, ARCHIVE):
        for p in _session_files(d, base):
            p.unlink(missing_ok=True)
            n += 1
    broadcast({"type": "recordings"})
    return {"ok": True, "deleted": n}


@app.get("/api/transcript")
def api_transcript(name: str):
    try:
        md = _find_md(name)
    except (ValueError, FileNotFoundError):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": md.name, "md": md.read_text(encoding="utf-8")}


@app.get("/api/export")
def api_export(name: str, fmt: str = "srt"):
    """文稿导出：fmt=srt 字幕 / fmt=docx Word 文档"""
    from fastapi import Response
    import export as _export

    try:
        md = _find_md(name)
    except (ValueError, FileNotFoundError):
        return JSONResponse({"error": "not found"}, status_code=404)
    base = md.name[: -len("_transcript.md")]
    md_text = md.read_text(encoding="utf-8")
    if fmt == "srt":
        return Response(
            content=_export.md_to_srt_text(md_text),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{base}.srt"'},
        )
    if fmt == "docx":
        docx = md.parent / f"{base}.docx"
        _export.md_to_docx(str(md), str(docx))
        return FileResponse(docx, filename=f"{base}.docx",
                            headers={"Content-Disposition": f'attachment; filename="{base}.docx"'})
    return JSONResponse({"error": "fmt must be srt or docx"}, status_code=400)


@app.get("/api/hotwords")
def api_hotwords():
    p = ROOT / "hotwords.txt"
    return {"text": p.read_text(encoding="utf-8") if p.exists() else ""}


@app.post("/api/hotwords", dependencies=[require_local])
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


@app.post("/api/config", dependencies=[require_local])
def api_config_save(body: dict):
    import json as _json
    cfg = _json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
    for k in ("api_base", "model", "temperature", "max_chars", "api_format", "max_tokens",
              "lan_access", "admin_hosts"):
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
    try:
        _check_origin(ws, _load_web_config())  # WebSocket 握手同受信任栅栏
    except HTTPException as e:
        await ws.close(code=1008, reason=e.detail)
        return
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
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def _port_busy() -> bool:
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        s.close()
        return False
    except OSError:
        return True


if __name__ == "__main__":
    bind = _bind_host()  # config.json -> lan_access: true 时监听 0.0.0.0（局域网可看）
    if _port_busy():
        print(f"[Diting] already running at http://127.0.0.1:{PORT}, opening browser")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    else:
        if bind == "0.0.0.0":
            print("[Diting] LAN access ON —— 局域网可看实时字幕/文稿；写操作仍仅限本机/白名单（信任栅栏）")
        threading.Thread(target=_open_browser, daemon=True).start()
        import uvicorn
        uvicorn.run(app, host=bind, port=PORT, log_level="warning")
