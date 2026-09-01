#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
import yaml

BAND_FREQS = ["31Hz", "62Hz", "125Hz", "250Hz", "500Hz",
              "1kHz", "2kHz", "4kHz", "8kHz", "16kHz"]

DEFAULT_Q_VALUES = [1.0, 1.0, 1.0, 1.0, 2.5, 1.0, 1.0, 1.0, 4.0, 1.0]

EQ_PRESETS = {
    "flat":       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "bass_boost": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "vocal_boost":[-2,-2, 0, 2, 4, 4, 3, 1, 0,-1],
    "treble_boost":[0, 0, 0, 0, 0, 1, 3, 5, 6, 6],
    "rock":       [4, 3, 0,-2,-3, 0, 2, 3, 4, 5],
    "pop":       [-1, 1, 3, 3, 0,-1,-1, 0, 1, 2],
}

THEME_FIELDS = [
    ("primary", "主色"), ("secondary", "次色"), ("accent", "强调色"),
    ("text", "文本色"), ("dim", "暗淡色"), ("bg_bar", "背景条色"),
    ("lyric_current", "当前歌词色"), ("lyric_context", "上下文歌词色"),
]


def _default_cfg_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[j:j + 2], 16) for j in (0, 2, 4))


def _rgb_to_hex(rgb):
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _extract_hex(style_str: str) -> str:
    """从 rich 样式字符串中提取十六进制颜色，如 'bold #ffe600' → '#ffe600'"""
    m = re.search(r'#[0-9a-fA-F]{6}', style_str)
    if m:
        return m.group(0)
    return style_str


def _ask_color(initial):
    """弹出系统取色器，返回十六进制色或 None"""
    result = colorchooser.askcolor(color=initial, title="选择颜色")
    if result and result[1]:
        return result[1]
    return None


def _gradient_color(t: float, colors: list) -> str:
    """t in [0,1]，在给定的颜色列表间做线性插值"""
    if len(colors) == 1:
        return colors[0]
    t = max(0.0, min(1.0, t))
    seg = t * (len(colors) - 1)
    i = min(int(seg), len(colors) - 2)
    local_t = seg - i
    c1 = _hex_to_rgb(colors[i])
    c2 = _hex_to_rgb(colors[i + 1])
    rgb = tuple(int(c1[k] + (c2[k] - c1[k]) * local_t) for k in range(3))
    return _rgb_to_hex(rgb)


class ConfigGUI:
    def __init__(self, cfg_path: str | None = None):
        self.cfg_path = cfg_path or _default_cfg_path()
        self.raw = self._load_config()

        self.root = tk.Tk()
        self.root.title("终端音乐播放器 — 配置")
        self.root.resizable(True, True)
        self._sliders_eq: list[ttk.Scale] = []
        self._labels_eq: list[ttk.Label] = []
        self._spins_q: list[ttk.Spinbox] = []
        self._theme_preview_canvas = None
        self._dirs_listbox = None
        self._theme_names = []

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 配置读写 ----------
    def _load_config(self) -> dict:
        if not os.path.exists(self.cfg_path):
            print(f"配置文件不存在: {self.cfg_path}")
            sys.exit(1)
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            print("配置文件格式错误")
            sys.exit(1)
        return data

    def _save_config(self):
        self._collect_eq()
        self._collect_theme()
        self._collect_spectrum_lyrics()
        self._collect_dsp()
        self._collect_dirs()
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.raw, f, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
        messagebox.showinfo("保存成功", f"配置已写入 {self.cfg_path}\n播放器中按 r 键热重载。")

    # ---------- UI 构建 ----------
    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_eq_tab(notebook)
        self._build_theme_tab(notebook)
        self._build_spectrum_tab(notebook)
        self._build_dsp_tab(notebook)
        self._build_dirs_tab(notebook)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="保存配置", command=self._save_config).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="取消", command=self.root.destroy).pack(side="right", padx=4)

    # --- 均衡器 Tab ---
    def _build_eq_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="均衡器")

        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="预设:").pack(side="left")
        preset_var = tk.StringVar(value=self.raw.get("equalizer", {}).get("preset", "custom"))
        preset_cb = ttk.Combobox(top, textvariable=preset_var,
                                 values=["custom"] + list(EQ_PRESETS.keys()),
                                 state="readonly", width=14)
        preset_cb.pack(side="left", padx=4)
        preset_cb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset(preset_var.get()))

        eq_enabled = tk.BooleanVar(value=self.raw.get("equalizer", {}).get("enabled", True))
        ttk.Checkbutton(top, text="启用均衡器", variable=eq_enabled).pack(side="left", padx=12)
        self._eq_enabled_var = eq_enabled

        bands_frame = ttk.Frame(frame)
        bands_frame.pack(fill="both", expand=True, padx=8, pady=8)

        eq_raw = self.raw.get("equalizer", {})
        bands_map = eq_raw.get("bands", {})
        current_vals = [float(bands_map.get(f, 0)) for f in BAND_FREQS]
        q_map = eq_raw.get("q_values", {})
        current_q = [float(q_map.get(f, dq)) for f, dq in zip(BAND_FREQS, DEFAULT_Q_VALUES)]

        for i, freq in enumerate(BAND_FREQS):
            col = ttk.Frame(bands_frame)
            col.grid(row=0, column=i, padx=4, sticky="ns")
            ttk.Label(col, text=freq, font=("", 8)).pack()
            val_label = ttk.Label(col, text=f"{current_vals[i]:+.0f}", width=4)
            val_label.pack()
            slider = ttk.Scale(col, from_=12, to=-12, orient="vertical")
            slider.pack(fill="y", pady=4)
            ttk.Label(col, text="Q", font=("", 7)).pack()
            q_spin = ttk.Spinbox(col, from_=0.1, to=10.0, increment=0.1,
                                 width=4, font=("", 7))
            q_spin.set(current_q[i])
            q_spin.pack()
            self._sliders_eq.append(slider)
            self._labels_eq.append(val_label)
            self._spins_q.append(q_spin)
            slider.set(current_vals[i])
            slider.configure(command=lambda v, idx=i: self._on_eq_slider(v, idx))

    def _on_eq_slider(self, value: str, idx: int):
        val = round(float(value))
        self._labels_eq[idx].config(text=f"{val:+d}")

    def _apply_preset(self, name: str):
        if name == "custom":
            return
        vals = EQ_PRESETS.get(name)
        if not vals:
            return
        for i, v in enumerate(vals):
            self._sliders_eq[i].set(v)
            self._labels_eq[i].config(text=f"{v:+d}")

    def _collect_eq(self):
        eq = self.raw.setdefault("equalizer", {})
        eq["enabled"] = self._eq_enabled_var.get()
        bands = {}
        q_values = {}
        for i, freq in enumerate(BAND_FREQS):
            bands[freq] = round(float(self._sliders_eq[i].get()))
            q_values[freq] = round(float(self._spins_q[i].get()), 2)
        eq["bands"] = bands
        eq["q_values"] = q_values

    # --- 主题 Tab ---
    def _build_theme_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="主题")

        left = ttk.Frame(frame)
        left.pack(side="left", fill="y", padx=8, pady=8)

        ttk.Label(left, text="主题列表").pack(anchor="w")
        themes = self.raw.get("themes", {})
        self._theme_names = list(themes.keys())
        current_theme = self.raw.get("theme", "neon")
        listbox = tk.Listbox(left, height=10, width=20)
        listbox.pack(fill="y", pady=4)
        for name in self._theme_names:
            listbox.insert("end", themes[name].get("name", name))
        if current_theme in self._theme_names:
            listbox.selection_set(self._theme_names.index(current_theme))
        self._theme_listbox = listbox
        listbox.bind("<<ListboxSelect>>", self._on_theme_select)

        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        self._theme_right = right
        self._theme_color_vars = {}
        self._theme_gradient_vars = {}

        # 预览 Canvas
        self._theme_preview_canvas = tk.Canvas(right, width=300, height=80, bg="#1a1a1a")
        self._theme_preview_canvas.pack(fill="x", pady=(0, 8))

        # 颜色按钮
        colors_frame = ttk.Frame(right)
        colors_frame.pack(fill="x")
        self._color_buttons = []
        self._on_theme_select()

        ttk.Button(right, text="复制当前主题", command=self._duplicate_theme).pack(anchor="w", pady=8)

    def _on_theme_select(self, event=None):
        sel = self._theme_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        key = self._theme_names[idx]
        theme = self.raw.get("themes", {}).get(key, {})
        self._current_theme_key = key
        self.raw["theme"] = key

        for btn in self._color_buttons:
            btn.destroy()
        self._color_buttons.clear()
        self._theme_color_vars.clear()

        for field, label in THEME_FIELDS:
            val = theme.get(field, "#888888")
            var = tk.StringVar(value=val)
            self._theme_color_vars[field] = var
            row = ttk.Frame(self._theme_right)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=14).pack(side="left")
            btn = tk.Button(row, textvariable=var, width=10,
                           command=lambda f=field: self._pick_color(f))
            btn.pack(side="left", padx=4)
            self._color_buttons.append(row)

        # 渐变色列表
        grad = theme.get("spectrum_gradient", [])
        for i, color in enumerate(grad):
            var = tk.StringVar(value=color)
            self._theme_gradient_vars[f"grad_{i}"] = var
            row = ttk.Frame(self._theme_right)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=f"渐变{i+1}", width=14).pack(side="left")
            btn = tk.Button(row, textvariable=var, width=10,
                           command=lambda idx=i: self._pick_gradient(idx))
            btn.pack(side="left", padx=4)
            self._color_buttons.append(row)

        self._draw_theme_preview()

    def _pick_color(self, field: str):
        current = _extract_hex(self._theme_color_vars[field].get())
        new = _ask_color(current)
        if new:
            self._theme_color_vars[field].set(new)
            self._draw_theme_preview()

    def _pick_gradient(self, idx: int):
        key = f"grad_{idx}"
        current = _extract_hex(self._theme_gradient_vars[key].get())
        new = _ask_color(current)
        if new:
            self._theme_gradient_vars[key].set(new)
            self._draw_theme_preview()

    def _draw_theme_preview(self):
        cvs = self._theme_preview_canvas
        cvs.delete("all")
        if not hasattr(self, "_current_theme_key"):
            return

        bg = _extract_hex(self._theme_color_vars.get("bg_bar", tk.StringVar(value="#1a1a1a")).get())
        primary = _extract_hex(self._theme_color_vars.get("primary", tk.StringVar(value="#ff2ec4")).get())
        accent = _extract_hex(self._theme_color_vars.get("accent", tk.StringVar(value="#ffe600")).get())
        text_c = _extract_hex(self._theme_color_vars.get("text", tk.StringVar(value="#e0e0e0")).get())
        lyric_cur = _extract_hex(self._theme_color_vars.get("lyric_current", tk.StringVar(value="#ffffff")).get())

        grad_colors = []
        i = 0
        while f"grad_{i}" in self._theme_gradient_vars:
            grad_colors.append(_extract_hex(self._theme_gradient_vars[f"grad_{i}"].get()))
            i += 1

        w, h = 300, 80
        cvs.config(bg=bg)
        # 标题行
        cvs.create_text(8, 8, anchor="nw", text="▶ 谜底 · 花僮/洛天依  01:35/04:29",
                        fill=text_c, font=("", 9, "bold"))
        # 进度条
        cvs.create_rectangle(8, 28, 200, 30, fill=accent, outline="")
        cvs.create_rectangle(200, 28, 292, 30, fill=text_c, outline="", stipple="gray50")
        # 歌词行
        cvs.create_text(8, 38, anchor="nw", text="♪ 我是真的真的很爱你 期待有天能和你相遇", fill=lyric_cur, font=("", 9))
        # 频谱预览（左右渐变，与终端一致）
        bar_x = 8
        bar_w = 5
        gap = 2
        for col in range(28):
            t = col / 28
            if len(grad_colors) >= 2:
                color = _gradient_color(t, grad_colors)
            else:
                color = primary
            bh = 8 + int(12 * (0.3 + 0.7 * abs(((col * 0.3) % 1.0))))
            cvs.create_rectangle(bar_x, h - bh, bar_x + bar_w, h - 2,
                                 fill=color, outline="")
            bar_x += bar_w + gap

    def _duplicate_theme(self):
        if not hasattr(self, "_current_theme_key"):
            return
        theme = self.raw["themes"][self._current_theme_key].copy()
        new_key = f"{self._current_theme_key}_copy"
        while new_key in self.raw.get("themes", {}):
            new_key += "2"
        theme["name"] = theme.get("name", new_key) + " (副本)"
        self.raw.setdefault("themes", {})[new_key] = theme
        self._theme_names.append(new_key)
        self._theme_listbox.insert("end", theme["name"])

    def _collect_theme(self):
        if not hasattr(self, "_current_theme_key"):
            return
        key = self._current_theme_key
        theme = self.raw["themes"][key]
        for field, _ in THEME_FIELDS:
            theme[field] = self._theme_color_vars[field].get()
        grad = []
        i = 0
        while f"grad_{i}" in self._theme_gradient_vars:
            grad.append(self._theme_gradient_vars[f"grad_{i}"].get())
            i += 1
        if grad:
            theme["spectrum_gradient"] = grad
        self.raw["theme"] = key

    # --- 频谱/歌词/播放 Tab ---
    def _build_spectrum_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="频谱·歌词·播放")
        self._build_spectrum_section(frame)
        self._build_lyrics_section(frame)
        self._build_playback_section(frame)

    def _build_spectrum_section(self, parent):
        grp = ttk.LabelFrame(parent, text="频谱")
        grp.pack(fill="x", padx=8, pady=4)

        spec = self.raw.get("spectrum", {})

        ttk.Label(grp, text="高度 (1~5):").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._spec_height_var = tk.IntVar(value=spec.get("height", 4))
        ttk.Spinbox(grp, from_=1, to=5, textvariable=self._spec_height_var, width=6).grid(
            row=0, column=1, sticky="w")

        ttk.Label(grp, text="高度缩放 (0.1~5.0):").grid(row=0, column=2, sticky="w", padx=(12, 4), pady=2)
        self._spec_height_scale_var = tk.DoubleVar(value=spec.get("height_scale", 1.0))
        ttk.Spinbox(grp, from_=0.1, to=5.0, increment=0.1,
                    textvariable=self._spec_height_scale_var, width=6).grid(
            row=0, column=3, sticky="w")

        ttk.Label(grp, text="占位符:").grid(row=1, column=2, sticky="w", padx=(12, 4), pady=2)
        self._zhanwei_char = tk.StringVar(value=spec.get("zhanwei_char", "▁"))
        ttk.Entry(grp, textvariable=self._zhanwei_char, width=6).grid(
            row=1, column=3, sticky="w")

        ttk.Label(grp, text="柱数:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._spec_bars_var = tk.IntVar(value=spec.get("bars", 48))
        ttk.Spinbox(grp, from_=8, to=120, textvariable=self._spec_bars_var, width=6).grid(
            row=1, column=1, sticky="w")

        ttk.Label(grp, text="FFT 大小:").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._spec_fft_var = tk.IntVar(value=spec.get("fft_size", 2048))
        ttk.Spinbox(grp, from_=256, to=8192, increment=256,
                    textvariable=self._spec_fft_var, width=6).grid(row=2, column=1, sticky="w")

        ttk.Label(grp, text="平滑系数:").grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self._spec_smooth_var = tk.DoubleVar(value=spec.get("smoothing", 0.6))
        self._spec_smooth_scale = ttk.Scale(grp, from_=0, to=0.97, orient="horizontal",
                  command=lambda v: self._spec_smooth_var.set(round(float(v), 2)))
        self._spec_smooth_scale.set(spec.get("smoothing", 0.6))
        self._spec_smooth_scale.grid(row=3, column=1, sticky="ew")

        self._spec_enabled_var = tk.BooleanVar(value=spec.get("enabled", True))
        ttk.Checkbutton(grp, text="默认显示频谱", variable=self._spec_enabled_var).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def _build_lyrics_section(self, parent):
        grp = ttk.LabelFrame(parent, text="歌词")
        grp.pack(fill="x", padx=8, pady=4)

        lyr = self.raw.get("lyrics", {})

        self._lyr_fuzzy_var = tk.BooleanVar(value=lyr.get("fuzzy_match", True))
        ttk.Checkbutton(grp, text="模糊匹配", variable=self._lyr_fuzzy_var).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Label(grp, text="阈值:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._lyr_threshold_var = tk.DoubleVar(value=lyr.get("fuzzy_threshold", 0.55))
        self._lyr_threshold_scale = ttk.Scale(grp, from_=0.3, to=1.0, orient="horizontal",
                  command=lambda v: self._lyr_threshold_var.set(round(float(v), 2)))
        self._lyr_threshold_scale.set(lyr.get("fuzzy_threshold", 0.55))
        self._lyr_threshold_scale.grid(row=1, column=1, sticky="ew")

        ttk.Label(grp, text="偏移(ms):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._lyr_offset_var = tk.IntVar(value=lyr.get("offset_ms", 0))
        ttk.Spinbox(grp, from_=-10000, to=10000, increment=100,
                    textvariable=self._lyr_offset_var, width=8).grid(row=2, column=1, sticky="w")

    def _build_playback_section(self, parent):
        grp = ttk.LabelFrame(parent, text="播放")
        grp.pack(fill="x", padx=8, pady=4)

        pb = self.raw.get("playback", {})

        ttk.Label(grp, text="默认音量:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._pb_vol_var = tk.DoubleVar(value=pb.get("default_volume", 0.8))
        self._pb_vol_scale = ttk.Scale(grp, from_=0, to=1.0, orient="horizontal",
                  command=lambda v: self._pb_vol_var.set(round(float(v), 2)))
        self._pb_vol_scale.set(pb.get("default_volume", 0.8))
        self._pb_vol_scale.grid(row=0, column=1, sticky="ew")

        ttk.Label(grp, text="播放模式:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._pb_mode_var = tk.StringVar(value=pb.get("playlist_mode", "sequential"))
        ttk.Combobox(grp, textvariable=self._pb_mode_var, state="readonly", width=10,
                     values=["sequential", "shuffle", "repeat_one", "repeat_all"]).grid(
            row=1, column=1, sticky="w")

        ttk.Label(grp, text="UI FPS:").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._pb_fps_var = tk.IntVar(value=pb.get("ui_fps", 20))
        ttk.Spinbox(grp, from_=5, to=60, textvariable=self._pb_fps_var, width=6).grid(
            row=2, column=1, sticky="w")

        ttk.Label(grp, text="界面宽度:").grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self._pb_width_var = tk.IntVar(value=pb.get("ui_width", 50))
        ttk.Spinbox(grp, from_=30, to=200, textvariable=self._pb_width_var, width=6).grid(
            row=3, column=1, sticky="w")

    def _collect_spectrum_lyrics(self):
        spec = self.raw.setdefault("spectrum", {})
        spec["height"] = max(1, min(5, self._spec_height_var.get()))
        spec["height_scale"] = round(max(0.1, float(self._spec_height_scale_var.get())), 2)
        spec["bars"] = self._spec_bars_var.get()
        spec["fft_size"] = self._spec_fft_var.get()
        spec["smoothing"] = round(self._spec_smooth_var.get(), 2)
        spec["enabled"] = self._spec_enabled_var.get()
        spec["zhanwei_char"] = self._zhanwei_char.get()

        lyr = self.raw.setdefault("lyrics", {})
        lyr["fuzzy_match"] = self._lyr_fuzzy_var.get()
        lyr["fuzzy_threshold"] = round(self._lyr_threshold_var.get(), 2)
        lyr["offset_ms"] = self._lyr_offset_var.get()

        pb = self.raw.setdefault("playback", {})
        pb["default_volume"] = round(self._pb_vol_var.get(), 2)
        pb["playlist_mode"] = self._pb_mode_var.get()
        pb["ui_fps"] = self._pb_fps_var.get()
        pb["ui_width"] = self._pb_width_var.get()

    # --- 音质增强 DSP Tab ---
    def _build_dsp_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="音质增强")
        dsp = self.raw.get("dsp", {})

        # ---- 响度均衡 ----
        loud = ttk.LabelFrame(frame, text="响度均衡 (LUFS)")
        loud.pack(fill="x", padx=8, pady=4)
        self._dsp_loud_enabled = tk.BooleanVar(value=dsp.get("loudness", {}).get("enabled", False))
        ttk.Checkbutton(loud, text="启用响度均衡", variable=self._dsp_loud_enabled).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(loud, text="目标响度 (LUFS):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._dsp_loud_target = tk.DoubleVar(value=dsp.get("loudness", {}).get("t   arget_lufs", -16.0))
        ttk.Spinbox(loud, from_=-30, to=-8, increment=1,
                    textvariable=self._dsp_loud_target, width=8).grid(
            row=1, column=1, sticky="w")

        # ---- 虚拟低音 ----
        vbe = ttk.LabelFrame(frame, text="虚拟低音增强 (VBE)")
        vbe.pack(fill="x", padx=8, pady=4)
        self._dsp_vbe_enabled = tk.BooleanVar(value=dsp.get("vbe", {}).get("enabled", False))
        ttk.Checkbutton(vbe, text="启用虚拟低音", variable=self._dsp_vbe_enabled).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(vbe, text="谐波混合增益 (dB):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._dsp_vbe_gain = tk.DoubleVar(value=dsp.get("vbe", {}).get("gain_db", -3.0))
        ttk.Spinbox(vbe, from_=-6, to=3, increment=0.5,
                    textvariable=self._dsp_vbe_gain, width=8).grid(
            row=1, column=1, sticky="w")

        # ---- 软限幅器 ----
        lim = ttk.LabelFrame(frame, text="软限幅器 (Soft Limiter)")
        lim.pack(fill="x", padx=8, pady=4)
        self._dsp_lim_enabled = tk.BooleanVar(value=dsp.get("limiter", {}).get("enabled", False))
        ttk.Checkbutton(lim, text="启用软限幅器", variable=self._dsp_lim_enabled).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(lim, text="阈值 (dBFS):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._dsp_lim_threshold = tk.DoubleVar(value=dsp.get("limiter", {}).get("threshold_db", -1.0))
        ttk.Spinbox(lim, from_=-12, to=0, increment=0.5,
                    textvariable=self._dsp_lim_threshold, width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Label(lim, text="释放时间 (ms):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._dsp_lim_release = tk.DoubleVar(value=dsp.get("limiter", {}).get("release_ms", 50.0))
        ttk.Spinbox(lim, from_=5, to=500, increment=5,
                    textvariable=self._dsp_lim_release, width=8).grid(
            row=2, column=1, sticky="w")

        ttk.Label(frame, text="提示: 各模块独立开关，全部关闭时不影响原有播放链路。",
                  foreground="#888").pack(anchor="w", padx=8, pady=4)

    def _collect_dsp(self):
        dsp = self.raw.setdefault("dsp", {})
        dsp.setdefault("loudness", {})["enabled"] = self._dsp_loud_enabled.get()
        dsp["loudness"]["target_lufs"] = self._dsp_loud_target.get()
        dsp.setdefault("vbe", {})["enabled"] = self._dsp_vbe_enabled.get()
        dsp["vbe"]["gain_db"] = self._dsp_vbe_gain.get()
        dsp.setdefault("limiter", {})["enabled"] = self._dsp_lim_enabled.get()
        dsp["limiter"]["threshold_db"] = self._dsp_lim_threshold.get()
        dsp["limiter"]["release_ms"] = self._dsp_lim_release.get()

    # --- 音频目录 Tab ---
    def _build_dirs_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="音频目录")

        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="添加文件夹", command=self._add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="添加文件", command=self._add_file).pack(side="left", padx=4)
        ttk.Button(top, text="删除选中", command=self._remove_dir).pack(side="left", padx=4)

        ttk.Label(frame, text="音频目录:").pack(anchor="w", padx=8)
        dirs = self.raw.get("music_dirs", [])
        if isinstance(dirs, str):
            dirs = [dirs]
        self._dirs_listbox = tk.Listbox(frame, height=8)
        self._dirs_listbox.pack(fill="both", expand=True, padx=8, pady=4)
        for d in dirs:
            self._dirs_listbox.insert("end", d)

        lyr_frame = ttk.Frame(frame)
        lyr_frame.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Label(lyr_frame, text="歌词目录:").pack(side="left")
        self._lyrics_dir_var = tk.StringVar(value=self.raw.get("lyrics_dir", ""))
        ttk.Entry(lyr_frame, textvariable=self._lyrics_dir_var, width=40).pack(
            side="left", padx=4, fill="x", expand=True)
        ttk.Button(lyr_frame, text="浏览…", command=self._browse_lyrics_dir).pack(side="left")

    def _add_folder(self):
        path = filedialog.askdirectory(title="选择音频文件夹")
        if path:
            self._dirs_listbox.insert("end", path)

    def _add_file(self):
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频", "*.mp3 *.wav *.flac *.ogg *.m4a *.aiff *.aif"), ("所有文件", "*.*")])
        if path:
            self._dirs_listbox.insert("end", path)

    def _remove_dir(self):
        sel = self._dirs_listbox.curselection()
        for idx in reversed(sel):
            self._dirs_listbox.delete(idx)

    def _browse_lyrics_dir(self):
        path = filedialog.askdirectory(title="选择歌词目录")
        if path:
            self._lyrics_dir_var.set(path)

    def _collect_dirs(self):
        dirs = list(self._dirs_listbox.get(0, "end"))
        self.raw["music_dirs"] = dirs
        self.raw.pop("music_dir", None)
        self.raw["lyrics_dir"] = self._lyrics_dir_var.get()

    # ---------- 关闭 ----------
    def _on_close(self):
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="终端音乐播放器 GUI 配置工具")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    args = parser.parse_args()
    cfg_path = args.config or _default_cfg_path()
    gui = ConfigGUI(cfg_path)
    gui.run()


if __name__ == "__main__":
    main()
