"""主程序：命令行入口，事件循环整合播放/UI/输入"""
from __future__ import annotations
import argparse
import os
import sys
import time

from rich.console import Console
from rich.live import Live

from .config import load_config, reload_config, ConfigError
from .player import Player, PlayState
from .playlist import Playlist
from .spectrum import SpectrumAnalyzer
from .lyrics import load_lyrics
from .metadata import read_metadata
from . import ui
from . import online_ui
from .keyinput import KeyReader
from .setup_wizard import ensure_music_dirs
from .stats import StatsTracker
from .online_music import OnlineMusicManager, OnlineTrack
from .logging_utils import log_error, log_warning, log_exception

MODE_CYCLE = ["sequential", "shuffle", "repeat_one", "repeat_all"]


def _enable_windows_vt() -> bool:
    """在 Windows 控制台输出句柄上开启 VT 处理。

    rich 检测不到 VT 时会回退到旧式 Win32 渲染：频谱每帧数百个颜色段
    逐段调用 SetConsoleTextAttribute/WriteConsoleW，控制台逐段重绘导致闪烁。
    开启 VT 后 rich 走 ANSI 路径，整帧一次写入。
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL

    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))


class App:
    def __init__(self, cfg_path: str, music_path: str | None, force_setup: bool = False):
        _enable_windows_vt()
        self.console = Console()
        try:
            self.cfg = load_config(cfg_path)
        except ConfigError as e:
            log_error(f"配置加载错误: {e}")
            sys.exit(1)

        self.playlist = Playlist(mode=self.cfg.playback.playlist_mode)

        if music_path:
            target_dirs = [music_path]
        else:
            target_dirs = ensure_music_dirs(self.console, self.cfg, force_setup=force_setup)

        self.playlist.load_dirs(target_dirs)
        if not self.playlist.tracks:
            dirs_desc = "、".join(target_dirs) if target_dirs else "(未设置任何目录)"
            log_warning(f"在 [{dirs_desc}] 中未找到支持的音乐文件（支持 mp3/wav/flac/ogg/m4a/aiff）。")

        self.spectrum = SpectrumAnalyzer(
            fs=44100,
            bars=self.cfg.spectrum.bars,
            fft_size=self.cfg.spectrum.fft_size,
            smoothing=self.cfg.spectrum.smoothing,
            min_db=self.cfg.spectrum.min_db,
            max_db=self.cfg.spectrum.max_db,
        )

        self.player = Player(
            spectrum=self.spectrum,
            volume=self.cfg.playback.default_volume,
            on_track_end=self._on_track_end,
            dsp_config=self.cfg.dsp,
        )

        self.current_track = None
        self.lyrics_data = None
        self._theme_keys = self.cfg.theme_names()
        self._running = True
        self._pending_dir_setup = False
        self._spectrum_on = self.cfg.spectrum.enabled
        self._selector_active = False
        self._search_str = ""
        self._selector_index = 0
        self._filtered_indices = list(range(len(self.playlist.tracks))) if self.playlist.tracks else []
        self._selector_search_pending = False
        self._selector_search_reset = False
        self._selector_last_input = 0.0
        stats_path = os.path.join(os.path.dirname(self.cfg.path), "stats.json")
        self.stats = StatsTracker(stats_path)
        self._stats_flush_timer = 0.0
        # 在线音乐
        online_cfg_dir = os.path.join(os.path.dirname(self.cfg.path), "online_data")
        self._online_data_dir = online_cfg_dir
        self.online_mgr = OnlineMusicManager(
            online_cfg_dir,
            qq_quality=self.cfg.online_music.qq_quality,
            wy_quality=self.cfg.online_music.wy_quality,
        )
        self._online_active = False
        self._online_view = online_ui.VIEW_PLATFORM
        self._online_view_stack = []
        self._online_platform = "qq"
        self._online_search_str = ""
        self._online_selector_index = 0
        self._online_tracks = []
        self._online_sub_items = []
        self._online_loading = False
        self._online_current = None
        self._temp_file_path = None
        self._online_login_data = None
        self._online_login_checking = False
        self._online_login_result = None
        self._online_search_pending = ""
        self._online_search_last_input = 0.0
        self._online_search_running = False
        self._online_search_result = None
        self._online_search_gen = 0

        if self.playlist.tracks:
            self._load_current_track()

    # ---------------- 加载/切歌 ----------------
    def _record_stats_if_played(self):
        """当前歌曲播放超过30秒 → 记录歌曲名 + 累加歌曲总时长"""
        if self.current_track is None or self.player.position_sec < 30:
            return
        self.stats.record_song(self.current_track.title)
        if self.current_track.duration_sec > 0:
            self.stats.add_seconds(self.current_track.duration_sec)

    def _load_current_track(self):
        # 切歌前：上一首播放超过30秒才计入统计（歌曲名 + 总时长）
        self._record_stats_if_played()
        self._cleanup_temp_file()
        # 在线歌曲
        if self._online_current is not None:
            self._load_online_track(self._online_current)
            return
        path = self.playlist.current
        if not path:
            return
        self.current_track = read_metadata(path)
        self.lyrics_data = load_lyrics(
            path,
            lyrics_dir=self.cfg.lyrics.lyrics_dir,
            fuzzy_match=self.cfg.lyrics.fuzzy_match,
            fuzzy_threshold=self.cfg.lyrics.fuzzy_threshold,
        )
        self.player.load(path, eq_bands=self.cfg.equalizer.bands_db,
                         q_values=self.cfg.equalizer.q_values)

    def _load_online_track(self, track: OnlineTrack):
        """加载在线歌曲：获取URL → ffmpeg流式播放或下载临时文件"""
        url = self.online_mgr.get_play_url(track)
        if not url:
            log_error(f"在线歌曲加载失败（获取URL失败）: {track.title}")
            self.player.stop()
            return
        # 构造 TrackInfo（元数据来自平台）
        from .metadata import TrackInfo
        self.current_track = TrackInfo(
            path=url, title=track.title, artist=track.artist,
            album=track.album, duration_sec=track.duration_sec,
            filetype="mp3",
        )
        self.lyrics_data = self.online_mgr.get_lyrics(track)
        try:
            self.player.load(url, eq_bands=self.cfg.equalizer.bands_db,
                             q_values=self.cfg.equalizer.q_values)
        except RuntimeError:
            temp = self._download_to_temp(url)
            if temp:
                self.player.load(temp, eq_bands=self.cfg.equalizer.bands_db,
                                 q_values=self.cfg.equalizer.q_values)
            else:
                log_error(f"在线歌曲加载失败（流式播放+下载回退均失败）: {track.title}")
                self.player.stop()

    def _download_to_temp(self, url: str) -> str:
        """下载URL到临时文件"""
        import tempfile
        import requests
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
            ext = ".mp3"
            ct = resp.headers.get("content-type", "")
            if "flac" in ct:
                ext = ".flac"
            elif "m4a" in ct or "mp4" in ct:
                ext = ".m4a"
            f = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            for chunk in resp.iter_content(8192):
                f.write(chunk)
            f.close()
            self._temp_file_path = f.name
            return f.name
        except Exception:
            return ""

    def _cleanup_temp_file(self):
        if self._temp_file_path and os.path.exists(self._temp_file_path):
            try:
                os.unlink(self._temp_file_path)
            except OSError:
                pass
        self._temp_file_path = None

    def _on_track_end(self):
        # 播放队列优先：队列中有歌曲时按 FIFO 顺序播放
        if self.playlist.has_queue():
            item = self.playlist.next_from_queue()
            if isinstance(item, OnlineTrack):
                # 在线歌曲预检 URL，不可用则跳过（不推入缓冲栈）
                while item is not None and isinstance(item, OnlineTrack):
                    if self.online_mgr.get_play_url(item):
                        break
                    log_error(f"在线歌曲无法获取播放URL（跳过）: {item.title}")
                    item = self.playlist.pop_queue_skip()
                if item is None:
                    self._online_current = None
                elif isinstance(item, OnlineTrack):
                    self._online_current = item
                else:
                    self._online_current = None
            else:
                self._online_current = None
            self._load_current_track()
            self.player.play()
            return

        # 队列已空：清空缓冲栈，回到本地模式
        self.playlist.clear_buffer()

        # 在线歌曲播放完毕（非队列来源）→ 清除并回到本地
        if self._online_current is not None:
            self._online_current = None
            if self.playlist.mode == "repeat_one":
                self._load_current_track()
                self.player.play()
                return
            if not self.playlist.tracks:
                return
            if self.playlist.mode == "sequential" and self.playlist.index == len(self.playlist.tracks) - 1:
                return
            self.playlist.next()
            self._load_current_track()
            self.player.play()
            return
        # 本地歌曲原有逻辑
        if self.playlist.mode == "repeat_one":
            self._load_current_track()
            self.player.play()
            return
        if not self.playlist.tracks:
            return
        if self.playlist.mode == "sequential" and self.playlist.index == len(self.playlist.tracks) - 1:
            return
        self.playlist.next()
        self._load_current_track()
        self.player.play()

    # ---------------- 播放控制 ----------------
    def play_pause(self):
        if self.player.state == PlayState.STOPPED:
            self.player.play()
        else:
            self.player.toggle_pause()

    def next_track(self):
        if not self.playlist.tracks:
            return
        was_playing = self.player.state == PlayState.PLAYING
        if self.playlist.has_queue():
            item = self.playlist.next_from_queue()
            if isinstance(item, OnlineTrack):
                self._online_current = item
            else:
                self._online_current = None
        else:
            self._online_current = None
            self.playlist.next()
        self._load_current_track()
        if was_playing:
            self.player.play()

    def prev_track(self):
        if not self.playlist.tracks:
            return
        was_playing = self.player.state == PlayState.PLAYING
        # 缓冲栈非空时：从栈取回上一首
        if self.playlist.has_buffer():
            item = self.playlist.prev_from_buffer()
            if isinstance(item, OnlineTrack):
                self._online_current = item
            else:
                self._online_current = None
            self._load_current_track()
            if was_playing:
                self.player.play()
            return
        # 缓冲栈空：走本地列表回退
        self._online_current = None
        self.playlist.prev()
        self._load_current_track()
        if was_playing:
            self.player.play()

    def cycle_theme(self):
        if not self._theme_keys:
            return
        cur = self.cfg.theme.key
        idx = self._theme_keys.index(cur) if cur in self._theme_keys else -1
        idx = (idx + 1) % len(self._theme_keys)
        self.cfg.theme = self.cfg.all_themes[self._theme_keys[idx]]

    def cycle_mode(self):
        cur = self.playlist.mode
        idx = MODE_CYCLE.index(cur) if cur in MODE_CYCLE else 0
        new_mode = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        self.playlist.set_mode(new_mode)

    def toggle_spectrum(self):
        self._spectrum_on = not self._spectrum_on

    def reload_cfg(self):
        try:
            new_cfg = reload_config(self.cfg)
        except ConfigError:
            return
        keep_theme = self.cfg.theme.key
        self.cfg = new_cfg
        if keep_theme in self.cfg.all_themes:
            self.cfg.theme = self.cfg.all_themes[keep_theme]
        self._theme_keys = self.cfg.theme_names()
        self.spectrum.smoothing = self.cfg.spectrum.smoothing
        self.spectrum.min_db = self.cfg.spectrum.min_db
        self.spectrum.max_db = self.cfg.spectrum.max_db
        if self.player.equalizer:
            self.player.set_eq_bands(self.cfg.equalizer.bands_db,
                                     q_values=self.cfg.equalizer.q_values)
        # 同步 DSP 开关
        self.player.loudness_enabled = self.cfg.dsp.loudness.enabled
        self.player.vbe_enabled = self.cfg.dsp.vbe.enabled
        self.player.limiter_enabled = self.cfg.dsp.limiter.enabled
        self.player.loudness_target_lufs = self.cfg.dsp.loudness.target_lufs
        self.player._init_dsp_modules()
        # 同步在线音质
        self.online_mgr._qq_quality = self.cfg.online_music.qq_quality
        self.online_mgr._wy_quality = self.cfg.online_music.wy_quality

    def rerun_dir_setup(self):
        self._pending_dir_setup = True

    def _do_dir_setup(self):
        was_playing = self.player.state == PlayState.PLAYING
        self.player.pause()
        dirs = ensure_music_dirs(self.console, self.cfg, force_setup=True)
        self.playlist.load_dirs(dirs)
        self._filtered_indices = list(range(len(self.playlist.tracks)))
        if self.playlist.tracks:
            self._load_current_track()
            if was_playing:
                self.player.play()
        else:
            self.player.stop()
            self.current_track = None
            self.lyrics_data = None

    # ---------------- 歌曲选择器 ----------------
    def _toggle_selector(self):
        if not self.playlist.tracks:
            return
        self._selector_active = not self._selector_active
        if self._selector_active:
            self._search_str = ""
            self._selector_index = 0
            self._filtered_indices = list(range(len(self.playlist.tracks)))
            self._selector_search_pending = False

    def _update_filtered_indices(self):
        """按搜索串过滤歌曲：关键词按空格拆分，每个需作为子串出现（顺序无关）"""
        keywords = self._search_str.lower().split()
        self._filtered_indices = [
            i for i in range(len(self.playlist.tracks))
            if all(kw in os.path.basename(self.playlist.tracks[i]).lower()
                   for kw in keywords)
        ]
        if self._filtered_indices:
            self._selector_index = min(self._selector_index,
                                       len(self._filtered_indices) - 1)
        else:
            self._selector_index = 0

    def _handle_selector_key(self, key: str):
        if key == "\t" or key == "ESC":
            self._selector_active = False
            self._selector_search_pending = False
        elif key == "UP":
            # 若有待刷新过滤，先同步应用（本地纯内存操作，很快）
            if self._selector_search_pending:
                self._selector_search_pending = False
                self._update_filtered_indices()
            if self._filtered_indices:
                self._selector_index = max(0, self._selector_index - 1)
        elif key == "DOWN":
            if self._selector_search_pending:
                self._selector_search_pending = False
                self._update_filtered_indices()
            if self._filtered_indices:
                self._selector_index = min(len(self._filtered_indices) - 1,
                                           self._selector_index + 1)
        elif key == "RIGHT":
            if self._selector_search_pending:
                self._selector_search_pending = False
                self._update_filtered_indices()
            if self._filtered_indices and self._selector_index < len(self._filtered_indices):
                orig_idx = self._filtered_indices[self._selector_index]
                self.playlist.add_to_queue(orig_idx)
                if self._selector_index < len(self._filtered_indices) - 1:
                    self._selector_index += 1
        elif key == "\r" or key == "\n":
            if self._selector_search_pending:
                self._selector_search_pending = False
                self._update_filtered_indices()
            if self._filtered_indices and self._selector_index < len(self._filtered_indices):
                orig_idx = self._filtered_indices[self._selector_index]
                self.playlist.jump_to(orig_idx)
                self._load_current_track()
                self.player.play()
                self._selector_active = False
        elif key == "\b" or key == "\x7f":
            self._search_str = self._search_str[:-1]
            self._selector_search_pending = True
            self._selector_search_reset = False
            self._selector_last_input = time.time()
        elif key and len(key) == 1 and key.isprintable():
            self._search_str += key
            self._selector_index = 0
            self._selector_search_pending = True
            self._selector_search_reset = True
            self._selector_last_input = time.time()

    def _tick_selector_search(self):
        """本地选择器搜索防抖：停顿后刷新过滤结果"""
        if not self._selector_search_pending:
            return
        if time.time() - self._selector_last_input < 0.15:
            return
        self._selector_search_pending = False
        if self._selector_search_reset:
            self._selector_index = 0
        self._update_filtered_indices()

    # ---------------- 在线音乐 ----------------
    def _enter_online_mode(self):
        self._online_active = True
        self._online_view = online_ui.VIEW_PLATFORM
        self._online_view_stack = []
        self._online_selector_index = 0

    def _exit_online_mode(self):
        self._online_active = False
        self._online_view = online_ui.VIEW_PLATFORM
        self._online_view_stack = []
        self._online_tracks = []
        self._online_sub_items = []
        self._online_search_pending = ""
        self._online_search_running = False
        self._online_search_result = None

    def _online_push_view(self, view):
        self._online_view_stack.append(self._online_view)
        self._online_view = view
        self._online_selector_index = 0

    def _online_pop_view(self):
        if self._online_view_stack:
            self._online_view = self._online_view_stack.pop()
            self._online_selector_index = 0
        else:
            self._exit_online_mode()

    def _play_online_track(self, track: OnlineTrack):
        self._online_current = track
        self._load_current_track()
        self.player.play()

    def _add_online_to_queue(self, track: OnlineTrack):
        self.playlist.add_online_to_queue(track)
        if self._online_selector_index < len(self._online_tracks) - 1:
            self._online_selector_index += 1

    def _online_toggle_like(self, track: OnlineTrack):
        """按 ← 收藏/取消收藏当前歌曲（仅 QQ 平台）"""
        if track.platform != "qq":
            return
        try:
            if not self.online_mgr.qq_has_credential():
                return
            if track.liked:
                ok = self.online_mgr.qq_unlike_song(track.song_id)
            else:
                ok = self.online_mgr.qq_like_song(track.song_id)
            if ok:
                track.liked = not track.liked
        except Exception:
            pass

    def _handle_online_key(self, key: str):
        if key == "ESC" or key == "o":
            if self._online_view == online_ui.VIEW_PLATFORM:
                self._exit_online_mode()
            else:
                self._online_pop_view()
            return
        if self._online_loading:
            return
        if self._online_view == online_ui.VIEW_LOGIN:
            return

        v = self._online_view
        if v == online_ui.VIEW_PLATFORM:
            if key == "UP":
                self._online_selector_index = max(0, self._online_selector_index - 1)
            elif key == "DOWN":
                self._online_selector_index = min(1, self._online_selector_index + 1)
            elif key == "\r" or key == "\n":
                self._online_platform = "qq" if self._online_selector_index == 0 else "wy"
                menu = online_ui.QQ_MENU if self._online_platform == "qq" else online_ui.WY_MENU
                self._online_push_view(
                    online_ui.VIEW_QQ_HOME if self._online_platform == "qq" else online_ui.VIEW_WY_HOME)

        elif v in (online_ui.VIEW_QQ_HOME, online_ui.VIEW_WY_HOME):
            menu = online_ui.QQ_MENU if v == online_ui.VIEW_QQ_HOME else online_ui.WY_MENU
            max_idx = len(menu) - 1
            if key == "UP":
                self._online_selector_index = max(0, self._online_selector_index - 1)
            elif key == "DOWN":
                self._online_selector_index = min(max_idx, self._online_selector_index + 1)
            elif key == "\r" or key == "\n":
                idx = self._online_selector_index
                if v == online_ui.VIEW_QQ_HOME:
                    self._enter_qq_menu(idx)
                else:
                    self._enter_wy_menu(idx)

        elif v in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH):
            self._handle_online_search_key(key, v)

        elif v in (online_ui.VIEW_SONG_LIST, online_ui.VIEW_QQ_FAV_SONGS,
                    online_ui.VIEW_QQ_DAILY):
            self._handle_online_song_list_key(key)

        elif v in (online_ui.VIEW_QQ_PLAYLISTS,
                    online_ui.VIEW_QQ_RANKINGS, online_ui.VIEW_WY_RANKINGS):
            self._handle_online_sub_list_key(key, v)

    def _enter_qq_menu(self, idx):
        if idx == 0:  # 搜索
            self._online_search_str = ""
            self._online_tracks = []
            self._online_search_pending = ""
            self._online_search_running = False
            self._online_search_result = None
            self._online_search_gen = 0
            self._online_push_view(online_ui.VIEW_QQ_SEARCH)
        elif idx == 1:  # 每日推荐
            self._online_push_view(online_ui.VIEW_QQ_DAILY)
            self._online_loading = True
            try:
                self._online_tracks = self.online_mgr.qq_get_daily_mix()
            except Exception:
                self._online_tracks = []
            self._online_loading = False
        elif idx == 2:  # 收藏歌曲
            self._online_push_view(online_ui.VIEW_QQ_FAV_SONGS)
            self._online_loading = True
            try:
                self._online_tracks = self.online_mgr.qq_get_fav_songs()
                for t in self._online_tracks:
                    t.liked = True
            except Exception:
                self._online_tracks = []
            self._online_loading = False
        elif idx == 3:  # 我的歌单
            self._online_push_view(online_ui.VIEW_QQ_PLAYLISTS)
            self._online_loading = True
            try:
                self._online_sub_items = (self.online_mgr.qq_get_fav_songlists()
                                          + self.online_mgr.qq_get_created_songlists())
            except Exception:
                self._online_sub_items = []
            self._online_loading = False
        elif idx == 4:  # 排行榜
            self._online_push_view(online_ui.VIEW_QQ_RANKINGS)
            self._online_loading = True
            try:
                tops = self.online_mgr.qq_get_top_list()
                self._online_sub_items = [{"name": t.get("title", ""),
                                           "dissid": str(t.get("id", ""))}
                                          for t in tops] if tops else []
            except Exception:
                self._online_sub_items = []
            self._online_loading = False
        elif idx == 5:  # 登录
            self._online_push_view(online_ui.VIEW_LOGIN)
            self._online_login_data = None
            self._online_login_result = None
            self._online_login_checking = False
            try:
                qr = self.online_mgr.qq_login_qr_start()
                self._online_login_data = qr
                if qr and qr.get("data"):
                    try:
                        os.makedirs(self._online_data_dir, exist_ok=True)
                    except OSError:
                        pass
                    self.online_mgr.save_qr_png(
                        qr["data"],
                        os.path.join(self._online_data_dir, "qq_login_qr.png"))
                    self._online_login_checking = True
                    import threading
                    threading.Thread(target=self._qq_login_check_worker,
                                     daemon=True).start()
            except Exception:
                pass

    def _enter_wy_menu(self, idx):
        if idx == 0:  # 搜索
            self._online_search_str = ""
            self._online_tracks = []
            self._online_search_pending = ""
            self._online_search_running = False
            self._online_search_result = None
            self._online_search_gen = 0
            self._online_push_view(online_ui.VIEW_WY_SEARCH)
        elif idx == 1:  # 排行榜
            self._online_push_view(online_ui.VIEW_WY_RANKINGS)
            self._online_loading = True
            try:
                charts = self.online_mgr.wy_get_toplists()
                self._online_sub_items = [{"name": c.get("name", ""),
                                           "dissid": str(c.get("id", ""))}
                                          for c in charts] if charts else []
            except Exception:
                self._online_sub_items = []
            self._online_loading = False
        elif idx == 2:  # 每日推荐
            self._online_push_view(online_ui.VIEW_SONG_LIST)
            self._online_loading = True
            try:
                self._online_tracks = self.online_mgr.wy_get_recommend_songs()
            except Exception:
                self._online_tracks = []
            self._online_loading = False
        elif idx == 3:  # 登录
            self._online_push_view(online_ui.VIEW_LOGIN)
            self._online_login_data = None
            self._online_login_result = None
            self._online_login_checking = False
            try:
                qr = self.online_mgr.wy_login_qr_start()
                self._online_login_data = qr
                unikey = (qr or {}).get("unikey", "")
                if unikey:
                    self._online_login_checking = True
                    import threading
                    threading.Thread(target=self._wy_login_check_worker,
                                     args=(unikey,), daemon=True).start()
            except Exception:
                pass

    def _qq_login_check_worker(self):
        """QQ音乐 MOBILE 走 MQTT 阻塞式轮询（最多120s），放后台线程"""
        try:
            result = self.online_mgr.qq_login_qr_check()
            self._online_login_result = result or {}
        except Exception:
            self._online_login_result = {"status": -1}
        self._online_login_checking = False

    def _wy_login_check_worker(self, unikey: str):
        """网易云 login_qr_check 单次非阻塞，循环轮询直到结果"""
        import time
        try:
            for _ in range(150):
                result = self.online_mgr.wy_login_qr_check(unikey)
                status = result.get("status")
                if status == "success":
                    self._online_login_result = {"status": 2}
                    break
                if status in ("expired", "error"):
                    self._online_login_result = {"status": -1}
                    break
                time.sleep(2)
        except Exception:
            self._online_login_result = {"status": -1}
        self._online_login_checking = False

    def _handle_online_search_key(self, key, view):
        if key == "UP":
            if self._online_tracks:
                self._online_selector_index = max(0, self._online_selector_index - 1)
        elif key == "DOWN":
            if self._online_tracks:
                self._online_selector_index = min(len(self._online_tracks) - 1,
                                                  self._online_selector_index + 1)
        elif key == "RIGHT" and self._online_tracks and self._online_selector_index < len(self._online_tracks):
            self._add_online_to_queue(self._online_tracks[self._online_selector_index])
        elif key == "LEFT" and self._online_tracks and self._online_selector_index < len(self._online_tracks):
            self._online_toggle_like(self._online_tracks[self._online_selector_index])
        elif key == "\r" or key == "\n":
            if self._online_tracks and self._online_selector_index < len(self._online_tracks):
                self._play_online_track(self._online_tracks[self._online_selector_index])
                self._exit_online_mode()
        elif key == "\b" or key == "\x7f":
            self._online_search_str = self._online_search_str[:-1]
            self._online_selector_index = 0
            self._schedule_online_search()
        elif key and len(key) == 1 and key.isprintable():
            self._online_search_str += key
            self._online_selector_index = 0
            self._schedule_online_search()

    def _schedule_online_search(self):
        """登记待搜索关键词，由主循环防抖后后台执行"""
        self._online_search_pending = self._online_search_str
        self._online_search_last_input = time.time()
        self._online_search_gen += 1

    def _do_online_search_async(self, keyword: str, gen: int, platform: str):
        """后台线程执行网络搜索"""
        try:
            tracks = self.online_mgr.search(platform, keyword)
        except Exception:
            tracks = []
        self._online_search_result = (tracks, gen)
        self._online_search_running = False

    def _tick_online_search(self):
        """主循环每帧调用：防抖启动后台搜索 + 应用结果（代际竞态取消）"""
        # 仅当位于搜索视图时才处理
        if self._online_view not in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH):
            self._online_search_pending = ""
            self._online_search_result = None
            return
        # 1. 防抖：停顿 400ms 且无搜索在跑 → 启动后台搜索
        if (self._online_search_pending and not self._online_search_running
                and time.time() - self._online_search_last_input >= 0.4):
            kw = self._online_search_pending
            platform = "qq" if self._online_view == online_ui.VIEW_QQ_SEARCH else "wy"
            gen = self._online_search_gen
            self._online_search_pending = ""
            self._online_search_running = True
            import threading
            threading.Thread(target=self._do_online_search_async,
                             args=(kw, gen, platform), daemon=True).start()
        # 2. 应用结果：仅当代际匹配最新输入才覆盖（旧请求结果丢弃）
        if self._online_search_result:
            tracks, gen = self._online_search_result
            self._online_search_result = None
            if gen == self._online_search_gen:
                self._online_tracks = tracks

    def _handle_online_song_list_key(self, key):
        if not self._online_tracks:
            return
        if key == "UP":
            self._online_selector_index = max(0, self._online_selector_index - 1)
        elif key == "DOWN":
            self._online_selector_index = min(len(self._online_tracks) - 1,
                                              self._online_selector_index + 1)
        elif key == "RIGHT" and self._online_selector_index < len(self._online_tracks):
            self._add_online_to_queue(self._online_tracks[self._online_selector_index])
        elif key == "LEFT" and self._online_selector_index < len(self._online_tracks):
            self._online_toggle_like(self._online_tracks[self._online_selector_index])
        elif key == "\r" or key == "\n":
            if self._online_selector_index < len(self._online_tracks):
                self._play_online_track(self._online_tracks[self._online_selector_index])
                self._exit_online_mode()

    def _handle_online_sub_list_key(self, key, view):
        if not self._online_sub_items:
            return
        if key == "UP":
            self._online_selector_index = max(0, self._online_selector_index - 1)
        elif key == "DOWN":
            self._online_selector_index = min(len(self._online_sub_items) - 1,
                                              self._online_selector_index + 1)
        elif key == "\r" or key == "\n":
            if self._online_selector_index < len(self._online_sub_items):
                item = self._online_sub_items[self._online_selector_index]
                dissid = item.get("dissid", "") if isinstance(item, dict) else ""
                if not dissid:
                    return
                self._online_push_view(online_ui.VIEW_SONG_LIST)
                self._online_loading = True
                try:
                    if view == online_ui.VIEW_QQ_PLAYLISTS:
                        self._online_tracks = self.online_mgr.qq_get_playlist_songs(dissid)
                    elif view == online_ui.VIEW_QQ_RANKINGS:
                        self._online_tracks = self.online_mgr.qq_get_top_songs(int(dissid))
                    elif view == online_ui.VIEW_WY_RANKINGS:
                        self._online_tracks = self.online_mgr.wy_get_top_songs(int(dissid))
                except Exception:
                    self._online_tracks = []
                self._online_loading = False

    def _build_online_renderable(self):
        cfg = self.cfg
        w = self.cfg.playback.ui_width
        v = self._online_view
        if self._online_loading:
            return online_ui.build_online_loading_ui(cfg)
        if v == online_ui.VIEW_PLATFORM:
            return online_ui.build_online_platform_ui(cfg, self._online_selector_index, w)
        if v in (online_ui.VIEW_QQ_HOME, online_ui.VIEW_WY_HOME):
            menu = online_ui.QQ_MENU if v == online_ui.VIEW_QQ_HOME else online_ui.WY_MENU
            platform = "qq" if v == online_ui.VIEW_QQ_HOME else "wy"
            logged_in = False
            user = ""
            try:
                if platform == "qq":
                    logged_in = self.online_mgr.qq_has_credential()
                else:
                    status = self.online_mgr.wy_get_auth_status()
                    logged_in = status.get("logged_in", False)
                    user = status.get("nickname", "")
            except Exception:
                pass
            return online_ui.build_online_menu_ui(cfg, platform, menu,
                                                    self._online_selector_index,
                                                    logged_in, user, w)
        if v in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH):
            platform = "qq" if v == online_ui.VIEW_QQ_SEARCH else "wy"
            return online_ui.build_online_search_ui(cfg, platform,
                                                      self._online_search_str,
                                                      self._online_tracks,
                                                      self._online_selector_index, w,
                                                      playlist=self.playlist)
        if v == online_ui.VIEW_SONG_LIST:
            return online_ui.build_online_song_list_ui(cfg, "歌曲列表",
                                                        self._online_tracks,
                                                        self._online_selector_index, w,
                                                        playlist=self.playlist)
        if v == online_ui.VIEW_QQ_FAV_SONGS:
            return online_ui.build_online_song_list_ui(cfg, "收藏歌曲",
                                                        self._online_tracks,
                                                        self._online_selector_index, w,
                                                        playlist=self.playlist)
        if v == online_ui.VIEW_QQ_DAILY:
            return online_ui.build_online_song_list_ui(cfg, "每日推荐",
                                                        self._online_tracks,
                                                        self._online_selector_index, w,
                                                        playlist=self.playlist)
        if v in (online_ui.VIEW_QQ_PLAYLISTS,
                  online_ui.VIEW_QQ_RANKINGS, online_ui.VIEW_WY_RANKINGS):
            title = {online_ui.VIEW_QQ_PLAYLISTS: "我的歌单",
                     online_ui.VIEW_QQ_RANKINGS: "排行榜",
                     online_ui.VIEW_WY_RANKINGS: "排行榜"}.get(v, "列表")
            return online_ui.build_online_playlist_list_ui(cfg, title,
                                                            self._online_sub_items,
                                                            self._online_selector_index, w)
        if v == online_ui.VIEW_LOGIN:
            qr_text = ""
            if self._online_login_data:
                if self._online_platform == "qq":
                    # QQ 返回 data 为 PNG 二进制，渲染成 ASCII 二维码
                    raw = self._online_login_data.get("data")
                    if raw:
                        qr_text = self.online_mgr.render_qr_ascii(raw)
                        if not qr_text:
                            qr_text = "二维码渲染失败，请打开 qq_login_qr.png 扫描"
                else:
                    # 网易云直接返回 ASCII 二维码文本
                    qr_text = self._online_login_data.get("qr_ascii", "")
            status = ("请用QQ音乐APP扫码，等待登录..." if self._online_platform == "qq"
                      else "请用网易云音乐APP扫码，等待登录...") if self._online_login_checking else ""
            return online_ui.build_online_login_ui(cfg, self._online_platform,
                                                    qr_text, status, w)
        return online_ui.build_online_loading_ui(cfg)

    # ---------------- 主循环 ----------------
    def handle_key(self, key: str):
        if key is None:
            return
        if self._online_active:
            self._handle_online_key(key)
            return
        if self._selector_active:
            self._handle_selector_key(key)
            return
        if key == "q":
            self._running = False
        elif key == "\t":
            self._toggle_selector()
        elif key == "o":
            self._enter_online_mode()
        elif key == " ":
            self.play_pause()
        elif key == "n":
            self.next_track()
        elif key == "p":
            self.prev_track()
        elif key == "LEFT":
            self.player.seek_relative(-5)
        elif key == "RIGHT":
            self.player.seek_relative(5)
        elif key == "UP":
            self.player.volume_relative(0.05)
        elif key == "DOWN":
            self.player.volume_relative(-0.05)
        elif key == "s":
            self.toggle_spectrum()
        elif key == "t":
            self.cycle_theme()
        elif key == "m":
            self.cycle_mode()
        elif key == "r":
            self.reload_cfg()
        elif key == "d":
            self.rerun_dir_setup()

    def run(self):
        fps = max(5, self.cfg.playback.ui_fps)
        frame_time = 1.0 / fps

        with KeyReader() as keys:
            with Live(console=self.console, screen=False, auto_refresh=False) as live:
                while self._running:
                    t0 = time.time()
                    key = keys.read_key(timeout=0.0)
                    if key:
                        self.handle_key(key)

                    if self._pending_dir_setup:
                        self._pending_dir_setup = False
                        live.stop()
                        self._do_dir_setup()
                        live.start()
                        continue

                    # 在线登录后台检测结果
                    if self._online_active and self._online_login_result:
                        result = self._online_login_result
                        self._online_login_result = None
                        if result.get("status") == 2 and self._online_view == online_ui.VIEW_LOGIN:
                            self._online_pop_view()

                    # 搜索防抖 tick（本地 + 在线）
                    if self._selector_active:
                        self._tick_selector_search()
                    if self._online_active:
                        self._tick_online_search()

                    if self.player.state == PlayState.PLAYING:
                        levels = self.spectrum.compute()
                    else:
                        levels = self.spectrum.silence_decay()

                    if self._online_active:
                        renderable = self._build_online_renderable()
                    elif self._selector_active:
                        renderable = ui.build_song_selector_ui(
                            self.cfg, self.playlist, self._search_str,
                            self._filtered_indices, self._selector_index,
                            width=self.cfg.playback.ui_width,
                        )
                    else:
                        renderable = ui.build_compact_ui(
                            self.cfg, self.current_track, self.player, levels,
                            self.lyrics_data, self.playlist, self._spectrum_on,
                            width=self.cfg.playback.ui_width,
                        )
                    live.update(renderable, refresh=True)

                    elapsed = time.time() - t0
                    sleep_left = frame_time - elapsed
                    if sleep_left > 0:
                        time.sleep(sleep_left)

                    frame_elapsed = time.time() - t0
                    self._stats_flush_timer += frame_elapsed
                    if self._stats_flush_timer >= StatsTracker.FLUSH_INTERVAL:
                        self.stats.flush()
                        self._stats_flush_timer = 0.0

        self.stats.flush()
        self._cleanup_temp_file()
        self.online_mgr.shutdown()
        self.player.stop()


def main():
    parser = argparse.ArgumentParser(description="终端音乐播放器")
    parser.add_argument("path", nargs="?", default=None,
                         help="音乐文件或目录路径（不指定则使用配置文件中的 music_dir）")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    parser.add_argument("--setup", action="store_true",
                         help="强制重新运行音频目录配置向导（忽略已保存的 music_dirs）")
    args = parser.parse_args()

    default_cfg = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    cfg_path = args.config or default_cfg

    try:
        app = App(cfg_path, args.path, force_setup=args.setup)
    except SystemExit:
        raise
    except Exception:
        log_exception("程序初始化时发生未处理异常")
        sys.exit(1)

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log_exception("程序运行过程中发生未处理异常")
        sys.exit(1)
    finally:
        app._record_stats_if_played()
        app.stats.flush()
        app._cleanup_temp_file()
        app.online_mgr.shutdown()
        app.player.stop()


if __name__ == "__main__":
    main()
