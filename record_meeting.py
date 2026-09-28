# -*- coding: utf-8 -*-
"""谛听 Diting：双轨会议录音 + 置顶实时字幕窗 + 会后精转 + 纪要（一条命令全流程）

用法:
  python record_meeting.py                 # 全流程（置顶字幕窗 + 录音 + 精转 + 纪要）
  python record_meeting.py --no-window    # 不开字幕窗，字幕输出到终端，回车停止
  python record_meeting.py --no-live      # 不开实时字幕
  python record_meeting.py --no-transcribe# 只录不转
  python record_meeting.py --no-summarize # 转写但不生成纪要
  python record_meeting.py --stop-after 1800  # 30 分钟后自动停止（配合定时散会）

停止方式: 字幕窗 ■ 按钮 / Esc；或 --no-window 时在控制台回车。
"""
import datetime
import os
import sys
import threading
import time

import pyaudiowpatch as pyaudio
from capture import Track, find_loopback_device

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


def meter(tracks):
    """每秒打印一次各轨音量（仅终端模式使用）"""
    bars = "▁▂▃▄▅▆▇█"
    while any(not t.stop_event.is_set() for t in tracks):
        cells = [f"{t.name} {bars[min(int(t.level * 200), 7)]}" for t in tracks]
        print(f"\r[{' | '.join(cells)}] 录音中，按回车停止... ", end="", flush=True)
        time.sleep(1)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    loopback_wav = os.path.join(OUT_DIR, f"{ts}_loopback.wav")
    mic_wav = os.path.join(OUT_DIR, f"{ts}_mic.wav")
    argv = sys.argv[1:]
    use_window = "--no-window" not in argv

    stop_event = threading.Event()
    window = None
    live = None
    if "--no-live" not in argv:
        from live_subtitles import LiveTranscriber

        print("加载模型中（约 10s）...")
        if use_window:
            from subtitle_window import WindowDisplay

            window = WindowDisplay()
            live = LiveTranscriber.create(display=window)
        else:
            live = LiveTranscriber.create()

    with pyaudio.PyAudio() as p:
        lb_dev = find_loopback_device(p)
        if lb_dev is None:
            sys.exit("未找到 WASAPI loopback 设备（Windows 10 及以上自带，请检查音频驱动）")
        print(f"系统声音 : {lb_dev['name']} ({int(lb_dev['defaultSampleRate'])}Hz)")
        tracks = [Track(p, int(lb_dev["index"]), "系统", True, loopback_wav,
                        on_audio=live.callback("系统", int(lb_dev["defaultSampleRate"]),
                                               min(2, int(lb_dev["maxInputChannels"]))) if live else None)]
        try:
            mic_dev = p.get_default_input_device_info()
            print(f"麦克风   : {mic_dev['name']}")
            tracks.append(Track(p, int(mic_dev["index"]), "麦克风", False, mic_wav,
                                on_audio=live.callback("麦克风", int(mic_dev["defaultSampleRate"]), 1) if live else None))
        except OSError:
            print("警告: 未找到麦克风，只录系统声音")
        print(f"保存到   : {OUT_DIR}")
        for t in tracks:
            t.start()

        if window:
            print("字幕窗已打开，点窗内 ■ 或按 Esc 停止录音。")
            auto = None
            if "--stop-after" in argv:
                try:
                    auto = float(argv[argv.index("--stop-after") + 1])
                    print(f"将在 {auto:.0f}s 后自动停止。")
                except (ValueError, IndexError):
                    pass
            window.run(stop_event, auto_stop_sec=auto)  # 阻塞到用户停止
        else:
            print("实时字幕中，按回车停止..." if live else "录音中，按回车停止...")
            mt = threading.Thread(target=meter, args=(tracks,), daemon=True)
            mt.start()
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                pass

        for t in tracks:
            t.stop()
        if live:
            live.flush_all()
        print("\n录音结束。")

    if "--no-transcribe" in argv:
        print(f"文件:\n  {loopback_wav}\n  {mic_wav}")
        return

    from transcribe import transcribe_pair
    md_path = transcribe_pair(loopback_wav, mic_wav, summarize="--no-summarize" not in argv)
    print(f"\n转写完成: {md_path}")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # 跳过 PortAudio/tkinter 清理（本机偶发崩溃，不影响数据）
