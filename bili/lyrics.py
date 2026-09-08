"""B站字幕 → LRC 歌词转换。

B站字幕条目结构（来自 get_video_subtitle 的原始 items）：
    {"from": 1.24, "to": 3.80, "content": "...", "location": ...}

输出与 mp/lyrics.py 的 LRC_TIME_RE 兼容的标准 LRC 文本：
    [mm:ss.xx]歌词内容
（分钟 1-2 位、秒 2 位、厘秒 2 位）
"""
from __future__ import annotations


def _seconds_to_lrc_stamp(seconds: float) -> str:
    """秒 → [mm:ss.xx] 时间戳（负数/非法值按 0 处理）。"""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    minutes, rem = divmod(total_cs, 6000)
    secs, cs = divmod(rem, 100)
    if minutes > 99:
        minutes = 99
        secs, cs = 59, 99
    return f"[{minutes:02d}:{secs:02d}.{cs:02d}]"


def subtitle_to_lrc(items: list[dict] | None) -> str:
    """把 B站字幕条目列表转换为 LRC 文本。

    - 按 from 时间排序
    - 跳过空内容行
    - 相邻时间戳重复的行只保留首条
    - 无有效内容时返回空字符串
    """
    if not items:
        return ""

    entries: list[tuple[float, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        start = item.get("from")
        if not isinstance(start, (int, float)):
            continue
        entries.append((float(start), content))

    if not entries:
        return ""

    entries.sort(key=lambda e: e[0])

    lines: list[str] = []
    last_stamp: str | None = None
    for start, content in entries:
        stamp = _seconds_to_lrc_stamp(start)
        if stamp == last_stamp:
            continue
        last_stamp = stamp
        lines.append(f"{stamp}{content}")

    return "\n".join(lines)
