"""凭证存储模块

负责登录凭证的加密保存、加载与清除（线程安全，原子写入）。
"""
import json
import os
import threading
import logging
from typing import Dict, Optional

from . import paths
from .crypto import _encrypt_data, _decrypt_data, _write_all
from .exceptions import EncryptionError

# 初始化 logger
logger = logging.getLogger(__name__)

_credential_lock = threading.Lock()


def save_credential(credential: Dict) -> None:
    """保存登录凭证（使用 Fernet 加密存储，线程安全，原子写入）"""
    paths.ensure_dirs()

    cred_file = paths.CREDENTIAL_FILE

    with _credential_lock:
        tmp_file = cred_file.with_suffix('.enc.tmp')
        try:
            json_data = json.dumps(credential, ensure_ascii=False).encode('utf-8')
            encrypted = _encrypt_data(json_data)

            fd = os.open(str(tmp_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                _write_all(fd, encrypted)
                os.fsync(fd)
            finally:
                os.close(fd)

            try:
                os.replace(str(tmp_file), str(cred_file))
            except OSError as replace_err:
                logger.debug("os.replace 失败，尝试回退方案: %s", replace_err)
                import shutil
                shutil.copy2(str(tmp_file), str(cred_file))
                tmp_file.unlink(missing_ok=True)
        except (TypeError, IOError, OSError) as e:
            logger.warning("保存凭证失败: %s: %s", type(e).__name__, e)
            _cleanup_tmp(tmp_file)
            raise
        except Exception as e:
            logger.warning("保存凭证失败: %s: %s", type(e).__name__, e, exc_info=True)
            _cleanup_tmp(tmp_file)
            raise


def _cleanup_tmp(tmp_file) -> None:
    try:
        tmp_file.unlink(missing_ok=True)
    except OSError:
        pass


def load_credential() -> Optional[Dict]:
    """加载登录凭证（解密，支持新旧加密格式）"""
    cred_file = paths.CREDENTIAL_FILE
    with _credential_lock:
        if cred_file.exists():
            try:
                # 防御性权限检查：凭证文件应对其他用户不可读
                try:
                    mode = cred_file.stat().st_mode & 0o777
                    if mode & 0o077:  # group/other 有任何权限位
                        logger.warning(
                            "凭证文件权限过宽 (0o%o)，自动修复为 0o600",
                            mode,
                        )
                        os.chmod(cred_file, 0o600)
                except OSError:
                    pass

                with open(cred_file, "rb") as f:
                    encrypted = f.read()

                json_data = _decrypt_data(encrypted)
                return json.loads(json_data.decode('utf-8'))
            except EncryptionError as e:
                logger.warning("凭证解密失败: %s: %s", type(e).__name__, e)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, IOError, OSError) as e:
                logger.debug("凭证读取失败: %s: %s", type(e).__name__, e)
            except Exception as e:
                logger.warning("凭证处理失败: %s: %s", type(e).__name__, e)
    return None


def clear_credential() -> None:
    """清除登录凭证"""
    with _credential_lock:
        try:
            paths.CREDENTIAL_FILE.unlink()
        except FileNotFoundError:
            pass
