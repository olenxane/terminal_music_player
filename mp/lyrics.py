"""LRC 歌词解析与匹配。

匹配策略：
 1. 精确匹配：与歌曲同名的 .lrc 文件（同目录或 lyrics_dir 中）
 2. 模糊匹配：当精确匹配失败且开启 fuzzy_match 时，
    在候选目录中使用字符串相似度（difflib）找最接近的 .lrc 文件名
"""
from __future__ import annotations
import os
import re
import difflib
from dataclasses import dataclass
from typing import Optional


LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class LyricLine:
    time_ms: int
    text: str


@dataclass
class LyricsData:
    lines: list  # list[LyricLine], 按时间升序
    source_path: Optional[str]
    match_type: str  # "exact" / "fuzzy" / "none"
    similarity: float = 1.0

    def find_index(self, position_ms: int) -> int:
        """返回当前时间对应的歌词行下标（最后一个 time_ms <= position_ms 的行），
        找不到则返回 -1"""
        if not self.lines:
            return -1
        lo, hi = 0, len(self.lines) - 1
        ans = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.lines[mid].time_ms <= position_ms:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans


def _parse_lrc_text(text: str) -> list:
    lines = []
    for raw_line in text.splitlines():
        matches = list(LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        content = LRC_TIME_RE.sub("", raw_line).strip()
        for m in matches:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac = m.group(3) or "0"
            frac = frac.ljust(3, "0")[:3]
            millis = int(frac) if len(frac) == 3 else int(frac) * (10 ** (3 - len(frac)))
            time_ms = minutes * 60_000 + seconds * 1000 + millis
            lines.append(LyricLine(time_ms=time_ms, text=content))
    lines.sort(key=lambda l: l.time_ms)
    return lines


def _read_lrc_file(path: str) -> list:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            return _parse_lrc_text(text)
        except (UnicodeDecodeError, LookupError):
            continue
    return []


def _candidate_dirs(song_path: str, lyrics_dir: str) -> list:
    dirs = []
    song_dir = os.path.dirname(os.path.abspath(song_path))
    dirs.append(song_dir)
    if lyrics_dir:
        ld = os.path.abspath(lyrics_dir)
        if ld not in dirs and os.path.isdir(ld):
            dirs.append(ld)
    return dirs


def load_lyrics(song_path: str, lyrics_dir: str = "", fuzzy_match: bool = True,
                 fuzzy_threshold: float = 0.64) -> LyricsData:
    base_name = os.path.splitext(os.path.basename(song_path))[0]
    dirs = _candidate_dirs(song_path, lyrics_dir)

    # 1. 精确匹配
    for d in dirs:
        exact_path = os.path.join(d, base_name + ".lrc")
        if os.path.isfile(exact_path):
            lines = _read_lrc_file(exact_path)
            if lines:
                return LyricsData(lines=lines, source_path=exact_path,
                                   match_type="exact", similarity=1.0)

    if not fuzzy_match:
        return LyricsData(lines=[], source_path=None, match_type="none")

    # 2. 模糊匹配：在候选目录里找最相似文件名的 .lrc
    best_path, best_ratio = None, 0.0
    norm_base = _normalize_name(base_name)
    for d in dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fname in entries:
            if not fname.lower().endswith(".lrc"):
                continue
            candidate = os.path.splitext(fname)[0]
            ratio = difflib.SequenceMatcher(
                None, norm_base, _normalize_name(candidate)
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_path = os.path.join(d, fname)

    if best_path and best_ratio >= fuzzy_threshold:
        lines = _read_lrc_file(best_path)
        if lines:
            return LyricsData(lines=lines, source_path=best_path,
                               match_type="fuzzy", similarity=best_ratio)

    return LyricsData(lines=[], source_path=None, match_type="none")


def _normalize_name(name: str) -> str:
    """去除常见干扰词（如 "官方版"、"Live"、艺人前后缀括号内容等）以提升模糊匹配准确率"""
    name = name.lower()
    name = re.sub(r"[\(\[（【][^)\]）】]*[\)\]）】]", "", name)  # 去括号内容
    name = re.sub(r"[-_·—]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name
