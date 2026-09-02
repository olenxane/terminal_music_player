# QQMusicBox API 库（集成版）

> 从 QQMusicBox 项目抽取的 **纯 API 库**，不含终端 UI 和音频播放器，专为集成到其他 Python 项目设计。

## 目录结构

```
qq/
├── qqmusicbox/            # 核心包（15 个文件，可独立导入）
│   ├── __init__.py        # 包入口（导出完整 API）
│   ├── api.py             # QQMusicClient + 数据模型（Song/Album/Playlist/Lyric 等）
│   ├── api_auth.py        # 登录认证（二维码扫码、凭证管理、收藏）
│   ├── api_search.py      # 搜索（歌曲/专辑/歌手/歌单）
│   ├── api_song.py        # 歌曲（播放URL、详情、专辑歌曲）
│   ├── api_lyric.py       # 歌词（原文/翻译/罗马音）
│   ├── api_playlist.py    # 歌单/排行榜/推荐/歌手
│   ├── config.py          # 配置与常量
│   ├── paths.py           # 路径解析 + configure_paths（自定义路径）
│   ├── crypto.py          # Fernet 凭证加密
│   ├── credential_store.py# 凭证加密存储（save/load/clear）
│   ├── utils.py           # 工具函数
│   ├── cache.py           # 异步缓存装饰器
│   ├── rate_limiter.py    # 令牌桶限流器
│   └── exceptions.py      # 自定义异常
├── pyproject.toml         # 依赖声明（pip install . 可安装）
├── API.md                 # 完整 API 接口文档
└── example.py             # 集成使用示例
```

## 快速开始

### 方式一：直接复制包使用

把 `qq/` 整个文件夹复制到你的项目目录，然后：

```python
import sys
sys.path.insert(0, "qq")  # 指向 qq/ 所在目录

from qqmusicbox import QQMusicClient
client = QQMusicClient()
```

### 方式二：pip 安装

```bash
cd qq
pip install .
```

## 最小示例

```python
import asyncio
from qqmusicbox import QQMusicClient

async def main():
    client = QQMusicClient()

    # 恢复已保存的登录凭证（如已登录过）
    client.load_credential()

    # 搜索歌曲
    songs = await client.search_songs("晴天")
    for song in songs:
        print(f"{song.name} - {song.singer} (mid={song.mid})")

    # 获取播放/下载链接
    url = await client.get_song_url(songs[0].mid, quality=320)

    # 获取歌词
    lyric = await client.get_lyric(songs[0].mid)
    for line in lyric.lines:
        print(f"[{line.time_ms}ms] {line.text}")

asyncio.run(main())
```

## 自定义存储路径

```python
from qqmusicbox import configure_paths

# 必须在创建 QQMusicClient 之前调用
configure_paths(config_dir="/my/app/config", cache_dir="/my/app/cache")
```

## 登录（QQ音乐APP扫码）

```python
import asyncio, os
from pathlib import Path
from qqmusicbox import QQMusicClient
from qqmusic_api.models.login import QRLoginType, QRCodeLoginEvents

async def login():
    c = QQMusicClient()
    qr = await c.get_qrcode(QRLoginType.MOBILE)  # MOBILE = QQ音乐APP扫码
    Path("qr.png").write_bytes(qr["data"])       # data 是 PNG 图片
    os.startfile("qr.png")                        # 打开图片（Windows）

    import anyio
    deadline = anyio.current_time() + 180
    async for result in c._client.login.checking_mobile_qrcode(c._login_qr, deadline=deadline):
        if result.event == QRCodeLoginEvents.DONE:
            c.set_credential(result.credential)
            await c._save_credential(result.credential)  # 自动加密保存
            print("登录成功")
            return

asyncio.run(login())
```

## 已验证的功能

- ✅ 搜索歌曲/专辑/歌手/歌单
- ✅ 获取歌词（原文+翻译+罗马音）
- ✅ 获取播放/下载 URL
- ✅ 推荐歌单、排行榜、热门歌曲、歌手歌曲
- ✅ 歌单歌曲、专辑歌曲、歌手信息
- ✅ 二维码扫码登录（QQ音乐APP）、凭证持久化
- ✅ 收藏/取消收藏歌曲、检查收藏状态
- ✅ 自定义路径、上下文管理器

## 完整文档

详细 API 方法签名和说明见 [API.md](API.md)。
