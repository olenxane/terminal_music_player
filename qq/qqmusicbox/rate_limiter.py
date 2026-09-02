"""
API 调用限流模块
基于时间窗口的简单限流器。

用法：
    limiter = RateLimiter(rate=10)
    await limiter.acquire()  # 阻塞直到可以发起请求
"""
import asyncio
import time
import logging
from typing import Dict

logger = logging.getLogger("qqmusicbox.rate_limiter")


class RateLimiter:
    """简单的基于时间窗口的限流器（支持分类限流）"""

    def __init__(self, rate: float = 15.0, timeout: float = 30.0):
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._interval = 1.0 / rate
        self._timeout = timeout
        self._last: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, category: str = "default") -> bool:
        """获取令牌，超时返回 False

        Args:
            category: 请求分类（如 "search", "fav", "song"），不同分类独立限流
        """
        async with self._lock:
            now = time.monotonic()
            last = self._last.get(category, 0.0)
            wait = last + self._interval - now
            if wait > self._timeout:
                logger.warning("获取令牌超时（分类: %s）", category)
                return False
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[category] = time.monotonic()
            return True


# 全局限流实例
_global_limiter = RateLimiter(rate=15.0)


def get_global_limiter() -> RateLimiter:
    return _global_limiter
