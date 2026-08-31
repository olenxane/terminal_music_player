"""基于 rich 的紧凑终端界面渲染（≤7行）"""
from __future__ import annotations
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
                    width: int = 50) -> Text:
    """把 0~1 的能量数组渲染成竖直条形频谱。
    渐变方向为左右（按列位置取色），空格位用 dim 色的 ▁ 填充消除割裂感。"""
    text = Text(no_wrap=True)
    rows = height

    if len(levels) != width:
        src = np.linspace(0, 1, len(levels))
        dst = np.linspace(0, 1, width)
        levels = np.interp(dst, src, levels)
    n = len(levels)

    columns = []
    for lvl in levels:
        col_chars = []
        remaining = float(lvl * rows)
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
        for col_idx, col in enumerate(columns):
            v = col[row]
            if v == 0:
                text.append("▁", style=theme.dim)
            else:
                color = _gradient_color(col_idx / n, theme.spectrum_gradient)
                text.append(SPECTRUM_CHARS[v], style=color)
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
    suffix = f" {_fmt_time(pos)}/{_fmt_time(dur)} [{mode_label}] 🔊{vol_pct}%"

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
                                height=cfg.spectrum.height, width=width)
    return Group(title, progress, lyrics, Text(""), spec_text)
