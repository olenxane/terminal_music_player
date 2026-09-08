"""响度均衡：基于 ITU-R BS.1770-4 的 LUFS 测量与播放增益计算。
后台线程分析整曲，不创建缓存文件，分析结果仅存于 Player 内存。
"""
from __future__ import annotations
import math
import threading
import numpy as np
from typing import Optional

try:
    from scipy.signal import lfilter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ITU-R BS.1770-4 常量
_ABS_GATE_LUFS = -70.0
_REL_GATE_OFFSET_LU = -10.0
_BLOCK_MS = 400.0
_STEP_MS = 100.0


def _high_shelf_coeffs(f0: float, gain_db: float, q: float, fs: float):
    """RBJ cookbook high-shelf biquad 系数"""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / 2 * math.sqrt((A + 1 / A) * (1 / q - 1) + 2)
    cosw0 = math.cos(w0)

    b0 = A * ((A + 1) - (A - 1) * cosw0 + 2 * math.sqrt(A) * alpha)
    b1 = 2 * A * ((A - 1) - (A + 1) * cosw0)
    b2 = A * ((A + 1) - (A - 1) * cosw0 - 2 * math.sqrt(A) * alpha)
    a0 = (A + 1) + (A - 1) * cosw0 + 2 * math.sqrt(A) * alpha
    a1 = -2 * ((A - 1) + (A + 1) * cosw0)
    a2 = (A + 1) + (A - 1) * cosw0 - 2 * math.sqrt(A) * alpha
    return np.array([b0, b1, b2]) / a0, np.array([1, a1 / a0, a2 / a0])


def _high_pass_coeffs(f0: float, q: float, fs: float):
    """RBJ cookbook high-pass biquad 系数"""
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    cosw0 = math.cos(w0)

    b0 = (1 + cosw0) / 2
    b1 = -(1 + cosw0)
    b2 = (1 + cosw0) / 2
    a0 = 1 + alpha
    a1 = -2 * cosw0
    a2 = 1 - alpha
    return np.array([b0, b1, b2]) / a0, np.array([1, a1 / a0, a2 / a0])


class LoudnessAnalyzer:
    """ITU-R BS.1770-4 响度分析器。
    measure() 在后台线程调用，返回 (measured_lufs, gain_db)。
    不创建缓存文件，不修改原始音频。"""

    def __init__(self, target_lufs: float = -16.0,
                 max_boost: float = 6.0, max_cut: float = -12.0):
        self.target_lufs = target_lufs
        self.max_boost = max_boost
        self.max_cut = max_cut

    def _k_weighting_coeffs(self, fs: float):
        """生成 K-weighting 两级滤波器系数（pre-filter + RLB）"""
        # Pre-filter: high shelf ~1.5kHz, +4dB
        b_pre, a_pre = _high_shelf_coeffs(1500.0, 4.0, 0.707, fs)
        # RLB: high-pass ~38Hz
        b_rlb, a_rlb = _high_pass_coeffs(38.0, 0.5, fs)
        return (b_pre, a_pre), (b_rlb, a_rlb)

    def measure(self, audio_path: str, fs: int, channels: int,
                headers: dict | None = None,
                cancel_event: Optional[threading.Event] = None) -> tuple:
        """分析整曲响度，返回 (measured_lufs, gain_db)。
        失败时返回 (-inf, 0.0)。headers 透传给 FFmpegAudioFile（在线直链需要）。
        cancel_event 置位时提前终止分析并关闭 ffmpeg 子进程。"""
        if not _HAS_SCIPY:
            return float("-inf"), 0.0

        from .ffmpeg_decoder import FFmpegAudioFile

        (b_pre, a_pre), (b_rlb, a_rlb) = self._k_weighting_coeffs(fs)

        block_size = int(fs * _BLOCK_MS / 1000)
        step_size = int(fs * _STEP_MS / 1000)

        # 滤波器状态（每声道独立）
        zi_pre = [np.zeros(2) for _ in range(channels)]
        zi_rlb = [np.zeros(2) for _ in range(channels)]

        # 累积器：每个 block 的加权均方能量
        block_means = []

        # 环形缓冲区，跨 block 重叠
        overlap_buf = np.zeros((0, channels), dtype=np.float32)

        try:
            f = FFmpegAudioFile(audio_path, headers=headers)
        except Exception:
            return float("-inf"), 0.0

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return float("-inf"), 0.0
                chunk = f.read(block_size, dtype="float32", always_2d=True)
                if chunk.shape[0] == 0:
                    break

                # K-weighting：每声道独立滤波
                weighted = np.empty_like(chunk)
                for ch in range(min(channels, chunk.shape[1])):
                    x = chunk[:, ch]
                    x, zi_pre[ch] = lfilter(b_pre, a_pre, x, zi=zi_pre[ch])
                    x, zi_rlb[ch] = lfilter(b_rlb, a_rlb, x, zi=zi_rlb[ch])
                    weighted[:, ch] = x

                # 拼接重叠缓冲区
                combined = np.vstack([overlap_buf, weighted]) if overlap_buf.shape[0] > 0 else weighted

                # 提取 block
                n_blocks = (combined.shape[0] - block_size) // step_size + 1
                for i in range(n_blocks):
                    start = i * step_size
                    block = combined[start:start + block_size]
                    # 加权均方：各声道等权重（立体声 L=R=1.0）
                    ms = np.mean(block ** 2)
                    block_means.append(ms)

                # 保留重叠部分
                used = n_blocks * step_size if n_blocks > 0 else 0
                overlap_buf = combined[used:] if used < combined.shape[0] else np.zeros((0, channels), dtype=np.float32)

                if chunk.shape[0] < block_size:
                    break
        except Exception:
            return float("-inf"), 0.0
        finally:
            f.close()

        if not block_means:
            return float("-inf"), 0.0

        # 转换为 LUFS
        block_lufs = []
        for ms in block_means:
            if ms > 0:
                block_lufs.append(-0.691 + 10 * math.log10(ms))
            else:
                block_lufs.append(float("-inf"))

        # 绝对门限 -70 LUFS
        gated = [l for l in block_lufs if l > _ABS_GATE_LUFS]
        if not gated:
            return float("-inf"), 0.0

        # 相对门限
        mean_lufs = sum(gated) / len(gated)
        rel_gate = mean_lufs + _REL_GATE_OFFSET_LU
        gated2 = [l for l in gated if l > rel_gate]
        if not gated2:
            gated2 = gated

        integrated = sum(gated2) / len(gated2)
        gain_db = self.target_lufs - integrated
        gain_db = float(np.clip(gain_db, self.max_cut, self.max_boost))

        return integrated, gain_db
