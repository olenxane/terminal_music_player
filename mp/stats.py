"""播放统计：静默记录每日听歌时长与播放歌曲名"""
from __future__ import annotations
import json
import os
from datetime import date


class StatsTracker:
    """静默运行的统计数据收集器。

    数据按天粒度存储，结构：
        {"_version": 1, "days": {"2026-09-02": {"seconds": 3600, "songs": [...]}}}
    songs 为纯列表，每次播放追加一首，不去重。
    """

    FLUSH_INTERVAL = 600.0  # 秒，定时刷写间隔

    def __init__(self, data_path: str):
        self._data_path = data_path
        self._data: dict = {}
        self._dirty = False
        self._load()

    # ---------- 内部 ----------
    def _load(self):
        if os.path.exists(self._data_path):
            try:
                with open(self._data_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if "days" not in self._data:
                    self._data = {}
            except (json.JSONDecodeError, OSError):
                self._data = {}
        if "days" not in self._data:
            self._data = {"_version": 1, "days": {}}

    def _save(self):
        try:
            tmp = self._data_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._data_path)
        except OSError:
            pass

    def _get_today(self) -> dict:
        key = date.today().isoformat()
        days = self._data.setdefault("days", {})
        if key not in days:
            days[key] = {"seconds": 0, "songs": []}
        return days[key]

    # ---------- 对外接口 ----------
    def record_song(self, name: str):
        """记录一首播放过的歌曲名（追加，不去重），立即写盘"""
        today = self._get_today()
        today["songs"].append(name)
        self._dirty = True
        self._save()

    def add_seconds(self, seconds: float):
        """累加当天听歌秒数（仅内存，不写盘）"""
        if seconds <= 0:
            return
        today = self._get_today()
        today["seconds"] += seconds
        self._dirty = True

    def flush(self):
        """将内存数据写入磁盘"""
        if self._dirty:
            self._save()
            self._dirty = False

    def get_today(self) -> dict:
        return self._get_today()
