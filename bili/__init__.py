"""bili — Bilibili 音频/搜索/收藏/字幕 接口包（裁剪自 bilibili-cli）。

子模块：
- bili.client    异步 API：搜索 / 视频信息 / 字幕 / 收藏夹 / 热门 / 音频直链与下载
- bili.auth      凭证管理：文件凭证 / 浏览器 Cookie / 扫码登录（QRLoginSession）
- bili.lyrics    字幕条目 → LRC 歌词转换
- bili.exceptions 本地异常类型

依赖：bilibili-api-python>=16.0、aiohttp、qrcode（browser-cookie3 可选）。
接口文档见 bili/README.md。
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["client", "auth", "lyrics", "exceptions", "configure_paths", "__version__"]


def configure_paths(config_dir=None):
    """配置凭证存放目录（转发到 bili.auth.configure_paths）。"""
    from . import auth
    return auth.configure_paths(config_dir)
