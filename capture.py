# -*- coding: utf-8 -*-
"""音频采集模块：WASAPI loopback（系统声）+ 麦克风 双轨采集

- 轮询式读取，静音期不阻塞，随时可停
- 按墙钟增量补静音帧，wav 时间轴与真实时间对齐
- Track(on_audio=...) 回调把每个音频块（int16 bytes，含补的静音）推给实时消费者
"""
import threading
import time
import wave

import numpy as np
import pyaudiowpatch as pyaudio

CHUNK = 2048


def find_loopback_device(p):
    """找到默认扬声器对应的 WASAPI loopback 设备（录"电脑正在播放的声音"）"""
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if default_out.get("isLoopbackDevice"):
        return default_out
    for lb in p.get_loopback_device_info_generator():
        if default_out["name"] in lb["name"]:
            return lb
    for lb in p.get_loopback_device_info_generator():  # 兜底：第一个 loopback
        return lb
    return None


class Track:
    """一条录音轨。start() 后在后台线程持续写 wav 并回调 on_audio"""

    def __init__(self, p, device_index, name, is_loopback, wav_path, on_audio=None):
        self.name = name
        self.wav_path = wav_path
        self.on_audio = on_audio
        self.level = 0.0
        self.frames = 0
        self.stop_event = threading.Event()
        dev = p.get_device_info_by_index(device_index)
        self.channels = min(int(dev["maxInputChannels"]), 2 if is_loopback else 1)
        self.rate = int(dev["defaultSampleRate"])
        self.format = pyaudio.paFloat32 if is_loopback else pyaudio.paInt16
        self._stream = p.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=device_index,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _emit(self, wf, arr_int16):
        """写 wav + 推送回调（arr 为 interleaved int16）"""
        if arr_int16.size:
            wf.writeframes(arr_int16.tobytes())
        if self.on_audio is not None:
            try:
                self.on_audio(arr_int16)
            except Exception as e:
                print(f"\n[实时字幕回调错误] {e}")
        self.frames += len(arr_int16) // self.channels

    def _run(self):
        wf = wave.open(self.wav_path, "wb")
        wf.setnchannels(self.channels)
        wf.setsampwidth(2)  # 统一 int16
        wf.setframerate(self.rate)
        start = time.time()
        written = 0  # 已写帧数（不含声道）
        try:
            while not self.stop_event.is_set():
                fed = False
                # 轮询可用数据，静音期 loopback 不产数据时不阻塞，保证随时可停
                if self._stream.get_read_available() >= CHUNK:
                    data = self._stream.read(CHUNK, exception_on_overflow=False)
                    if self.format == pyaudio.paFloat32:
                        arr = np.frombuffer(data, dtype=np.float32)
                        self.level = float(np.sqrt(np.mean(arr**2)))
                        arr = np.clip(arr * 32767, -32768, 32767).astype(np.int16)
                    else:
                        self.level = float(np.sqrt(np.mean((np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768) ** 2)))
                        arr = np.frombuffer(data, dtype=np.int16).copy()
                    self._emit(wf, arr)
                    written += len(arr) // self.channels
                    fed = True
                # 静音期按墙钟增量补零帧，使 wav 时间轴与真实时间对齐
                expected = int((time.time() - start) * self.rate)
                gap = expected - written
                if gap > self.rate // 20:  # 落后超过 50ms 开始补，每次最多 100ms
                    n = min(gap, self.rate // 10)
                    self._emit(wf, np.zeros(n * self.channels, dtype=np.int16))
                    written += n
                    fed = True
                if not fed:
                    time.sleep(0.02)
            self.frames = written
        finally:
            expected = int((time.time() - start) * self.rate)  # 补齐结尾静音
            gap = max(expected - written, 0)
            if gap:
                self._emit(wf, np.zeros(gap * self.channels, dtype=np.int16))
            self.frames = written + gap
            wf.close()
            self._stream.stop_stream()
            self._stream.close()

    def start(self):
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        self._thread.join(timeout=5)
