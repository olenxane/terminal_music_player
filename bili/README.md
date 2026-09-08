# bili — Bilibili 接口包

`bili/` 是从 [bilibili-cli](../bilibili-cli-main/)（v0.6.2, Apache-2.0）裁剪而来的**独立异步接口包**，为终端播放器（`mp/`）提供 B站音频源能力。去掉了 CLI 命令层（click/rich），只保留数据获取与认证。

```
bili/
├── __init__.py      包入口（configure_paths 转发）
├── client.py        异步 API：搜索/视频信息/字幕/收藏夹/热门/音频直链与下载
├── auth.py          凭证管理：文件凭证/浏览器Cookie/扫码登录
├── lyrics.py        B站字幕条目 → LRC 歌词转换
├── exceptions.py    本地异常类型
└── smoke_test.py    冒烟测试（python -m bili.smoke_test）
```

## 依赖与安装

```bash
pip install "bilibili-api-python>=16.0" aiohttp   # 已写入根 requirements.txt
pip install browser-cookie3                        # 可选：浏览器 Cookie 自动提取
```

`qrcode` 为扫码渲染所需，播放器主依赖中已包含。

## 模块结构总览

- 所有网络函数均为 **async**（`bili/client.py`、`bili/auth.py` 的登录方法）。同步调用方请用 `asyncio.run(coro)` 或后台事件循环线程桥接（播放器内统一走 `mp/online_music.py` 的 `AsyncRunner`）。
- 异常统一为本地类型（见 [错误模型](#错误模型)），不会泄漏 SDK 异常。
- 日志走标准 `logging`（logger 名 `bili.client` / `bili.auth`），播放器侧由 `attach_external_logger` 汇入 `log/error.log`，不污染终端 UI。

---

## 一、搜索

### `client.search_video(keyword, page=1) -> list[dict]`

按关键词搜索视频（匿名可调）。

返回条目（取 `bvid` 非空的项）常用字段：

| 字段 | 说明 |
|---|---|
| `bvid` | 视频BV号（**作为曲目 song_id**） |
| `title` | 标题，**含 `<em class="keyword">` 高亮标签，展示前需剥离 HTML** |
| `author` | UP主名（→ 曲目 artist） |
| `duration` | 时长字符串 `"MM:SS"` 或 `"H:MM:SS"` |
| `play` | 播放量 |

### `client.search_user(keyword, page=1) -> list[dict]`

按关键词搜索 UP 主。字段：`mid`（UID）、`uname`、`fans`、`videos`、`usign`（签名）。

### `client.get_user_videos(uid, count=10, credential=None) -> list[dict]`

UP 主最新视频列表（内部自动翻页凑满 `count`，上限 20 页）。字段同热门/搜索视频（`bvid/title/length/play`），`length` 可能是 `"MM:SS"` 字符串或秒数。

---

## 二、首页推荐（热门视频翻页叠加）

### `client.get_hot_videos(pn=1, ps=20) -> dict`

B站热门视频，**支持 pn/ps 分页**。返回 `{"list": [...]}`，条目含 `bvid/title/owner.name/stat.view/duration`（秒）。

"多级叠加"用法——调用方持有页码，把新页 append 到已有列表：

```python
all_tracks = []
for pn in range(1, 4):                      # 3 页叠加 = 60 条
    data = await client.get_hot_videos(pn=pn, ps=20)
    all_tracks.extend(to_tracks(data["list"]))
```

---

## 三、个人收藏夹（需登录）

```python
cred = auth.get_credential(mode="read")     # 未登录返回 None
```

### `client.get_favorite_list(credential) -> list[dict]`

登录用户全部收藏夹：`{"id", "title", "media_count"}`。

### `client.get_favorite_videos(fav_id, credential, page=1) -> dict`

收藏夹内容，每页 20 条。返回 `{"medias": [...], "has_more": bool}`；
`medias` 条目：`bvid / title / upper.name / duration`（秒）。

**未登录行为**：抛 `AuthenticationError`。调用方应先 `auth.has_credential()` 判断。

---

## 四、音频（播放核心）

### `client.STREAM_HEADERS`

```python
{"User-Agent": "Mozilla/5.0 ...", "Referer": "https://www.bilibili.com"}
```

**B站 CDN 强校验 Referer**——任何访问音频直链的客户端（ffmpeg、requests、aiohttp）都必须携带。这是与 QQ/网易直链最大的差异。

### `client.get_audio_url(bvid, credential=None, max_quality=320) -> str`

获取音频流直链（DASH 优先，FLV/MP4 老流兜底）。

- `max_quality`：音质上限 kbps。**B站标准音频最高 192K**（SDK 17.x 枚举无 320K），`320` 语义为"尽力最高"，实际落到 `AudioQuality._192K`；取不到时按 `192 → 132 → 64` 自动回退。
- `credential`：可选，登录后可取会员视频音频。
- 失败（会员专属/无音频）抛 `BiliError`。

**ffmpeg 播放集成**（输入选项必须放在 `-i` 之前，多行头以 `\r\n` 分隔）：

```bash
ffmpeg -headers "Referer: https://www.bilibili.com\r\nUser-Agent: Mozilla/5.0 ...\r\n" \
       -i "<audio_url>" -f f32le -acodec pcm_f32le -
```

### `client.download_audio(audio_url, output_path) -> int`

下载音频流到文件（自带 STREAM_HEADERS、3 次重试、256KB 分块流式写盘），返回字节数。用作流式播放失败的回退路径。

---

## 五、歌词（视频字幕 → LRC）

B站字幕接口**要求登录**。`get_video_subtitle` 内部会自动加载已保存凭证；无凭证/凭证失效时**返回空而不是抛异常**。

### `client.get_video_subtitle(bvid, credential=None) -> tuple[str, list]`

返回 `(纯文本, 原始条目)`，条目结构 `{"from": 起始秒, "to": 结束秒, "content": 文本}`。优先中文字幕轨。空元组 = 未登录或视频无字幕（多数音乐视频无字幕，调用方应作"无歌词"兜底）。

### `lyrics.subtitle_to_lrc(items) -> str`

字幕条目 → 标准 LRC 文本（`[mm:ss.xx]内容`，按时间排序、去空行、时间戳重复行只留首条）。输出与播放器 `mp/lyrics.py` 的 `LRC_TIME_RE` 解析器直接兼容：

```python
from bili import client, lyrics
from mp.lyrics import _parse_lrc_text, LyricsData

_, items = await client.get_video_subtitle(bvid)
lrc_text = lyrics.subtitle_to_lrc(items)
lines = _parse_lrc_text(lrc_text)                     # list[LyricLine]
data = LyricsData(lines=lines, source_path=None, match_type="online", similarity=1.0)
```

---

## 六、元数据

### `client.get_video_info(bvid, credential=None) -> dict`

视频完整信息。播放器映射：`title` → 曲目标题、`owner.name` → artist、`duration`（秒）、`bvid` → song_id。

### `client.extract_bvid(url_or_bvid) -> str`

从任意 B站 URL 或裸 BV 号提取 BV 号（正则 `\bBV[0-9A-Za-z]{10}\b`），解析失败抛 `InvalidBvidError`。

---

## 七、认证

### 凭证存放

`auth.configure_paths(config_dir)` 配置凭证目录（文件为 `credential.json`，明文 JSON + 0o600 权限）。**播放器在管理器初始化时传入 `online_data/bili`**；未配置时默认 `~/.bilibili-cli/`。

### `auth.get_credential(mode="read") -> Credential | None`

| mode | 行为 |
|---|---|
| `optional` | 只读已保存凭证/进程缓存，**不联网校验、不扫浏览器**（每首歌取直链/字幕用这个，快） |
| `read` | 缓存/文件 → 联网校验（三态）→ 浏览器 Cookie（best effort） |
| `write` | 同 read 且必须有 `bili_jct` |

三态校验：`True`=有效；`False`=确认失效（自动清除后走下一级）；`None`=网络原因无法判断（best effort 返回，避免断网误删凭证）。凭证超过 7 天自动尝试从浏览器刷新。进程内缓存使重复调用零开销。

其余：`auth.has_credential()`（纯文件判断，不联网）、`auth.is_logged_in()`（联网校验）、`auth.logout()`（清缓存+删文件）。

### 扫码登录：`auth.QRLoginSession`

```python
sess = auth.get_qr_login_session()          # 进程级单例
info = await sess.start()                    # {"qr_link": str, "qr_png": bytes(PNG)}
result = await sess.check()                  # {"status": "waiting"|"scanned"|"success"|"expired"}
```

- `start()` 返回**二维码 PNG 字节**（qrcode 库本地生成，标准 4 模块静区）。认证层不做任何终端编码假设，也不静默降级。
- 终端显示由显示层完成（`mp/qr_terminal.py`，QQ/B站共用）：PNG → 模块矩阵 → **原生模块分辨率**半块字符渲染（1 模块 = 1 字符宽，2 行模块 = 1 字符行，模块物理正方形，可被手机扫描）。显示端编码不支持 `▀▄█` 时返回空，界面显式提示打开已保存的 PNG 文件（`online_data/bili/bili_login_qr.png`）。
- `check()` 返回 `success` 时凭证已自动保存（凭证包含 `finger/spi` 获取的 buvid3/buvid4 设备指纹）。
- 二维码有效期约 3 分钟，过期返回 `expired`，需重新 `start()`。
- **原生实现**：扫码流程（generate/poll/ticket 兑换）由本包直接以 requests 实现，不依赖 `bilibili_api.login_v2`（其仓库已删除，且解析不适配 B站新版响应——成功响应只回一次性 ticket，Cookie 需访问 crossDomain 链接从 Set-Cookie 头兑换，并跟随 gourl 完成收尾）。

### 凭证刷新（短期/长期凭证轮换）

B站 Web 凭证分两层：SESSDATA 等为**短期凭证**（会过期）；登录响应中的 `refresh_token`（存储为 `ac_time_value`）是**长期凭证**，可换发新短期凭证。

- `auth.refresh_needed(cred)` — `cookie/info` 查询是否需要刷新
- `auth.refresh_credential(cred)` — 四步轮换：固定 RSA 公钥加密时间戳得 `correspond_path` → `wc` → HMAC-SHA256 派生 `new_csrf` → `cookie/refresh` 换新短期凭证 → `confirm/refresh` 作废旧长期凭证。**nav 校验通过才覆盖本地凭证；任何一步失败（含风控 geetest）保留旧凭证**。
- `auth.maybe_auto_refresh()` — 播放器启动时自动检查（每 24 小时一次）
- 注意：刷新仅适用于"自然过期"的会话；被服务端主动撤销的会话无法通过刷新复活。

### 备用通道：bilicookies.txt 导入

`auth.import_cookies_txt(path)` — 解析 Netscape 格式（yt-dlp / "Get cookies.txt LOCALLY" 扩展导出，B站菜单"导入Cookie"入口读取工作区根目录的 `bilicookies.txt`），nav 校验通过才落盘。注意浏览器 Cookie 不含 refresh_token，不参与刷新轮换，过期后需重新导出。

---

## 错误模型

`bili/exceptions.py`，全部继承 `BiliError`：

| 异常 | 触发场景 | 建议处理 |
|---|---|---|
| `InvalidBvidError` | BV 号解析失败 | 输入提示 |
| `AuthenticationError` | 未登录访问需登录接口 / SDK 响应 -101/-111 | 引导扫码登录 |
| `NotFoundError` | 视频/资源不存在（-404 等） | 跳过该曲目 |
| `RateLimitError` | 风控限流（HTTP 412/-412） | 提示稍后重试；登录可降低概率 |
| `NetworkError` | 网络失败/超时/下载重试耗尽 | 重试或回退下载路径 |
| `BiliError` | 其余上游错误（含会员视频无音频流） | 记日志，跳过 |

---

## 播放器集成（mp/ 侧改动一览）

| 文件 | 改动 |
|---|---|
| `mp/online_music.py` | `OnlineTrack` 增 `stream_headers` 字段；`_ensure_bili_*` 延迟导入；`search/get_play_url/get_lyrics` 加 `"bili"` 分支；`_bili_video_to_track` 归一化（剥离标题 HTML、解析 `MM:SS` 时长、`song_id=bvid`）；新增 `bili_search_users / bili_get_user_videos / bili_get_home_recommend / bili_get_favorite_* / bili_login_qr_* / bili_has_credential / bili_is_logged_in / bili_logout` |
| `mp/ffmpeg_decoder.py` | `FFmpegAudioFile(path, headers=None)`：`-headers` 输入选项（probe 与 pipe 均携带） |
| `mp/player.py` | `Player.load(..., headers=None)` 透传 |
| `mp/app.py` | 平台选择 3 项；B站菜单五项（搜索视频/UP主搜索/首页推荐/我的收藏/登录退出）；首页推荐与收藏夹**列表到底自动追加下一页**；`_load_online_track` 传 `stream_headers` |
| `mp/online_ui.py` | `BILI_MENU`、`VIEW_BILI_*` 视图、`PLATFORM_NAMES` 映射替换二元三元式 |
| `mp/config.py` / `config.yaml` | `online_music.bi_quality: 320`（320=尽力最高，实际 192K） |

曲目映射：`OnlineTrack(platform="bili", song_id=bvid, title=标题, artist=UP主, album="", duration_sec=秒, stream_headers=client.STREAM_HEADERS)`。

## 冒烟测试

```bash
python -m bili.smoke_test
```

覆盖：搜索（视频/用户）、元数据、**音频直链 + ffmpeg 带 Referer 实流解码 5 秒**、热门两页叠加、字幕→LRC、收藏夹（已登录时）。

## 已知限制

- 字幕接口必须登录；多数音乐视频本身无字幕 → 歌词常为空，属预期行为。
- 匿名搜索偶发 412 风控，登录（凭证含 buvid3）后显著缓解；失败记入 `log/error.log`。
- 凭证明文存储（0o600），与 bilibili-cli 行为一致。
- 未集成：弹幕、评论、AI 总结、动态、点赞/投币、音频切分（PyAV/ASR）等 CLI 功能。
