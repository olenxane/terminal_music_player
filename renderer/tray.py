"""任务栏托盘：锁定穿透开关（左键即切换）、背景透明度、置顶、重载播放器、退出。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QSlider,
                               QSystemTrayIcon, QWidget, QWidgetAction)


def make_icon(size: int = 64) -> QIcon:
    """程序化绘制托盘图标：圆角深色底 + 音符，避免二进制资源文件。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#10141a"))
    p.setPen(QColor("#3ddc97"))
    p.drawRoundedRect(2, 2, size - 4, size - 4, size // 5, size // 5)
    f = QFont("Segoe UI Symbol", int(size * 0.55))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#3ddc97"))
    p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    quit_requested = Signal()

    def __init__(self, window, host, cfg):
        super().__init__()
        self._window = window
        self._host = host
        self._cfg = cfg

        self._tray = QSystemTrayIcon(make_icon())
        self._tray.setToolTip("MP 渲染器 — 播放器运行中")
        self._build_menu()
        # 不用 setContextMenu：Qt 内置弹出在屏幕底部会被任务栏截断，
        # 改为在 activated(Context) 里自行 exec（QMenu 自动翻转入屏）
        self._tray.activated.connect(self._on_activated)

        window.lock_changed.connect(self._act_lock.setChecked)
        window.opacity_changed.connect(self._sync_slider)
        host.state_changed.connect(self._on_state)

        self._tray.show()

    # ---------- 菜单 ----------

    def _build_menu(self):
        self._menu = QMenu()

        self._act_lock = QAction("锁定（鼠标穿透）", checkable=True)
        self._act_lock.setChecked(self._window._locked)
        self._act_lock.toggled.connect(self._window.set_locked)
        self._menu.addAction(self._act_lock)

        opacity_menu = self._menu.addMenu("背景透明度")
        widget = QWidget()
        lay = QHBoxLayout(widget)
        lay.setContentsMargins(10, 4, 10, 4)
        label = QLabel()
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(5, 100)
        self._slider.setValue(int(round(self._window._opacity * 100)))
        label.setText(f"{self._slider.value()}%")
        self._slider.valueChanged.connect(
            lambda v: (label.setText(f"{v}%"),
                       self._window.set_opacity(v / 100),
                       self._window.opacity_changed.emit(v / 100)))
        lay.addWidget(QLabel("透"))
        lay.addWidget(self._slider)
        lay.addWidget(label)
        wa = QWidgetAction(opacity_menu)
        wa.setDefaultWidget(widget)
        opacity_menu.addAction(wa)

        self._act_top = QAction("窗口置顶", checkable=True)
        self._act_top.setChecked(bool(self._cfg.data["window"].get("always_on_top", True)))
        self._act_top.toggled.connect(self._window.set_topmost)
        self._menu.addAction(self._act_top)

        self._menu.addSeparator()

        # 注意必须持有引用：无 parent 的 QAction 由 Python GC 管理，局部变量会被回收导致菜单丢项
        self._act_reload = QAction("重载播放器")
        self._act_reload.triggered.connect(lambda: self._host.restart())
        self._menu.addAction(self._act_reload)

        self._menu.addSeparator()
        self._act_quit = QAction("退出")
        self._act_quit.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(self._act_quit)

    def _sync_slider(self, v: float):
        val = int(round(v * 100))
        self._slider.blockSignals(True)
        self._slider.setValue(val)
        self._slider.blockSignals(False)

    def _on_activated(self, reason):
        # 左键单击托盘图标 = 快速切换锁定穿透；右键 = 弹出菜单
        if reason == QSystemTrayIcon.Trigger:
            self._window.set_locked(not self._window._locked)
        elif reason == QSystemTrayIcon.Context:
            self._popup_menu()

    def _popup_menu(self):
        # 托盘图标贴着屏幕底边，直接在光标处弹出会向下出屏；
        # 显式把菜单放到光标左上方并夹紧到屏幕可用区域
        from PySide6.QtGui import QCursor, QGuiApplication
        from PySide6.QtCore import QPoint
        menu = self._menu
        menu.adjustSize()
        cur = QCursor.pos()
        scr = QGuiApplication.screenAt(cur) or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry()
        x = cur.x() - menu.width()
        y = cur.y() - menu.height()
        x = max(avail.left(), min(x, avail.right() - menu.width()))
        y = max(avail.top(), min(y, avail.bottom() - menu.height()))
        menu.exec(QPoint(x, y))

    def _on_state(self, state: str):
        if state == "running":
            self._tray.setToolTip("MP 渲染器 — 播放器运行中")
        elif state == "error":
            self._tray.setToolTip("MP 渲染器 — 播放器启动失败")
        else:
            self._tray.setToolTip("MP 渲染器 — 播放器已退出")
