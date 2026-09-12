"""主程序：命令行入口，事件循环整合播放/UI/输入"""
from __future__ import annotations
import argparse
import os
import random
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
from .metadata import write_track_tags
from .logging_utils import log_error, log_warning, log_exception
from .lyrics import format_lrc_time

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


def _ensure_unicode_stdout() -> None:
    """stdout 为管道且编码无法表示界面块状字符时（如 Git Bash 管道的 cp936），
    切换为 utf-8。真实控制台本就是 WindowsConsoleIO(utf-8)，不受影响；
    mintty / VS Code 等管道终端均按 UTF-8 显示，切换后中英文不受影响。
    """
    try:
        out = sys.stdout
        if out is None or not hasattr(out, "reconfigure"):
            return
        enc = (getattr(out, "encoding", "") or "").lower()
        if enc.startswith("utf"):
            return
        try:
            "▀▄█♪".encode(enc)
            return  # 当前编码能表示界面字符，不动
        except (LookupError, UnicodeEncodeError):
            pass
        if out.isatty():
            return  # 真控制台出现非 utf-8 编码（legacy stdio），不动以免中文乱码
        out.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class App:
    def __init__(self, cfg_path: str, music_path: str | None, force_setup: bool = False):
        _enable_windows_vt()
        _ensure_unicode_stdout()
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
        self._selector_shuffle_order = None  # shuffle 模式下选择器的随机显示顺序（index → 位置）
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
            bi_quality=self.cfg.online_music.bi_quality,
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
        # B站平台状态
        self._online_song_list_title = "歌曲列表"
        self._bili_home_page = 1
        self._bili_home_has_more = False
        self._bili_home_loading = False
        self._bili_fav_fid = ""
        self._bili_fav_page = 1
        self._bili_fav_has_more = False
        self._bili_fav_loading = False
        self._bili_load_result = None
        self._bili_list_kind = "hot"  # VIEW_BILI_RECOMMEND 当前数据源: hot / rcmd
        self._bili_list_offset = 0    # 推荐列表粘性视口的窗口首行索引
        # B站视频搜索分页（结果滑到底自动叠加下一页）
        self._bili_search_page = 1
        self._bili_search_has_more = False
        self._bili_search_loading = False
        self._bili_search_keyword = ""

        if self.playlist.tracks:
            self._load_current_track()

    # ---------------- 加载/切歌 ----------------
    def _record_stats_if_played(self):
        """当前歌曲实际收听超过30秒 → 记录播放明细 + 累加歌曲总时长"""
        if self.current_track is None or self.player.listened_sec < 30:
            return
        platform = ("local" if self._online_current is None
                    else self._online_current.platform)
        self.stats.record_song(self.current_track.title,
                               artist=self.current_track.artist,
                               platform=platform)
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
        """加载在线歌曲：取直链（多线路）→ 逐线路流式播放 → 下载回退。

        B站 playurl 的主线路可能分配到不可达的 mcdn P2P 节点，因此
        流式与下载两个阶段都要遍历全部候选线路（baseUrl + backupUrl）。
        """
        url = self.online_mgr.get_play_url(track)
        if not url:
            log_error(f"在线歌曲加载失败（获取URL失败）: {track.title}")
            self.player.stop()
            return

        from .metadata import TrackInfo
        headers = getattr(track, "stream_headers", None)
        filetype = "m4a" if track.platform == "bili" else "mp3"
        candidates = [url] + [u for u in (getattr(track, "stream_urls_backup", None) or []) if u]
        eq = {"eq_bands": self.cfg.equalizer.bands_db, "q_values": self.cfg.equalizer.q_values}

        loaded_path = None
        # 第一轮：逐线路流式播放（ffmpeg 直接拉流）
        for cand in candidates:
            try:
                self.player.load(cand, **eq, headers=headers)
                loaded_path = cand
                break
            except RuntimeError:
                continue

        # 第二轮：下载回退（逐线路下载到临时文件后播放）
        if loaded_path is None:
            for cand in candidates:
                temp = self._download_to_temp(cand, headers=headers)
                if not temp:
                    continue
                try:
                    self.player.load(temp, **eq)
                    loaded_path = temp
                    break
                except RuntimeError:
                    self._cleanup_temp_file()
                    continue

        if loaded_path is None:
            log_error(f"在线歌曲加载失败（流式播放+下载回退均失败）: {track.title}")
            self.player.stop()
            return

        self.current_track = TrackInfo(
            path=loaded_path, title=track.title, artist=track.artist,
            album=track.album, duration_sec=track.duration_sec,
            filetype=filetype,
        )
        self.lyrics_data = self.online_mgr.get_lyrics(track)

    def _download_to_temp(self, url: str, headers: dict | None = None) -> str:
        """下载URL到临时文件（headers：B站直链必须带 Referer/UA）"""
        import tempfile
        import requests
        try:
            resp = requests.get(url, timeout=30, stream=True, headers=headers)
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
        except Exception as e:
            log_warning(f"临时文件下载失败: {type(e).__name__} {str(e)[:120]}")
            return ""

    def _cleanup_temp_file(self):
        if self._temp_file_path and os.path.exists(self._temp_file_path):
            try:
                os.unlink(self._temp_file_path)
            except OSError:
                pass
        self._temp_file_path = None

    def _current_queue_item(self):
        """当前播放项对应的队列实体：在线曲目 → OnlineTrack；本地 → int 索引；无 → None。"""
        if self._online_current is not None:
            return self._online_current
        if self.playlist.tracks and 0 <= self.playlist.index < len(self.playlist.tracks):
            return self.playlist.index
        return None

    def _leave_current_to_history(self):
        """切走当前曲目：压入历史缓冲栈（n 前进方向）。"""
        outgoing = self._current_queue_item()
        if outgoing is not None:
            self.playlist.push_buffer(outgoing)

    def _on_track_end(self):
        # 播放队列优先：队列中有歌曲时按 FIFO 顺序播放
        if self.playlist.has_queue():
            self._leave_current_to_history()
            item = self.playlist.next_from_queue()
            if isinstance(item, OnlineTrack):
                # 在线歌曲预检 URL，不可用则跳过（不入缓冲栈）
                while item is not None and isinstance(item, OnlineTrack):
                    if self.online_mgr.get_play_url(item):
                        break
                    log_error(f"在线歌曲无法获取播放URL（跳过）: {item.title}")
                    item = self.playlist.pop_queue_skip()
            if not isinstance(item, OnlineTrack):
                self._online_current = None
            else:
                self._online_current = item
            self._load_current_track()
            self.player.play()
            return

        # 队列已空，回到本地模式（保留历史栈，p 可回到刚播过的在线曲目）
        if self._online_current is not None:
            if self.playlist.mode == "repeat_one":
                self._load_current_track()
                self.player.play()
                return
            self._leave_current_to_history()
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
        was_playing = self.player.state == PlayState.PLAYING
        if self.playlist.has_queue():
            self._leave_current_to_history()
            item = self.playlist.next_from_queue()
            self._online_current = item if isinstance(item, OnlineTrack) else None
            self._load_current_track()
            if was_playing:
                self.player.play()
            return
        if not self.playlist.tracks:
            return
        self._leave_current_to_history()
        self._online_current = None
        self.playlist.next()
        self._load_current_track()
        if was_playing:
            self.player.play()

    def prev_track(self):
        was_playing = self.player.state == PlayState.PLAYING
        # 历史栈非空：当前曲目放回队首（n 可精确返回），从栈取回上一首
        if self.playlist.has_buffer():
            outgoing = self._current_queue_item()
            if outgoing is not None:
                self.playlist.push_queue_front(outgoing)
            item = self.playlist.prev_from_buffer()
            self._online_current = item if isinstance(item, OnlineTrack) else None
            self._load_current_track()
            if was_playing:
                self.player.play()
            return
        # 历史栈空：在线曲目原地重播（不丢当前曲目、不动队列）
        if self._online_current is not None:
            self._load_current_track()
            if was_playing:
                self.player.play()
            return
        if not self.playlist.tracks:
            return
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
        self.online_mgr._bi_quality = self.cfg.online_music.bi_quality

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
            self._selector_search_pending = False
            # 随机播放模式下选择器按随机顺序显示（打开时固定一次，避免输入时跳动）
            if self.playlist.mode == "shuffle":
                order = list(range(len(self.playlist.tracks)))
                random.shuffle(order)
                self._selector_shuffle_order = {idx: pos for pos, idx in enumerate(order)}
            else:
                self._selector_shuffle_order = None
            self._update_filtered_indices()

    def _update_filtered_indices(self):
        """按搜索串过滤歌曲：关键词按空格拆分，每个需作为子串出现（顺序无关）"""
        keywords = self._search_str.lower().split()
        self._filtered_indices = [
            i for i in range(len(self.playlist.tracks))
            if all(kw in os.path.basename(self.playlist.tracks[i]).lower()
                   for kw in keywords)
        ]
        if self._selector_shuffle_order:
            order = self._selector_shuffle_order
            self._filtered_indices.sort(key=lambda i: order.get(i, len(order)))
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
        self._online_song_list_title = "歌曲列表"
        self._online_search_pending = ""
        self._online_search_running = False
        self._online_search_result = None
        # B站翻页状态一并清除：VIEW_SONG_LIST 由多平台共用，残留的收藏夹 fid
        # 会让网易云/QQ 歌曲列表误触发 B站翻页，把 dict 混进 OnlineTrack 列表
        self._bili_fav_fid = ""
        self._bili_fav_page = 1
        self._bili_fav_has_more = False
        self._bili_fav_loading = False
        self._bili_home_page = 1
        self._bili_home_has_more = False
        self._bili_home_loading = False

    def _online_push_view(self, view):
        # 保存当前视图数据，弹出时恢复——各视图共用 _online_tracks/_online_sub_items
        # 等列表，若不快照，返回上级视图时会拿到子视图的残留数据
        # （如从 UP主视频列表返回 UP主搜索视图时，_online_tracks 里是 OnlineTrack
        #   而非用户 dict，渲染时 u.get('uname') 直接崩溃）。
        self._online_view_stack.append({
            "view": self._online_view,
            "tracks": self._online_tracks,
            "sub_items": self._online_sub_items,
            "search_str": self._online_search_str,
            "selector_index": self._online_selector_index,
            "song_list_title": self._online_song_list_title,
            "bili_list_offset": self._bili_list_offset,
            "bili_fav_fid": self._bili_fav_fid,
        })
        self._online_view = view
        self._online_selector_index = 0

    def _online_pop_view(self):
        if self._online_view_stack:
            entry = self._online_view_stack.pop()
            self._online_view = entry["view"]
            self._online_tracks = entry["tracks"]
            self._online_sub_items = entry["sub_items"]
            self._online_search_str = entry["search_str"]
            self._online_selector_index = entry["selector_index"]
            self._online_song_list_title = entry["song_list_title"]
            self._bili_list_offset = entry["bili_list_offset"]
            self._bili_fav_fid = entry["bili_fav_fid"]
        else:
            self._exit_online_mode()

    def _play_online_track(self, track: OnlineTrack):
        # 直接点播：把被替换的当前曲目记入历史（p 可返回），同曲不重复入栈
        outgoing = self._current_queue_item()
        same = (isinstance(outgoing, OnlineTrack) and isinstance(track, OnlineTrack)
                and outgoing.platform == track.platform and outgoing.song_id == track.song_id)
        if outgoing is not None and not same:
            self.playlist.push_buffer(outgoing)
        self._online_current = track
        self._load_current_track()
        self.player.play()

    def _add_online_to_queue(self, track: OnlineTrack):
        # 添加到队列后光标保持在当前行，不自动下移
        self.playlist.add_online_to_queue(track)

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
                self._online_selector_index = min(2, self._online_selector_index + 1)
            elif key == "\r" or key == "\n":
                self._online_platform = ("qq", "wy", "bili")[self._online_selector_index]
                self._online_push_view({
                    "qq": online_ui.VIEW_QQ_HOME,
                    "wy": online_ui.VIEW_WY_HOME,
                    "bili": online_ui.VIEW_BILI_HOME,
                }[self._online_platform])

        elif v in (online_ui.VIEW_QQ_HOME, online_ui.VIEW_WY_HOME, online_ui.VIEW_BILI_HOME):
            menu = {online_ui.VIEW_QQ_HOME: online_ui.QQ_MENU,
                    online_ui.VIEW_WY_HOME: online_ui.WY_MENU,
                    online_ui.VIEW_BILI_HOME: online_ui.BILI_MENU}[v]
            max_idx = len(menu) - 1
            if key == "UP":
                self._online_selector_index = max(0, self._online_selector_index - 1)
            elif key == "DOWN":
                self._online_selector_index = min(max_idx, self._online_selector_index + 1)
            elif key == "\r" or key == "\n":
                idx = self._online_selector_index
                if v == online_ui.VIEW_QQ_HOME:
                    self._enter_qq_menu(idx)
                elif v == online_ui.VIEW_WY_HOME:
                    self._enter_wy_menu(idx)
                else:
                    self._enter_bili_menu(idx)

        elif v in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH,
                    online_ui.VIEW_BILI_SEARCH, online_ui.VIEW_BILI_USER_SEARCH):
            self._handle_online_search_key(key, v)

        elif v in (online_ui.VIEW_SONG_LIST, online_ui.VIEW_QQ_FAV_SONGS,
                    online_ui.VIEW_QQ_DAILY, online_ui.VIEW_BILI_RECOMMEND):
            self._handle_online_song_list_key(key, v)

        elif v in (online_ui.VIEW_QQ_PLAYLISTS,
                    online_ui.VIEW_QQ_RANKINGS, online_ui.VIEW_WY_RANKINGS,
                    online_ui.VIEW_BILI_FAVORITES):
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

    # ---------------- B站平台 ----------------
    def _reset_online_search_state(self):
        """重置在线搜索状态（进入搜索视图前调用）"""
        self._online_search_str = ""
        self._online_tracks = []
        self._online_search_pending = ""
        self._online_search_running = False
        self._online_search_result = None
        self._online_search_gen = 0
        self._bili_list_offset = 0
        self._bili_search_page = 1
        self._bili_search_has_more = False
        self._bili_search_loading = False
        self._bili_search_keyword = ""

    def _enter_bili_menu(self, idx):
        """B站菜单分发：搜索视频 / UP主搜索 / 热门 / 首页推荐 / 我的收藏 / 导入Cookie / 登录退出"""
        if idx == 0:  # 搜索视频
            self._reset_online_search_state()
            self._online_push_view(online_ui.VIEW_BILI_SEARCH)
        elif idx == 1:  # UP主搜索
            self._reset_online_search_state()
            self._online_push_view(online_ui.VIEW_BILI_USER_SEARCH)
        elif idx in (2, 3):  # 热门 / 首页推荐（个性化推荐流，滑到底自动叠加）
            self._bili_list_kind = "hot" if idx == 2 else "rcmd"
            self._open_bili_list(self._bili_list_kind, page=1)
        elif idx == 4:  # 我的收藏
            self._online_push_view(online_ui.VIEW_BILI_FAVORITES)
            self._online_loading = True
            try:
                if self.online_mgr.bili_has_credential():
                    self._online_sub_items = self.online_mgr.bili_get_favorite_folders()
                else:
                    self._online_sub_items = []
            except Exception:
                self._online_sub_items = []
            self._online_loading = False
        elif idx == 5:  # 导入Cookie（bilicookies.txt，备用登录通道）
            path = os.path.join(os.path.dirname(self.cfg.path), "bilicookies.txt")
            try:
                ok, msg = self.online_mgr.import_bili_cookies(path)
            except Exception as e:
                ok, msg = False, str(e)
            log_warning(f"导入B站Cookie({os.path.basename(path)}): {msg}")
        elif idx == 6:  # 登录/退出
            self._enter_bili_login()

    def _update_bili_list_offset(self, view):
        """维护搜索/推荐/收藏夹列表的粘性视口：仅在选择器越出视口边缘时滚动窗口，
        列表尾部追加内容不改变视口 → UI 无跳动。"""
        if view not in (online_ui.VIEW_BILI_RECOMMEND, online_ui.VIEW_SONG_LIST,
                        online_ui.VIEW_BILI_SEARCH):
            return
        max_visible = online_ui.LIST_MAX_VISIBLE
        sel = self._online_selector_index
        if sel < self._bili_list_offset:
            self._bili_list_offset = sel
        elif sel >= self._bili_list_offset + max_visible:
            self._bili_list_offset = sel - max_visible + 1
        self._bili_list_offset = max(0, min(self._bili_list_offset,
                                            max(0, len(self._online_tracks) - max_visible)))

    def _open_bili_list(self, kind: str, page: int):
        """打开热门/首页推荐列表（首屏加载；滑到底自动叠加由 worker 处理）"""
        self._online_push_view(online_ui.VIEW_BILI_RECOMMEND)
        self._online_loading = True
        self._bili_list_offset = 0
        self._bili_home_page = 1
        self._bili_home_loading = False
        try:
            if kind == "hot":
                result = self.online_mgr.bili_get_hot_recommend(page=page)
            else:
                result = self.online_mgr.bili_get_rcmd(page=page)
            self._online_tracks = result.get("items", [])
            self._bili_home_has_more = result.get("has_more", False)
        except Exception:
            self._online_tracks = []
            self._bili_home_has_more = False
        self._online_loading = False

    def _enter_bili_login(self):
        """B站登录入口：已有凭证则清除（退出登录），否则进入扫码视图"""
        if self.online_mgr.bili_has_credential():
            try:
                self.online_mgr.bili_logout()
            except Exception:
                pass
            return
        self._online_push_view(online_ui.VIEW_LOGIN)
        self._online_login_data = None
        self._online_login_result = None
        self._online_login_checking = False
        try:
            qr = self.online_mgr.bili_login_qr_start()
            self._online_login_data = qr
            if qr and qr.get("data"):
                try:
                    os.makedirs(os.path.join(self._online_data_dir, "bili"), exist_ok=True)
                except OSError:
                    pass
                self.online_mgr.save_qr_png(
                    qr["data"],
                    os.path.join(self._online_data_dir, "bili", "bili_login_qr.png"))
                self._online_login_checking = True
                import threading
                threading.Thread(target=self._bili_login_check_worker,
                                 daemon=True).start()
        except Exception:
            pass

    def _bili_login_check_worker(self):
        """B站扫码状态轮询（每2秒，最长180秒），成功后凭证自动保存"""
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                result = self.online_mgr.bili_login_qr_check()
            except Exception:
                result = {"status": "error"}
            status = result.get("status")
            if status == "success":
                self._online_login_result = {"status": 2}
                break
            if status in ("expired", "error"):
                self._online_login_result = {"status": -1}
                break
            time.sleep(2)
        else:
            self._online_login_result = {"status": -1}
        self._online_login_checking = False

    def _select_bili_user(self, user: dict):
        """UP主搜索结果 Enter → 载入该UP主的视频列表"""
        uid = user.get("mid", "") if isinstance(user, dict) else ""
        if not uid:
            return
        self._online_push_view(online_ui.VIEW_SONG_LIST)
        self._online_loading = True
        self._bili_list_offset = 0
        self._bili_fav_fid = ""  # 非收藏夹列表，禁用收藏夹翻页叠加
        try:
            self._online_tracks = self.online_mgr.bili_get_user_videos(uid, count=30)
            self._online_song_list_title = f"UP主: {user.get('uname', '')}"
        except Exception:
            self._online_tracks = []
            self._online_song_list_title = "歌曲列表"
        self._online_loading = False

    def _maybe_load_more_bili(self, view):
        """B站搜索/首页推荐/收藏夹视频：选择器接近列表底部时自动叠加下一页"""
        if self._online_selector_index < len(self._online_tracks) - 3:
            return
        import threading
        if view == online_ui.VIEW_BILI_RECOMMEND:
            if self._bili_home_loading or not self._bili_home_has_more:
                return
            self._bili_home_loading = True
            threading.Thread(target=self._bili_load_more_worker,
                             args=(self._bili_list_kind, self._bili_home_page + 1),
                             daemon=True).start()
        elif view == online_ui.VIEW_BILI_SEARCH:
            if self._bili_search_loading or not self._bili_search_has_more:
                return
            self._bili_search_loading = True
            threading.Thread(target=self._bili_load_more_worker,
                             args=("search", self._bili_search_page + 1,
                                   self._bili_search_keyword, self._online_search_gen),
                             daemon=True).start()
        elif (view == online_ui.VIEW_SONG_LIST and self._bili_fav_fid
              and self._online_tracks
              and getattr(self._online_tracks[0], "platform", "") == "bili"):
            # 当前列表须确为 B站曲目（VIEW_SONG_LIST 与 QQ/网易歌曲列表共用）
            if self._bili_fav_loading or not self._bili_fav_has_more:
                return
            self._bili_fav_loading = True
            threading.Thread(target=self._bili_load_more_worker,
                             args=("fav", self._bili_fav_page + 1),
                             daemon=True).start()

    def _bili_load_more_worker(self, kind: str, page: int,
                               keyword: str = "", gen: int | None = None):
        """后台加载B站下一页（搜索/热门/首页推荐/收藏夹）"""
        try:
            if kind == "hot":
                data = self.online_mgr.bili_get_hot_recommend(page=page)
            elif kind == "rcmd":
                data = self.online_mgr.bili_get_rcmd(page=page)
            elif kind == "search":
                data = self.online_mgr.bili_search_page(keyword, page=page)
                data["gen"] = gen
            else:
                data = self.online_mgr.bili_get_favorite_videos(self._bili_fav_fid, page=page)
        except Exception:
            data = {"items": [], "has_more": False}
        self._bili_load_result = (kind, data, page)

    def _tick_bili_load_more(self):
        """主循环每帧调用：应用后台加载的下一页结果（追加到当前列表）"""
        if not self._bili_load_result:
            return
        kind, data, page = self._bili_load_result
        self._bili_load_result = None
        items = data.get("items", [])
        has_more = data.get("has_more", False)
        if kind in ("hot", "rcmd"):
            self._bili_home_page = page
            self._bili_home_has_more = has_more
            self._bili_home_loading = False
            # 仅当仍停留在对应视图时追加，避免串列表
            if self._online_view == online_ui.VIEW_BILI_RECOMMEND:
                self._online_tracks.extend(items)
        elif kind == "search":
            self._bili_search_loading = False
            # 代际匹配（期间用户没输入新关键词）且仍在搜索视图才追加
            if (data.get("gen") == self._online_search_gen
                    and self._online_view == online_ui.VIEW_BILI_SEARCH):
                self._bili_search_page = page
                self._bili_search_has_more = has_more
                self._online_tracks.extend(items)
        else:
            self._bili_fav_page = page
            self._bili_fav_has_more = has_more
            self._bili_fav_loading = False
            # VIEW_SONG_LIST 由 B站收藏夹与 QQ/网易歌曲列表共用，
            # 仅当当前列表确实是 B站曲目时才追加，防止跨平台污染
            if (self._online_view == online_ui.VIEW_SONG_LIST and self._bili_fav_fid
                    and self._online_tracks
                    and getattr(self._online_tracks[0], "platform", "") == "bili"):
                self._online_tracks.extend(items)

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
                self._update_bili_list_offset(view)
        elif key == "DOWN":
            if self._online_tracks:
                self._online_selector_index = min(len(self._online_tracks) - 1,
                                                  self._online_selector_index + 1)
                self._maybe_load_more_bili(view)
                self._update_bili_list_offset(view)
        elif key == "RIGHT" and self._online_tracks and self._online_selector_index < len(self._online_tracks):
            self._add_online_to_queue(self._online_tracks[self._online_selector_index])
        elif key == "LEFT" and self._online_tracks and self._online_selector_index < len(self._online_tracks):
            self._online_toggle_like(self._online_tracks[self._online_selector_index])
        elif key == "\r" or key == "\n":
            if self._online_tracks and self._online_selector_index < len(self._online_tracks):
                if view == online_ui.VIEW_BILI_USER_SEARCH:
                    self._select_bili_user(self._online_tracks[self._online_selector_index])
                else:
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

    def _do_online_search_async(self, keyword: str, gen: int, platform: str,
                                kind: str = "song"):
        """后台线程执行网络搜索（kind=bili_user 时搜UP主）"""
        try:
            if kind == "bili_user":
                tracks = self.online_mgr.bili_search_users(keyword)
                self._bili_search_has_more = False
            elif platform == "bili":
                # B站视频搜索走分页接口（结果滑到底自动叠加下一页）
                data = self.online_mgr.bili_search_page(keyword, page=1)
                tracks = data.get("items", [])
                self._bili_search_page = 1
                self._bili_search_keyword = keyword
                self._bili_search_has_more = data.get("has_more", False)
            else:
                tracks = self.online_mgr.search(platform, keyword)
                self._bili_search_has_more = False
        except Exception:
            tracks = []
            self._bili_search_has_more = False
        self._online_search_result = (tracks, gen)
        self._online_search_running = False

    def _tick_online_search(self):
        """主循环每帧调用：防抖启动后台搜索 + 应用结果（代际竞态取消）"""
        # 仅当位于搜索视图时才处理
        if self._online_view not in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH,
                                      online_ui.VIEW_BILI_SEARCH, online_ui.VIEW_BILI_USER_SEARCH):
            self._online_search_pending = ""
            self._online_search_result = None
            return
        # 1. 防抖：停顿 400ms 且无搜索在跑 → 启动后台搜索
        if (self._online_search_pending and not self._online_search_running
                and time.time() - self._online_search_last_input >= 0.4):
            kw = self._online_search_pending
            platform = {online_ui.VIEW_QQ_SEARCH: "qq",
                        online_ui.VIEW_WY_SEARCH: "wy",
                        online_ui.VIEW_BILI_SEARCH: "bili",
                        online_ui.VIEW_BILI_USER_SEARCH: "bili"}[self._online_view]
            kind = "bili_user" if self._online_view == online_ui.VIEW_BILI_USER_SEARCH else "song"
            gen = self._online_search_gen
            self._online_search_pending = ""
            self._online_search_running = True
            import threading
            threading.Thread(target=self._do_online_search_async,
                             args=(kw, gen, platform, kind), daemon=True).start()
        # 2. 应用结果：仅当代际匹配最新输入才覆盖（旧请求结果丢弃）
        if self._online_search_result:
            tracks, gen = self._online_search_result
            self._online_search_result = None
            if gen == self._online_search_gen:
                self._online_tracks = tracks
                self._bili_list_offset = 0

    def _handle_online_song_list_key(self, key, view=None):
        if not self._online_tracks:
            return
        if key == "UP":
            self._online_selector_index = max(0, self._online_selector_index - 1)
            self._update_bili_list_offset(view)
        elif key == "DOWN":
            self._online_selector_index = min(len(self._online_tracks) - 1,
                                              self._online_selector_index + 1)
            self._maybe_load_more_bili(view)
            self._update_bili_list_offset(view)
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
                if view == online_ui.VIEW_BILI_FAVORITES:
                    self._open_bili_favorite_folder(item)
                    return
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

    def _open_bili_favorite_folder(self, item):
        """B站收藏夹 Enter → 载入收藏夹内视频（滑到底自动叠加下一页）"""
        fid = item.get("fid", "") if isinstance(item, dict) else ""
        if not fid:
            return
        self._online_push_view(online_ui.VIEW_SONG_LIST)
        self._online_loading = True
        self._bili_list_offset = 0
        try:
            result = self.online_mgr.bili_get_favorite_videos(fid, page=1)
            self._online_tracks = result.get("items", [])
            self._bili_fav_fid = fid
            self._bili_fav_page = 1
            self._bili_fav_has_more = result.get("has_more", False)
            self._online_song_list_title = f"收藏夹: {item.get('name', '')}"
        except Exception:
            self._online_tracks = []
            self._bili_fav_fid = ""
            self._bili_fav_has_more = False
            self._online_song_list_title = "歌曲列表"
        self._online_loading = False

    def _build_online_renderable(self):
        cfg = self.cfg
        w = self.cfg.playback.ui_width
        v = self._online_view
        if self._online_loading:
            return online_ui.build_online_loading_ui(cfg)
        if v == online_ui.VIEW_PLATFORM:
            return online_ui.build_online_platform_ui(cfg, self._online_selector_index, w)
        if v in (online_ui.VIEW_QQ_HOME, online_ui.VIEW_WY_HOME, online_ui.VIEW_BILI_HOME):
            menu = {online_ui.VIEW_QQ_HOME: online_ui.QQ_MENU,
                    online_ui.VIEW_WY_HOME: online_ui.WY_MENU,
                    online_ui.VIEW_BILI_HOME: online_ui.BILI_MENU}[v]
            platform = {online_ui.VIEW_QQ_HOME: "qq",
                        online_ui.VIEW_WY_HOME: "wy",
                        online_ui.VIEW_BILI_HOME: "bili"}[v]
            logged_in = False
            user = ""
            try:
                if platform == "qq":
                    logged_in = self.online_mgr.qq_has_credential()
                elif platform == "bili":
                    logged_in = self.online_mgr.bili_has_credential()
                else:
                    status = self.online_mgr.wy_get_auth_status()
                    logged_in = status.get("logged_in", False)
                    user = status.get("nickname", "")
            except Exception:
                pass
            return online_ui.build_online_menu_ui(cfg, platform, menu,
                                                    self._online_selector_index,
                                                    logged_in, user, w)
        if v in (online_ui.VIEW_QQ_SEARCH, online_ui.VIEW_WY_SEARCH, online_ui.VIEW_BILI_SEARCH):
            platform = {online_ui.VIEW_QQ_SEARCH: "qq",
                        online_ui.VIEW_WY_SEARCH: "wy",
                        online_ui.VIEW_BILI_SEARCH: "bili"}[v]
            return online_ui.build_online_search_ui(cfg, platform,
                                                      self._online_search_str,
                                                      self._online_tracks,
                                                      self._online_selector_index, w,
                                                      playlist=self.playlist,
                                                      view_start=(self._bili_list_offset
                                                                  if v == online_ui.VIEW_BILI_SEARCH
                                                                  else None))
        if v == online_ui.VIEW_BILI_USER_SEARCH:
            return online_ui.build_online_user_search_ui(cfg, self._online_search_str,
                                                          self._online_tracks,
                                                          self._online_selector_index, w)
        if v == online_ui.VIEW_SONG_LIST:
            # 收藏夹内容列表启用粘性视口（滑到底自动叠加，避免窗口跳动）；
            # 其它歌曲列表（UP主/每日推荐等）保持原有居中窗口逻辑
            return online_ui.build_online_song_list_ui(cfg, self._online_song_list_title,
                                                        self._online_tracks,
                                                        self._online_selector_index, w,
                                                        playlist=self.playlist,
                                                        view_start=(self._bili_list_offset
                                                                    if self._bili_fav_fid else None))
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
        if v == online_ui.VIEW_BILI_RECOMMEND:
            kind_title = {"hot": "哔哩哔哩·热门", "rcmd": "哔哩哔哩·首页推荐"}.get(
                self._bili_list_kind, "哔哩哔哩·热门")
            title = f"{kind_title} (已加载 {len(self._online_tracks)})"
            return online_ui.build_online_song_list_ui(cfg, title,
                                                        self._online_tracks,
                                                        self._online_selector_index, w,
                                                        playlist=self.playlist,
                                                        view_start=self._bili_list_offset)
        if v == online_ui.VIEW_BILI_FAVORITES:
            title = "我的收藏"
            try:
                if not self.online_mgr.bili_has_credential():
                    title = "我的收藏（未登录，请返回菜单选择 登录/退出）"
            except Exception:
                pass
            return online_ui.build_online_playlist_list_ui(cfg, title,
                                                            self._online_sub_items,
                                                            self._online_selector_index, w)
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
                if self._online_platform == "wy":
                    # 网易云直接返回 ASCII 二维码文本
                    qr_text = self._online_login_data.get("qr_ascii", "")
                else:
                    # QQ/B站返回 data 为 PNG 二进制，渲染成原生分辨率终端二维码
                    raw = self._online_login_data.get("data")
                    if raw:
                        qr_text = self.online_mgr.render_qr_ascii(
                            raw, encoding=self.console.encoding)
                        if not qr_text:
                            png_name = ("bili_login_qr.png" if self._online_platform == "bili"
                                        else "qq_login_qr.png")
                            qr_text = f"终端编码不支持二维码字符，请打开 {png_name} 扫描"
            status = {"qq": "请用QQ音乐APP扫码，等待登录...",
                      "bili": "请用哔哩哔哩APP扫码，等待登录...",
                      "wy": "请用网易云音乐APP扫码，等待登录..."}.get(
                self._online_platform, "") if self._online_login_checking else ""
            return online_ui.build_online_login_ui(cfg, self._online_platform,
                                                    qr_text, status, w)
        return online_ui.build_online_loading_ui(cfg)

    # ---------------- 在线歌曲下载 ----------------
    def _set_status(self, msg: str):
        """主界面状态行（下载进度/结果），显示数秒后自动消失"""
        self._status_msg = msg
        self._status_time = time.time()

    def _current_status(self) -> str:
        msg = getattr(self, "_status_msg", "")
        if not msg or time.time() - getattr(self, "_status_time", 0.0) > 5.0:
            return ""
        return msg

    def _download_current_online(self):
        """下载当前播放的在线歌曲到配置目录（后台线程，不阻塞 UI）"""
        track = self._online_current
        if track is None:
            return
        if getattr(self, "_download_running", False):
            self._set_status("⬇ 已有下载任务进行中，请稍候")
            return
        self._download_running = True
        self._set_status(f"⬇ 开始下载: {track.title}")
        import threading
        threading.Thread(target=self._download_worker, args=(track,),
                         daemon=True).start()

    def _download_worker(self, track: OnlineTrack):
        try:
            path, note = self._download_online_track(track)
            if path:
                suffix = f"（{note}）" if note else ""
                self._set_status(f"✓ 已下载: {os.path.basename(path)}{suffix}")
            else:
                self._set_status(f"✗ 下载失败: {track.title}")
        except Exception as e:
            log_warning(f"在线歌曲下载异常: {type(e).__name__} {str(e)[:120]}")
            self._set_status(f"✗ 下载失败: {track.title}")
        finally:
            self._download_running = False

    def _download_online_track(self, track: OnlineTrack) -> str:
        """取直链（含备用线路）流式下载到目标目录，写入标签/封面/歌词，返回文件路径，失败返回空"""
        import re
        import requests
        url = self.online_mgr.get_play_url(track)
        if not url:
            log_error(f"下载失败（获取URL失败）: {track.title}")
            return ""
        candidates = [url] + [u for u in (getattr(track, "stream_urls_backup", None) or []) if u]
        headers = getattr(track, "stream_headers", None)

        dl_dir = (self.cfg.online_music.download_dir or "").strip()
        if dl_dir and not os.path.isabs(dl_dir):
            # 相对路径以程序目录（config.yaml 所在目录）为基准
            out_dir = os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), dl_dir)
        elif dl_dir:
            out_dir = dl_dir
        else:
            out_dir = os.getcwd()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            log_warning(f"创建下载目录失败: {out_dir} - {e}")
            return ""

        base = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", f"{track.title} - {track.artist}").strip() or track.song_id
        for cand in candidates:
            try:
                resp = requests.get(cand, timeout=30, stream=True, headers=headers)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                url_low = cand.lower()
                if "flac" in ct or ".flac" in url_low:
                    ext = ".flac"
                elif "m4a" in ct or "mp4" in ct or ".m4a" in url_low or track.platform == "bili":
                    ext = ".m4a"
                else:
                    ext = ".mp3"
                out_path = os.path.join(out_dir, base + ext)
                n = 1
                while os.path.exists(out_path):
                    out_path = os.path.join(out_dir, f"{base} ({n}){ext}")
                    n += 1
                tmp_path = out_path + ".part"
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)
                os.replace(tmp_path, out_path)
                note = self._post_process_download(track, out_path)
                return out_path, note
            except Exception as e:
                log_warning(f"下载线路失败: {type(e).__name__} {str(e)[:120]}")
                continue
        return "", ""

    def _post_process_download(self, track: OnlineTrack, path: str) -> str:
        """下载完成后补充元数据：保存 .lrc 歌词、拉取封面、写入标签。

        返回结果说明（如 "歌词+封面" / "无字幕歌词" / "无封面"），供状态行展示。
        """
        parts = []
        # 歌词：原始 LRC 优先，兜底由缓存的 LyricsData 重建
        lrc_text = self.online_mgr.get_raw_lrc(track)
        if not lrc_text and track.lyric_data and track.lyric_data.lines:
            lrc_text = "\n".join(f"[{format_lrc_time(l.time_ms)}]{l.text}"
                                 for l in track.lyric_data.lines)
        if lrc_text:
            try:
                with open(os.path.splitext(path)[0] + ".lrc", "w",
                          encoding="utf-8") as f:
                    f.write(lrc_text + "\n")
                parts.append("歌词")
            except OSError as e:
                log_warning(f"歌词文件写入失败: {e}")
        else:
            # B站歌词只能来自视频字幕，明确提示原因，避免误以为是下载失败
            why = "B站视频无字幕或未登录B站" if track.platform == "bili" else "未获取到歌词"
            log_warning(f"{why}: [{track.platform}] {track.title}")
            parts.append("无字幕歌词" if track.platform == "bili" else "无歌词")

        # 封面
        cover_bytes = None
        cover_url = self.online_mgr.get_cover_url(track)
        if cover_url:
            try:
                import requests
                r = requests.get(cover_url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.ok and r.content:
                    cover_bytes = r.content
            except Exception as e:
                log_warning(f"封面下载失败: {type(e).__name__} {str(e)[:120]}")
        if cover_bytes:
            parts.append("封面")
        else:
            log_warning(f"未获取到封面: [{track.platform}] {track.title}")
            parts.append("无封面")

        # 标签（含嵌入封面/歌词），失败不影响已下载的音频
        try:
            write_track_tags(path, title=track.title, artist=track.artist,
                             album=track.album, cover_bytes=cover_bytes,
                             lyrics_text=lrc_text or None)
        except Exception as e:
            log_warning(f"标签写入失败: {type(e).__name__} {str(e)[:120]}")
        return "+".join(parts)

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
        kb = self.cfg.keybindings
        if key == kb.quit:
            self._running = False
        elif key == kb.selector:
            self._toggle_selector()
        elif key == kb.online:
            self._enter_online_mode()
        elif key == kb.play_pause:
            self.play_pause()
        elif key == kb.next:
            self.next_track()
        elif key == kb.prev:
            self.prev_track()
        elif key == "LEFT":
            self.player.seek_relative(-5)
        elif key == "RIGHT":
            self.player.seek_relative(5)
        elif key == "UP":
            self.player.volume_relative(0.05)
        elif key == "DOWN":
            self.player.volume_relative(-0.05)
        elif key == kb.spectrum:
            self.toggle_spectrum()
        elif key == kb.theme:
            self.cycle_theme()
        elif key == kb.mode:
            self.cycle_mode()
        elif key == kb.reload:
            self.reload_cfg()
        elif key == kb.download and self._online_current is not None:
            self._download_current_online()
        elif key == kb.dir_setup and self._online_current is None:
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
                        self._tick_bili_load_more()

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
                            status=self._current_status(),
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
