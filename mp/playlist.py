"""音乐文件扫描与播放列表管理"""
from __future__ import annotations
import os
import random
from dataclasses import dataclass, field

SUPPORTED_EXTS = {".mp3",".wav",".flac",".ogg",".m4a",".aac"}


def scan_music_dir(path: str) -> list:
    path = os.path.expanduser(path)
    results = []
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() in SUPPORTED_EXTS:
            return [os.path.abspath(path)]
        return []
    if not os.path.isdir(path):
        return []
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                results.append(os.path.abspath(os.path.join(root, fn)))
    return results


def scan_music_dirs(paths: list) -> list:
    """扫描多个目录/文件，合并结果并去重（保留首次出现的顺序）"""
    seen = set()
    results = []
    for p in paths:
        for f in scan_music_dir(p):
            if f not in seen:
                seen.add(f)
                results.append(f)
    return results


@dataclass
class Playlist:
    tracks: list = field(default_factory=list)
    index: int = 0
    mode: str = "sequential"  # sequential / shuffle / repeat_one / repeat_all
    _shuffle_order: list = field(default_factory=list)
    _queue: list = field(default_factory=list)  # FIFO 播放队列（存储 track 索引）

    def load_dir(self, path: str):
        self.tracks = scan_music_dir(path)
        self.index = 0
        self._rebuild_shuffle()

    def load_dirs(self, paths: list):
        self.tracks = scan_music_dirs(paths)
        self.index = 0
        self._rebuild_shuffle()

    def _rebuild_shuffle(self):
        self._shuffle_order = list(range(len(self.tracks)))
        random.shuffle(self._shuffle_order)

    @property
    def current(self):
        if not self.tracks:
            return None
        return self.tracks[self.index]

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "shuffle":
            self._rebuild_shuffle()

    def next(self):
        if not self.tracks:
            return None
        if self.mode == "repeat_one":
            return self.current
        if self.mode == "shuffle":
            if self.index in self._shuffle_order:
                pos = self._shuffle_order.index(self.index)
            else:
                pos = -1
            pos = (pos + 1) % len(self._shuffle_order)
            self.index = self._shuffle_order[pos]
        else:
            self.index = (self.index + 1) % len(self.tracks)
        return self.current

    def prev(self):
        if not self.tracks:
            return None
        if self.mode == "shuffle" and self._shuffle_order:
            pos = self._shuffle_order.index(self.index) if self.index in self._shuffle_order else 0
            pos = (pos - 1) % len(self._shuffle_order)
            self.index = self._shuffle_order[pos]
        else:
            self.index = (self.index - 1) % len(self.tracks)
        return self.current

    def jump_to(self, i: int):
        if 0 <= i < len(self.tracks):
            self.index = i
        return self.current

    # ---------------- 播放队列（FIFO） ----------------
    def add_to_queue(self, i: int):
        """添加歌曲索引到播放队列末尾"""
        if 0 <= i < len(self.tracks):
            self._queue.append(i)

    def has_queue(self) -> bool:
        return len(self._queue) > 0

    def queue_len(self) -> int:
        return len(self._queue)

    def is_in_queue(self, i: int) -> bool:
        return i in self._queue

    def next_from_queue(self):
        """弹出队列首项，跳转到该歌曲"""
        if not self._queue:
            return None
        idx = self._queue.pop(0)
        self.index = idx
        return self.current

    def clear_queue(self):
        self._queue.clear()
