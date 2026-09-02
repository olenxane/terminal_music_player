"""工具函数"""
import asyncio
import functools
import logging
from typing import Any, List, Tuple

from .config import MAX_SEARCH_LENGTH, MIN_SEARCH_LENGTH

logger = logging.getLogger(__name__)


# 哨兵值，用于区分"属性存在但值为 None"和"属性不存在"
_SENTINEL = object()


def safe_get(item: Any, default: Any, *attr_names: str) -> Any:
    """安全地从对象获取属性，支持回退属性名

    封装常见的 getattr(item, name1, getattr(item, name2, default)) 模式。

    Args:
        item: 要获取属性的对象
        default: 所有属性都不存在时的默认值
        attr_names: 一个或多个属性名，按优先级依次尝试

    Returns:
        第一个存在的属性的值，如果都不存在返回 default
    """
    for name in attr_names:
        val = getattr(item, name, _SENTINEL)
        if val is not _SENTINEL:
            return val
    return default


def log_api_error(operation: str, error: Exception) -> None:
    """记录 API 错误日志（统一格式）

    根据错误类型选择合适的日志级别：
    - 网络瞬时错误（超时、连接失败）→ DEBUG（减少日志噪音）
    - 凭证失效、数据解析错误 → WARNING（用户可操作）

    Args:
        operation: 操作描述（如"搜索歌曲""获取歌词"）
        error: 异常对象
    """
    from .exceptions import NETWORK_ERRORS

    # 网络瞬时错误降级为 DEBUG，减少日志噪音
    if isinstance(error, NETWORK_ERRORS):
        logger.debug("%s失败(网络瞬时错误): %s", operation, error)
        return

    logger.warning("%s失败: %s", operation, error)
    logger.debug("%s失败详情", operation, exc_info=True)


def handle_api_errors(operation: str, default: Any = None):
    """装饰器：统一处理 API 调用中的常见异常

    消除 api_*.py 中重复的四段式 try/except 模式：
    - RateLimitError → 限流
    - _QQMusicCredentialError → 凭证失效
    - NETWORK_ERRORS → 网络错误
    - (AttributeError, KeyError, TypeError, ValueError) → 数据解析错误

    Args:
        operation: 操作描述（如"搜索歌曲"）
        default: 发生错误时的返回值（如 [] 或 None）

    Usage:
        @handle_api_errors("搜索歌曲", default=[])
        async def search_songs(self, keyword): ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            from .exceptions import (
                RateLimitError, NETWORK_ERRORS, CredentialError,
            )
            try:
                return await func(*args, **kwargs)
            except RateLimitError as e:
                log_api_error(f"{operation}（限流）", e)
                return default
            except CredentialError as e:
                log_api_error(f"{operation}（凭证失效）", e)
                return default
            except NETWORK_ERRORS as e:
                log_api_error(f"{operation}（网络错误）", e)
                return default
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                log_api_error(f"{operation}（数据解析错误）", e)
                return default
        return wrapper
    return decorator


def handle_task_error(task: asyncio.Task, context: str = "后台任务") -> None:
    """统一的异步任务错误处理回调
    
    适用于 task.add_done_callback()，替代各处重复的错误处理代码。
    
    Args:
        task: 已完成的异步任务
        context: 错误上下文描述（用于日志）
    """
    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug("%s被取消", context)
    except Exception as e:
        logger.warning("%s失败: %s: %s", context, type(e).__name__, e)


def validate_search_query(query: str) -> Tuple[bool, str, str]:
    """验证搜索关键词

    Args:
        query: 搜索关键词

    Returns:
        (是否有效, 错误消息, 清洗后的关键词)  # R3 修复：返回清洗后的 query
    """
    if not query:
        return False, "搜索关键词不能为空", ""

    # 去除首尾空白
    query = query.strip()

    if len(query) < MIN_SEARCH_LENGTH:
        return False, f"搜索关键词太短（至少{MIN_SEARCH_LENGTH}个字符）", ""

    if len(query) > MAX_SEARCH_LENGTH:
        return False, f"搜索关键词太长（最多{MAX_SEARCH_LENGTH}个字符）", ""

    if any(ord(c) < 32 and c not in (' ', '\t') for c in query):
        return False, "搜索关键词包含非法字符", ""

    return True, "", query


def ensure_player_installed() -> bool:
    """检查播放器是否已安装"""
    from .player_volume import find_available_players
    return bool(find_available_players())


def get_player_install_hint() -> str:
    """获取播放器安装提示"""
    return """
📦 音频播放器（系统依赖，需用系统包管理器安装）:
  • mpv (推荐):
    - Debian/Ubuntu: sudo apt install mpv
    - Fedora:        sudo dnf install mpv
    - macOS:         brew install mpv
  • mpg123 (轻量):
    - Debian/Ubuntu: sudo apt install mpg123
    - Fedora:        sudo dnf install mpg123
  • ffplay (ffmpeg):
    - Debian/Ubuntu: sudo apt install ffmpeg
    - Fedora:        sudo dnf install ffmpeg
"""


def check_dependencies() -> Tuple[bool, List[str]]:
    """检查依赖

    Returns:
        (是否全部通过, 缺失依赖列表)
    """
    missing = []

    # 检查Python依赖（pip 可安装）
    try:
        import qqmusic_api
    except ImportError:
        missing.append("qqmusic-api-python (pip install qqmusic-api-python)")

    try:
        from PIL import Image
    except ImportError:
        missing.append("Pillow (pip install Pillow)")

    try:
        import qrcode
    except ImportError:
        missing.append("qrcode (pip install qrcode)")

    # pyzbar 需要系统库 libzbar，单独检查并给出详细提示
    try:
        from pyzbar.pyzbar import decode
    except ImportError:
        missing.append("pyzbar + libzbar0 (见下方系统依赖说明)")

    # 检查播放器（系统依赖）
    if not ensure_player_installed():
        missing.append("音频播放器 (见下方系统依赖说明)")

    return len(missing) == 0, missing
