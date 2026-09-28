# -*- coding: utf-8 -*-
"""设备自检：列出 loopback 与麦克风设备，不录音"""
import pyaudiowpatch as pyaudio


def main():
    with pyaudio.PyAudio() as p:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        print(f"默认输出: {default_out['name']} ({int(default_out['defaultSampleRate'])}Hz)")

        loopbacks = list(p.get_loopback_device_info_generator())
        print(f"\nloopback 设备 {len(loopbacks)} 个:")
        for d in loopbacks:
            mark = " <-- 将使用" if default_out["name"] in d["name"] else ""
            print(f"  [{d['index']}] {d['name']} ch={d['maxInputChannels']} {int(d['defaultSampleRate'])}Hz{mark}")

        try:
            mic = p.get_default_input_device_info()
            print(f"\n默认麦克风: [{mic['index']}] {mic['name']} ch={mic['maxInputChannels']} {int(mic['defaultSampleRate'])}Hz")
        except OSError:
            print("\n未找到麦克风设备")


if __name__ == "__main__":
    main()
