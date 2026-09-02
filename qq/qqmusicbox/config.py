"""配置管理模块"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional

from . import paths
from .paths import (
    APPID, CONFIG_DIR, CONFIG_FILE, CREDENTIAL_FILE, CACHE_DIR, LOG_FILE,
    ensure_dirs,
)
from .credential_store import save_credential, load_credential, clear_credential
from .crypto import _write_all

logger = logging.getLogger(__name__)


PLAY_MODE_NAMES = ["顺序播放", "随机播放", "单曲循环"]


COLOR_TITLE = 1
COLOR_MENU = 2
COLOR_SELECTED = 3
COLOR_STATUS = 4
COLOR_LYRIC = 5
COLOR_PROGRESS = 6
COLOR_HELP = 7
COLOR_MESSAGE = 8


# ========== UI 布局常量 ==========

UI_TITLE_HEIGHT = 2  # 标题栏高度（仅主页显示）
UI_LYRIC_HEIGHT = 1
UI_PROGRESS_HEIGHT = 1
UI_STATUS_HEIGHT = 1
UI_HELP_HEIGHT = 1
UI_INPUT_HEIGHT = 1
UI_MESSAGE_HEIGHT = 1  # 消息与歌词共用区域

UI_CONTENT_TOP_OFFSET = 2
UI_MAIN_CONTENT_TOP_OFFSET = 3

# 底部栏总高度（从内容区底部计算）
# 注意：消息与歌词共用 UI_LYRIC_HEIGHT 区域，不再单独占用空间
UI_BOTTOM_RESERVED = (
    UI_STATUS_HEIGHT +
    UI_PROGRESS_HEIGHT +
    UI_LYRIC_HEIGHT +
    UI_INPUT_HEIGHT +
    UI_HELP_HEIGHT
)


# ========== 运行时常量 ==========

URL_FETCH_TIMEOUT = 5.0
LOGIN_TIMEOUT = 90
NETWORK_TIMEOUT = 10

DEFAULT_VOLUME = 80
VOLUME_STEP = 5
MAX_PLAYLIST_SIZE = 300
DEFAULT_SEARCH_RESULTS = 20
DEFAULT_HOT_SONGS = 50
PLAYER_MONITOR_INTERVAL = 0.1

FAV_INITIAL_LOAD_COUNT = 30
FAV_MAX_LOAD_COUNT = 500

MAX_SEARCH_LENGTH = 100
MIN_SEARCH_LENGTH = 1

USER_INTERACTION_TIMEOUT = 3.0  # 交互超时（秒），缩短以减少切换歌曲后的跳转延迟
RANDOM_HISTORY_SIZE = 5
LYRIC_UPDATE_INTERVAL = 1.0

UI_LYRIC_MAX_LINES = 2
UI_MENU_SCROLL_MARGIN = 3

# ========== API 配置常量 ==========

API_MAX_RETRIES = 3
API_RETRY_DELAY = 1.0
API_RETRY_BACKOFF = 2
API_MAX_PAGE_SIZE = 100

UI_CONTENT_PADDING = 4
UI_STATUS_RIGHT_WIDTH = 20
UI_PROGRESS_BAR_MIN_WIDTH = 10
UI_SEARCH_BLOCK_MIN_WIDTH = 28
UI_SEARCH_BLOCK_MAX_WIDTH = 48
UI_SEARCH_BLOCK_PERCENT = 60

# 底部固定区域高度（用于自适应内容区计算）
# 注意：lyric/message 区域是歌词和消息的专用显示区，内容区不应覆盖
UI_BOTTOM_FIXED_HEIGHT = (
    UI_HELP_HEIGHT +
    UI_STATUS_HEIGHT +
    UI_PROGRESS_HEIGHT +
    UI_LYRIC_MAX_LINES
)

# UI 布局偏移量（用于计算各区域在屏幕上的位置）
# 这些是相对于屏幕高度的偏移量，表示各区域距离屏幕底部的距离
UI_STATUS_OFFSET = 3
UI_PROGRESS_OFFSET = 4
UI_LYRIC_OFFSET = 6
UI_HELP_OFFSET = 2
UI_INPUT_OFFSET = 7

LOGIN_CHECK_INTERVAL = 60.0
LOGIN_REDIRECT_DELAY = 2.0
PROACTIVE_REFRESH_INTERVAL = 1800  # 每30分钟检查一次，提前刷新凭证
PROACTIVE_REFRESH_ADVANCE = 3600  # 凭证到期前1小时触发刷新

FAV_PLAYLIST_NAME = "我喜欢"
HOT_SONGS_CHART_ID = 26

# ========== 播放器运行时常量 ==========

SUBPROCESS_TIMEOUT = 2
PROCESS_WAIT_TIMEOUT = 2
MONITOR_TASK_WAIT_TIMEOUT = 2.0

MPV_COMMAND_RETRIES = 3
AUTO_PLAY_RETRIES = 3
PREMATURE_EXIT_THRESHOLD = 5

TASK_CLEANUP_INTERVAL = 60.0

# ========== 播放器内部常量 ==========

MPV_DEFAULT_VOLUME = 100
SOCKET_TIMEOUT = 0.5


@dataclass
class LogConfig:
    """日志配置
    
    使用示例:
        # 启用 DEBUG 模式（开发调试）
        log_config = LogConfig(debug_mode=True, console_enabled=True)
        
        # 生产环境配置
        log_config = LogConfig(
            level="WARNING",
            file_enabled=True,
            max_size_mb=10,
            backup_count=3
        )
        
        # 详细日志（用于问题排查）
        log_config = LogConfig(
            level="INFO",
            file_enabled=True,
            console_enabled=True,
            max_size_mb=20,
            backup_count=5
        )
    """
    level: str = "INFO"
    file_enabled: bool = True
    console_enabled: bool = False  # 默认关闭控制台日志（避免干扰 TUI）
    max_size_mb: int = 10
    backup_count: int = 3
    debug_mode: bool = False  # 调试模式开关（启用后自动切换为 DEBUG 级别）
    
    def get_effective_level(self) -> str:
        """根据调试模式返回有效的日志级别"""
        return "DEBUG" if self.debug_mode else self.level


@dataclass
class KeyBindings:
    """快捷键配置"""
    up: str = "k"
    down: str = "j"
    left: str = "h"
    right: str = "l"
    prev_page: str = "u"
    next_page: str = "d"
    search: str = "f"
    prev_song: str = "["
    next_song: str = "]"
    volume_up: str = "="
    volume_down: str = "-"
    play_pause: str = " "  # 空格
    menu: str = "m"
    playlist: str = "p"
    play_mode: str = "P"  # Shift+p
    first: str = "g"
    last: str = "G"  # Shift+g
    star: str = "s"
    remove: str = "r"
    quit: str = "q"


@dataclass
class Config:
    """应用配置"""
    # 音量设置
    volume: int = 80
    
    # 播放模式: 0=顺序, 1=随机, 2=单曲循环
    play_mode: int = 1
    
    # 显示设置
    show_lyrics: bool = True
    show_progress: bool = True
    show_lyric_translation: bool = True  # 显示歌词翻译
    show_lyric_romanization: bool = False  # 显示歌词罗马音
    
    # 缓存设置
    cache_size_mb: int = 500
    auto_cache: bool = False
    
    # 快捷键
    keys: KeyBindings = field(default_factory=KeyBindings)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从字典创建"""
        # 快捷键
        keys_data = data.get("keys", {})
        valid_keys = {k: v for k, v in keys_data.items() if k in KeyBindings.__dataclass_fields__}
        keys = KeyBindings(**valid_keys)

        # 边界检查
        volume = max(0, min(100, int(data.get("volume", 80))))
        play_mode = data.get("play_mode")
        if play_mode is None:
            play_mode = cls.__dataclass_fields__['play_mode'].default
        play_mode = max(0, min(2, int(play_mode)))

        # 边界检查：cache_size_mb 应为正数
        cache_size_mb = data.get("cache_size_mb", 500)
        if not isinstance(cache_size_mb, (int, float)) or cache_size_mb <= 0:
            cache_size_mb = 500
        cache_size_mb = max(1, int(cache_size_mb))

        # 布尔值校验
        def _to_bool(val, default: bool) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "on")
            if isinstance(val, (int, float)):
                return bool(val)
            return default

        return cls(
            volume=volume,
            play_mode=play_mode,
            show_lyrics=_to_bool(data.get("show_lyrics", True), True),
            show_progress=_to_bool(data.get("show_progress", True), True),
            show_lyric_translation=_to_bool(data.get("show_lyric_translation", True), True),
            show_lyric_romanization=_to_bool(data.get("show_lyric_romanization", False), False),
            cache_size_mb=cache_size_mb,
            auto_cache=_to_bool(data.get("auto_cache", False), False),
            keys=keys,
        )


# 全局配置缓存（避免重复读取文件）
_config_cache: Optional[Config] = None
_config_mtime: float = 0
_config_load_time: float = 0  # 缓存加载时间（monotonic：用于 TTL 兜底）
_config_lock = threading.Lock()


def load_config() -> Config:
    """加载配置文件（带缓存优化，线程安全）"""
    global _config_cache, _config_mtime, _config_load_time

    with _config_lock:
        # 检查缓存是否有效
        config_file = paths.CONFIG_FILE
        if _config_cache is not None and config_file.exists():
            try:
                current_mtime = config_file.stat().st_mtime
                # B2 修复：使用 == 比较 mtime + TTL 兜底，避免系统时钟回退（NTP
                # 校时、VM 挂起恢复）导致 current_mtime 持续小于 _config_mtime 而
                # 使缓存永不刷新。mtime 相同时，5 秒 TTL 强制重新检查。
                if current_mtime == _config_mtime:
                    if time.monotonic() - _config_load_time < 5.0:
                        return _config_cache
            except OSError:
                pass

        # 重新加载配置
        ensure_dirs()

        if config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                config = Config.from_dict(data)
                _config_cache = config
                _config_load_time = time.monotonic()
                try:
                    _config_mtime = config_file.stat().st_mtime
                except OSError:
                    pass
                return config
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logger.debug("配置文件加载失败，使用默认配置: %s", e)

        # 使用默认配置
        config = Config()
        _config_cache = config
        _config_load_time = time.monotonic()
        return config


def save_config(config: Config) -> None:
    """保存配置文件（线程安全，原子写入）

    使用 tmp 文件 + os.replace 实现原子写入。
    在 os.replace 失败时回退到复制+删除（非原子，但优于直接写入）。
    """
    global _config_cache, _config_mtime

    ensure_dirs()

    with _config_lock:
        tmp_file = paths.CONFIG_FILE.with_suffix(".json.tmp")
        try:
            fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                content = json.dumps(config.to_dict(), indent=2, ensure_ascii=False).encode('utf-8')
                _write_all(fd, content)
                # 强制将数据从内存刷到磁盘，避免系统崩溃导致数据丢失
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)

            try:
                os.replace(str(tmp_file), str(paths.CONFIG_FILE))
            except OSError as replace_err:
                # os.replace() 可能在跨文件系统时失败，回退到复制+删除
                # 注意：回退到非原子写入，但已写入 tmp 文件，至少保证部分成功
                logger.debug("os.replace 失败，尝试回退方案: %s", replace_err)
                # 使用 copy2 保留文件元数据（权限、时间戳等）
                import shutil
                shutil.copy2(str(tmp_file), str(paths.CONFIG_FILE))
                tmp_file.unlink(missing_ok=True)
        except (IOError, OSError) as e:
            logger.warning("保存配置文件失败: %s", e)
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        _config_cache = config
        try:
            _config_mtime = paths.CONFIG_FILE.stat().st_mtime
        except OSError:
            pass



def setup_logging(log_config: Optional[LogConfig] = None) -> logging.Logger:
    """配置日志系统
    
    支持日志轮转（RotatingFileHandler），自动管理日志文件大小和备份数量。
    
    Args:
        log_config: 日志配置，默认使用默认配置
        
    Returns:
        配置好的根日志器
    
    使用示例:
        # 快速启用 DEBUG 模式
        from qqmusicbox.config import LogConfig, setup_logging
        log_config = LogConfig(debug_mode=True, console_enabled=True)
        logger = setup_logging(log_config)
        
        # 生产环境（仅文件日志）
        log_config = LogConfig(level="WARNING", file_enabled=True)
        logger = setup_logging(log_config)
        
        # 详细日志（用于问题排查）
        log_config = LogConfig(
            level="INFO",
            file_enabled=True,
            console_enabled=True,
            max_size_mb=20,  # 单个日志文件最大 20MB
            backup_count=5   # 保留 5 个备份文件
        )
        logger = setup_logging(log_config)
    
    Note:
        - 日志文件位置: CACHE_DIR/qqmusicbox.log (根据当前配置路径)
        - 默认配置: WARNING 级别，10MB 单文件，3 个备份
        - DEBUG 模式会自动将级别设置为 DEBUG，无论 level 参数如何
    """
    if log_config is None:
        log_config = LogConfig()
    
    # 确保缓存目录存在
    ensure_dirs()

    # 获取根日志器
    root_logger = logging.getLogger("qqmusicbox")
    root_logger.setLevel(getattr(logging, log_config.get_effective_level(), logging.WARNING))

    # 清除现有处理器
    root_logger.handlers.clear()

    # 日志格式
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 文件处理器
    if log_config.file_enabled:
        try:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                paths.LOG_FILE,
                maxBytes=log_config.max_size_mb * 1024 * 1024,
                backupCount=log_config.backup_count,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            # 显式设置日志文件权限为 0o600（仅所有者可读写），
            # 避免依赖 umask 导致日志文件被其他用户读取。
            # 缓存目录已设为 0o700，此处防御性加固文件级权限。
            try:
                os.chmod(paths.LOG_FILE, 0o600)
            except OSError:
                pass
        except (IOError, OSError) as e:
            logger.warning("无法创建日志文件: %s: %s", type(e).__name__, e)
        except Exception as e:
            # 日志系统初始化失败时，使用 sys.stderr 输出警告
            import sys
            sys.stderr.write(f"警告: 无法创建日志文件处理器: {e}\n")
    
    # 控制台处理器
    if log_config.console_enabled:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    return root_logger
