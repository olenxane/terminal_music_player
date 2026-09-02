"""在线音乐管理器：封装QQ音乐（异步）和网易云音乐（同步）API"""
from __future__ import annotations
import asyncio
import os
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
QQMusicClient = None
qq_configure_paths = None
QRLoginType = None
MusicAPI = None
MusicAPIError = None


def _ensure_qq_import():
    global _qq_imported, QQMusicClient, qq_configure_paths, QRLoginType
    if not _qq_imported:
        from qqmusicbox import QQMusicClient as _C, configure_paths as _cp
        from qqmusic_api.models.login import QRLoginType as _QT
        QQMusicClient = _C
        qq_configure_paths = _cp
        QRLoginType = _QT
        _qq_imported = True


def _ensure_wy_import():
    global _wy_imported, MusicAPI, MusicAPIError
    if not _wy_imported:
        from NEMbox import MusicAPI as _M, MusicAPIError as _E
        MusicAPI = _M
        MusicAPIError = _E
        _wy_imported = True


@dataclass
class OnlineTrack:
    """在线歌曲统一表示"""
    title: str
    artist: str
    album: str
    duration_sec: float
    platform: str            # "qq" / "wy"
    song_id: str             # QQ的mid / 网易的song_id
    is_vip: bool = False
    lyric_data: Optional[LyricsData] = None

    @property
    def display_name(self) -> str:
        return f"{self.title} - {self.artist}"


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
                 wy_quality: str = "exhigh"):
        self._config_dir = config_dir
        self._qq_quality = qq_quality
        self._wy_quality = wy_quality
        self._async = AsyncRunner()
        self._qq_client = None
        self._wy_api = None

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

    def shutdown(self):
        self._async.shutdown()

    # ---------- 搜索 ----------
    def search(self, platform: str, keyword: str, num: int = 20) -> list:
        if platform == "qq":
            return self._qq_search(keyword, num)
        elif platform == "wy":
            return self._wy_search(keyword, num)
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
        """获取播放URL，QQ 320失败回退128"""
        try:
            if track.platform == "qq":
                return self._qq_get_url(track.song_id)
            elif track.platform == "wy":
                return self._wy_get_url(track.song_id)
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

    # ---------- 歌词获取 ----------
    def get_lyrics(self, track: OnlineTrack) -> Optional[LyricsData]:
        if track.lyric_data is not None:
            return track.lyric_data
        result = None
        if track.platform == "qq":
            result = self._qq_get_lyrics(track.song_id)
        elif track.platform == "wy":
            result = self._wy_get_lyrics(track.song_id)
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
    def render_qr_ascii(data: bytes, width: int = 44) -> str:
        """把二维码 PNG 二进制渲染成终端 ASCII 字符画（可扫码）"""
        from PIL import Image
        import io
        try:
            img = Image.open(io.BytesIO(data)).convert("L")
        except Exception:
            return ""
        img = img.point(lambda p: 255 if p > 128 else 0)
        w, h = img.size
        if w == 0 or h == 0:
            return ""
        cell_w = max(1, w // width)
        cell_h = max(1, cell_w * 2)
        rows = []
        for y in range(0, h, cell_h):
            line_chars = []
            for x in range(0, w, cell_w):
                block = img.crop((x, y, min(x + cell_w, w), min(y + cell_h, h)))
                px = list(block.getdata())
                black = sum(1 for p in px if p < 128)
                line_chars.append("█" if black * 2 >= len(px) else " ")
            rows.append("".join(line_chars).rstrip())
        return "\n".join(rows)

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
