"""播放引擎：基于 ffmpeg 流式解码 + sounddevice 输出。
每个音频回调块都会先过均衡器，再输出到声卡；同时把处理后的
单声道混合数据喂给频谱分析器，供 UI 实时绘制。
"""
from __future__ import annotations
import threading
from enum import Enum
from typing import Optional, Callable

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

from .equalizer import Equalizer
from .spectrum import SpectrumAnalyzer
from .ffmpeg_decoder import FFmpegAudioFile, ffmpeg_available


class PlayState(Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class Player:
    def __init__(self, spectrum: SpectrumAnalyzer, block_size: int = 1024,
                 volume: float = 0.8, on_track_end: Optional[Callable] = None):
        if sd is None:
            raise RuntimeError(
                "缺少音频依赖，请先安装: pip install sounddevice"
            )
        if not ffmpeg_available():
            raise RuntimeError(
                "未找到 ffmpeg，请安装 ffmpeg 或运行 pip install imageio-ffmpeg"
            )
        self.spectrum = spectrum
        self.block_size = block_size
        self.volume = float(np.clip(volume, 0.0, 1.5))
        self.on_track_end = on_track_end

        self.state = PlayState.STOPPED
        self.equalizer: Optional[Equalizer] = None
        self._file: Optional[FFmpegAudioFile] = None
        self._stream: Optional[sd.OutputStream] = None
        self._lock = threading.Lock()
        self._frames_played = 0
        self._samplerate = 44100
        self._channels = 2
        self._duration_frames = 0
        self._path = ""
        self._eof_notified = False
        self._seek_target: Optional[int] = None

    # ---------- 属性 ----------
    @property
    def position_sec(self) -> float:
        with self._lock:
            return self._frames_played / self._samplerate if self._samplerate else 0.0

    @property
    def duration_sec(self) -> float:
        with self._lock:
            return self._duration_frames / self._samplerate if self._samplerate else 0.0

    @property
    def path(self) -> str:
        return self._path

    # ---------- 加载/控制 ----------
    def load(self, path: str, eq_bands=None):
        self.stop()
        self._file = FFmpegAudioFile(path)
        self._samplerate = self._file.samplerate
        self._channels = self._file.channels
        self._duration_frames = len(self._file)
        self._frames_played = 0
        self._path = path
        self._eof_notified = False
        self.equalizer = Equalizer(fs=self._samplerate,
                                    bands_db=eq_bands or [0.0] * 10,
                                    channels=self._channels)
        self.spectrum.fs = self._samplerate

    def play(self):
        if self._file is None:
            return
        if self._stream is not None and self.state == PlayState.PAUSED:
            self.state = PlayState.PLAYING
            return
        self._open_stream()
        self.state = PlayState.PLAYING

    def _open_stream(self):
        if self._stream is not None:
            self._stream.close()

        def callback(outdata, frames, time_info, status):
            if self.state != PlayState.PLAYING:
                outdata[:] = 0
                return
            with self._lock:
                if self._seek_target is not None:
                    self._file.seek(self._seek_target)
                    self._frames_played = self._seek_target
                    self._seek_target = None
                    if self.equalizer:
                        self.equalizer.reset()
                data = self._file.read(frames, dtype="float32", always_2d=True)
            n_read = data.shape[0]
            if n_read < frames:
                pad = np.zeros((frames - n_read, self._channels), dtype=np.float32)
                data = np.vstack([data, pad]) if n_read > 0 else pad
            if self.equalizer and self.equalizer.enabled:
                data = self.equalizer.process(data)
            data = np.clip(data * self.volume, -1.0, 1.0)
            outdata[:] = data
            with self._lock:
                self._frames_played += n_read
            mono = data.mean(axis=1) if data.ndim > 1 else data
            self.spectrum.feed(mono.astype(np.float32))
            if n_read < frames:
                if not self._eof_notified:
                    self._eof_notified = True
                    self.state = PlayState.STOPPED
                    if self.on_track_end:
                        threading.Thread(target=self.on_track_end, daemon=True).start()
                raise sd.CallbackStop()

        self._stream = sd.OutputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            blocksize=self.block_size,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def pause(self):
        if self.state == PlayState.PLAYING:
            self.state = PlayState.PAUSED

    def resume(self):
        if self.state == PlayState.PAUSED:
            self.state = PlayState.PLAYING

    def toggle_pause(self):
        if self.state == PlayState.PLAYING:
            self.pause()
        elif self.state == PlayState.PAUSED:
            self.resume()

    def stop(self):
        self.state = PlayState.STOPPED
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def seek(self, seconds: float):
        if self._file is None:
            return
        target_frame = int(max(0, min(seconds, self.duration_sec)) * self._samplerate)
        with self._lock:
            self._seek_target = target_frame

    def seek_relative(self, delta_seconds: float):
        self.seek(self.position_sec + delta_seconds)

    def set_volume(self, vol: float):
        self.volume = float(np.clip(vol, 0.0, 1.5))

    def volume_relative(self, delta: float):
        self.set_volume(self.volume + delta)

    def set_eq_bands(self, bands_db):
        if self.equalizer:
            self.equalizer.set_bands(bands_db)

    def toggle_eq(self):
        if self.equalizer:
            self.equalizer.enabled = not self.equalizer.enabled
            return self.equalizer.enabled
        return False
