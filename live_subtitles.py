# -*- coding: utf-8 -*-
"""实时字幕：流式 Paraformer + 流式 VAD（CPU），2pass 架构的"第一遍"

- 每 100ms 喂流式 VAD（静音 ~0.8s 判句尾）
- 每 600ms 喂流式 Paraformer 出增量文字
- 句尾定稿：标点模型补标点，打印 [mm:ss] 定稿行
- 会后由 transcribe.py 的离线大模型精转（第二遍），实时误差不会留在文稿里

独立演示: python live_subtitles.py
被 record_meeting.py 作为模块调用（推荐）
"""
import audioop
import os
import threading
import time

import numpy as np

SR = 16000
VAD_BLOCK = 1600   # 100ms @16k
ASR_BLOCK = 9600   # 600ms @16k
_ONLINE_DIR = "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx"
_VAD_DIR = "speech_fsmn_vad_zh-cn-16k-common-onnx"


def _fmt_ts(sec):
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


class _StreamState:
    """一条音轨的流式状态：重采样 → 双缓冲（VAD 100ms / ASR 600ms）"""

    def __init__(self, name, rate, channels, live):
        self.name = name
        self.live = live
        self.rate = rate
        self.channels = channels
        self.resample_state = None
        self.vad_buf = np.zeros(0, dtype=np.float32)
        self.asr_buf = np.zeros(0, dtype=np.float32)
        self.asr_cache = {}
        self.vad_param = {}
        self.partial = ""
        self.utt_start_ms = None  # 当前句起始时间(ms，流内时间轴，来自 VAD 开始事件)

    def _asr(self, chunk, is_final=False):
        res = self.live.asr_online(
            chunk, param_dict={"is_final": is_final, "cache": self.asr_cache}
        )
        text = ""
        for item in res or []:
            if isinstance(item, dict):
                v = item.get("preds")
                if isinstance(v, (tuple, list)) and v and isinstance(v[0], str):
                    text += v[0]
        return text

    def _finalize(self):
        """定稿当前句：flush 剩余音频给 ASR(is_final=True)，补标点，落行"""
        text = self.partial
        if self.asr_buf.size:
            rest, self.asr_buf = self.asr_buf, np.zeros(0, dtype=np.float32)
            try:
                text += self._asr(rest, is_final=True)
            except Exception as e:
                print(f"\n[ASR flush 错误] {e}")
        self.asr_cache = {}
        self.live.clear_partial(self.name)
        if text.strip():
            try:
                punctuated = self.live.extract_text(self.live.punc(text.strip()))
                text = punctuated.strip() or text
            except Exception:
                pass
            self.live.on_final(self.name, (self.utt_start_ms or 0) / 1000, text.strip())
        self.partial = ""
        self.utt_start_ms = None

    @staticmethod
    def _vad_events(segs):
        """解析流式 VAD 事件：返回 (说话开始ms列表, 说话结束ms列表)
        事件形如 [[beg, -1]]（开始）/ [[-1, end]]（结束）"""
        starts, ends = [], []
        try:
            stack = [segs]
            while stack:
                x = stack.pop()
                if isinstance(x, (list, tuple)):
                    if (
                        len(x) == 2
                        and all(isinstance(v, (int, float)) for v in x)
                        and not isinstance(x, bool)
                    ):
                        b, e = x
                        if b >= 0 and e < 0:
                            starts.append(float(b))
                        if e >= 0:
                            ends.append(float(e))
                    else:
                        stack.extend(x)
        except Exception:
            pass
        return starts, ends

    def push(self, arr_int16):
        """Track 回调入口：arr 为该轨采样率/声道的 interleaved int16"""
        data = arr_int16.tobytes()
        if self.channels == 2:
            data = audioop.tomono(data, 2, 0.5, 0.5)
        if self.rate != SR:
            data, self.resample_state = audioop.ratecv(data, 2, 1, self.rate, SR, self.resample_state)
        block = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

        self.vad_buf = np.concatenate([self.vad_buf, block])
        self.asr_buf = np.concatenate([self.asr_buf, block])

        # 每 100ms 喂 VAD：开始事件记时间戳，结束事件触发定稿
        while self.vad_buf.size >= VAD_BLOCK:
            chunk, self.vad_buf = self.vad_buf[:VAD_BLOCK], self.vad_buf[VAD_BLOCK:]
            try:
                segs = self.live.vad_online(chunk, param_dict=self.vad_param)
            except Exception:
                segs = None  # 纯静音/噪声时 onnxruntime 可能报错，正常现象
            starts, ends = self._vad_events(segs)
            if starts and self.utt_start_ms is None:
                self.utt_start_ms = starts[0]
            if ends:
                self._finalize()
                break  # 定稿已消费 asr_buf，剩余 vad_buf 下轮处理

        # 每 600ms 喂流式 ASR，出增量文字
        while self.asr_buf.size >= ASR_BLOCK:
            chunk, self.asr_buf = self.asr_buf[:ASR_BLOCK], self.asr_buf[ASR_BLOCK:]
            try:
                inc = self._asr(chunk)
            except Exception as e:
                print(f"\n[ASR 错误] {e}")
                inc = ""
            if inc:
                if self.utt_start_ms is None:
                    self.utt_start_ms = self.live.now_sec() * 1000
                self.partial += inc
                self.live.render_partial(self.name, self.partial)

    def flush(self):
        self._finalize()


class ConsoleDisplay:
    """终端显示：当前句原地上划，定稿落行"""

    def __init__(self):
        self._disp_len = {}

    def on_final(self, track_name, ts_text, text):
        print(f"\r\033[K[{ts_text}] {track_name}｜{text}", flush=True)
        self._disp_len[track_name] = 0

    def render_partial(self, track_name, partial):
        line = f"▶ {track_name}｜{partial}"
        pad = " " * max(0, self._disp_len.get(track_name, 0) - len(line))
        print("\r" + line + pad, end="", flush=True)
        self._disp_len[track_name] = len(line)

    def clear_partial(self, track_name):
        if self._disp_len.get(track_name):
            print("\r\033[K", end="", flush=True)
            self._disp_len[track_name] = 0


class LiveTranscriber:
    """多轨实时字幕。用法:
        live = LiveTranscriber.create()
        track = Track(..., on_audio=live.callback("系统", rate, channels))
        ...
        live.flush_all()
    """

    def __init__(self, asr_online, vad_online, punc, extract_text, display=None):
        self.asr_online = asr_online
        self.vad_online = vad_online
        self.punc = punc
        self.extract_text = extract_text
        self.display = display or ConsoleDisplay()
        self.streams = {}
        self.lock = threading.Lock()
        self.start_time = time.time()

    @classmethod
    def create(cls, display=None):
        """加载流式模型；标点模型复用 transcribe 的缓存，避免重复加载 1GB"""
        from transcribe import VAD_MODEL, _extract_text, get_models
        from funasr_onnx.paraformer_online_bin import Paraformer as OnlineParaformer
        from funasr_onnx.vad_bin import Fsmn_vad_online

        root = os.path.dirname(os.path.abspath(__file__))
        _, _, punc = get_models()  # 标点复用缓存；离线 asr/vad 留给会后精转
        asr_online = OnlineParaformer(
            model_dir=os.path.join(root, "models", _ONLINE_DIR),
            quantize=True, chunk_size=[5, 10, 5], device_id="-1",
        )
        vad_online = Fsmn_vad_online(
            model_dir=os.path.join(root, "models", _VAD_DIR), quantize=True, device_id="-1",
        )
        return cls(asr_online, vad_online, punc, _extract_text, display)

    def callback(self, name, rate, channels):
        """为一条音轨生成 on_audio 回调"""
        st = _StreamState(name, rate, channels, self)
        self.streams[name] = st
        return st.push

    def now_sec(self):
        """当前相对录音开始的秒数（按墙钟，近似值）"""
        return time.time() - self.start_time

    def on_final(self, track_name, start_sec, text):
        with self.lock:
            self.display.on_final(track_name, _fmt_ts(start_sec), text)

    def render_partial(self, track_name, partial):
        with self.lock:
            self.display.render_partial(track_name, partial)

    def clear_partial(self, track_name):
        with self.lock:
            self.display.clear_partial(track_name)

    def flush_all(self):
        for st in self.streams.values():
            st.flush()


def main():
    """演示：录音 + 实时字幕（含 wav 存档），回车停止。完整流程用 record_meeting.py"""
    import datetime

    import pyaudiowpatch as pyaudio
    from capture import Track, find_loopback_device

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    print("加载流式模型中（约 10s）...")
    live = LiveTranscriber.create()

    with pyaudio.PyAudio() as p:
        lb = find_loopback_device(p)
        if lb is None:
            raise SystemExit("未找到 loopback 设备")
        print(f"系统声: {lb['name']}")
        tracks = [Track(p, int(lb["index"]), "系统", True,
                        os.path.join(out_dir, f"{ts}_loopback.wav"),
                        on_audio=live.callback("系统", int(lb["defaultSampleRate"]),
                                               min(2, int(lb["maxInputChannels"]))))]
        try:
            mic = p.get_default_input_device_info()
            print(f"麦克风: {mic['name']}")
            tracks.append(Track(p, int(mic["index"]), "麦克风", False,
                                os.path.join(out_dir, f"{ts}_mic.wav"),
                                on_audio=live.callback("麦克风", int(mic["defaultSampleRate"]), 1)))
        except OSError:
            print("未找到麦克风，只录系统声")
        for t in tracks:
            t.start()
        print("实时字幕中，回车停止...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        for t in tracks:
            t.stop()
    live.flush_all()
    print("\n演示结束。wav 已存档；完整流程（离线精转+纪要）请用 record_meeting.py")


if __name__ == "__main__":
    main()
    import sys
    sys.stdout.flush()
    os._exit(0)  # 跳过 PortAudio 清理（同 record_meeting）
