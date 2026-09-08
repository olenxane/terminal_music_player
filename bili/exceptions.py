"""Bilibili 模块异常定义（复制自 bilibili-cli）"""


class BiliError(Exception):
    """Bilibili 模块基础异常。"""


class InvalidBvidError(BiliError):
    """BV 号无法解析或格式非法。"""


class NetworkError(BiliError):
    """上游网络 / API 请求失败。"""


class AuthenticationError(BiliError):
    """认证数据缺失或失效。"""


class RateLimitError(BiliError):
    """Bilibili 风控限流（HTTP 412/429）。"""


class NotFoundError(BiliError):
    """视频、用户或资源不存在。"""
