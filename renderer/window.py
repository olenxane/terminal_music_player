"""无边框透明渲染窗口：网格绘制、拖拽、键盘转发、鼠标穿透锁定。

- 背景 = bg_color + bg_opacity（逐像素半透明，文字不透明）
- 锁定 = Win32 WS_EX_TRANSPARENT，锁定后所有鼠标操作穿透到下层窗口
- 行级位图缓存：只有内容变化的行才重新渲染
"""
from __future__ import annotations

import ctypes

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from terminal_model import char_span

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE

# pyte 16 色名 → 终端标准色（播放器走 truecolor，此处仅兜底）
_NAMED_COLORS = {
    "black": "#000000", "red": "#cd0000", "green": "#00cd00",
    "brown": "#cdcd00", "yellow": "#cdcd00", "blue": "#0000ee",
    "magenta": "#cd00cd", "cyan": "#00cdcd", "white": "#e5e5e5",
    "bright_black": "#7f7f7f", "bright_red": "#ff0000",
    "bright_green": "#00ff00", "bright_yellow": "#ffff00",
    "bright_blue": "#5c5cff", "bright_magenta": "#ff00ff",
    "bright_cyan": "#00ffff", "bright_white": "#ffffff",
}

_KEYSEQ = {
    Qt.Key_Return: "\r", Qt.Key_Enter: "\r", Qt.Key_Backspace: "\x08",
    Qt.Key_Escape: "\x1b", Qt.Key_Tab: "\t",
    Qt.Key_Up: "\x1b[A", Qt.Key_Down: "\x1b[B",
    Qt.Key_Right: "\x1b[C", Qt.Key_Left: "\x1b[D",
    Qt.Key_Delete: "\x1b[3~", Qt.Key_Insert: "\x1b[2~",
    Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F",
    Qt.Key_PageUp: "\x1b[5~", Qt.Key_PageDown: "\x1b[6~",
    Qt.Key_F1: "\x1bOP", Qt.Key_F2: "\x1bOQ", Qt.Key_F3: "\x1bOR",
    Qt.Key_F4: "\x1bOS", Qt.Key_F5: "\x1b[15~", Qt.Key_F6: "\x1b[17~",
    Qt.Key_F7: "\x1b[18~", Qt.Key_F8: "\x1b[19~", Qt.Key_F9: "\x1b[20~",
    Qt.Key_F10: "\x1b[21~",
}


class RendererWindow(QWidget):
    key_forwarded = Signal(str)
    opacity_changed = Signal(float)
    position_committed = Signal(int, int)
    lock_changed = Signal(bool)

    def __init__(self, model, host, view_cfg, metrics):
        self._topmost = bool(view_cfg.get("always_on_top", True))
        # Qt.Tool：不占任务栏，作为桌面挂件存在（控制入口在托盘）
        flags = Qt.Tool | Qt.FramelessWindowHint
        if self._topmost:
            flags |= Qt.WindowStaysOnTopHint  # 构造时带上，避免被 Qt 显示流程覆盖
        super().__init__(None, flags)
        self._model = model
        self._host = host
        self._cfg = view_cfg
        self._cols = metrics["cols"]
        self._rows_auto = metrics["rows_auto"]
        self._cell_w = metrics["cell_w"]
        self._cell_h = metrics["cell_h"]
        self._auto_height = view_cfg.get("rows") == "auto"
        self._fixed_rows = None if self._auto_height else int(view_cfg["rows"])
        self._height_rows = self._rows_auto if self._auto_height else self._fixed_rows
        self._opacity = float(view_cfg.get("bg_opacity", 0.85))
        self._locked = bool(view_cfg.get("locked", False))

        self.setWindowTitle("MP 渲染器")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)

        font = QFont(view_cfg.get("font_family", "Cascadia Mono"),
                     int(view_cfg.get("font_size", 12)))
        font.setFixedPitch(True)
        font.setStyleHint(QFont.Monospace)
        self._font = font
        self._font_bold = QFont(font)
        self._font_bold.setBold(True)
        fm = QFontMetrics(font)
        self._ascent = fm.ascent()
        # 单元格实测值允许外部覆盖（metrics 已算好则以其为准）

        self._color_cache = {}
        self._row_cache = {}   # row_index -> (key, QImage)
        self._snapshot = None
        self._last_version = -1
        self._drag_from = None
        self._moved = False

        self.setFixedWidth(self._cols * self._cell_w)
        self.setFixedHeight(self._height_rows * self._cell_h)
        self._place_initially()

        refresh_ms = max(15, int(1000 / max(5, min(60, metrics.get("fps", 24)))))
        self._timer = QTimer(self)
        self._timer.setInterval(refresh_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._dirty = True
        # 状态变化（播放器退出/启动失败）必须立刻重绘横幅，不能等下一帧数据
        host.state_changed.connect(self._on_host_state)

    def _on_host_state(self, _state: str):
        self._dirty = True
        self.update()

    # ---------- 布局与颜色 ----------

    def _place_initially(self):
        from PySide6.QtGui import QGuiApplication
        x, y = self._cfg.get("x"), self._cfg.get("y")
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)
            return
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.move(avail.right() - self.width() - 24, avail.top() + 60)

    def _qt_color(self, spec: str) -> QColor:
        c = self._color_cache.get(spec)
        if c is None:
            if spec in ("default", ""):
                c = QColor(self._cfg.get("fg_color", "#e6edf3"))
            elif spec.startswith("#"):
                c = QColor(spec)
            elif spec in _NAMED_COLORS:
                c = QColor(_NAMED_COLORS[spec])
            else:
                c = QColor("#" + spec) if len(spec) == 6 else QColor(spec)
            self._color_cache[spec] = c
        return c

    # ---------- 帧刷新与绘制 ----------

    def _tick(self):
        if self._model.version == self._last_version and not self._dirty:
            return
        self._dirty = False
        self._last_version = self._model.version
        self._snapshot = self._model.snapshot()
        if self._auto_height:
            used = max(self._rows_auto, self._model.used_height())
            used = min(used, self._model.rows)
            if used != self._height_rows:
                self._height_rows = used
                self._row_cache.clear()
                self.setFixedHeight(self._height_rows * self._cell_h)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        bg = QColor(self._cfg.get("bg_color", "#10141a"))
        bg.setAlphaF(self._opacity)
        p.fillRect(self.rect(), bg)

        snap = self._snapshot
        if snap is None:
            p.setPen(QPen(QColor(200, 205, 215, 160)))
            p.setFont(self._font)
            p.drawText(20, self._ascent + 8, "正在启动播放器…")
        else:
            for y in range(min(self._height_rows, len(snap))):
                line = snap[y]
                key = tuple(line)
                cached = self._row_cache.get(y)
                if cached is None or cached[0] != key:
                    img = self._render_line(line)
                    self._row_cache[y] = (key, img)
                else:
                    img = cached[1]
                p.drawImage(0, y * self._cell_h, img)

        if not self._host.is_alive:
            self._draw_exit_banner(p)
        p.end()

    def _render_line(self, line) -> QImage:
        w, h = self._cell_w, self._cell_h
        img = QImage(self._cols * w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        baseline = self._ascent
        for i, cell in enumerate(line):
            if cell is None:
                continue
            span = char_span(cell.ch)
            x = i * w
            if cell.bg != "default":
                p.fillRect(x, 0, span * w, h, self._qt_color(cell.bg))
            if cell.ch and cell.ch != " ":
                p.setFont(self._font_bold if cell.bold else self._font)
                p.setPen(QPen(self._qt_color(cell.fg)))
                p.drawText(x, baseline, cell.ch)
        p.end()
        return img

    def _draw_exit_banner(self, p):
        band_h = self._cell_h * 2
        p.fillRect(0, self.height() - band_h, self.width(), band_h,
                   QColor(120, 30, 30, 150))
        p.setFont(self._font)
        p.setPen(QPen(QColor(255, 220, 220)))
        p.drawText(8, self.height() - band_h + self._ascent + 4,
                   "播放器已退出 · 托盘菜单可重载 / 保存代码自动重启")

    # ---------- 鼠标：拖拽 / 滚轮调透明度 ----------

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_from = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            self.activateWindow()
            self.raise_()

    def mouseMoveEvent(self, ev):
        if self._drag_from is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_from)
            self._moved = True

    def mouseReleaseEvent(self, ev):
        if self._drag_from is not None:
            self._drag_from = None
            self.position_committed.emit(self.x(), self.y())

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            step = 0.05 if ev.angleDelta().y() > 0 else -0.05
            self.set_opacity(self._opacity + step)
            self.opacity_changed.emit(self._opacity)
        else:
            ev.ignore()

    # ---------- 键盘：转发给播放器（经 ConPTY 还原成真实按键） ----------

    def keyPressEvent(self, ev):
        mods = ev.modifiers()
        key = ev.key()
        if mods & Qt.ControlModifier:
            if key == Qt.Key_C:
                self.key_forwarded.emit("\x03")  # 播放器把 Ctrl+C 视为 q
                return
            if key == Qt.Key_D:
                self.key_forwarded.emit("\x04")
                return
            return  # 其余 Ctrl 组合不转发，避免误触
        seq = _KEYSEQ.get(key)
        if seq is None:
            text = ev.text()
            seq = text if text and text.isprintable() else None
        if seq:
            self.key_forwarded.emit(seq)

    def inputMethodEvent(self, ev):
        # 中文输入法上屏（选择器搜索等场景）
        commit = ev.commitString()
        if commit:
            self.key_forwarded.emit(commit)

    # ---------- 锁定穿透 / 置顶 ----------

    def set_locked(self, v: bool):
        self._locked = bool(v)
        self._apply_ex_style()
        self.lock_changed.emit(self._locked)

    def set_topmost(self, v: bool):
        self._topmost = bool(v)
        self._apply_topmost()

    def set_opacity(self, v: float):
        self._opacity = max(0.05, min(1.0, float(v)))
        self.update()

    def _apply_ex_style(self):
        if not self.testAttribute(Qt.WA_WState_Created):
            return
        u = ctypes.windll.user32
        hwnd = int(self.winId())
        style = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if self._locked:
            style |= _WS_EX_TRANSPARENT | _WS_EX_LAYERED
        else:
            style &= ~_WS_EX_TRANSPARENT
        u.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)

    def _apply_topmost(self):
        if not self.testAttribute(Qt.WA_WState_Created):
            return
        u = ctypes.windll.user32
        hwnd = int(self.winId())
        u.SetWindowPos(hwnd, _HWND_TOPMOST if self._topmost else _HWND_NOTOPMOST,
                       0, 0, 0, 0, _SWP_FLAGS)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._apply_ex_style()
        self._apply_topmost()
