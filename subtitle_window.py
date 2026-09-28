# -*- coding: utf-8 -*-
"""置顶字幕小窗：无边框、半透明、总在最前，可拖动，■ 按钮或 Esc 停止录音

字幕事件经线程安全队列送达（采集线程 → tkinter 主线程），由 run() 内的
定时轮询刷新界面。run() 阻塞直到用户停止（或 auto_stop_sec 到时）。
"""
import queue
import tkinter as tk

BG, FG, FG_DIM, ACCENT = "#1e1f22", "#f2f2f2", "#9aa0a6", "#7ee787"


class WindowDisplay:
    """LiveTranscriber 的显示端实现（接口: on_final / render_partial / clear_partial）"""

    def __init__(self):
        self.q = queue.Queue()
        self.stop_event = None  # run() 时由外部传入

    # ---- LiveTranscriber 回调（采集线程调用，只入队，不碰 tkinter）----
    def on_final(self, track_name, ts_text, text):
        self.q.put(("final", f"[{ts_text}] {track_name}｜{text}"))

    def render_partial(self, track_name, partial):
        self.q.put(("partial", f"▶ {track_name}｜{partial}"))

    def clear_partial(self, track_name):
        self.q.put(("clear", None))

    # ---- 主线程 ----
    def run(self, stop_event, auto_stop_sec=None):
        """构建窗口并阻塞运行；返回时表示用户已请求停止"""
        self.stop_event = stop_event
        root = tk.Tk()
        root.title("谛听 · 实时字幕")
        root.overrideredirect(True)  # 无边框
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.88)
        root.configure(bg=BG)
        w, h = 760, 150
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 90}")

        final_lbl = tk.Label(root, text="谛听已就绪，等待发言…", font=("Microsoft YaHei UI", 15),
                             fg=FG_DIM, bg=BG, wraplength=w - 40, justify="left", anchor="w")
        final_lbl.pack(fill="both", expand=True, padx=18, pady=(12, 0))
        part_lbl = tk.Label(root, text="", font=("Microsoft YaHei UI", 15),
                            fg=ACCENT, bg=BG, wraplength=w - 40, justify="left", anchor="w")
        part_lbl.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        stop_btn = tk.Label(root, text="■ 停止录音（Esc）", font=("Microsoft YaHei UI", 10),
                            fg=FG_DIM, bg=BG, cursor="hand2")
        stop_btn.pack(pady=(0, 8))

        def do_stop(*_):
            if stop_event:
                stop_event.set()
            root.destroy()

        stop_btn.bind("<Button-1>", do_stop)
        root.bind("<Escape>", do_stop)

        drag = {"x": 0, "y": 0}

        def on_press(e):
            drag["x"], drag["y"] = e.x, e.y

        def on_move(e):
            root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

        for wdg in (root, final_lbl, part_lbl):
            wdg.bind("<Button-1>", on_press, add="+")
            wdg.bind("<B1-Motion>", on_move, add="+")

        def poll():
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "final":
                        final_lbl.configure(text=payload)
                    elif kind == "partial":
                        part_lbl.configure(text=payload)
                    elif kind == "clear":
                        part_lbl.configure(text="")
            except queue.Empty:
                pass
            root.after(80, poll)

        root.after(80, poll)
        if auto_stop_sec:
            root.after(int(auto_stop_sec * 1000), do_stop)
        root.mainloop()
