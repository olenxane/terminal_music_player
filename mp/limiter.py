"""自适应软限幅器（Adaptive Soft Limiter）。
零延迟版本：包络跟随器 + 软膝过渡，替代 np.clip 硬削波。
"""
from __future__ import annotations
import math
import numpy as np


class SoftLimiter:
    """零延迟软限幅器。

    包络跟随：瞬时攻击 + 指数释放。
    软膝：阈值附近平滑过渡，避免硬拐点。

    Seek 时调用 reset() 清空包络状态。
    """

    def __init__(self, fs: int,
                 threshold_db: float = -1.0,
                 release_ms: float = 50.0,
                 knee_db: float = 3.0):
        self.threshold = float(10 ** (threshold_db / 20.0))  # ≈0.891
        self.knee = float(knee_db)
        # 指数释放系数
        self.release_coeff = float(math.exp(-1.0 / (fs * release_ms / 1000.0)))
        self._envelope = 0.0
        self.enabled = True

    def process(self, data: np.ndarray) -> np.ndarray:
        """处理音频块。data: float32, 可能 (n,) 或 (n, channels)。"""
        if not self.enabled:
            return data

        # 取所有声道的最大绝对值作为包络源
        if data.ndim > 1:
            abs_max = np.max(np.abs(data), axis=1)
        else:
            abs_max = np.abs(data)

        gain = np.empty_like(abs_max)
        env = self._envelope

        for i in range(len(abs_max)):
            x = abs_max[i]
            # 包络跟随：瞬时攻击（取最大），平滑释放
            if x > env:
                env = x
            else:
                env = self.release_coeff * env + (1 - self.release_coeff) * x

            # 软膝过渡
            if env <= self.threshold:
                g = 1.0
            else:
                # 软膝区间：threshold ~ threshold * 10^(knee/20)
                knee_upper = self.threshold * (10 ** (self.knee / 20.0))
                if env >= knee_upper:
                    g = self.threshold / env
                else:
                    # 平滑过渡区
                    t = (env - self.threshold) / (knee_upper - self.threshold)
                    # 二次平滑
                    linear_gain = self.threshold / env
                    g = 1.0 + t * t * (linear_gain - 1.0)

            gain[i] = g

        self._envelope = env

        # 应用增益
        if data.ndim > 1:
            return data * gain[:, np.newaxis]
        else:
            return data * gain

    def reset(self):
        """清空包络状态（Seek 时调用）"""
        self._envelope = 0.0
