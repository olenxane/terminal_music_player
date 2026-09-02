# QQMusicBox API 接口文档

> 本文档面向将 QQMusicBox 作为 Python 库集成到其他程序中的开发者。
> 终端 UI 相关内容请参考 [UI 说明文档](UI.md)。

---

## 快速开始

### 安装

```bash
pip install .
```

### 最小示例

```python
import asyncio
from qqmusicbox import QQMusicClient, Song

async def main():
    client = QQMusicClient()

    # 恢复已保存的登录凭证
    client.load_credential()

    # 搜索歌曲
    songs = await client.search_songs("晴天")
    for song in songs:
        print(f"{song.name} - {song.singer} (mid={song.mid})")

    # 获取播放/下载链接
    url = await client.get_song_url(songs[0].mid, quality=320)
    print(f"播放链接: {url}")

    # 获取歌词
    lyric = await client.get_lyric(songs[0].mid)
    for line in lyric.lines:
        print(f"[{line.time_ms}ms] {line.text}  {line.translation}")

asyncio.run(main())
```

---

## 自定义路径

默认情况下，凭证、配置、日志等文件存放在 XDG 标准目录。作为库集成时，可以指定自定义路径：

```python
from qqmusicbox import configure_paths

# 必须在创建 QQMusicClient 之前调用
configure_paths(
    config_dir="/my/app/config",   # 存放 credential.enc、密钥文件
    cache_dir="/my/app/cache",     # 存放日志、收藏缓存
)

# 之后正常使用
from qqmusicbox import QQMusicClient
client = QQMusicClient()
```

> **注意**：`configure_paths` 会创建目录（权限 0o700），可以只传其中一个参数。

---

## 客户端

### 创建客户端

```python
from qqmusicbox import QQMusicClient

# 方式一：直接实例化（推荐，独立实例不受全局单例影响）
client = QQMusicClient()

# 方式二：全局单例（线程安全，适合多处共享）
from qqmusicbox import get_client
client = get_client()

# 关闭全局单例
from qqmusicbox import close_client
await close_client()
```

`QQMusicClient` 是一个异步客户端，所有 API 方法都是 `async` 方法，需要在事件循环中调用。

### 上下文管理

```python
async with QQMusicClient() as client:
    songs = await client.search_songs("周杰伦")
# 退出时自动关闭
```

---

## 登录认证

### 二维码扫码登录

```python
import asyncio
from qqmusicbox import QQMusicClient

async def login():
    client = QQMusicClient()

    # 1. 获取二维码
    qr = await client.get_qrcode()
    print(f"二维码数据: {qr['data']}")
    print(f"二维码类型: {qr['mimetype']}")

    # 2. 轮询扫码状态
    while True:
        result = await client.check_qrcode()
        status = result["status"]
        # status: 0=已扫描, 1=已确认, 2=登录成功, -1=等待/过期
        if status == 2:
            print("登录成功！")
            break
        elif status == 0:
            print("已扫描，请在手机上确认")
        elif status == -1:
            print("等待扫描中...")
        await asyncio.sleep(3)

asyncio.run(login())
```

### 凭证持久化

```python
from qqmusicbox import QQMusicClient, save_credential, load_credential, clear_credential

# 保存凭证（登录成功后自动调用，也可手动调用）
# save_credential(cred_dict)

# 加载凭证
client = QQMusicClient()
client.load_credential()  # 从加密文件加载，返回 bool

# 清除凭证（退出登录）
client.logout()
# 或直接调用
# clear_credential()
```

凭证使用 Fernet 对称加密存储，密钥保存在配置目录下的 `.credential_key` 文件中。

### 检查登录状态

```python
# 同步快速检查（不触发网络请求）
if client.has_credential():
    print("凭证字段完整")

# 异步完整检查（含凭证过期自动刷新）
if await client.is_logged_in():
    print("已登录")
```

### 凭证刷新

```python
# 主动刷新（建议在凭证即将过期时调用）
success = await client.refresh_credential()

# 主动提前刷新（内部有频率控制，30分钟一次）
success = await client.proactive_refresh()
```

---

## 数据模型

### Song

| 属性 | 类型 | 说明 |
|------|------|------|
| `mid` | `str` | 歌曲唯一标识 |
| `name` | `str` | 歌曲名 |
| `singer` | `str` | 歌手名（多人用 `/` 分隔） |
| `album` | `str` | 专辑名 |
| `album_mid` | `str` | 专辑 mid |
| `duration` | `int` | 时长（秒） |
| `image_url` | `str` | 封面图 URL |
| `is_vip` | `bool` | 是否需要 VIP |
| `display_name` | `str` | `"歌名 - 歌手"` 格式的显示名（property） |

### Album

| 属性 | 类型 | 说明 |
|------|------|------|
| `mid` | `str` | 专辑唯一标识 |
| `name` | `str` | 专辑名 |
| `singer` | `str` | 歌手名 |
| `image_url` | `str` | 封面图 URL |
| `songs` | `List[Song]` | 包含的歌曲列表 |

### Artist

| 属性 | 类型 | 说明 |
|------|------|------|
| `mid` | `str` | 歌手唯一标识 |
| `name` | `str` | 歌手名 |
| `image_url` | `str` | 头像 URL |

### Playlist

| 属性 | 类型 | 说明 |
|------|------|------|
| `dissid` | `str` | 歌单唯一标识 |
| `name` | `str` | 歌单名 |
| `image_url` | `str` | 封面图 URL |
| `songs` | `List[Song]` | 包含的歌曲列表 |

### Lyric

| 属性/方法 | 类型 | 说明 |
|-----------|------|------|
| `lines` | `List[LyricLine]` | 歌词行列表（按时间排序） |
| `get_line_at_time(time_ms)` | `str` | 根据时间戳获取当前歌词行 |
| `get_display_lines_at_time(time_ms, show_translation, show_romanization)` | `List[str]` | 获取当前应显示的歌词行（支持双语） |

### LyricLine

| 属性 | 类型 | 说明 |
|------|------|------|
| `time_ms` | `int` | 时间戳（毫秒） |
| `text` | `str` | 原文歌词 |
| `translation` | `str` | 中文翻译（如有） |
| `romanization` | `str` | 罗马音/拼音（如有） |

### SearchType

| 枚举值 | 说明 |
|--------|------|
| `SONG` (0) | 歌曲 |
| `ALBUM` (2) | 专辑 |
| `PLAYLIST` (5) | 歌单 |

---

## API 方法一览

### 搜索（api_search.py）

#### `search_songs(keyword, num=None) -> List[Song]`

搜索歌曲。

| 参数 | 类型 | 说明 |
|------|------|------|
| `keyword` | `str` | 搜索关键词 |
| `num` | `int?` | 返回数量（默认 30） |

```python
songs = await client.search_songs("周杰伦", num=20)
```

#### `search_albums(keyword, num=None) -> List[Album]`

搜索专辑。

#### `search_artists(keyword, num=None) -> List[Artist]`

搜索歌手。

#### `search_playlists(keyword, num=None) -> List[Playlist]`

搜索歌单。

---

### 歌曲（api_song.py）

#### `get_song_url(song_mid, quality=320) -> Optional[str]`

获取歌曲播放/下载 URL。

| 参数 | 类型 | 说明 |
|------|------|------|
| `song_mid` | `str` | 歌曲 mid |
| `quality` | `int` | 音质：128 或 320（默认 320） |

返回 CDN 直链（`https://ws.stream.qqmusic.qq.com/...`），VIP 歌曲需登录 VIP 账号。包含自动重试和凭证刷新逻辑。

```python
url = await client.get_song_url("001c0IjS4U2Djm", quality=320)
if url:
    # 下载
    import httpx
    resp = await httpx.AsyncClient().get(url)
    with open("song.mp3", "wb") as f:
        f.write(resp.content)
```

#### `get_song_info(song_mid) -> Optional[Song]`

获取歌曲详情（名称、歌手、专辑、时长）。

#### `get_album_songs(album_mid) -> List[Song]`

获取专辑中的所有歌曲，支持分页自动加载。

---

### 歌词（api_lyric.py）

#### `get_lyric(song_mid) -> Optional[Lyric]`

获取歌词（含原文、翻译、罗马音）。结果有 24 小时缓存。

```python
lyric = await client.get_lyric(song.mid)
if lyric:
    for line in lyric.lines:
        print(f"[{line.time_ms}ms] {line.text}")
        if line.translation:
            print(f"  翻译: {line.translation}")
        if line.romanization:
            print(f"  罗马音: {line.romanization}")
```

---

### 歌单与排行榜（api_playlist.py）

#### `get_playlist_songs(dissid, num=300) -> List[Song]`

获取歌单中的歌曲。

| 参数 | 类型 | 说明 |
|------|------|------|
| `dissid` | `str` | 歌单 ID |
| `num` | `int` | 最多获取数量（默认 300） |

#### `get_hot_songs(num=None) -> List[Song]`

获取热门歌曲（热歌榜）。

#### `get_recommend_playlists(num=None) -> List[Playlist]`

获取推荐歌单。

#### `get_top_list() -> List[Dict]`

获取所有排行榜列表。

返回字典列表，每项包含：
- `id`: 排行榜 ID
- `title`: 排行榜名称
- `listen_num`: 播放次数

#### `get_top_songs(top_id=0, num=None) -> List[Song]`

获取排行榜歌曲。

#### `get_artist_songs(singer_mid, num=None) -> List[Song]`

获取歌手热门歌曲。

#### `get_artist_info(singer_mid) -> Optional[Dict]`

获取歌手信息。

---

### 用户功能（api_auth.py）

#### `get_fav_songs(num=300, start_page=1, use_cache=True) -> List[Song]`

获取"我喜欢的"歌曲列表。

#### `get_created_songlist(num=None) -> List[Playlist]`

获取我创建的歌单。

#### `get_fav_songlist(num=None) -> List[Playlist]`

获取我收藏的歌单。

#### `get_fav_albums(num=None) -> List[Album]`

获取我喜欢的专辑。

#### `like_song(song_mid) -> bool`

收藏歌曲到"我喜欢"。

#### `unlike_song(song_mid) -> bool`

从"我喜欢"移除歌曲。

#### `is_song_liked(song_mid) -> bool`

检查歌曲是否已收藏（带缓存）。

---

## 错误处理

所有 API 方法在失败时返回默认值（空列表或 None），不会抛出异常。内部已处理以下错误类型：

- **网络错误**：连接超时、DNS 失败等（自动重试）
- **凭证失效**：musickey 过期等（自动刷新凭证后重试）
- **限流**：请求频率超限（内置令牌桶限流器）
- **数据解析错误**：API 返回格式异常

如需捕获特定异常：

```python
from qqmusicbox import EncryptionError, RateLimitError
```

---

## 模块依赖关系

```
__init__.py
  └── api.py (QQMusicClient + 数据模型)
        ├── api_search.py    (SearchMixin)
        ├── api_song.py       (SongMixin)
        ├── api_playlist.py   (PlaylistMixin)
        ├── api_lyric.py      (LyricMixin)
        ├── api_auth.py       (AuthMixin)
        ├── rate_limiter.py   (令牌桶限流器)
        ├── cache.py          (缓存装饰器)
        ├── utils.py          (工具函数)
        └── exceptions.py     (异常定义)

credential_store.py → crypto.py → paths.py
config.py → paths.py
```

作为库使用时，**不需要**导入以下模块：
- `ui*.py`（终端 UI）
- `player*.py`（音频播放器）
- `main.py`（TUI 入口）
- `input_handler.py`（键盘输入）
- `error_handler.py`（TUI 错误处理）
