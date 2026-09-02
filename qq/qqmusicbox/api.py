"""
QQ音乐API封装模块
基于 qqmusic-api-python 库

提供数据模型定义和QQMusicClient客户端类
功能模块已拆分为:
- api_search.py: 搜索功能
- api_song.py: 歌曲功能
- api_playlist.py: 歌单与排行榜功能
- api_lyric.py: 歌词功能

限流机制已集成，所有API请求都会经过自适应限流控制
"""
import asyncio
import bisect
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum

from qqmusic_api import Client

from .api_search import SearchMixin
from .api_song import SongMixin
from .api_playlist import PlaylistMixin
from .api_lyric import LyricMixin
from .api_auth import AuthMixin
from .rate_limiter import get_global_limiter
from .utils import log_api_error
from .config import (
    API_MAX_RETRIES,
    API_RETRY_DELAY,
    API_RETRY_BACKOFF,
    API_MAX_PAGE_SIZE,
)

logger = logging.getLogger(__name__)


class SearchType(Enum):
    """搜索类型"""
    SONG = 0
    ALBUM = 2
    PLAYLIST = 5


class QQMusicErrorCode(Enum):
    """QQ音乐 API 错误码"""
    # 成功
    SUCCESS = 0
    
    # 权限相关
    VIP_REQUIRED = 104003          # 需要VIP
    LOGIN_REQUIRED = 100031        # 需要登录
    COPYRIGHT_RESTRICTED = 100021  # 版权限制
    URL_EXPIRED = 101404           # URL过期或需要重新获取
    
    @classmethod
    def get_error_message(cls, code: int) -> str:
        """获取错误码对应的中文消息
        
        Args:
            code: 错误码
            
        Returns:
            错误消息字符串
        """
        error_messages = {
            cls.VIP_REQUIRED.value: "需要VIP",
            cls.LOGIN_REQUIRED.value: "需要登录",
            cls.COPYRIGHT_RESTRICTED.value: "版权限制",
            cls.URL_EXPIRED.value: "URL过期，请重新获取",
        }
        return error_messages.get(code, f"错误码: {code}")


@dataclass
class Song:
    """歌曲信息"""
    mid: str
    name: str
    singer: str
    album: str = ""
    album_mid: str = ""
    duration: int = 0  # 秒
    image_url: str = ""
    is_vip: bool = False  # 是否需要VIP会员
    
    @property
    def display_name(self) -> str:
        """显示名称"""
        return f"{self.name} - {self.singer}"


@dataclass
class Album:
    """专辑信息"""
    mid: str
    name: str
    singer: str
    image_url: str = ""
    songs: List[Song] = field(default_factory=list)


@dataclass
class Artist:
    """歌手信息"""
    mid: str
    name: str
    image_url: str = ""


@dataclass
class Playlist:
    """歌单信息"""
    dissid: str
    name: str
    image_url: str = ""
    songs: List[Song] = field(default_factory=list)


@dataclass
class LyricLine:
    """歌词行信息"""
    time_ms: int  # 时间戳（毫秒）
    text: str  # 原文歌词
    translation: str = ""  # 中文翻译（如果有）
    romanization: str = ""  # 罗马音/拼音（如果有）


@dataclass
class Lyric:
    """歌词信息"""
    lines: List[LyricLine]  # 歌词行列表
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _times: List[int] = field(default_factory=list, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # 预计算并缓存时间戳列表，避免 get_display_lines_at_time 每次调用重建
        # 确保 lines 按 time_ms 排序，bisect 才能正确工作
        # 使用 sorted() 避免修改调用方传入的列表（dataclass 默认不复制）
        self.lines = sorted(self.lines, key=lambda line: line.time_ms)
        self._times = [line.time_ms for line in self.lines]

    def get_line_at_time(self, time_ms: int) -> str:
        """根据时间获取当前歌词行（仅返回原文）"""
        lines = self.get_display_lines_at_time(
            time_ms, show_translation=False, show_romanization=False
        )
        return lines[0] if lines else ""

    def get_display_lines_at_time(self, time_ms: int, show_translation: bool = True, show_romanization: bool = False) -> List[str]:
        """根据时间获取当前应显示的歌词行列表（支持双语显示）

        Args:
            time_ms: 当前时间（毫秒）
            show_translation: 是否显示翻译
            show_romanization: 是否显示罗马音

        Returns:
            歌词行列表，可能包含原文、翻译、罗马音
        """
        if not self.lines:
            return []

        current_line = None
        current_index = -1

        with self._lock:
            idx = bisect.bisect_right(self._times, time_ms) - 1
            if idx >= 0:
                current_line = self.lines[idx]
                current_index = idx

            if current_line is None:
                return []

        display_lines = [current_line.text]

        if show_translation and current_line.translation:
            display_lines.append(current_line.translation)

        if show_romanization and current_line.romanization:
            display_lines.append(current_line.romanization)

        return display_lines


from dataclasses import dataclass, field

@dataclass
class _FavCache:
    timestamp: float = 0.0
    mids: set = field(default_factory=set)
    songs: list = field(default_factory=list)


class QQMusicClient(AuthMixin, SearchMixin, SongMixin, PlaylistMixin, LyricMixin):
    """QQ音乐 API 客户端"""

    def __init__(self):
        self._client = Client()
        self._fav_dirid: Optional[int] = None
        self._fav_cache: Optional[_FavCache] = None
        self.__fav_cache_lock: Optional[asyncio.Lock] = None
        self.__refresh_lock: Optional[asyncio.Lock] = None
        self._login_qr = None
        self._limiter = get_global_limiter()
        self._credential_lock = threading.Lock()
        self._song_id_cache: Dict[str, int] = {}
        self._song_id_cache_max: int = 500
        self._last_proactive_refresh: float = 0.0

    @property
    def _fav_cache_lock(self) -> asyncio.Lock:
        """获取收藏缓存锁（惰性初始化，事件循环启动后才创建）"""
        if self.__fav_cache_lock is None:
            self.__fav_cache_lock = asyncio.Lock()
        return self.__fav_cache_lock

    @property
    def _refresh_lock(self) -> asyncio.Lock:
        """获取凭证刷新锁（惰性初始化，事件循环启动后才创建）"""
        if self.__refresh_lock is None:
            self.__refresh_lock = asyncio.Lock()
        return self.__refresh_lock

    def get_credential(self):
        """线程安全地获取凭证引用

        无锁读取：利用 Python GIL 保证引用赋值的原子性。
        set_credential() 在锁内写入，确保跨线程可见性。
        """
        return self._client.credential

    def set_credential(self, credential) -> None:
        """线程安全地设置凭证（写入加锁）

        由事件循环线程（check_qrcode / refresh_credential）调用，
        在锁内写入，确保 UI 线程的 has_credential() 能立即看到最新值。
        """
        with self._credential_lock:
            self._client.credential = credential

    async def _rate_limit(self, category: str = "default") -> None:
        """在 API 调用前获取限流令牌

        A4 修复：必须尊重 acquire() 的返回值。当限流器在超时窗口内无法
        获取令牌时返回 False，此时抛出 RateLimitError 通知调用方，
        而不是默默放行请求（那会让限流器形同虚设）。
        """
        acquired = await self._limiter.acquire(category)
        if not acquired:
            from .exceptions import RateLimitError
            raise RateLimitError(
                f"限流器在超时窗口内无法获取令牌 (category={category!r})"
            )

    def has_credential(self) -> bool:
        """同步检查凭证是否存在且完整（不触发异步刷新）

        用于 UI 渲染循环等需要同步快速检查登录状态的场景。
        如需完整的登录状态检查（含凭证刷新），请使用 is_logged_in()。

        线程安全说明：credential 引用赋值在 set_credential() 中受锁保护，
        此处无锁读取利用 Python GIL 保证原子性。
        """
        credential = self.get_credential()
        if credential is None:
            return False
        if getattr(credential, 'musicid', 0) == 0:
            return False
        if not getattr(credential, 'musickey', ''):
            return False
        if not getattr(credential, 'refresh_key', ''):
            return False
        return True
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self._client.close()
    
    async def close(self):
        """关闭客户端"""
        await self._client.close()
    
    def _parse_singers(self, item: Any) -> str:
        """解析歌手列表为字符串"""
        singers = getattr(item, 'singer', [])
        if isinstance(singers, str):
            return singers
        if isinstance(singers, list):
            return "/".join(s.name for s in singers if s.name) if singers else ""
        return ""
    
    def _parse_album(self, item: Any) -> Tuple[str, str]:
        """解析专辑信息，返回 (name, mid)"""
        album_obj = getattr(item, 'album', None)
        if album_obj:
            return (album_obj.name or "", album_obj.mid or "")
        return ("", "")
    
    def _parse_song(self, item: Any) -> Song:
        """解析歌曲信息（通用方法）"""
        singer = self._parse_singers(item)
        album_name, album_mid = self._parse_album(item)

        # 解析VIP信息：pay.pay_month == 1 表示需要绿钻
        pay = getattr(item, 'pay', None)
        is_vip = False
        if pay is not None:
            pay_month = getattr(pay, 'pay_month', 0)
            is_vip = (pay_month == 1)
        
        return Song(
            mid=getattr(item, 'mid', '') or '',
            name=getattr(item, 'name', '') or '',
            singer=singer,
            album=album_name,
            album_mid=album_mid,
            duration=getattr(item, 'interval', 0) or 0,
            is_vip=is_vip,
        )


_client: Optional[QQMusicClient] = None
_client_lock = threading.Lock()


def get_client() -> QQMusicClient:
    """获取全局API客户端（线程安全单例）"""
    global _client

    if _client is not None:
        return _client

    with _client_lock:
        if _client is None:
            new_client = QQMusicClient()
            _client = new_client
        return _client


async def close_client():
    """关闭全局API客户端（线程安全）"""
    global _client
    client_to_close: Optional[QQMusicClient] = None

    with _client_lock:
        if _client is not None:
            client_to_close = _client
            _client = None

    # 在锁外执行关闭操作（避免长时间持有锁）
    if client_to_close is not None:
        try:
            await client_to_close.close()
        except Exception as e:
            logger.debug("关闭客户端失败: %s", e)