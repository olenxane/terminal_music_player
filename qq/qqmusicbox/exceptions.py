"""自定义异常类"""


class EncryptionError(Exception):
    """加密/解密相关错误"""
    pass


class RateLimitError(Exception):
    """限流器在超时窗口内无法获取令牌"""
    pass


try:
    from qqmusic_api.core.exceptions import (
        CredentialExpiredError,
        CredentialInvalidError,
        CredentialRefreshError,
    )
    CredentialError = (CredentialExpiredError, CredentialInvalidError, CredentialRefreshError)
except ImportError:
    CredentialError = ()


try:
    import httpx
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError, httpx.HTTPError)
except ImportError:
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError)
