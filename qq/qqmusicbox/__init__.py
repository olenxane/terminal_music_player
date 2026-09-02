"""
QQ音乐终端版播放器
灵感来自 musicbox (网易云音乐命令行版本)

作为库使用时，直接导入 QQMusicClient 和数据模型即可：

    from qqmusicbox import QQMusicClient
    from qqmusicbox import Song, Album, Artist, Playlist, Lyric, LyricLine

    client = QQMusicClient()
    songs = await client.search_songs("周杰伦")
"""

__version__ = "1.8.13.0"
__author__ = "QQMusicBox"


def main():
    """启动终端 UI（延迟导入，避免循环导入）"""
    from .main import main as _main
    return _main()


# ========== 编程 API 导出 ==========

from .api import (
    QQMusicClient,
    get_client,
    close_client,
    Song,
    Album,
    Artist,
    Playlist,
    Lyric,
    LyricLine,
    SearchType,
    QQMusicErrorCode,
)
from .paths import configure_paths
from .credential_store import (
    save_credential,
    load_credential,
    clear_credential,
)
from .exceptions import EncryptionError, RateLimitError

__all__ = [
    # 入口
    "main",
    "__version__",
    "__author__",
    # API 客户端
    "QQMusicClient",
    "get_client",
    "close_client",
    # 数据模型
    "Song",
    "Album",
    "Artist",
    "Playlist",
    "Lyric",
    "LyricLine",
    "SearchType",
    "QQMusicErrorCode",
    # 路径配置
    "configure_paths",
    # 凭证管理
    "save_credential",
    "load_credential",
    "clear_credential",
    # 异常
    "EncryptionError",
    "RateLimitError",
]
