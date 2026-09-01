"""播放引擎：基于 ffmpeg 流式解码 + sounddevice 输出。

DSP 链（可选，按配置逐级启用，全部关闭时保持原有 EQ+clip 行为）：
    LUFS响度补偿 → EQ → VBE虚拟低音 → 用户音量 → 软限幅器 → Output
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

# ---- 可选 DSP 模块（缺失/不可用不影响主播放链路） ----
try:
    from .loudness import LoudnessAnalyzer
    _HAS_LOUDNESS = True
except ImportError:
    _HAS_LOUDNESS = False

try:
    from .vbe import VirtualBassEnhancer
    _HAS_VBE = True
except ImportError:
    _HAS_VBE = False

try:
    from .limiter import SoftLimiter
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False


class PlayState(Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class Player:
    def __init__(self, spectrum: SpectrumAnalyzer, block_size: int = 1024,
                 volume: float = 0.8, on_track_end: Optional[Callable] = None,
                 dsp_config=None):
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

        # DSP 配置（各模块可独立开关）
        self.dsp_config = dsp_config
        self.loudness_enabled = bool(dsp_config and dsp_config.loudness.enabled)
        self.vbe_enabled = bool(dsp_config and dsp_config.vbe.enabled)
        self.limiter_enabled = bool(dsp_config and dsp_config.limiter.enabled)
        self.loudness_target_lufs = -16.0
        if dsp_config:
            self.loudness_target_lufs = dsp_config.loudness.target_lufs

        self.state = PlayState.STOPPED
        self.equalizer: Optional[Equalizer] = None
        self.vbe = None
        self.limiter = None
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

        # 响度分析（后台线程）
        self.loudness_gain_db = 0.0
        self._loudness_generation = 0
        self._loudness_task: Optional[threading.Thread] = None

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
    def load(self, path: str, eq_bands=None, q_values=None):
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
                                    q_values=q_values,
                                    channels=self._channels)
        self.spectrum.fs = self._samplerate

        # 初始化可选 DSP 模块
        self._init_dsp_modules()

        # 后台启动响度分析（不阻塞播放）
        self._start_loudness_analysis(path)

    def _init_dsp_modules(self):
        """按配置初始化 VBE / Limiter（模块缺失或关闭则跳过）"""
        self.vbe = None
        self.limiter = None
        if self.vbe_enabled and _HAS_VBE:
            try:
                self.vbe = VirtualBassEnhancer(
                    fs=self._samplerate, channels=self._channels,
                    gain_db=self.dsp_config.vbe.gain_db,
                )
            except Exception:
                self.vbe = None
        if self.limiter_enabled and _HAS_LIMITER:
            try:
                self.limiter = SoftLimiter(
                    fs=self._samplerate,
                    threshold_db=self.dsp_config.limiter.threshold_db,
                    release_ms=self.dsp_config.limiter.release_ms,
                )
            except Exception:
                self.limiter = None

    def _start_loudness_analysis(self, path: str):
        """后台线程分析整曲响度，generation 防止旧结果覆盖新歌"""
        if not (self.loudness_enabled and _HAS_LOUDNESS):
            return
        self.loudness_gain_db = 0.0
        self._loudness_generation += 1
        gen = self._loudness_generation
        self._loudness_task = threading.Thread(
            target=self._analyze_loudness,
            args=(path, self._samplerate, self._channels, gen),
            daemon=True,
        )
        self._loudness_task.start()

    def _analyze_loudness(self, path: str, fs: int, channels: int, gen: int):
        """分析完成后仅在 generation 匹配时应用增益"""
        try:
            analyzer = LoudnessAnalyzer(target_lufs=self.loudness_target_lufs)
            _lufs, gain_db = analyzer.measure(path, fs, channels)
            if gen == self._loudness_generation:
                self.loudness_gain_db = gain_db
        except Exception:
            pass  # 分析失败保持 0dB，正常播放

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
                    if self.vbe:
                        self.vbe.reset()
                    if self.limiter:
                        self.limiter.reset()
                data = self._file.read(frames, dtype="float32", always_2d=True)
            n_read = data.shape[0]
            if n_read < frames:
                pad = np.zeros((frames - n_read, self._channels), dtype=np.float32)
                data = np.vstack([data, pad]) if n_read > 0 else pad

            # ---- DSP 链（每级可选）----
            # 1. LUFS 响度补偿
            if self.loudness_enabled and self.loudness_gain_db != 0.0:
                data = data * float(10 ** (self.loudness_gain_db / 20.0))

            # 2. EQ（原有，恒可用）
            if self.equalizer and self.equalizer.enabled:
                data = self.equalizer.process(data)

            # 3. VBE 虚拟低音
            if self.vbe and self.vbe.enabled:
                data = self.vbe.process(data)

            # 4. 用户音量
            data = data * self.volume

            # 5. 软限幅器（替代硬削波）；关闭时回退 np.clip
            if self.limiter and self.limiter.enabled:
                data = self.limiter.process(data)
            else:
                data = np.clip(data, -1.0, 1.0)

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

    # ---------- DSP 控制接口 ----------
    def set_eq_bands(self, bands_db, q_values=None):
        if self.equalizer:
            self.equalizer.set_bands(bands_db, q_values=q_values)

    def toggle_eq(self):
        if self.equalizer:
            self.equalizer.enabled = not self.equalizer.enabled
            return self.equalizer.enabled
        return False

    def toggle_loudness(self):
        self.loudness_enabled = not self.loudness_enabled
        return self.loudness_enabled

    def set_loudness_target(self, lufs: float):
        self.loudness_target_lufs = float(lufs)

    def toggle_vbe(self):
        self.vbe_enabled = not self.vbe_enabled
        if self.vbe_enabled and self.vbe is None and _HAS_VBE:
            self._init_dsp_modules()
        return self.vbe_enabled

    def set_vbe_gain(self, db: float):
        if self.vbe:
            self.vbe.gain = float(10 ** (db / 20.0))

    def toggle_limiter(self):
        self.limiter_enabled = not self.limiter_enabled
        if self.limiter_enabled and self.limiter is None and _HAS_LIMITER:
            self._init_dsp_modules()
        return self.limiter_enabled
