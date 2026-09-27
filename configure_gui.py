#!/usr/bin/env python3
from __future__ import annotations
import os
import re
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog
import yaml

from mp.logging_utils import log_error

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

# 按键绑定动作：(配置字段, 界面说明, 默认键)
KEYBIND_ACTIONS = [
    ("quit", "退出程序", "q"),
    ("selector", "歌曲选择器", "tab"),
    ("online", "在线音乐", "o"),
    ("play_pause", "播放/暂停", "space"),
    ("next", "下一首", "n"),
    ("prev", "上一首（历史回退）", "p"),
    ("spectrum", "频谱开关", "s"),
    ("theme", "切换主题", "t"),
    ("mode", "切换播放模式", "m"),
    ("reload", "热重载配置", "r"),
    ("download", "下载当前在线歌曲", "d"),
    ("dir_setup", "重新配置音频目录", "u"),
    ("exciter", "激励器开关", "e"),
    ("widener", "声场展宽开关", "w"),
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
            log_error(f"配置文件不存在: {self.cfg_path}")
            sys.exit(1)
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            log_error("配置文件格式错误")
            sys.exit(1)
        return data

    def _save_config(self):
        self._collect_eq()
        self._collect_theme()
        self._collect_spectrum_lyrics()
        self._collect_dsp()
        self._collect_dirs()
        self._collect_online()
        self._collect_keybindings()
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
        self._build_online_tab(notebook)
        self._build_keybind_tab(notebook)

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
                                 values=self._eq_preset_names(),
                                 state="readonly", width=14)
        preset_cb.pack(side="left", padx=4)
        preset_cb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset(preset_var.get()))
        self._preset_var = preset_var
        self._preset_cb = preset_cb

        ttk.Button(top, text="保存配置", command=self._save_eq_preset).pack(side="left", padx=4)
        ttk.Button(top, text="删除配置", command=self._delete_eq_preset).pack(side="left", padx=4)

        eq_enabled = tk.BooleanVar(value=self.raw.get("equalizer", {}).get("enabled", True))
        ttk.Checkbutton(top, text="启用均衡器", variable=eq_enabled).pack(side="left", padx=12)
        self._eq_enabled_var = eq_enabled

        bands_frame = ttk.Frame(frame)
        bands_frame.pack(fill="both", expand=True, padx=8, pady=8)

        eq_raw = self.raw.get("equalizer", {})
        preset_name = eq_raw.get("preset", "custom")
        config_presets = eq_raw.get("presets", {})
        if preset_name != "custom" and preset_name in config_presets:
            current_vals = [float(v) for v in config_presets[preset_name]]
        else:
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
        # 手动调整滑块后不再属于任何预设，切回自定义
        if self._preset_var.get() != "custom":
            self._preset_var.set("custom")

    def _apply_preset(self, name: str):
        if name == "custom":
            return
        vals = EQ_PRESETS.get(name)
        if vals is None:
            vals = self.raw.get("equalizer", {}).get("presets", {}).get(name)
        if not vals:
            return
        for i, v in enumerate(vals):
            self._sliders_eq[i].set(v)
            self._labels_eq[i].config(text=f"{v:+d}")

    def _eq_preset_names(self) -> list:
        """下拉框可用预设：内置 + 配置文件中已有的自定义预设（去重）"""
        names = ["custom"] + list(EQ_PRESETS.keys())
        config_presets = self.raw.get("equalizer", {}).get("presets", {})
        for key in config_presets:
            if key not in names:
                names.append(key)
        return names

    def _refresh_preset_values(self):
        self._preset_cb["values"] = self._eq_preset_names()

    def _save_eq_preset(self):
        name = simpledialog.askstring("保存均衡器配置", "请输入配置名称：", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in EQ_PRESETS:
            messagebox.showwarning("无法保存", f"“{name}”是内置预设名称，请换一个名称。")
            return
        eq = self.raw.setdefault("equalizer", {})
        presets = eq.setdefault("presets", {})
        vals = [round(float(self._sliders_eq[i].get())) for i in range(len(BAND_FREQS))]
        presets[name] = vals
        self._refresh_preset_values()
        self._preset_var.set(name)
        messagebox.showinfo("已保存", f"均衡器配置“{name}”已保存。\n点击底部“保存配置”写入 config.yaml 生效。")

    def _delete_eq_preset(self):
        name = self._preset_var.get()
        if name == "custom":
            messagebox.showinfo("提示", "当前没有选中可删除的自定义配置。")
            return
        if name in EQ_PRESETS:
            messagebox.showwarning("无法删除", "内置预设不能删除。")
            return
        eq = self.raw.get("equalizer", {})
        presets = eq.get("presets", {})
        if name not in presets:
            return
        if messagebox.askyesno("确认删除", f"确定删除自定义配置“{name}”吗？"):
            del presets[name]
            self._preset_var.set("custom")
            self._refresh_preset_values()

    def _collect_eq(self):
        eq = self.raw.setdefault("equalizer", {})
        eq["enabled"] = self._eq_enabled_var.get()
        eq["preset"] = self._preset_var.get()
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

        self._build_marquee_section(parent)

    def _build_marquee_section(self, parent):
        grp = ttk.LabelFrame(parent, text="标题滚动（超长歌名跑马灯）")
        grp.pack(fill="x", padx=8, pady=4)

        mq = self.raw.get("playback", {}).get("marquee", {})

        self._mq_enabled_var = tk.BooleanVar(value=mq.get("enabled", True))
        ttk.Checkbutton(grp, text="开启截断滚动", variable=self._mq_enabled_var).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=4, pady=2)

        ttk.Label(grp, text="停留秒数:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._mq_hold_var = tk.DoubleVar(value=mq.get("hold_secs", 5.0))
        ttk.Spinbox(grp, from_=0, to=30, increment=0.5,
                    textvariable=self._mq_hold_var, width=6).grid(
            row=1, column=1, sticky="w")

        ttk.Label(grp, text="滚动速度 (秒/列):").grid(row=1, column=2, sticky="w",
                                                     padx=(12, 4), pady=2)
        self._mq_step_var = tk.DoubleVar(value=mq.get("step_interval", 0.3))
        ttk.Spinbox(grp, from_=0.05, to=2.0, increment=0.05,
                    textvariable=self._mq_step_var, width=6).grid(
            row=1, column=3, sticky="w")

        ttk.Label(grp, text="循环间隙 (列):").grid(row=2, column=2, sticky="w",
                                                     padx=(12, 4), pady=2)
        self._mq_gap_var = tk.IntVar(value=mq.get("gap_cols", 4))
        ttk.Spinbox(grp, from_=0, to=20, increment=1,
                    textvariable=self._mq_gap_var, width=6).grid(
            row=2, column=3, sticky="w")

        ttk.Label(grp, text="关闭后超长歌名恢复为静态截断加 …；改完保存后按 r 热重载生效。",
                  foreground="#888").grid(row=2, column=0, columnspan=2,
                                          sticky="w", padx=4, pady=2)

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
        mq = pb.setdefault("marquee", {})
        mq["enabled"] = self._mq_enabled_var.get()
        mq["hold_secs"] = round(max(0.0, float(self._mq_hold_var.get())), 2)
        mq["step_interval"] = round(max(0.05, float(self._mq_step_var.get())), 2)
        mq["gap_cols"] = max(0, int(self._mq_gap_var.get()))

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
        self._dsp_loud_target = tk.DoubleVar(value=dsp.get("loudness", {}).get("target_lufs", -16.0))
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
        self._dsp_lim_release = tk.DoubleVar(value=dsp.get("limiter", {}).get("release_ms", 150.0))
        ttk.Spinbox(lim, from_=5, to=500, increment=5,
                    textvariable=self._dsp_lim_release, width=8).grid(
            row=2, column=1, sticky="w")

        # ---- 谐波激励器 ----
        exc = ttk.LabelFrame(frame, text="谐波激励器 (Exciter)")
        exc.pack(fill="x", padx=8, pady=4)
        self._dsp_exc_enabled = tk.BooleanVar(value=dsp.get("exciter", {}).get("enabled", True))
        ttk.Checkbutton(exc, text="启用激励器（高频光泽）", variable=self._dsp_exc_enabled).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(exc, text="起始频率 (Hz):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._dsp_exc_freq = tk.DoubleVar(value=dsp.get("exciter", {}).get("freq_hz", 3500.0))
        ttk.Spinbox(exc, from_=1000, to=8000, increment=250,
                    textvariable=self._dsp_exc_freq, width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Label(exc, text="混合比例 (0~0.5):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._dsp_exc_mix = tk.DoubleVar(value=dsp.get("exciter", {}).get("mix", 0.15))
        ttk.Spinbox(exc, from_=0.0, to=0.5, increment=0.05,
                    textvariable=self._dsp_exc_mix, width=8).grid(
            row=2, column=1, sticky="w")

        # ---- 声场展宽器 ----
        wid = ttk.LabelFrame(frame, text="声场展宽器 (Stereo Widener)")
        wid.pack(fill="x", padx=8, pady=4)
        self._dsp_wid_enabled = tk.BooleanVar(value=dsp.get("widener", {}).get("enabled", True))
        ttk.Checkbutton(wid, text="启用声场展宽（M/S 扩展）", variable=self._dsp_wid_enabled).grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(wid, text="展宽系数 (1~2):").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._dsp_wid_width = tk.DoubleVar(value=dsp.get("widener", {}).get("width", 1.3))
        ttk.Spinbox(wid, from_=1.0, to=2.0, increment=0.1,
                    textvariable=self._dsp_wid_width, width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Label(wid, text="低频保护 (Hz):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self._dsp_wid_hp = tk.DoubleVar(value=dsp.get("widener", {}).get("hp_freq_hz", 250.0))
        ttk.Spinbox(wid, from_=80, to=500, increment=10,
                    textvariable=self._dsp_wid_hp, width=8).grid(
            row=2, column=1, sticky="w")
        ttk.Label(wid, text="房间混响 (0~1):").grid(row=3, column=0, sticky="w", padx=4, pady=2)
        self._dsp_wid_room = tk.DoubleVar(value=dsp.get("widener", {}).get("room_mix", 0.0))
        ttk.Spinbox(wid, from_=0.0, to=1.0, increment=0.02,
                    textvariable=self._dsp_wid_room, width=8).grid(
            row=3, column=1, sticky="w")

        ttk.Label(frame, text="提示: 各模块独立开关，全部关闭时为纯净输出。"
                              "处理顺序 (dsp.chain) 请在 config.yaml 中编辑；保存后按 r 热重载。",
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
        exc = dsp.setdefault("exciter", {})
        exc["enabled"] = self._dsp_exc_enabled.get()
        exc["freq_hz"] = self._dsp_exc_freq.get()
        exc["mix"] = round(self._dsp_exc_mix.get(), 3)
        wid = dsp.setdefault("widener", {})
        wid["enabled"] = self._dsp_wid_enabled.get()
        wid["width"] = round(self._dsp_wid_width.get(), 2)
        wid["hp_freq_hz"] = self._dsp_wid_hp.get()
        wid["room_mix"] = round(self._dsp_wid_room.get(), 3)

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

    # --- 在线音乐 Tab ---
    def _build_online_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="在线音乐")
        om = self.raw.get("online_music", {})

        grp = ttk.LabelFrame(frame, text="音质设置")
        grp.pack(fill="x", padx=8, pady=8)

        ttk.Label(grp, text="QQ音乐音质:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._om_qq_quality = tk.IntVar(value=om.get("qq_quality", 320))
        ttk.Combobox(grp, textvariable=self._om_qq_quality, state="readonly",
                     width=8, values=[128, 320]).grid(row=0, column=1, sticky="w")

        ttk.Label(grp, text="网易云音乐音质:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self._om_wy_quality = tk.StringVar(value=om.get("wy_quality", "exhigh"))
        ttk.Combobox(grp, textvariable=self._om_wy_quality, state="readonly",
                     width=10, values=["standard", "higher", "exhigh",
                                        "lossless", "hires"]).grid(row=1, column=1, sticky="w")

        ttk.Label(frame, text="提示: QQ音乐默认320k，失败自动回退128k。网易云默认exhigh(320k)。",
                  foreground="#888").pack(anchor="w", padx=8, pady=4)
        ttk.Label(frame, text="登录请在播放器中按 o 进入在线模式，选择平台后扫码登录。",
                  foreground="#888").pack(anchor="w", padx=8, pady=2)

        dl = ttk.LabelFrame(frame, text="下载设置")
        dl.pack(fill="x", padx=8, pady=8)
        ttk.Label(dl, text="下载目录:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self._om_download_dir = tk.StringVar(value=om.get("download_dir", ""))
        ttk.Entry(dl, textvariable=self._om_download_dir, width=44).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(dl, text="浏览…", command=self._browse_download_dir).grid(
            row=0, column=2, padx=4)
        ttk.Label(dl, text="播放在线歌曲时按 d 下载到该目录，同时保存歌词并写入标签封面；"
                  "默认 download_musics 子目录（相对路径以程序目录为基准）。",
                  foreground="#888").grid(row=1, column=0, columnspan=3,
                                          sticky="w", padx=4, pady=2)

    def _browse_download_dir(self):
        path = filedialog.askdirectory(title="选择在线歌曲下载目录")
        if path:
            self._om_download_dir.set(path)

    def _collect_online(self):
        om = self.raw.setdefault("online_music", {})
        om["qq_quality"] = int(self._om_qq_quality.get())
        om["wy_quality"] = self._om_wy_quality.get()
        om["download_dir"] = self._om_download_dir.get().strip()

    # --- 按键绑定 Tab ---
    def _build_keybind_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="按键绑定")
        self._keybind_vars = {}

        kb_raw = self.raw.get("keybindings", {}) or {}
        for row, (field, label, default) in enumerate(KEYBIND_ACTIONS):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w",
                                              padx=8, pady=3)
            var = tk.StringVar(value=kb_raw.get(field, default))
            ent = ttk.Entry(frame, textvariable=var, width=10, justify="center")
            ent.grid(row=row, column=1, padx=4, pady=3)
            self._keybind_vars[field] = var
            ttk.Label(frame, text=f"默认: {default}", foreground="#888").grid(
                row=row, column=2, sticky="w")

        ttk.Label(frame, text="输入单个字符；空格请输入 space，Tab 请输入 tab（不区分大小写）。"
                  "\n方向键（音量/快进快退）与选择器/在线模式内部按键不支持自定义。"
                  "\n方向键（音量/快进快退）与选择器/在线模式内部按键不支持自定义。",
                  foreground="#888").grid(row=len(KEYBIND_ACTIONS), column=0,
                                          columnspan=3, sticky="w", padx=8, pady=8)

    def _collect_keybindings(self):
        kb = self.raw.setdefault("keybindings", {})
        for field, _, default in KEYBIND_ACTIONS:
            v = self._keybind_vars[field].get().strip().lower()
            kb[field] = v if v else default

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
