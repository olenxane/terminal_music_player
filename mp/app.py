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
from .keyinput import KeyReader
from .setup_wizard import ensure_music_dirs

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
            self.console.print(f"[bold red]配置错误:[/bold red] {e}")
            sys.exit(1)

        self.playlist = Playlist(mode=self.cfg.playback.playlist_mode)

        if music_path:
            target_dirs = [music_path]
        else:
            target_dirs = ensure_music_dirs(self.console, self.cfg, force_setup=force_setup)

        self.playlist.load_dirs(target_dirs)
        if not self.playlist.tracks:
            dirs_desc = "、".join(target_dirs) if target_dirs else "(未设置任何目录)"
            self.console.print(f"[bold yellow]警告:[/bold yellow] 在 [{dirs_desc}] 中未找到支持的音乐文件"
                                f"（支持 mp3/wav/flac/ogg/m4a/aiff）。")

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

        if self.playlist.tracks:
            self._load_current_track()

    # ---------------- 加载/切歌 ----------------
    def _load_current_track(self):
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
        self.player.load(path, eq_bands=self.cfg.equalizer.bands_db)

    def _on_track_end(self):
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
        self.playlist.next()
        self._load_current_track()
        if was_playing:
            self.player.play()

    def prev_track(self):
        if not self.playlist.tracks:
            return
        was_playing = self.player.state == PlayState.PLAYING
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
            self.player.set_eq_bands(self.cfg.equalizer.bands_db)
        # 同步 DSP 开关
        self.player.loudness_enabled = self.cfg.dsp.loudness.enabled
        self.player.vbe_enabled = self.cfg.dsp.vbe.enabled
        self.player.limiter_enabled = self.cfg.dsp.limiter.enabled
        self.player.loudness_target_lufs = self.cfg.dsp.loudness.target_lufs
        self.player._init_dsp_modules()

    def rerun_dir_setup(self):
        self._pending_dir_setup = True

    def _do_dir_setup(self):
        was_playing = self.player.state == PlayState.PLAYING
        self.player.pause()
        dirs = ensure_music_dirs(self.console, self.cfg, force_setup=True)
        self.playlist.load_dirs(dirs)
        if self.playlist.tracks:
            self._load_current_track()
            if was_playing:
                self.player.play()
        else:
            self.player.stop()
            self.current_track = None
            self.lyrics_data = None

    # ---------------- 主循环 ----------------
    def handle_key(self, key: str):
        if key is None:
            return
        if key == "q":
            self._running = False
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

                    if self.player.state == PlayState.PLAYING:
                        levels = self.spectrum.compute()
                    else:
                        levels = self.spectrum.silence_decay()

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

    app = App(cfg_path, args.path, force_setup=args.setup)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.player.stop()


if __name__ == "__main__":
    main()
