# NEMbox 编程 API 文档

> 为其他 Python 程序提供完整的网易云音乐调用接口，支持搜索、播放、队列管理、歌词、登录等全部功能。

## 快速开始

```python
from NEMbox import MusicAPI

api = MusicAPI()

# 搜索歌曲
results = api.search("九九八十一")
print(results[0]["song_name"], results[0]["artist"])

# 播放
api.play_song(results[0]["song_id"])

# 查看状态
print(api.status())

# 暂停
api.pause()

# 恢复
api.resume()

# 使用完毕后关闭
api.close()
```

### 上下文管理器

```python
with MusicAPI() as api:
    api.play_song(33894312)
    # 退出 with 块时自动停止播放并保存状态
```

## 安装

```bash
# 已安装 NEMbox 的环境中可直接导入
pip install NetEase-MusicBox

# 或从源码安装
cd musicbox-master
uv sync --frozen
```

## 依赖说明

### 音频后端

播放功能需要系统安装以下至少一个音频后端：

| 后端 | 格式 | 安装 |
|------|------|------|
| `mpg123` | MP3（默认） | Windows: [官网下载](https://www.mpg123.de/download.shtml) / Linux: `apt-get install mpg123` |
| `mpv` | MP3 / FLAC / Hi-Res | Windows: [官网下载](https://mpv.io/installation/) / Linux: `apt-get install mpv` |

- **mpg123** 是默认后端，支持 MP3 播放
- **mpv** 支持 FLAC 无损播放和 seek（跳转）功能
- 在配置文件中将 `player_backend` 设为 `"mpv"` 可强制使用 mpv

### Python 依赖

以下包会在安装 NEMbox 时自动安装：

- `requests` / `requests-cache` — HTTP 请求与缓存
- `qrcode` — 二维码登录显示（可选，缺失时返回 URL）

---

## API 参考

### 类：`MusicAPI`

```python
from NEMbox import MusicAPI

api = MusicAPI(auto_load=True)
```

**参数：**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_load` | `bool` | `True` | 是否在构造时自动加载本地存储 |

### 异常：`MusicAPIError`

所有 API 方法在失败时抛出 `MusicAPIError`，包含以下属性：

```python
try:
    api.play_song(999999999)
except MusicAPIError as e:
    print(e.error_type)  # "api_error"
    print(e.message)     # "歌曲 999999999 不可播放"
    print(e.hint)        # "" 或提示文本
    print(e.to_dict())   # {"type": "api_error", "message": "..."}
```

---

## 搜索

### `search(keyword, *, stype="song", limit=20)`

搜索歌曲 / 歌手 / 专辑 / 歌单 / 电台。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `keyword` | `str` | — | 搜索关键词 |
| `stype` | `str` | `"song"` | 搜索类型：`song` / `artist` / `album` / `playlist` / `dj` |
| `limit` | `int` | `20` | 返回结果数量上限 |

**返回值：** `list[dict]`

对于 `stype="song"`，每项包含：

```python
{
    "song_id": 33894312,
    "song_name": "九九八十一",
    "artist": "乐正绫",
    "album_name": "...",
    "album_id": 12345,
    "mp3_url": "http://...",
    "type": "mp3",       # 或 "flac"
    "level": "exhigh",   # standard/higher/exhigh/lossless/hires
    "duration": 240,     # 秒
    "quality": "HD 320k",
    "expires": 1234567890,
    "get_time": 1234567890.0
}
```

**示例：**

```python
# 搜歌
results = api.search("周杰伦", stype="song", limit=10)

# 搜歌单
playlists = api.search("华语", stype="playlist", limit=5)
```

---

## 歌曲信息

### `get_song_info(song_id)`

获取歌曲元数据（网易云原始字段）。

```python
info = api.get_song_info(33894312)
print(info["name"], info["ar"], info["al"], info["dt"])
```

### `get_song_url(song_id, *, quality=None)`

获取歌曲播放 / 下载链接。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `song_id` | `int` | — | 歌曲 ID |
| `quality` | `str \| None` | `None` | 音质，不指定则用配置文件设置 |

可选音质：`standard` / `higher` / `exhigh` / `lossless` / `hires` / `jymaster`

```python
url_info = api.get_song_url(33894312, quality="lossless")
print(url_info["url"])  # 下载链接
print(url_info["br"])   # 码率
```

### `get_lyrics(song_id=None)`

获取歌词（原文 + 翻译）。不传 `song_id` 则获取当前播放歌曲的歌词。

```python
lyrics = api.get_lyrics(33894312)
for line in lyrics["lyric"]:
    print(line)
# 翻译歌词
for line in lyrics["tlyric"]:
    print(line)
```

---

## 播放控制

### `play_song(song_id)`

播放单首歌曲（替换当前队列）。

```python
api.play_song(33894312)
```

### `play_songs(song_ids)`

播放多首歌曲（替换当前队列）。

```python
api.play_songs([33894312, 34723012, 43652345])
```

### `play_playlist(playlist_id)`

播放整个歌单。

```python
api.play_playlist(3778678)
```

### `play_artist(artist_id, *, limit=20)`

播放某歌手的热门歌曲。

```python
api.play_artist(6452, limit=30)  # 周杰伦前30首
```

### `play_album(album_id)`

播放整张专辑。

```python
api.play_album(32311)
```

### `play_index(index)`

播放队列中指定索引的歌曲。

```python
api.play_index(3)  # 播放队列第4首
```

### `pause()` / `resume()` / `toggle_play_pause()`

```python
api.pause()              # 暂停
api.resume()             # 恢复
api.toggle_play_pause()  # 切换
```

### `stop()`

停止播放（保留队列）。

### `next(n=1)` / `prev(n=1)`

切换歌曲，`n` 为跳过的首数。

```python
api.next()      # 下一首
api.prev(2)     # 前2首
```

### `seek(seconds, *, relative=False)`

跳转到指定位置（仅 mpv 后端支持）。

```python
api.seek(60)                # 跳到第60秒
api.seek(-10, relative=True) # 后退10秒
```

---

## 音量控制

```python
api.set_volume(80)       # 设置音量为80
api.adjust_volume(-10)   # 音量减10
api.volume_up(5)         # 音量加5
api.volume_down(5)       # 音量减5
```

---

## 播放模式

```python
api.set_mode("single-loop")  # 单曲循环
api.cycle_mode()              # 循环切换到下一个模式
```

可选模式：

| 模式 | 说明 |
|------|------|
| `ordered` | 顺序播放 |
| `ordered-loop` | 列表循环 |
| `single-loop` | 单曲循环 |
| `random` | 随机播放 |
| `random-loop` | 随机循环 |

---

## 队列管理

### `queue_list()`

返回当前播放队列。

```python
queue = api.queue_list()
print(f"队列共 {queue['size']} 首，当前第 {queue['index']} 首")
for item in queue["items"]:
    mark = "▶" if item["current"] else " "
    print(f"{mark} [{item['index']}] {item['name']} — {item['artist']}")
```

### `queue_add(song_ids)`

向队列末尾添加歌曲。

```python
api.queue_add([111, 222, 333])
```

### `queue_clear()`

清空播放队列。

---

## 状态查询

### `status()`

返回当前播放状态。

```python
status = api.status()
# {
#     "state": "playing",       # playing / paused / stopped
#     "song": {
#         "id": 33894312,
#         "name": "九九八十一",
#         "artist": "乐正绫",
#         "album": "...",
#         "duration": 240
#     },
#     "position": 45.3,         # 当前播放位置（秒）
#     "length": 240,            # 歌曲总时长（秒）
#     "volume": 60,             # 音量
#     "mode": "ordered",        # 播放模式
#     "backend": "mpg123",      # 音频后端
#     "queue_index": 0,         # 当前在队列中的索引
#     "queue_size": 1           # 队列总长度
# }
```

---

## 歌单 / 排行榜 / 推荐

### `get_playlist(playlist_id)`

获取歌单中的歌曲列表（不播放）。

```python
songs = api.get_playlist(3778678)
for s in songs:
    print(s["song_id"], s["song_name"])
```

### `get_toplists()`

获取排行榜列表。

```python
charts = api.get_toplists()
for chart in charts:
    print(f"[{chart['index']}] {chart['name']} (id={chart['id']})")
```

### `get_top_songs(index)`

获取排行榜中的歌曲。

```python
songs = api.get_top_songs(0)  # 获取第0个排行榜的歌曲
```

### `get_recommend_songs(*, limit=20)` — 需要登录

获取每日推荐歌曲。

### `get_recommend_playlists()` — 需要登录

获取推荐歌单列表。

### `get_personal_fm()` — 需要登录

获取私人 FM 推荐歌曲。

### `get_user_playlists(user_id, *, limit=50)`

获取指定用户的歌单列表。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user_id` | `int` | — | 用户 ID |
| `limit` | `int` | `50` | 返回数量上限 |

**返回值：** `list[dict]`，每项包含 `id` / `name` / `creator` / `trackCount` / `coverImgUrl` 等字段。

```python
# 获取当前登录用户的所有歌单
auth = api.get_auth_status()
if auth["logged_in"]:
    playlists = api.get_user_playlists(auth["user_id"])
    for pl in playlists:
        print(f"[{pl['id']}] {pl['name']} ({pl['trackCount']}首)")
```

---

## 互动

### `get_comments(song_id, *, limit=20)`

获取歌曲评论。

```python
comments = api.get_comments(33894312, limit=5)
for c in comments["comments"]:
    print(f"{c['user']['nickname']}: {c['content']}")
print(f"共 {comments['total']} 条评论")
```

### `like_song(song_id)` — 需要登录

红心（喜欢）一首歌曲。

---

## 登录认证

NEMbox 仅支持二维码扫码登录。

### `login_qr_start()`

发起二维码登录流程，返回二维码信息。

```python
login_info = api.login_qr_start()
print(login_info["qr_ascii"])  # 打印 ASCII 二维码
print(login_info["qr_url"])    # 二维码 URL
# 保存 unikey 用于后续检查
unikey = login_info["unikey"]
```

### `login_qr_check(unikey)`

检查扫码状态（需轮询调用）。

```python
while True:
    result = api.login_qr_check(unikey)
    status = result["status"]
    if status == "success":
        print(f"登录成功: {result['nickname']} (id={result['user_id']})")
        break
    elif status == "expired":
        print("二维码已过期")
        break
    time.sleep(2)
```

### `login_qr_wait(unikey, *, timeout=300)`

阻塞等待扫码登录完成（自动轮询）。

```python
login_info = api.login_qr_start()
print(login_info["qr_ascii"])
# 用户扫码后...
result = api.login_qr_wait(login_info["unikey"], timeout=300)
print(f"登录成功: {result['nickname']}")
```

### `get_auth_status()`

获取当前登录状态。

```python
status = api.get_auth_status()
print(status)  # {"logged_in": True, "user_id": 12345, "nickname": "..."}
```

### `logout()`

退出登录，清除 cookie 与账号缓存。

---

## 资源管理

### `close()`

停止播放并保存状态。调用后不应再使用此实例。

```python
api.close()
```

### 上下文管理器

```python
with MusicAPI() as api:
    api.play_song(33894312)
    # 退出时自动调用 close()
```

---

## 跨平台说明

| 平台 | 搜索/歌词/歌单 | 播放 (mpg123) | 播放 (mpv) | seek |
|------|:---:|:---:|:---:|:---:|
| Windows | ✅ | ✅ | ✅ | ⚠️ |
| Linux | ✅ | ✅ | ✅ | ✅ |
| macOS | ✅ | ✅ | ✅ | ✅ |

- Windows 上使用 mpv 的 seek 功能需要 Windows 10 1803+ 且 mpv 支持 `--input-ipc-server`
- 网易云 API 需要中国区域访问，其他区域请配置 HTTP 代理

## 配置

API 使用与 CLI 相同的配置文件（`~/.netease-musicbox/config.json`）。

关键配置项：

```json
{
    "music_quality": {"value": "exhigh"},
    "player_backend": {"value": "mpg123"},
    "notifier": {"value": false}
}
```

可通过原有 `Config` 类修改：

```python
from NEMbox.config import Config

config = Config()
config.config["music_quality"]["value"] = "lossless"
config.config["player_backend"]["value"] = "mpv"
config.save_config_file()
```

---

## 完整示例：搜索并播放

```python
import time
from NEMbox import MusicAPI

with MusicAPI() as api:
    # 搜索
    results = api.search("九九八十一", stype="song", limit=5)
    if not results:
        print("未找到结果")
        exit()

    for i, song in enumerate(results):
        print(f"[{i}] {song['song_name']} — {song['artist']}")

    # 播放第一首
    api.play_song(results[0]["song_id"])
    print(f"正在播放: {results[0]['song_name']}")

    # 等待播放
    time.sleep(10)

    # 查看状态
    status = api.status()
    print(f"位置: {status['position']}/{status['length']}秒")
    print(f"状态: {status['state']}")
```
