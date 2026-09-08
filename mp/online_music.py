"""在线音乐管理器：封装QQ音乐（异步）、网易云音乐（同步）和哔哩哔哩（异步）API"""
from __future__ import annotations
import asyncio
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

from .lyrics import LyricsData, LyricLine, lyrics_from_lines, _parse_lrc_text

# ---- sys.path 设置 ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QQ_DIR = os.path.join(_PROJECT_ROOT, "qq")
_WY_DIR = os.path.join(_PROJECT_ROOT, "wy")
for _d in (_QQ_DIR, _WY_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# ---- 延迟导入标记 ----
_qq_imported = False
_wy_imported = False
_bili_imported = False
QQMusicClient = None
qq_configure_paths = None
QRLoginType = None
MusicAPI = None
MusicAPIError = None
bili_client = None
bili_auth = None
bili_lyrics = None


def _ensure_qq_import():
    global _qq_imported, QQMusicClient, qq_configure_paths, QRLoginType
    if not _qq_imported:
        from qqmusicbox import QQMusicClient as _C, configure_paths as _cp
        from qqmusic_api.models.login import QRLoginType as _QT
        QQMusicClient = _C
        qq_configure_paths = _cp
        QRLoginType = _QT
        # 库日志静默：路由到 log/error.log，不输出到终端
        from .logging_utils import attach_external_logger
        attach_external_logger("qqmusicbox")
        _qq_imported = True


def _ensure_wy_import():
    global _wy_imported, MusicAPI, MusicAPIError
    if not _wy_imported:
        from NEMbox import MusicAPI as _M, MusicAPIError as _E
        MusicAPI = _M
        MusicAPIError = _E
        _wy_imported = True


def _ensure_bili_import():
    global _bili_imported, bili_client, bili_auth, bili_lyrics
    if not _bili_imported:
        if _PROJECT_ROOT not in sys.path:
            sys.path.insert(0, _PROJECT_ROOT)
        from bili import client as _c, auth as _a, lyrics as _l
        bili_client, bili_auth, bili_lyrics = _c, _a, _l
        # 库日志静默：路由到 log/error.log，不输出到终端
        from .logging_utils import attach_external_logger
        for name in ("bili.client", "bili.auth", "bilibili_api"):
            attach_external_logger(name)
        _bili_imported = True


@dataclass
class OnlineTrack:
    """在线歌曲统一表示"""
    title: str
    artist: str
    album: str
    duration_sec: float
    platform: str            # "qq" / "wy" / "bili"
    song_id: str             # QQ的mid / 网易的song_id / B站的bvid
    is_vip: bool = False
    liked: bool = False      # 是否已收藏（仅 QQ 平台有效）
    lyric_data: Optional[LyricsData] = None
    stream_headers: Optional[dict] = None   # 直链所需 HTTP 请求头（B站需 Referer）
    stream_urls_backup: Optional[list] = None  # 备用直链线路（B站多 CDN）

    @property
    def display_name(self) -> str:
        return f"{self.title} - {self.artist}"


def _strip_html(text: str) -> str:
    """剥离 B站搜索结果标题中的高亮标签（<em class="keyword"> 等）"""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_bili_duration(raw) -> float:
    """解析 B站时长字段：秒数（int/float）或 "MM:SS"/"H:MM:SS" 字符串"""
    if isinstance(raw, (int, float)):
        return float(raw or 0)
    if isinstance(raw, str):
        parts = raw.strip().split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return 0.0
    return 0.0


class AsyncRunner:
    """在后台线程中运行 asyncio 事件循环，供同步代码调用异步函数"""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro):
        """阻塞执行 async 协程，返回结果"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def shutdown(self):
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
        except Exception:
            pass


class OnlineMusicManager:
    """在线音乐统一管理器"""

    def __init__(self, config_dir: str, qq_quality: int = 320,
                 wy_quality: str = "exhigh", bi_quality: int = 320):
        self._config_dir = config_dir
        self._qq_quality = qq_quality
        self._wy_quality = wy_quality
        self._bi_quality = bi_quality
        self._async = AsyncRunner()
        self._qq_client = None
        self._wy_api = None
        self._bili_ready = False

    # ---------- 客户端管理 ----------
    def _ensure_qq(self):
        if self._qq_client is None:
            _ensure_qq_import()
            qq_dir = os.path.join(self._config_dir, "qq")
            qq_cache = os.path.join(self._config_dir, "qq_cache")
            qq_configure_paths(config_dir=qq_dir, cache_dir=qq_cache)
            self._qq_client = QQMusicClient()
            self._qq_client.load_credential()
        return self._qq_client

    def _ensure_wy(self):
        if self._wy_api is None:
            _ensure_wy_import()
            self._wy_api = MusicAPI()
        return self._wy_api

    def _ensure_bili(self):
        """初始化 B站凭证目录（模块级导入由 _ensure_bili_import 完成一次）"""
        if not self._bili_ready:
            _ensure_bili_import()
            bili_auth.configure_paths(os.path.join(self._config_dir, "bili"))
            self._bili_ready = True
            # 每进程一次：凭证需要刷新时自动续期（长期凭证轮换）
            try:
                bili_auth.maybe_auto_refresh()
            except Exception:
                pass

    def shutdown(self):
        self._async.shutdown()

    # ---------- 搜索 ----------
    def search(self, platform: str, keyword: str, num: int = 20) -> list:
        if platform == "qq":
            return self._qq_search(keyword, num)
        elif platform == "wy":
            return self._wy_search(keyword, num)
        elif platform == "bili":
            return self._bili_search(keyword, num)
        return []

    def _qq_search(self, keyword: str, num: int) -> list:
        client = self._ensure_qq()
        songs = self._async.run(client.search_songs(keyword, num=num))
        return [self._qq_song_to_track(s) for s in songs]

    def _wy_search(self, keyword: str, num: int) -> list:
        api = self._ensure_wy()
        results = api.search(keyword, stype="song", limit=num)
        tracks = []
        for r in results:
            tracks.append(OnlineTrack(
                title=r.get("song_name", "未知"),
                artist=r.get("artist", "未知艺人"),
                album=r.get("album_name", "未知专辑"),
                duration_sec=float(r.get("duration", 0)),
                platform="wy",
                song_id=str(r.get("song_id", "")),
            ))
        return tracks

    # ---------- QQ音乐功能 ----------
    def _qq_song_to_track(self, song) -> OnlineTrack:
        return OnlineTrack(
            title=song.name,
            artist=song.singer,
            album=song.album,
            duration_sec=float(song.duration or 0),
            platform="qq",
            song_id=song.mid,
            is_vip=song.is_vip,
        )

    def qq_get_recommend_playlists(self) -> list:
        client = self._ensure_qq()
        playlists = self._async.run(client.get_recommend_playlists())
        return [{"dissid": p.dissid, "name": p.name, "image_url": getattr(p, "image_url", "")}
                for p in playlists]

    def qq_get_daily_mix(self) -> list:
        """获取QQ音乐每日30首个性化推荐

        官方 /discover/daily-mix 接口返回的 H5 链接不能直接播放，
        因此按歌名走搜索流程，取第一个搜索结果构造可播放的 OnlineTrack。
        """
        import requests

        key_path = os.path.join(_QQ_DIR, "key")
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                api_key = f.read().strip()
        except OSError:
            return []
        if not api_key:
            return []

        try:
            resp = requests.post(
                "https://a.y.qq.com/discover/daily-mix",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"params": {}, "comm": {"skill_version": "0.0.2"}},
                timeout=10,
            )
            data = resp.json()
        except Exception:
            return []

        songlist = data.get("songlist") or []
        client = self._ensure_qq()
        tracks = []
        for item in songlist:
            title = item.get("songName", "").strip()
            singer = item.get("singerName", "").strip()
            if not title:
                continue
            keyword = f"{title} {singer}".strip()
            try:
                songs = self._async.run(client.search_songs(keyword, num=1))
            except Exception:
                songs = []
            if not songs:
                continue
            tracks.append(self._qq_song_to_track(songs[0]))
        return tracks

    def qq_get_playlist_songs(self, dissid: str) -> list:
        client = self._ensure_qq()
        songs = self._async.run(client.get_playlist_songs(dissid))
        return [self._qq_song_to_track(s) for s in songs]

    def qq_get_fav_songs(self) -> list:
        client = self._ensure_qq()
        songs = self._async.run(client.get_fav_songs())
        return [self._qq_song_to_track(s) for s in songs]

    def qq_get_fav_songlists(self) -> list:
        client = self._ensure_qq()
        playlists = self._async.run(client.get_fav_songlist())
        return [{"dissid": p.dissid, "name": p.name}
                for p in playlists]

    def qq_get_created_songlists(self) -> list:
        client = self._ensure_qq()
        playlists = self._async.run(client.get_created_songlist())
        return [{"dissid": p.dissid, "name": p.name}
                for p in playlists]

    def qq_get_hot_songs(self) -> list:
        client = self._ensure_qq()
        songs = self._async.run(client.get_hot_songs())
        return [self._qq_song_to_track(s) for s in songs]

    def qq_get_top_list(self) -> list:
        client = self._ensure_qq()
        return self._async.run(client.get_top_list())

    def qq_get_top_songs(self, top_id: int) -> list:
        client = self._ensure_qq()
        songs = self._async.run(client.get_top_songs(top_id=top_id))
        return [self._qq_song_to_track(s) for s in songs]

    def qq_like_song(self, song_mid: str) -> bool:
        client = self._ensure_qq()
        return self._async.run(client.like_song(song_mid))

    def qq_unlike_song(self, song_mid: str) -> bool:
        client = self._ensure_qq()
        return self._async.run(client.unlike_song(song_mid))

    def qq_is_song_liked(self, song_mid: str) -> bool:
        client = self._ensure_qq()
        return self._async.run(client.is_song_liked(song_mid))

    # ---------- 网易云功能 ----------
    def wy_get_toplists(self) -> list:
        api = self._ensure_wy()
        return api.get_toplists()

    def wy_get_top_songs(self, index: int) -> list:
        api = self._ensure_wy()
        songs = api.get_top_songs(index)
        tracks = []
        for r in songs:
            tracks.append(OnlineTrack(
                title=r.get("song_name", "未知"),
                artist=r.get("artist", "未知艺人"),
                album=r.get("album_name", "未知专辑"),
                duration_sec=float(r.get("duration", 0)),
                platform="wy",
                song_id=str(r.get("song_id", "")),
            ))
        return tracks

    def wy_get_recommend_songs(self) -> list:
        api = self._ensure_wy()
        songs = api.get_recommend_songs()
        tracks = []
        for r in songs:
            tracks.append(OnlineTrack(
                title=r.get("song_name", "未知"),
                artist=r.get("artist", "未知艺人"),
                album=r.get("album_name", "未知专辑"),
                duration_sec=float(r.get("duration", 0)),
                platform="wy",
                song_id=str(r.get("song_id", "")),
            ))
        return tracks

    # ---------- 播放URL获取 ----------
    def get_play_url(self, track: OnlineTrack) -> str:
        """获取播放URL，QQ 320失败回退128，B站音质阶梯自动回退"""
        try:
            if track.platform == "qq":
                return self._qq_get_url(track.song_id)
            elif track.platform == "wy":
                return self._wy_get_url(track.song_id)
            elif track.platform == "bili":
                return self._bili_get_url(track)
        except Exception as e:
            from .logging_utils import log_error
            log_error(f"获取在线歌曲播放URL异常: [{track.platform}] {track.title} - {e}")
        return ""

    def _qq_get_url(self, song_mid: str) -> str:
        client = self._ensure_qq()
        quality = self._qq_quality
        url = self._async.run(client.get_song_url(song_mid, quality=quality))
        if not url and quality != 128:
            url = self._async.run(client.get_song_url(song_mid, quality=128))
        return url or ""

    def _wy_get_url(self, song_id: str) -> str:
        api = self._ensure_wy()
        try:
            info = api.get_song_url(int(song_id), quality=self._wy_quality)
            return info.get("url", "") if info else ""
        except Exception:
            try:
                info = api.get_song_url(int(song_id), quality="standard")
                return info.get("url", "") if info else ""
            except Exception:
                return ""

    def _bili_get_url(self, track: OnlineTrack) -> str:
        self._ensure_bili()
        cred = bili_auth.get_credential(mode="optional")
        urls = self._async.run(
            bili_client.get_audio_urls(track.song_id, credential=cred,
                                       max_quality=self._bi_quality)
        )
        # 备用线路存到 track，供 _load_online_track 在主线路失败时遍历
        track.stream_urls_backup = urls[1:]
        return urls[0] if urls else ""

    # ---------- 歌词获取 ----------
    def get_lyrics(self, track: OnlineTrack) -> Optional[LyricsData]:
        if track.lyric_data is not None:
            return track.lyric_data
        result = None
        if track.platform == "qq":
            result = self._qq_get_lyrics(track.song_id)
        elif track.platform == "wy":
            result = self._wy_get_lyrics(track.song_id)
        elif track.platform == "bili":
            result = self._bili_get_lyrics(track.song_id)
        if result is not None:
            track.lyric_data = result
        return result

    def _qq_get_lyrics(self, song_mid: str) -> Optional[LyricsData]:
        client = self._ensure_qq()
        lyric = self._async.run(client.get_lyric(song_mid))
        if not lyric or not lyric.lines:
            return None
        return lyrics_from_lines(
            [(l.time_ms, l.text) for l in lyric.lines]
        )

    def _wy_get_lyrics(self, song_id: str) -> Optional[LyricsData]:
        api = self._ensure_wy()
        try:
            result = api.get_lyrics(int(song_id))
            lrc_text = result.get("lyric", "")
            if not lrc_text:
                return None
            lines = _parse_lrc_text(lrc_text)
            if not lines:
                return None
            return LyricsData(lines=lines, source_path=None,
                              match_type="online", similarity=1.0)
        except Exception:
            return None

    # ---------- Bilibili 功能 ----------
    def _bili_video_to_track(self, item: dict) -> OnlineTrack:
        """B站视频条目（搜索/热门/收藏/视频信息）→ OnlineTrack"""
        owner = item.get("owner") or {}
        upper = item.get("upper") or {}
        title = _strip_html(str(item.get("title", "") or "未知标题"))
        artist = (item.get("author") or upper.get("name")
                  or owner.get("name") or "未知UP主")
        return OnlineTrack(
            title=title[:80],
            artist=str(artist)[:30],
            album="",
            duration_sec=_parse_bili_duration(item.get("duration", 0)),
            platform="bili",
            song_id=str(item.get("bvid", "")),
            stream_headers=dict(bili_client.STREAM_HEADERS),
        )

    def _bili_search(self, keyword: str, num: int) -> list:
        self._ensure_bili()
        results = self._async.run(bili_client.search_video(keyword, page=1))
        videos = [r for r in results if r.get("bvid")][:num]
        return [self._bili_video_to_track(v) for v in videos]

    def _bili_get_lyrics(self, bvid: str) -> Optional[LyricsData]:
        """B站字幕 → LRC → LyricsData（无字幕/无凭证返回 None）"""
        self._ensure_bili()
        try:
            _, items = self._async.run(bili_client.get_video_subtitle(bvid))
        except Exception as e:
            from .logging_utils import log_warning
            log_warning(f"获取B站字幕失败: {bvid} - {e}")
            return None
        lrc_text = bili_lyrics.subtitle_to_lrc(items)
        if not lrc_text:
            return None
        lines = _parse_lrc_text(lrc_text)
        if not lines:
            return None
        return LyricsData(lines=lines, source_path=None,
                          match_type="online", similarity=1.0)

    def bili_search_users(self, keyword: str, num: int = 20) -> list:
        """搜索UP主，返回 [{"mid","uname","fans","videos","usign"}]"""
        self._ensure_bili()
        results = self._async.run(bili_client.search_user(keyword, page=1))
        users = []
        for u in results[:num]:
            users.append({
                "mid": str(u.get("mid", "")),
                "uname": u.get("uname", "") or "未知用户",
                "fans": u.get("fans", 0) or 0,
                "videos": u.get("videos", 0) or 0,
                "usign": _strip_html(str(u.get("usign", "") or ""))[:30],
            })
        return users

    def bili_get_user_videos(self, uid, count: int = 30) -> list:
        """获取UP主的视频列表"""
        self._ensure_bili()
        videos = self._async.run(
            bili_client.get_user_videos(int(uid), count=count, credential=None)
        )
        return [self._bili_video_to_track(v) for v in videos]

    def bili_get_hot_recommend(self, page: int = 1, per_page: int = 20) -> dict:
        """热门视频（编辑榜单，天级更新）。返回 {"items","page","has_more"}"""
        self._ensure_bili()
        data = self._async.run(bili_client.get_hot_videos(pn=page, ps=per_page))
        vlist = data.get("list") or []
        return {
            "items": [self._bili_video_to_track(v) for v in vlist],
            "page": page,
            "has_more": len(vlist) >= per_page,
        }

    def bili_get_rcmd(self, page: int = 1, per_page: int = 12) -> dict:
        """首页个性化推荐流（每次请求返回不同内容；有账号时按账号个性化）。

        返回 {"items","page","has_more"}，推荐流近乎无限，has_more 恒真。
        """
        self._ensure_bili()
        cred = bili_auth.get_credential(mode="optional")
        items = self._async.run(
            bili_client.get_recommend_videos(page=page, per_page=per_page,
                                             credential=cred)
        )
        return {"items": [self._bili_video_to_track(v) for v in items],
                "page": page, "has_more": True}

    def bili_get_favorite_folders(self) -> list:
        """获取登录用户的收藏夹列表 [{"fid","name","count"}]（需登录）"""
        self._ensure_bili()
        cred = bili_auth.get_credential(mode="read")
        folders = self._async.run(bili_client.get_favorite_list(cred))
        return [{
            "fid": str(f.get("id", "")),
            "name": f.get("title", "") or "未命名收藏夹",
            "count": f.get("media_count", 0) or 0,
        } for f in folders]

    def bili_get_favorite_videos(self, fid: str, page: int = 1) -> dict:
        """获取收藏夹内容（需登录）。返回 {"items": [OnlineTrack], "has_more": bool}"""
        self._ensure_bili()
        cred = bili_auth.get_credential(mode="read")
        data = self._async.run(
            bili_client.get_favorite_videos(int(fid), cred, page=page)
        )
        medias = data.get("medias") or []
        return {
            "items": [self._bili_video_to_track(m) for m in medias],
            "has_more": bool(data.get("has_more")),
        }

    def bili_login_qr_start(self) -> dict:
        """生成B站扫码登录二维码。返回 {"data": PNG字节, "qr_link": 链接}（与QQ结构一致）"""
        self._ensure_bili()
        session = bili_auth.get_qr_login_session()
        result = self._async.run(session.start())
        return {"data": result.get("qr_png", b""),
                "qr_link": result.get("qr_link", "")}

    def bili_login_qr_check(self) -> dict:
        """查询B站扫码状态一次。{"status": waiting|scanned|success|expired|error}"""
        self._ensure_bili()
        session = bili_auth.get_qr_login_session()
        try:
            return self._async.run(session.check())
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def bili_has_credential(self) -> bool:
        """是否有已保存凭证（不联网）"""
        self._ensure_bili()
        return bili_auth.has_credential()

    def import_bili_cookies(self, path: str) -> tuple:
        """从 Netscape cookies.txt 导入B站凭证（备用登录通道）。返回 (成功?, 消息)"""
        self._ensure_bili()
        return bili_auth.import_cookies_txt(path)

    def bili_is_logged_in(self) -> bool:
        """联网校验凭证是否有效"""
        self._ensure_bili()
        try:
            return bili_auth.is_logged_in()
        except Exception:
            return False

    def bili_logout(self) -> bool:
        """清除B站凭证"""
        self._ensure_bili()
        return bili_auth.logout()

    # ---------- 登录 ----------
    def qq_login_qr_start(self) -> dict:
        """获取 QQ音乐手机APP 扫码登录二维码"""
        client = self._ensure_qq()
        qr = self._async.run(client.get_qrcode(login_type=QRLoginType.MOBILE))
        return qr or {}

    def qq_login_qr_check(self) -> dict:
        client = self._ensure_qq()
        result = self._async.run(client.check_qrcode())
        return result or {}

    @staticmethod
    def render_qr_ascii(data: bytes, encoding: str | None = None) -> str:
        """把二维码 PNG 渲染为终端字符（原生模块分辨率，可扫码）。

        encoding 传显示端实际编码（如 rich console 的 .encoding）：
        ▀▄█ 可编码时输出紧凑二维码；否则返回 ""，由调用方提示打开 PNG 文件。
        """
        from . import qr_terminal
        try:
            matrix = qr_terminal.matrix_from_png(data)
        except Exception as e:
            from .logging_utils import log_warning
            log_warning(f"二维码网格解析失败: {e}")
            return ""
        return qr_terminal.render_terminal_qr(matrix, encoding=encoding)

    @staticmethod
    def save_qr_png(data: bytes, path: str) -> str:
        """保存二维码 PNG 到文件，返回路径"""
        try:
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError:
            return ""

    def qq_is_logged_in(self) -> bool:
        client = self._ensure_qq()
        return self._async.run(client.is_logged_in())

    def qq_has_credential(self) -> bool:
        client = self._ensure_qq()
        return client.has_credential()

    def wy_login_qr_start(self) -> dict:
        api = self._ensure_wy()
        try:
            return api.login_qr_start() or {}
        except Exception:
            return {}

    def wy_login_qr_check(self, unikey: str) -> dict:
        api = self._ensure_wy()
        try:
            return api.login_qr_check(unikey) or {}
        except Exception:
            return {"status": "error"}

    def wy_get_auth_status(self) -> dict:
        api = self._ensure_wy()
        try:
            return api.get_auth_status() or {}
        except Exception:
            return {"logged_in": False}
