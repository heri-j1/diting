# -*- coding: utf-8 -*-
"""置顶字幕小窗（交互借鉴 DSH 小鲸鱼 widget）：

- 无边框、半透明、总在最前，拖动移动
- 拖到屏幕边缘自动吸附（贴边后文字镜像对齐）
- 按下有 Q 弹挤压动画
- 滚轮缩放字号（0.7x ~ 1.6x）
- 右键菜单：停止录音 / 紧凑模式（隐藏按钮）/ 重置缩放；Esc 停止

字幕事件经线程安全队列送达（采集线程 → tkinter 主线程），run() 阻塞到停止。
"""
import queue
import tkinter as tk

BG, FG, FG_DIM, ACCENT = "#1e1f22", "#f2f2f2", "#9aa0a6", "#7ee787"
EDGE_SNAP = 40      # 距屏幕边缘多少像素内吸附
W, H = 760, 150
BASE_FONT = ("Microsoft YaHei UI", 15)
BASE_FONT_S = ("Microsoft YaHei UI", 10)


class WindowDisplay:
    """LiveTranscriber 的显示端实现（接口: on_final / render_partial / clear_partial）"""

    def __init__(self):
        self.q = queue.Queue()
        self.stop_event = None  # run() 时由外部传入
        self._scale = 1.0
        self._snapped = None    # None | 'left' | 'right'
        self._root = None

    # ---- LiveTranscriber 回调（采集线程调用，只入队，不碰 tkinter）----
    def on_final(self, track_name, ts_text, text):
        self.q.put(("final", f"[{ts_text}] {track_name}｜{text}"))

    def render_partial(self, track_name, partial):
        self.q.put(("partial", f"▶ {track_name}｜{partial}"))

    def clear_partial(self, track_name):
        self.q.put(("clear", None))

    # ---- 主线程 ----
    def run(self, stop_event, auto_stop_sec=None):
        self.stop_event = stop_event
        root = tk.Tk()
        self._root = root
        root.title("谛听 · 实时字幕")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.88)
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{W}x{H}+{(sw - W) // 2}+{sh - H - 90}")

        self.final_lbl = tk.Label(root, text="谛听已就绪，等待发言…", font=BASE_FONT,
                                  fg=FG_DIM, bg=BG, wraplength=W - 40,
                                  justify="left", anchor="w")
        self.final_lbl.pack(fill="both", expand=True, padx=18, pady=(12, 0))
        self.part_lbl = tk.Label(root, text="", font=BASE_FONT,
                                 fg=ACCENT, bg=BG, wraplength=W - 40,
                                 justify="left", anchor="w")
        self.part_lbl.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.stop_btn = tk.Label(root, text="■ 停止录音（Esc）", font=BASE_FONT_S,
                                 fg=FG_DIM, bg=BG, cursor="hand2")
        self.stop_btn.pack(pady=(0, 8))

        def do_stop(*_):
            if stop_event:
                stop_event.set()
            root.destroy()

        self.stop_btn.bind("<Button-1>", do_stop)
        root.bind("<Escape>", do_stop)

        # ---- 拖动 + 边缘吸附 ----
        drag = {"x": 0, "y": 0}

        def on_press(e):
            drag["x"], drag["y"] = e.x, e.y
            self._squish()

        def on_move(e):
            root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
            self._snapped = None  # 拖动中脱离吸附

        def on_release(e):
            self._snap_check()

        for wdg in (root, self.final_lbl, self.part_lbl):
            wdg.bind("<Button-1>", on_press, add="+")
            wdg.bind("<B1-Motion>", on_move, add="+")
            wdg.bind("<ButtonRelease-1>", on_release, add="+")

        # ---- 滚轮缩放 ----
        def on_wheel(e):
            self._scale = min(1.6, max(0.7, self._scale + (0.1 if e.delta > 0 else -0.1)))
            self._apply_fonts()

        for wdg in (root, self.final_lbl, self.part_lbl):
            wdg.bind("<MouseWheel>", on_wheel, add="+")

        # ---- 右键菜单 ----
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="停止录音", command=do_stop)
        menu.add_command(label="紧凑模式（隐藏按钮）", command=lambda: self._toggle_compact())
        menu.add_command(label="重置缩放", command=lambda: (setattr(self, "_scale", 1.0), self._apply_fonts()))

        def popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()

        for wdg in (root, self.final_lbl, self.part_lbl, self.stop_btn):
            wdg.bind("<Button-3>", popup, add="+")

        def poll():
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "final":
                        self.final_lbl.configure(text=payload)
                    elif kind == "partial":
                        self.part_lbl.configure(text=payload)
                    elif kind == "clear":
                        self.part_lbl.configure(text="")
            except queue.Empty:
                pass
            root.after(80, poll)

        root.after(80, poll)
        if auto_stop_sec:
            root.after(int(auto_stop_sec * 1000), do_stop)
        root.mainloop()

    # ---- 交互细节 ----
    def _apply_fonts(self):
        s = self._scale
        self.final_lbl.config(font=("Microsoft YaHei UI", max(9, int(15 * s))))
        self.part_lbl.config(font=("Microsoft YaHei UI", max(9, int(15 * s))))
        self.stop_btn.config(font=("Microsoft YaHei UI", max(8, int(10 * s))))

    def _toggle_compact(self):
        if self.stop_btn.winfo_ismapped():
            self.stop_btn.pack_forget()
        else:
            self.stop_btn.pack(pady=(0, 8), after=self.part_lbl)

    def _snap_check(self):
        """松手时距屏幕左右边缘 < EDGE_SNAP 则吸附贴边，文字镜像对齐"""
        root = self._root
        sw = root.winfo_screenwidth()
        x = root.winfo_x()
        y = root.winfo_y()
        w = root.winfo_width()
        if x <= EDGE_SNAP:
            self._snapped, nx, anchor, just = "left", 0, "w", "left"
        elif x + w >= sw - EDGE_SNAP:
            self._snapped, nx, anchor, just = "right", sw - w, "e", "right"
        else:
            self._snapped, anchor, just = None, "w", "left"
            return
        root.geometry(f"+{nx}+{y}")
        for lbl in (self.final_lbl, self.part_lbl):
            lbl.config(anchor=anchor, justify=just)

    def _squish(self):
        """按下时 Q 弹挤压：横向微胀 → 回弹"""
        root = self._root
        w, h = root.winfo_width(), root.winfo_height()
        x, y = root.winfo_x(), root.winfo_y()
        dx = -10 if self._snapped == "right" else 0  # 右吸附时保持右缘不动
        root.geometry(f"{w + 10}x{max(h - 6, 60)}+{x + dx}+{y}")

        def settle():
            root.geometry(f"{w}x{h}+{x + dx}+{y}")
        root.after(110, settle)
