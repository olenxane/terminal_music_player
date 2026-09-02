"""加密模块 — 基于 Fernet 的凭证加密。

加密密钥保存在配置文件目录下的 .credential_key 文件中。
首次使用时自动生成，后续复用。
"""
import os
import logging
from pathlib import Path

from . import paths
from .exceptions import EncryptionError

logger = logging.getLogger(__name__)

_ENCRYPTION_VERSION = b"v2:"

_cached_key: bytes | None = None
_cached_key_mtime: float = 0.0


def _key_file() -> Path:
    """获取密钥文件路径（动态解析，支持 configure_paths 后的路径变更）"""
    return paths.CONFIG_DIR / ".credential_key"


def _write_all(fd: int, data: bytes) -> None:
    """写入全部数据，处理 POSIX write(2) 短写语义。"""
    while data:
        n = os.write(fd, data)
        if n == 0:
            raise OSError("write 返回 0（文件系统已满或 fd 无效）")
        data = data[n:]


def _get_key() -> bytes:
    """获取 Fernet 密钥（自动创建/持久化）"""
    global _cached_key, _cached_key_mtime
    key_file = _key_file()
    if _cached_key is not None:
        try:
            current_mtime = key_file.stat().st_mtime
            if current_mtime == _cached_key_mtime:
                return _cached_key
            logger.debug("加密密钥文件已修改，重新加载")
        except OSError:
            pass

    if key_file.exists():
        _cached_key = key_file.read_bytes()
        try:
            _cached_key_mtime = key_file.stat().st_mtime
        except OSError:
            _cached_key_mtime = 0.0
        return _cached_key

    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    try:
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, key)
            os.fsync(fd)
        except OSError:
            os.close(fd)
            key_file.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        try:
            _cached_key_mtime = key_file.stat().st_mtime
        except OSError:
            _cached_key_mtime = 0.0
    except OSError:
        logger.debug("无法持久化加密密钥，使用内存密钥（重启后需重新登录）")
        _cached_key_mtime = 0.0
    _cached_key = key
    return key


def _encrypt_data(data: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet
        key = _get_key()
        f = Fernet(key)
        encrypted = f.encrypt(data)
        return _ENCRYPTION_VERSION + encrypted
    except Exception as e:
        raise EncryptionError(f"加密失败: {type(e).__name__}: {e}") from e


def _decrypt_data(data: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet

        if not data.startswith(_ENCRYPTION_VERSION):
            raise EncryptionError(
                "检测到旧版凭证格式，请重新登录以使用更安全的加密方式。\n"
                f"提示：删除 {paths.CREDENTIAL_FILE} 后重新登录即可"
            )

        ciphertext = data[len(_ENCRYPTION_VERSION):]
        key = _get_key()
        f = Fernet(key)
        return f.decrypt(ciphertext)
    except EncryptionError:
        raise
    except Exception as e:
        raise EncryptionError(f"解密失败: {type(e).__name__}: {e}") from e
