"""基于 rich 的紧凑终端界面渲染（≤7行）"""
from __future__ import annotations
import os
import numpy as np
from rich.console import Group
from rich.text import Text
from rich.cells import cell_len

SPECTRUM_CHARS = " ▁▂▃▄▅▆▇█"

MODE_LABEL_ZH = {
    "sequential": "顺序",
    "shuffle": "随机",
    "repeat_one": "单曲",
    "repeat_all": "列表",
}


def _gradient_color(t: float, colors: list) -> str:
    """t in [0,1]，在给定的颜色列表间做线性插值"""
    if len(colors) == 1:
        return colors[0]
    t = float(np.clip(t, 0.0, 1.0))
    seg = t * (len(colors) - 1)
    i = min(int(seg), len(colors) - 2)
    local_t = seg - i

    def hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[j:j + 2], 16) for j in (0, 2, 4))

    c1 = hex_to_rgb(colors[i])
    c2 = hex_to_rgb(colors[i + 1])
    rgb = tuple(int(c1[k] + (c2[k] - c1[k]) * local_t) for k in range(3))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _fit_text(text: str, target_width: int) -> str:
    """调整文本使显示宽度恰好为 target_width：
    短则右侧补空格，长则截断加 …"""
    w = cell_len(text)
    if w < target_width:
        return text + " " * (target_width - w)
    if w == target_width:
        return text
    # 截断：找到最长前缀使 prefix + "…" 的宽度 <= target_width
    result = ""
    for ch in text:
        if cell_len(result + ch + "…") > target_width:
            break
        result += ch
    return result + "…"


def render_spectrum(levels: np.ndarray, theme, height: int = 4,
                    width: int = 50, scale: float = 1.0,
                    placeholder: str = "▁") -> Text:
    """把 0~1 的能量数组渲染成竖直条形频谱。
    渐变方向为左右（按列位置取色），空格位用 dim 色的占位符填充消除割裂感。
    scale 为高度缩放因子：<1 变矮（柱体更内敛），>1 变高（柱体更丰满），
    有效填充量始终 clamp 在 [0, rows] 之间不会溢出。
    占位符为未填充格位使用的字符，可配置为 ▁ / . / - / 空格 等。
    渐变色按宽度量化为相邻不可分辨的色级，同色同字符的相邻列合并为单个
    样式段，减少每帧输出段数（旧式 Windows 控制台逐段重绘，段数越多越闪烁）。"""
    text = Text(no_wrap=True)
    rows = height

    if len(levels) != width:
        src = np.linspace(0, 1, len(levels))
        dst = np.linspace(0, 1, width)
        levels = np.interp(dst, src, levels)
    n = len(levels)

    # 预计算每列颜色：量化级数按宽度缩放（约每 3 列一个色级），
    # 色带宽度小于一个柱宽，肉眼不可见；相邻同色列仍可合并为长段
    grad_steps = min(max(width // 3, 16), 48)
    col_colors = []
    for col_idx in range(n):
        t = col_idx / n
        qt = round(t * (grad_steps - 1)) / (grad_steps - 1)
        col_colors.append(_gradient_color(qt, theme.spectrum_gradient))

    columns = []
    for lvl in levels:
        col_chars = []
        remaining = float(min(lvl * rows * scale, rows))
        for _ in range(rows):
            if remaining >= 1:
                col_chars.append(8)
                remaining -= 1
            elif remaining > 0:
                col_chars.append(max(1, int(remaining * 8)))
                remaining = 0
            else:
                col_chars.append(0)
        columns.append(col_chars)

    for row in range(rows - 1, -1, -1):
        run_ch = None
        run_style = None
        run_len = 0
        for col_idx, col in enumerate(columns):
            v = col[row]
            ch = placeholder if v == 0 else SPECTRUM_CHARS[v]
            style = theme.dim if v == 0 else col_colors[col_idx]
            if ch == run_ch and style == run_style:
                run_len += 1
            else:
                if run_len:
                    text.append(run_ch * run_len, style=run_style)
                run_ch, run_style, run_len = ch, style, 1
        if run_len:
            text.append(run_ch * run_len, style=run_style)
        if row > 0:
            text.append("\n")
    return text


def _render_title_line(track, player, playlist, theme, width: int) -> Text:
    """行1: 状态图标 标题 · 艺人 时间 [模式] 音量
    所有部分拼接后显示宽度恰好等于 width。"""
    mode_label = MODE_LABEL_ZH.get(playlist.mode, playlist.mode)
    state_icon = {"playing": "▶", "paused": "⏸", "stopped": "⏹"}.get(
        player.state.value, "▶")
    pos = player.position_sec
    dur = max(track.duration_sec, player.duration_sec, 0.001) if track else 0.001
    vol_pct = int(player.volume * 100)

    # 图标 + 1空格
    icon_part = f"{state_icon} "
    # 后缀：1空格间隔各元素
    suffix = f" {_fmt_time(pos)}/{_fmt_time(dur)} [{mode_label}]"
    if playlist.queue_len() > 0:
        suffix += f" [队列:{playlist.queue_len()}]"
    suffix += f" 🔊{vol_pct}%"

    icon_w = cell_len(icon_part)
    suffix_w = cell_len(suffix)
    available = max(6, width - icon_w - suffix_w)

    if track:
        title_str = f"{track.title} · {track.artist}"
    else:
        title_str = "无曲目"

    fitted = _fit_text(title_str, available)

    line = Text(no_wrap=True)
    line.append(icon_part, style=theme.accent)
    line.append(fitted, style=f"bold {theme.primary}")
    line.append(suffix, style=theme.secondary)
    return line


def _render_progress_bar(track, player, theme, width: int = 50) -> Text:
    """行2: 进度条"""
    if track is None:
        return Text("─" * width, style=theme.dim)
    pos = player.position_sec
    dur = max(track.duration_sec, player.duration_sec, 0.001)
    ratio = min(pos / dur, 1.0) if dur > 0 else 0.0
    filled = int(ratio * width)
    bar = Text(no_wrap=True)
    bar.append("━" * filled, style=theme.accent)
    if filled < width:
        bar.append("╸", style=theme.accent)
        bar.append("─" * max(0, width - filled - 1), style=theme.dim)
    return bar


def _render_lyrics_line(lyrics_data, position_ms: int, theme, width: int) -> Text:
    """行3: 歌词单行（超长截断）"""
    if lyrics_data is None or not lyrics_data.lines:
        return Text("♪", style=theme.dim)
    idx = lyrics_data.find_index(position_ms)
    if idx < 0:
        return Text("♪", style=theme.dim)
    content = lyrics_data.lines[idx].text or "♪"
    return Text(_fit_text(content, width), style=theme.lyric_current)


def build_compact_ui(cfg, track, player, spectrum_levels, lyrics_data,
                     playlist, spectrum_on: bool, width: int = 50) -> Group:
    """构建紧凑界面（开频谱 ≤7行，关频谱 ≤3行）"""
    theme = cfg.theme
    pos_ms = int(player.position_sec * 1000) + cfg.lyrics.offset_ms
    title = _render_title_line(track, player, playlist, theme, width)
    progress = _render_progress_bar(track, player, theme, width)
    lyrics = _render_lyrics_line(lyrics_data, pos_ms, theme, width)

    if not spectrum_on or cfg.spectrum.height < 1:
        return Group(title, progress, lyrics)

    spec_text = render_spectrum(spectrum_levels, theme,
                                height=cfg.spectrum.height, width=width,
                                scale=cfg.spectrum.height_scale,
                                placeholder=cfg.spectrum.zhanwei_char)
    return Group(title, progress, lyrics, Text(""), spec_text)


def build_song_selector_ui(cfg, playlist, search_str: str,
                           filtered_indices: list, selector_index: int,
                           width: int = 50) -> Group:
    """构建歌曲选择界面：搜索框 + 歌曲列表 + 操作提示"""
    theme = cfg.theme
    lines = []

    # 标题
    lines.append(Text("🎵 歌曲选择", style=f"bold {theme.accent}"))

    # 搜索输入行
    search_line = Text(no_wrap=True)
    search_line.append("搜索: ", style=theme.secondary)
    search_line.append(search_str, style=theme.primary)
    search_line.append("█", style=theme.accent)
    lines.append(search_line)

    # 歌曲列表（窗口滚动）
    total = len(filtered_indices)
    max_visible = 15

    if total == 0:
        lines.append(Text("  无匹配结果", style=theme.dim))
    else:
        start = max(0, selector_index - max_visible // 2)
        end = min(total, start + max_visible)
        if end - start < max_visible and start > 0:
            start = max(0, end - max_visible)

        for i in range(start, end):
            orig_idx = filtered_indices[i]
            filename = os.path.basename(playlist.tracks[orig_idx])
            name = os.path.splitext(filename)[0]
            name = _fit_text(name, width - 10)

            line = Text(no_wrap=True)
            if i == selector_index:
                line.append(f"  ▶ {name}", style=f"bold {theme.accent}")
            else:
                line.append(f"    {name}", style=theme.text)

            if orig_idx == playlist.index:
                line.append(" [播放中]", style=theme.primary)
            if playlist.is_in_queue(orig_idx):
                line.append(" [队列]", style=theme.secondary)

            lines.append(line)

        # 滚动提示
        if total > max_visible:
            hint = f"  ({selector_index + 1}/{total})"
            lines.append(Text(hint, style=theme.dim))

    # 操作提示
    lines.append(Text("Enter: 立即播放  →: 加入队列  Tab/Esc: 返回",
                       style=theme.dim))

    return Group(*lines)
