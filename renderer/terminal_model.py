"""pyte 虚拟终端：把子进程的 ANSI 输出流还原成带颜色的字符网格。

读线程调用 feed()（pywinpty 阻塞读），UI 线程调用 snapshot()/used_height()，
两者通过一把锁互斥。快照中宽字符的第二格输出 None（由首格一次画两个格宽）。
"""
from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass

import pyte


@dataclass(frozen=True)
class Cell:
    ch: str      # 字符；'' 表示宽字符占位格，不单独绘制
    fg: str      # 'default' 或十六进制（无 #）
    bg: str      # 'default' 或十六进制（无 #）
    bold: bool


def char_span(ch: str) -> int:
    """字符在终端网格中占的列数（东亚宽字符占 2 格）。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class TerminalModel:
    def __init__(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()
        self.version = 0  # 每次收到新数据 +1，UI 侧据此判断是否需要重绘

    def feed(self, data: str) -> None:
        with self._lock:
            self._stream.feed(data)
            self.version += 1

    def clear(self) -> None:
        """清屏（子进程重启时调用，避免旧画面残留）。"""
        with self._lock:
            self._screen.reset()
            self.version += 1

    def snapshot(self) -> list:
        """返回 rows×cols 的单元格矩阵，占位格为 None。"""
        with self._lock:
            buf = self._screen.buffer
            out = []
            for y in range(self.rows):
                row = buf[y]
                line = []
                for x in range(self.cols):
                    c = row.get(x)
                    if c is None or c.data == "":
                        line.append(None)
                    else:
                        line.append(Cell(c.data, c.fg or "default",
                                         c.bg or "default", bool(c.bold)))
                out.append(line)
            return out

    def used_height(self) -> int:
        """最后一处可见内容（非空白字符或非默认背景色）所在行 + 1。

        频谱静默时占位符是空格，不计入高度，因此调用方需与初始行数取 max。
        """
        with self._lock:
            buf = self._screen.buffer
            for y in range(self.rows - 1, -1, -1):
                row = buf[y]
                for x in range(self.cols):
                    c = row.get(x)
                    if c is None:
                        continue
                    if (c.data and c.data.strip()) or (c.bg and c.bg != "default"):
                        return y + 1
            return 0
