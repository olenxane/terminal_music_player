"""路径解析模块

负责解析 XDG 规范的配置/缓存目录、旧版路径迁移以及关键文件路径常量。
"""
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

APPID = "io.gitee.ysdlm.qqmusicbox"

def _resolve_xdg_path(env_var: str, default: Path) -> Path:
    """从环境变量解析 XDG 路径，非绝对路径时回退到默认值。"""
    value = os.environ.get(env_var)
    if value:
        path = Path(value)
        if path.is_absolute():
            return path
        logger.warning("环境变量 %s=%s 不是绝对路径，回退到默认值", env_var, value)
    return default


_XDG_CONFIG_HOME = _resolve_xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
_XDG_CACHE_HOME = _resolve_xdg_path("XDG_CACHE_HOME", Path.home() / ".cache")
_XDG_DATA_HOME = _resolve_xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")

_LEGACY_CONFIG_DIR = Path.home() / ".config" / "qqmusicbox"
_LEGACY_CACHE_DIR = Path.home() / ".cache" / "qqmusicbox"

_NEW_CONFIG_DIR = _XDG_CONFIG_HOME / APPID
_NEW_CACHE_DIR = _XDG_CACHE_HOME / APPID


def _migrate_legacy_dir(legacy: Path, new: Path) -> bool:
    """将旧版目录迁移到 XDG 规范路径，成功返回 True。

    UOS 应用打包规范 §4.2 禁止直接写入 $HOME，因此旧版路径
    ``~/.config/qqmusicbox`` / ``~/.cache/qqmusicbox`` 仅在此做一次性迁移；
    迁移后所有读写均使用 ``$XDG_*_HOME/io.gitee.ysdlm.qqmusicbox``。

    先尝试原子 rename（同文件系统），失败则回退到递归复制。
    若整个迁移不可行（如父目录只读），返回 False 以便调用方回退。
    """
    if not legacy.exists() or new.exists():
        return True
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.rename(new)
        return True
    except OSError:
        pass
    try:
        import shutil
        shutil.copytree(str(legacy), str(new), dirs_exist_ok=True)
        shutil.rmtree(str(legacy), ignore_errors=True)
        return True
    except OSError:
        return False


def _resolve_dir(legacy: Path, new: Path) -> Path:
    """解析 XDG 目录，无法创建时回退旧版路径。"""
    migrated = _migrate_legacy_dir(legacy, new)

    if not migrated:
        return legacy

    try:
        new.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if new.exists():
        return new

    if legacy.exists():
        return legacy
    return new




# 解析后的配置文件路径 (延迟求值, 首次访问时确定)
CONFIG_DIR = _resolve_dir(_LEGACY_CONFIG_DIR, _NEW_CONFIG_DIR)
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIAL_FILE = CONFIG_DIR / "credential.enc"  # 加密文件
CACHE_DIR = _resolve_dir(_LEGACY_CACHE_DIR, _NEW_CACHE_DIR)
LOG_FILE = CACHE_DIR / "qqmusicbox.log"
FAV_CACHE_FILE = CACHE_DIR / "fav_songs.cache"


def ensure_dirs() -> None:
    """确保配置和缓存目录存在

    配置目录设置为 0o700 权限（仅所有者可访问），保护敏感凭证。
    缓存目录使用默认权限（通常为 0o755），因为不包含敏感数据。
    """
    # 创建配置目录并设置严格权限
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError as e:
        logger.warning("无法设置配置目录权限: %s", e)

    # 创建缓存目录（D2 修复：收紧权限至 0700，error.log 可能含敏感数据）
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(str(CACHE_DIR), 0o700)
    except OSError as e:
        logger.debug("无法设置缓存目录权限: %s", e)


def configure_paths(config_dir: str | None = None, cache_dir: str | None = None) -> None:
    """配置自定义路径（作为库集成时使用）

    必须在创建 QQMusicClient 实例前调用。传入的路径会覆盖默认的 XDG 路径，
    使凭证文件、配置文件、日志等写入到指定目录。

    Args:
        config_dir: 自定义配置目录（存放 config.json、credential.enc、密钥文件）
        cache_dir: 自定义缓存目录（存放日志、收藏缓存等）

    Usage:
        from qqmusicbox.paths import configure_paths
        configure_paths(config_dir="/my/app/config", cache_dir="/my/app/cache")

        from qqmusicbox import QQMusicClient
        client = QQMusicClient()
    """
    global CONFIG_DIR, CONFIG_FILE, CREDENTIAL_FILE, CACHE_DIR, LOG_FILE, FAV_CACHE_FILE

    if config_dir is not None:
        CONFIG_DIR = Path(config_dir)
        CONFIG_FILE = CONFIG_DIR / "config.json"
        CREDENTIAL_FILE = CONFIG_DIR / "credential.enc"

    if cache_dir is not None:
        CACHE_DIR = Path(cache_dir)
        LOG_FILE = CACHE_DIR / "qqmusicbox.log"
        FAV_CACHE_FILE = CACHE_DIR / "fav_songs.cache"

    ensure_dirs()
