"""虚拟低音增强（Virtual Bass Enhancer）。
从低频信号中提取基频，通过非线性处理生成谐波，
频段限制后与原始信号混合，增强低频心理声学感知。
"""
from __future__ import annotations
import numpy as np

try:
    from scipy.signal import butter, lfilter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


class VirtualBassEnhancer:
    """VBE 处理器：HPF→LPF 提取低频→非线性谐波→BPF 限制→混合。

    所有 IIR 滤波器保存内部状态，支持流式分块处理。
    Seek 时调用 reset() 清空状态。
    """

    def __init__(self, fs: int, channels: int = 2,
                 gain_db: float = -3.0,
                 hpf_fc: float = 30.0,
                 lpf_fc: float = 120.0,
                 bpf_low: float = 125.0,
                 bpf_high: float = 275.0):
        if not _HAS_SCIPY:
            raise RuntimeError("VBE 需要 scipy")

        self.fs = fs
        self.channels = channels
        self.gain = float(10 ** (gain_db / 20.0))
        self.enabled = True

        # 滤波器系数
        self._b_hpf, self._a_hpf = butter(2, hpf_fc / (fs / 2), btype="high")
        self._b_lpf, self._a_lpf = butter(2, lpf_fc / (fs / 2), btype="low")
        self._b_bpf, self._a_bpf = butter(2, [bpf_low / (fs / 2), bpf_high / (fs / 2)],
                                           btype="band")

        # 每声道独立的滤波器状态（状态长度 = 滤波器系数长度 - 1）
        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(channels)]
        self._zi_bpf = [np.zeros(_zi_len(self._b_bpf, self._a_bpf)) for _ in range(channels)]

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: shape (n, channels) float32。
        返回与 data 相同形状的数组。"""
        if not self.enabled:
            return data

        out = data.copy()
        n_ch = min(out.shape[1] if out.ndim > 1 else 1, self.channels)

        for ch in range(n_ch):
            x = out[:, ch] if out.ndim > 1 else out

            # 低频提取：HPF 30Hz → LPF 120Hz
            bass, self._zi_hpf[ch] = lfilter(self._b_hpf, self._a_hpf, x, zi=self._zi_hpf[ch])
            bass, self._zi_lpf[ch] = lfilter(self._b_lpf, self._a_lpf, bass, zi=self._zi_lpf[ch])

            # 非线性谐波生成：对称软饱和
            b = np.clip(bass, -1.0, 1.0)
            nonlinear = b - (b ** 3) / 3.0
            harm = nonlinear - b  # 只保留非线性产生的附加分量

            # 谐波频段限制：BPF 125~275Hz
            harm, self._zi_bpf[ch] = lfilter(self._b_bpf, self._a_bpf, harm, zi=self._zi_bpf[ch])

            # 混合
            if out.ndim > 1:
                out[:, ch] = x + harm * self.gain
            else:
                out = x + harm * self.gain

        return out

    def reset(self):
        """清空所有滤波器状态（Seek 时调用）"""
        def _zi_len(b, a):
            return max(len(b), len(a)) - 1

        self._zi_hpf = [np.zeros(_zi_len(self._b_hpf, self._a_hpf)) for _ in range(self.channels)]
        self._zi_lpf = [np.zeros(_zi_len(self._b_lpf, self._a_lpf)) for _ in range(self.channels)]
        self._zi_bpf = [np.zeros(_zi_len(self._b_bpf, self._a_bpf)) for _ in range(self.channels)]
