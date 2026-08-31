"""使用 mutagen 读取歌曲基础信息（标题/艺人/专辑/时长）"""
from __future__ import annotations
import os
from dataclasses import dataclass

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None


@dataclass
class TrackInfo:
    path: str
    title: str
    artist: str
    album: str
    duration_sec: float
    filetype: str


def _first(tags, keys, default=""):
    if tags is None:
        return default
    for k in keys:
        if k in tags:
            v = tags[k]
            if isinstance(v, list) and v:
                return str(v[0])
            if v:
                return str(v)
    return default


def read_metadata(path: str) -> TrackInfo:
    filename = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    title, artist, album, duration = filename, "未知艺人", "未知专辑", 0.0

    if MutagenFile is not None:
        try:
            audio = MutagenFile(path, easy=True)
            if audio is not None:
                tags = audio.tags
                title = _first(tags, ["title"], filename) or filename
                artist = _first(tags, ["artist"], "未知艺人") or "未知艺人"
                album = _first(tags, ["album"], "未知专辑") or "未知专辑"
                if audio.info is not None:
                    duration = float(getattr(audio.info, "length", 0.0) or 0.0)
        except Exception:
            pass

    return TrackInfo(path=path, title=title, artist=artist, album=album,
                      duration_sec=duration, filetype=ext)
