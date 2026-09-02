"""
NEMbox 编程 API — 为其他 Python 程序提供完整的网易云音乐调用接口。

本模块不依赖 daemon（守护进程），直接在进程内通过 ``Player`` + ``NullUi``
管理播放，通过 ``NetEase`` 访问 API。可在 Windows / Linux / macOS 上使用。

基本用法::

    from NEMbox.music_api import MusicAPI

    api = MusicAPI()

    # 搜索
    results = api.search("九九八十一")
    print(results[0]["song_name"], results[0]["artist"])

    # 播放
    api.play_song(results[0]["song_id"])

    # 暂停 / 恢复
    api.pause()
    api.resume()

    # 查看状态
    status = api.status()
    print(status)

    api.stop()
"""

from __future__ import annotations

import io
import time
from typing import Any

from .api import NetEase
from .player import NullUi, Player
from .storage import Storage

# ── 常量 ──────────────────────────────────────────────────────────────

SEARCH_TYPE_MAP: dict[str, tuple[int, str]] = {
    "song": (1, "songs"),
    "artist": (100, "artists"),
    "album": (10, "albums"),
    "playlist": (1000, "playlists"),
    "dj": (1009, "djRadios"),
}

LOGIN_STATUS_TEXT: dict[int, str] = {
    800: "expired",
    801: "waiting_scan",
    802: "waiting_confirm",
    803: "success",
}

MODE_NAMES: dict[int, str] = {
    Player.MODE_ORDERED: "ordered",
    Player.MODE_ORDERED_LOOP: "ordered-loop",
    Player.MODE_SINGLE_LOOP: "single-loop",
    Player.MODE_RANDOM: "random",
    Player.MODE_RANDOM_LOOP: "random-loop",
}
NAME_TO_MODE: dict[str, int] = {v: k for k, v in MODE_NAMES.items()}


class MusicAPIError(Exception):
    """API 调用异常，包含 error_type / message / hint。"""

    def __init__(self, error_type: str, message: str, hint: str = ""):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        text = self.message
        if self.hint:
            text += f"\n提示: {self.hint}"
        return text

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.error_type, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


class MusicAPI:
    """网易云音乐编程接口。

    封装搜索、播放控制、队列管理、歌词获取、登录认证等全部功能。
    所有方法均在调用线程内同步执行（播放本身在后台线程中进行）。

    Parameters
    ----------
    auto_load : bool, default True
        是否在构造时自动加载本地存储（database.json）。
    """

    def __init__(self, auto_load: bool = True):
        self.netease = NetEase()
        self.storage = Storage()
        if auto_load:
            self.storage.load()
        self.player = Player(ui=NullUi())
        self.player.end_callback = None

    # ── 搜索 ──────────────────────────────────────────────────────────

    def search(
        self,
        keyword: str,
        *,
        stype: str = "song",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """搜索歌曲 / 歌手 / 专辑 / 歌单 / 电台。

        Parameters
        ----------
        keyword : str
            搜索关键词。
        stype : str
            搜索类型，可选 ``"song"`` / ``"artist"`` / ``"album"`` / ``"playlist"`` / ``"dj"``。
        limit : int
            返回结果数量上限。

        Returns
        -------
        list[dict]
            对于 ``stype="song"``，返回标准化歌曲列表，每项包含
            ``song_id`` / ``song_name`` / ``artist`` / ``album_name`` /
            ``mp3_url`` / ``duration`` / ``quality`` 等字段。
        """
        if stype not in SEARCH_TYPE_MAP:
            raise MusicAPIError(
                "invalid_args",
                f"未知搜索类型: {stype}",
                "可选: song | artist | album | playlist | dj",
            )
        api_type, category = SEARCH_TYPE_MAP[stype]
        result = self.netease.search(keyword, api_type, limit=limit) or {}
        items = result.get(category, [])
        if stype == "song":
            items = self.netease.dig_info(items, "songs")
        elif stype == "artist":
            items = self.netease.dig_info(items, "artists")
        elif stype == "album":
            items = self.netease.dig_info(items, "albums")
        elif stype == "playlist":
            items = self.netease.dig_info(items, "playlists")
        return items

    # ── 歌曲信息 ──────────────────────────────────────────────────────

    def get_song_info(self, song_id: int) -> dict[str, Any]:
        """获取歌曲元数据（来自 songs_detail API）。

        返回网易云原始字段（``id`` / ``name`` / ``ar`` / ``al`` / ``dt`` 等）。
        """
        songs = self.netease.songs_detail([song_id])
        if not songs:
            raise MusicAPIError("api_error", f"未找到歌曲 {song_id}")
        return songs[0]

    def get_song_url(
        self,
        song_id: int,
        *,
        quality: str | None = None,
    ) -> dict[str, Any]:
        """获取歌曲播放 / 下载链接。

        Parameters
        ----------
        song_id : int
            歌曲 ID。
        quality : str | None
            音质，可选 ``"standard"`` / ``"higher"`` / ``"exhigh"`` /
            ``"lossless"`` / ``"hires"`` / ``"jymaster"``。
            默认使用配置文件中的音质设置。

        Returns
        -------
        dict
            包含 ``url`` / ``br``（码率）/ ``type`` / ``level`` / ``expi``（过期时间戳）等字段。
        """
        from .config import Config

        config = Config()
        old_quality = config.get("music_quality")
        if quality:
            config.config.setdefault("music_quality", {})["value"] = quality
        try:
            urls = self.netease.songs_url([song_id])
        finally:
            if quality:
                config.config["music_quality"]["value"] = old_quality
        if not urls:
            raise MusicAPIError("api_error", f"无法获取歌曲 {song_id} 的播放链接")
        return urls[0]

    def get_lyrics(self, song_id: int | None = None) -> dict[str, Any]:
        """获取歌词（原文 + 翻译）。

        Parameters
        ----------
        song_id : int | None
            歌曲 ID。若为 ``None`` 则获取当前播放歌曲的歌词。

        Returns
        -------
        dict
            ``{"song_id": int, "lyric": list[str], "tlyric": list[str]}``
        """
        if song_id is None:
            song_id = self.player.playing_id
            if not song_id:
                raise MusicAPIError("invalid_args", "当前没有正在播放的歌曲")
        lyric = self.netease.song_lyric(song_id)
        tlyric = self.netease.song_tlyric(song_id)
        return {
            "song_id": song_id,
            "lyric": lyric,
            "tlyric": tlyric,
        }

    # ── 播放控制 ────────────────────────────────────────────────────────

    def _resolve_songs(self, song_ids: list[int]) -> list[dict[str, Any]]:
        songs = self.netease.dig_info([{"id": sid} for sid in song_ids], "songs")
        if not songs:
            raise MusicAPIError("api_error", "无法获取这些歌曲的信息")
        return songs

    def play_song(self, song_id: int) -> dict[str, Any]:
        """播放单首歌曲（替换当前队列）。

        Parameters
        ----------
        song_id : int
            歌曲 ID。
        """
        songs = self._resolve_songs([song_id])
        p = self.player
        p.new_player_list("songs", f"song-{song_id}", songs, -1)
        p.end_callback = None
        p.stop()
        p.info["idx"] = 0
        p.replay()
        return self.status()

    def play_playlist(self, playlist_id: int) -> dict[str, Any]:
        """播放整个歌单（替换当前队列，从第一首开始）。

        Parameters
        ----------
        playlist_id : int
            歌单 ID。
        """
        track_ids = self.netease.playlist_songlist(playlist_id)
        songs = self.netease.dig_info(track_ids, "songs") or []
        if not songs:
            raise MusicAPIError("api_error", f"歌单 {playlist_id} 为空或不存在")
        p = self.player
        p.new_player_list("songs", f"playlist-{playlist_id}", songs, -1)
        p.end_callback = None
        p.stop()
        p.info["idx"] = 0
        p.replay()
        return self.status()

    def play_songs(self, song_ids: list[int]) -> dict[str, Any]:
        """播放多首歌曲（替换当前队列，从第一首开始）。

        Parameters
        ----------
        song_ids : list[int]
            歌曲 ID 列表。
        """
        songs = self._resolve_songs(song_ids)
        p = self.player
        p.new_player_list("songs", f"songs-{len(songs)}", songs, -1)
        p.end_callback = None
        p.stop()
        p.info["idx"] = 0
        p.replay()
        return self.status()

    def play_artist(self, artist_id: int, *, limit: int = 20) -> dict[str, Any]:
        """播放某歌手的热门歌曲。

        Parameters
        ----------
        artist_id : int
            歌手 ID。
        limit : int
            最多取多少首。
        """
        raw = self.netease.artists(artist_id)
        if not raw:
            raise MusicAPIError("api_error", f"歌手 {artist_id} 没有歌曲")
        song_ids = [s["id"] for s in raw[:limit] if s.get("id")]
        if not song_ids:
            raise MusicAPIError("api_error", f"无法获取歌手 {artist_id} 的歌曲信息")
        return self.play_songs(song_ids)

    def play_album(self, album_id: int) -> dict[str, Any]:
        """播放整张专辑。

        Parameters
        ----------
        album_id : int
            专辑 ID。
        """
        raw = self.netease.album(album_id)
        if not raw:
            raise MusicAPIError("api_error", f"专辑 {album_id} 的歌曲为空")
        song_ids = [s["id"] for s in raw if s.get("id")]
        if not song_ids:
            raise MusicAPIError("api_error", f"无法获取专辑 {album_id} 的歌曲信息")
        return self.play_songs(song_ids)

    def play_index(self, index: int) -> dict[str, Any]:
        """播放队列中指定索引的歌曲。

        Parameters
        ----------
        index : int
            队列索引（从 0 开始）。
        """
        p = self.player
        if index < 0 or index >= len(p.list):
            raise MusicAPIError(
                "invalid_args",
                f"索引 {index} 超出范围 (0-{max(len(p.list) - 1, 0)})",
            )
        p.stop()
        p.info["idx"] = index
        p.replay()
        return self.status()

    def pause(self) -> dict[str, Any]:
        """暂停播放。"""
        p = self.player
        if p.popen_handler and p.popen_handler.poll() is None and p.playing_flag:
            p.switch()
        return self.status()

    def resume(self) -> dict[str, Any]:
        """恢复播放（若已停止则从头播放当前歌曲）。"""
        p = self.player
        if not p.popen_handler or p.popen_handler.poll() is not None:
            if not p.is_empty:
                p.replay()
        elif not p.playing_flag:
            p.switch()
        return self.status()

    def toggle_play_pause(self) -> dict[str, Any]:
        """切换播放 / 暂停状态。"""
        p = self.player
        if not p.popen_handler or p.popen_handler.poll() is not None:
            if not p.is_empty:
                p.replay()
        else:
            p.switch()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """停止播放（不清空队列）。"""
        self.player.stop()
        return self.status()

    def next(self, n: int = 1) -> dict[str, Any]:
        """切换到下一首。

        Parameters
        ----------
        n : int
            前进 n 首。
        """
        p = self.player
        if p.is_empty:
            raise MusicAPIError("invalid_args", "播放队列为空")
        for _ in range(max(int(n), 1)):
            p.next()
        return self.status()

    def prev(self, n: int = 1) -> dict[str, Any]:
        """切换到上一首。

        Parameters
        ----------
        n : int
            后退 n 首。
        """
        p = self.player
        if p.is_empty:
            raise MusicAPIError("invalid_args", "播放队列为空")
        for _ in range(max(int(n), 1)):
            p.prev()
        return self.status()

    def seek(self, seconds: int, *, relative: bool = False) -> dict[str, Any]:
        """跳转到指定位置（仅 mpv 后端支持）。

        Parameters
        ----------
        seconds : int
            目标位置（秒）。absolute 为绝对位置，relative 为相对偏移。
        relative : bool
            ``True`` 表示相对当前位置跳转，``False`` 表示跳到绝对位置。
        """
        p = self.player
        if not p.popen_handler or p.popen_handler.poll() is not None:
            raise MusicAPIError("invalid_args", "当前没有正在播放的歌曲")
        if p.current_backend != "mpv":
            raise MusicAPIError(
                "not_supported",
                "mpg123 后端不支持 seek",
                "将 player_backend 配置为 mpv，或播放无损以自动切换",
            )
        if not p.seek(seconds, relative):
            raise MusicAPIError("api_error", "seek 失败")
        return self.status()

    # ── 音量 ──────────────────────────────────────────────────────────

    def set_volume(self, volume: int) -> dict[str, Any]:
        """设置音量（绝对值，0-100+）。"""
        self.player.set_volume(volume)
        return self.status()

    def adjust_volume(self, delta: int) -> dict[str, Any]:
        """调整音量（相对增量，可正可负）。"""
        self.player.tune_volume(delta)
        return self.status()

    def volume_up(self, step: int = 5) -> dict[str, Any]:
        """音量增加。"""
        self.player.tune_volume(step)
        return self.status()

    def volume_down(self, step: int = 5) -> dict[str, Any]:
        """音量减少。"""
        self.player.tune_volume(-step)
        return self.status()

    # ── 播放模式 ────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> dict[str, Any]:
        """设置播放模式。

        Parameters
        ----------
        mode : str
            ``"ordered"`` / ``"ordered-loop"`` / ``"single-loop"`` /
            ``"random"`` / ``"random-loop"``
        """
        if mode not in NAME_TO_MODE:
            raise MusicAPIError(
                "invalid_args",
                f"未知播放模式: {mode}",
                "ordered | ordered-loop | single-loop | random | random-loop",
            )
        self.player.info["playing_mode"] = NAME_TO_MODE[mode]
        return self.status()

    def cycle_mode(self) -> dict[str, Any]:
        """循环切换到下一个播放模式。"""
        self.player.change_mode()
        return self.status()

    # ── 队列管理 ────────────────────────────────────────────────────────

    def queue_list(self) -> dict[str, Any]:
        """返回当前播放队列。"""
        p = self.player
        items = []
        for idx, sid in enumerate(p.list):
            song = p.songs.get(sid, {})
            items.append(
                {
                    "index": idx,
                    "song_id": song.get("song_id") or sid,
                    "name": song.get("song_name"),
                    "artist": song.get("artist"),
                    "current": idx == p.index,
                }
            )
        return {"items": items, "index": p.index, "size": len(p.list)}

    def queue_add(self, song_ids: list[int]) -> dict[str, Any]:
        """向队列末尾添加歌曲。

        Parameters
        ----------
        song_ids : list[int]
            歌曲 ID 列表。
        """
        songs = self._resolve_songs(song_ids)
        self.player.append_songs(songs)
        self._save()
        return self.queue_list()

    def queue_clear(self) -> dict[str, Any]:
        """清空播放队列。"""
        self.player.stop()
        self.player.new_player_list("", "", [], -1)
        self.player.info["idx"] = 0
        self._save()
        return self.queue_list()

    # ── 状态 ──────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """返回当前播放状态。"""
        p = self.player
        song = p.current_song
        alive = bool(p.popen_handler and p.popen_handler.poll() is None)
        if p.is_empty or not p.is_index_valid:
            state = "stopped"
        elif alive:
            state = "playing" if p.playing_flag else "paused"
        else:
            state = "stopped"
        song_data: dict[str, Any] = {}
        if song:
            song_data = {
                "id": song.get("song_id"),
                "name": song.get("song_name"),
                "artist": song.get("artist"),
                "album": song.get("album_name"),
                "duration": song.get("duration"),
            }
        return {
            "state": state,
            "song": song_data,
            "position": round(float(p.process_location or 0), 1),
            "length": int(p.process_length or 0),
            "volume": p.info["playing_volume"],
            "mode": MODE_NAMES.get(p.mode, "ordered"),
            "backend": p.current_backend,
            "queue_index": p.index,
            "queue_size": len(p.list),
        }

    # ── 歌单 / 排行榜 / 推荐 ─────────────────────────────────────────────

    def get_playlist(self, playlist_id: int) -> list[dict[str, Any]]:
        """获取歌单中的歌曲列表（标准化为歌曲信息）。"""
        track_ids = self.netease.playlist_songlist(playlist_id)
        if not track_ids:
            raise MusicAPIError("api_error", f"歌单 {playlist_id} 为空或不存在")
        return self.netease.dig_info(track_ids, "songs")

    def get_toplists(self) -> list[dict[str, Any]]:
        """获取排行榜列表。

        Returns
        -------
        list[dict]
            每项包含 ``index`` / ``name`` / ``id``。
        """
        charts = self.netease.fetch_toplists()
        return [
            {"index": idx, "name": name, "id": int(chart_id)}
            for idx, (name, chart_id) in enumerate(charts)
        ]

    def get_top_songs(self, index: int) -> list[dict[str, Any]]:
        """获取排行榜中的歌曲。

        Parameters
        ----------
        index : int
            排行榜索引（通过 :meth:`get_toplists` 获取）。
        """
        charts = self.netease.fetch_toplists()
        if index < 0 or index >= len(charts):
            raise MusicAPIError(
                "invalid_args",
                f"索引 {index} 超出范围 (0-{len(charts) - 1})",
            )
        songs = self.netease.top_songlist(index)
        return self.netease.dig_info(songs, "songs")

    def get_recommend_songs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """获取每日推荐歌曲（需要登录）。"""
        if not self._check_login():
            raise MusicAPIError(
                "not_logged_in",
                "未登录或登录已过期",
                "请先调用 login_qr_start() 扫码登录",
            )
        raw = self.netease.recommend_playlist(limit=limit)
        return self.netease.dig_info(raw, "songs")

    def get_recommend_playlists(self) -> list[dict[str, Any]]:
        """获取推荐歌单列表（需要登录）。"""
        if not self._check_login():
            raise MusicAPIError(
                "not_logged_in",
                "未登录或登录已过期",
                "请先调用 login_qr_start() 扫码登录",
            )
        return self.netease.recommend_resource()

    def get_personal_fm(self) -> list[dict[str, Any]]:
        """获取私人 FM 推荐歌曲（需要登录）。"""
        if not self._check_login():
            raise MusicAPIError(
                "not_logged_in",
                "未登录或登录已过期",
                "请先调用 login_qr_start() 扫码登录",
            )
        raw = self.netease.personal_fm()
        return self.netease.dig_info(raw, "fmsongs")

    def get_user_playlists(
        self,
        user_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取用户的歌单列表。

        Parameters
        ----------
        user_id : int
            用户 ID（可通过 :meth:`get_auth_status` 获取自己的 user_id）。
        limit : int
            返回数量上限。

        Returns
        -------
        list[dict]
            网易云原始歌单信息列表，每项包含 ``id`` / ``name`` /
            ``creator`` / ``trackCount`` / ``coverImgUrl`` 等字段。
        """
        return self.netease.user_playlist(user_id, limit=limit)

    def get_comments(self, song_id: int, *, limit: int = 20) -> dict[str, Any]:
        """获取歌曲评论。

        Returns
        -------
        dict
            ``{"comments": list, "total": int}``
        """
        result = self.netease.song_comments(song_id, limit=limit)
        return {
            "comments": result.get("comments", []),
            "total": result.get("total"),
        }

    def like_song(self, song_id: int) -> bool:
        """红心（喜欢）一首歌曲（需要登录）。"""
        if not self._check_login():
            raise MusicAPIError(
                "not_logged_in",
                "未登录或登录已过期",
                "请先调用 login_qr_start() 扫码登录",
            )
        ok = self.netease.song_like(song_id)
        if not ok:
            raise MusicAPIError("api_error", f"红心歌曲 {song_id} 失败")
        return True

    # ── 登录认证 ────────────────────────────────────────────────────────

    def _check_login(self) -> bool:
        info = self.netease.get_account_info()
        return bool(info.get("account") or info.get("profile"))

    def get_auth_status(self) -> dict[str, Any]:
        """获取当前登录状态。"""
        raw_user = self.storage.database.get("user", {})
        user = raw_user if isinstance(raw_user, dict) else {}
        info = self.netease.get_account_info()
        account = info.get("account") or {}
        profile = info.get("profile") or {}
        logged_in = bool(account or profile)
        return {
            "logged_in": logged_in,
            "user_id": account.get("id") or user.get("user_id"),
            "nickname": profile.get("nickname") or user.get("nickname"),
        }

    def login_qr_start(self) -> dict[str, Any]:
        """发起二维码登录流程（第一步）。

        Returns
        -------
        dict
            ``{"unikey": str, "qr_url": str, "qr_ascii": str}``
            其中 ``qr_ascii`` 是终端可显示的 ASCII 二维码，
            ``qr_url`` 是二维码内容 URL。
        """
        unikey = self.netease.login_qr_key()
        if not unikey:
            raise MusicAPIError("api_error", "获取登录二维码失败")
        qr_url = self.netease.login_qr_url(unikey)
        qr_ascii = self._render_qr_ascii(qr_url)
        return {
            "unikey": unikey,
            "qr_url": qr_url,
            "qr_ascii": qr_ascii,
        }

    def login_qr_check(self, unikey: str) -> dict[str, Any]:
        """检查扫码状态（第二步，需轮询调用）。

        Parameters
        ----------
        unikey : str
            :meth:`login_qr_start` 返回的 unikey。

        Returns
        -------
        dict
            ``{"status": str, "code": int, ...}``
            status 可选 ``"waiting_scan"`` / ``"waiting_confirm"`` /
            ``"success"`` / ``"expired"``。
            登录成功时额外返回 ``user_id`` / ``nickname``。
        """
        resp = self.netease.login_qr_check(unikey)
        code = resp.get("code")
        status = LOGIN_STATUS_TEXT.get(code, "unknown")
        if code == 803:
            user = self._on_login_success()
            return {
                "status": status,
                "code": code,
                **user,
            }
        return {
            "status": status,
            "code": code,
            "message": resp.get("message", ""),
        }

    def login_qr_wait(self, unikey: str, *, timeout: int = 300) -> dict[str, Any]:
        """阻塞等待扫码登录完成（自动轮询）。

        Parameters
        ----------
        unikey : str
            :meth:`login_qr_start` 返回的 unikey。
        timeout : int
            最长等待秒数（默认 300 秒）。

        Returns
        -------
        dict
            同 :meth:`login_qr_check`。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.login_qr_check(unikey)
            code = resp.get("code")
            if code == 803:
                return resp
            if code == 800:
                raise MusicAPIError("api_error", "二维码已过期，请重新获取")
            time.sleep(2)
        raise MusicAPIError("api_error", f"等待扫码超时（{timeout}秒）")

    def logout(self) -> bool:
        """退出登录，清除 cookie 与账号缓存。"""
        self.netease.logout()
        return True

    def _on_login_success(self) -> dict[str, Any]:
        info = self.netease.get_account_info()
        account = info.get("account") or {}
        profile = info.get("profile") or {}
        userid = account.get("id")
        nickname = profile.get("nickname") or ""
        self.storage.login(nickname, "", userid, nickname)
        self.storage.save()
        return {
            "user_id": userid,
            "nickname": nickname,
        }

    # ── 内部工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _render_qr_ascii(url: str) -> str:
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            buf = io.StringIO()
            qr.print_ascii(out=buf, invert=True)
            return buf.getvalue().rstrip()
        except ImportError:
            return f"(需要 qrcode 库来显示 ASCII 二维码)\n二维码 URL: {url}"

    def _save(self) -> None:
        self.storage.save()

    # ── 资源清理 ────────────────────────────────────────────────────────

    def close(self) -> None:
        """停止播放并保存状态。调用后不应再使用此实例。"""
        self.player.stop()
        self.storage.save()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
