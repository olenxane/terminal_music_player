"""MP 渲染器入口。

流程：读 config.json → 起 QApplication → 按播放器 config.yaml 推导网格尺寸并用
字体度量换算成像素 → ConPTY 子进程运行播放器 → pyte 还原画面 → 透明窗口渲染。
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from pty_host import PtyHost, ReloadWatcher
from terminal_model import TerminalModel
from tray import TrayController, make_icon
from window import RendererWindow

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULTS = {
    "player": {
        "cwd": "..",
        "command": ["python", "main.py"],
        "player_config": "config.yaml",
        "graceful_key": "q",
        "auto_reload": True,
        "watch_interval_sec": 2.0,
        "watch_debounce_sec": 1.5,
        "watch_globs": ["mp/**/*.py", "*.py", "config.yaml"],
    },
    "window": {
        "cols": "auto",
        "rows": "auto",
        "pty_rows": 40,
        "font_family": "Cascadia Mono",
        "font_size": 12,
        "fg_color": "#e6edf3",
        "bg_color": "#10141a",
        "bg_opacity": 0.85,
        "always_on_top": True,
        "locked": False,
        "x": None,
        "y": None,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """config.json 读写：缺失键回落默认值，保存时原子替换。"""

    def __init__(self, path: Path):
        self.path = path
        loaded = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                loaded = {}
        self.data = _deep_merge(DEFAULTS, loaded)

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)


def _read_player_yaml(cfg: Config) -> dict:
    player_root = (BASE_DIR / cfg.data["player"]["cwd"]).resolve()
    cfg_path = player_root / cfg.data["player"].get("player_config", "config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def compute_metrics(cfg: Config) -> dict:
    """需求 3：窗口宽高在初始化时计算。

    cols="auto" → 播放器 config.yaml 的 playback.ui_width；
    rows="auto" → 主界面行数 3 +（频谱开启时 1+height）；
    像素尺寸 = 网格 × 字体单元格（字体需与终端一致才能像素级对齐）。
    """
    ycfg = _read_player_yaml(cfg)
    w = cfg.data["window"]
    wcfg_play = ycfg.get("playback", {}) or {}
    spec = ycfg.get("spectrum", {}) or {}

    cols = w["cols"] if isinstance(w["cols"], int) else int(wcfg_play.get("ui_width", 80))
    cols = max(30, cols)
    rows_auto = 3
    if spec.get("enabled", True):
        rows_auto += 1 + max(1, min(5, int(spec.get("height", 4))))

    font = QFont(w.get("font_family", "Cascadia Mono"), int(w.get("font_size", 12)))
    font.setFixedPitch(True)
    font.setStyleHint(QFont.Monospace)
    fm = QFontMetrics(font)

    pty_rows = int(w.get("pty_rows", 40))
    pty_rows = max(pty_rows, rows_auto + 2)
    if isinstance(w["rows"], int):
        pty_rows = max(pty_rows, w["rows"] + 2)

    return {
        "cols": cols,
        "rows_auto": rows_auto,
        "cell_w": fm.horizontalAdvance("M"),
        "cell_h": fm.height(),
        "pty_rows": pty_rows,
        "fps": int(wcfg_play.get("ui_fps", 20)),
    }


def main() -> int:
    cfg = Config(CONFIG_PATH)
    app = QApplication(sys.argv)
    app.setApplicationName("MP 渲染器")
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)

    # 字体度量依赖 QApplication，须先建 app 再计算尺寸
    metrics = compute_metrics(cfg)

    player_root = (BASE_DIR / cfg.data["player"]["cwd"]).resolve()
    model = TerminalModel(metrics["cols"], metrics["pty_rows"])
    host = PtyHost(cfg.data["player"]["command"], player_root,
                   (metrics["pty_rows"], metrics["cols"]), model,
                   graceful_key=cfg.data["player"].get("graceful_key", "q"))
    window = RendererWindow(model, host, cfg.data["window"], metrics)
    tray = TrayController(window, host, cfg)

    # ---- 持久化：位置 / 透明度 / 锁定 ----
    opacity_save = QTimer()
    opacity_save.setSingleShot(True)
    opacity_save.setInterval(800)

    def persist_window_state():
        w = cfg.data["window"]
        w["bg_opacity"] = round(window._opacity, 3)
        w["locked"] = window._locked
        w["x"], w["y"] = window.x(), window.y()
        cfg.save()

    opacity_save.timeout.connect(persist_window_state)
    window.opacity_changed.connect(lambda _v: opacity_save.start())
    window.position_committed.connect(lambda _x, _y: persist_window_state())
    window.lock_changed.connect(lambda _v: persist_window_state())

    # ---- 键盘输入 → 子进程 ----
    window.key_forwarded.connect(host.write)

    # ---- 意外退出自动重启（防语法错误死循环：运行满 10s 才自动拉起） ----
    def request_restart():
        host.restart()

    auto_reload = bool(cfg.data["player"].get("auto_reload", True))
    watcher = ReloadWatcher(
        player_root,
        cfg.data["player"].get("watch_globs", ["mp/**/*.py"]),
        cfg.data["player"].get("watch_interval_sec", 2.0),
        cfg.data["player"].get("watch_debounce_sec", 1.5),
        5.0,
        request_restart,
    )

    def on_state(state: str):
        if state == "exited" and auto_reload:
            uptime = time.time() - host.started_at
            if uptime > 10:
                QTimer.singleShot(1000, request_restart)

    host.state_changed.connect(on_state)

    def shutdown():
        watcher.stop()
        host.stop()
        persist_window_state()
        app.quit()

    tray.quit_requested.connect(shutdown)

    if auto_reload:
        watcher.start()
    if not host.start():
        pass  # 启动失败：窗口与托盘照常显示，可在托盘手动重载
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
