"""缓存模块 — 简单的异步缓存装饰器"""
import time
import logging
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger("qqmusicbox.cache")

_cache: dict = {}
_NONE = object()
_MAX_CACHE_SIZE = 1000


def cached(ttl: int = 3600, key_prefix: str = "", none_ttl: int = 300) -> Callable:
    """缓存装饰器（异步函数用）

    Args:
        ttl: 成功结果的缓存时间（秒）
        key_prefix: 缓存键前缀
        none_ttl: None 结果的缓存时间（秒），瞬态错误避免长时间缓存
    """
    def decorator(func: Callable) -> Callable:
        prefix = key_prefix or func.__qualname__

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            key = (prefix,) + args[1:] + tuple(sorted(kwargs.items()))

            now = time.monotonic()
            if key in _cache:
                val, ts = _cache[key]
                effective_ttl = none_ttl if val is _NONE else ttl
                if now - ts < effective_ttl:
                    return None if val is _NONE else val

            result = await func(*args, **kwargs)

            if len(_cache) >= _MAX_CACHE_SIZE:
                oldest_key = next(iter(_cache))
                del _cache[oldest_key]

            _cache[key] = (_NONE if result is None else result, now)
            return result
        return wrapper
    return decorator
