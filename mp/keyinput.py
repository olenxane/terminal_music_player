"""非阻塞键盘输入（Windows 用 msvcrt，类 Unix 用 termios/tty）"""
from __future__ import annotations
import sys
import select

try:
    import termios
    import tty
    _UNIX = True
except ImportError:
    _UNIX = False

try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    _WINDOWS = False

# Windows 扩展键扫描码 → 统一键名
_WIN_SCAN_MAP = {
    72: "UP", 80: "DOWN", 75: "LEFT", 77: "RIGHT",
    83: "DELETE", 71: "HOME", 79: "END",
    73: "PAGEUP", 81: "PAGEDOWN",
    59: "F1", 60: "F2", 61: "F3", 62: "F4",
    63: "F5", 64: "F6", 65: "F7", 66: "F8",
    67: "F9", 68: "F10",
}


class KeyReader:
    def __init__(self):
        self._fd = None
        self._old_settings = None

    def __enter__(self):
        if _WINDOWS:
            pass
        elif _UNIX and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if _UNIX and self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)

    def read_key(self, timeout: float = 0.0):
        """非阻塞读取一个按键，若无输入返回 None。
        支持方向键（返回 'UP'/'DOWN'/'LEFT'/'RIGHT'）。"""
        if _WINDOWS:
            return self._read_key_windows()
        if not _UNIX or not sys.stdin.isatty():
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            r2, _, _ = select.select([sys.stdin], [], [], 0.01)
            if r2:
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    r3, _, _ = select.select([sys.stdin], [], [], 0.01)
                    if r3:
                        ch3 = sys.stdin.read(1)
                        return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3)
            return "ESC"
        return ch

    def _read_key_windows(self):
        """Windows 非阻塞按键读取。
        方向键等特殊键使用 0xE0 (或 0x00) 前缀 + 扫描码。"""
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getch()
        code = ch[0] if isinstance(ch, bytes) else ord(ch)

        # 0xE0 或 0x00 前缀 → 扩展键（方向键、功能键等）
        if code == 0 or code == 224:
            if msvcrt.kbhit():
                ch2 = msvcrt.getch()
                code2 = ch2[0] if isinstance(ch2, bytes) else ord(ch2)
                return _WIN_SCAN_MAP.get(code2)
            return None

        # ESC
        if code == 27:
            return "ESC"

        # Ctrl+C → 视为退出
        if code == 3:
            return "q"

        # 常规 ASCII 字符
        try:
            return bytes([code]).decode("utf-8", errors="replace")
        except Exception:
            return None
