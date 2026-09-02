"""
QQ音乐API - 歌词功能模块

提供歌词获取和解析功能
"""

import re
import asyncio
import bisect
import logging
from typing import Dict, Optional, List

from .cache import cached
from .utils import handle_api_errors

__all__ = ["LyricMixin"]

logger = logging.getLogger(__name__)

# 翻译歌词时间戳匹配的容差（毫秒）
# 修复 M18：允许一定的时间偏差，因为实际 LRC 文件常有微小偏差
_TRANSLATION_TIME_TOLERANCE_MS = 500


def _find_nearest_translation(time_ms: int, times: List[int], lines_dict: Dict[int, str]) -> str:
    """在容差范围内查找最近的翻译"""
    if not times:
        return ""

    pos = bisect.bisect_left(times, time_ms)

    candidates = []
    if pos < len(times):
        candidates.append(times[pos])
    if pos > 0:
        candidates.append(times[pos - 1])

    best_time = None
    best_diff = float('inf')
    for t in candidates:
        diff = abs(t - time_ms)
        if diff <= _TRANSLATION_TIME_TOLERANCE_MS and diff < best_diff:
            best_diff = diff
            best_time = t

    return lines_dict.get(best_time, "") if best_time is not None else ""


class LyricMixin:
    """歌词功能混入类
    
    提供歌词获取、解析等功能
    需与QQMusicClient组合使用
    """
    
    _LRC_TIMESTAMP_RE = re.compile(r'\[(\d{1,2}:\d{2}(?:[.:,]\d{1,3})?)\]')

    def _parse_lrc_text(self, text: str) -> Dict[int, str]:
        """解析 LRC 格式歌词文本

        Args:
            text: LRC 格式的歌词文本

        Returns:
            {time_ms: content} 字典
        """
        lines_dict = {}
        if not text:
            return lines_dict

        for line in text.split("\n"):
            line = line.strip()
            if not line or not line.startswith("["):
                continue

            timestamps = self._LRC_TIMESTAMP_RE.findall(line)
            if not timestamps:
                continue

            content = self._LRC_TIMESTAMP_RE.sub('', line).strip()

            for ts in timestamps:
                try:
                    # 归一化逗号为点号（兼容 [01:23,45] 格式）
                    ts_normalized = ts.replace(',', '.')
                    parts = ts_normalized.split(":")
                    if len(parts) == 2:
                        minutes = int(parts[0])
                        seconds = float(parts[1])
                        time_ms = int((minutes * 60 + seconds) * 1000)
                    elif len(parts) == 3:
                        hours = int(parts[0])
                        minutes = int(parts[1])
                        seconds = float(parts[2])
                        time_ms = int((hours * 3600 + minutes * 60 + seconds) * 1000)
                    else:
                        continue

                    lines_dict[time_ms] = content
                except (ValueError, IndexError):
                    pass

        return lines_dict
    
    @cached(ttl=86400, key_prefix="lyric")
    @handle_api_errors("获取歌词", default=None)
    async def get_lyric(self, song_mid: str) -> Optional['Lyric']:
        """获取歌词（包含原文、翻译、罗马音）"""
        from .api import LyricLine, Lyric

        await self._rate_limit("lyric")
        result = await self._client.lyric.get_lyric(song_mid, trans=True, roma=True)
        lines = []

        # 解密歌词（歌词数据是加密的）
        if hasattr(result, 'decrypt'):
            result = await asyncio.get_running_loop().run_in_executor(None, result.decrypt)

        # 解析三种歌词文本
        lyric_text = getattr(result, 'lyric', '')
        trans_text = getattr(result, 'trans', '')
        roma_text = getattr(result, 'roma', '')

        # 使用辅助方法解析
        lyric_lines_dict = self._parse_lrc_text(lyric_text)
        trans_lines_dict = self._parse_lrc_text(trans_text)
        roma_lines_dict = self._parse_lrc_text(roma_text)

        trans_times = sorted(trans_lines_dict.keys())
        roma_times = sorted(roma_lines_dict.keys())

        # 合并所有歌词行
        all_times = sorted(set(lyric_lines_dict.keys()))
        for time_ms in all_times:
            text = lyric_lines_dict.get(time_ms, "")
            # 使用容差匹配翻译和罗马音
            translation = _find_nearest_translation(time_ms, trans_times, trans_lines_dict)
            romanization = _find_nearest_translation(time_ms, roma_times, roma_lines_dict)

            if text:  # 只要有原文就添加
                lines.append(LyricLine(
                    time_ms=time_ms,
                    text=text,
                    translation=translation,
                    romanization=romanization
                ))

        return Lyric(lines=lines)
