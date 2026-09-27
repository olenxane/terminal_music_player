"""ConPTY 子进程托管：启动播放器、读取输出流、转发键盘输入、优雅/强制重启。

渲染器与播放器零代码耦合：只按 config.json 里的命令行拉起子进程，
解析标准终端输出流。播放器代码更新后重启子进程即加载新版本。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import glob as _glob
import os

from PySide6.QtCore import QObject, QTimer, Signal

_PY_ALIASES = {"python", "python3", "python.exe", "python3.exe"}


class PtyHost(QObject):
    """一个播放器子进程会话。

    state_changed：running=已拉起；stopped=主动停止/重载；exited=意外退出；
    error=启动失败。信号可能在读线程发出（Qt 自动排队）。
    """

    state_changed = Signal(str)  # running / stopped / exited / error

    def __init__(self, command, cwd, dimensions, model, graceful_key="q"):
        super().__init__()
        self._command = list(command)
        self._cwd = str(cwd)
        self._rows, self._cols = dimensions
        self._model = model
        self._graceful_key = graceful_key
        self._proc = None
        self._alive = False
        self._stopping = False
        self._gen = 0          # 会话代数：旧读线程不得给新会话发状态
        self._reader_thread = None
        self._lock = threading.Lock()
        self.started_at = 0.0

    @property
    def is_alive(self) -> bool:
        return self._alive

    def start(self) -> bool:
        with self._lock:
            if self._alive:
                return False
            self._stopping = False
            argv = list(self._command)
            if argv and argv[0].lower() in _PY_ALIASES:
                # 用渲染器自己的解释器启动，保证与播放器依赖环境一致
                argv[0] = sys.executable
            try:
                from winpty import PtyProcess
                self._proc = PtyProcess.spawn(
                    argv, cwd=self._cwd, dimensions=(self._rows, self._cols))
            except Exception as e:
                self._last_error = str(e)
                self.state_changed.emit("error")
                return False
            self._alive = True
            self._gen += 1
            gen = self._gen
            proc = self._proc
            self.started_at = time.time()
            self._model.clear()  # 重启场景下清掉上个进程的残留画面
        self.state_changed.emit("running")
        self._reader_thread = threading.Thread(
            target=self._reader, args=(proc, gen), daemon=True)
        self._reader_thread.start()
        return True

    def _reader(self, proc, gen):
        try:
            while proc.isalive():
                try:
                    data = proc.read()  # 阻塞读，进程退出后返回空或抛错
                except Exception:
                    break
                if not data:
                    break
                self._model.feed(data)
        finally:
            with self._lock:
                if gen != self._gen:
                    return  # 旧会话的读线程，新会话已在运行，不发状态
                self._alive = False
                emit = "stopped" if self._stopping else "exited"
            self.state_changed.emit(emit)

    def write(self, text: str) -> None:
        with self._lock:
            if self._alive and self._proc is not None:
                try:
                    self._proc.write(text)
                except Exception:
                    pass

    def stop(self, timeout: float = 3.0) -> None:
        """先发优雅退出键，超时则按进程树强杀（连带 ffmpeg 等孙进程）。

        无论读线程是否从阻塞 read() 中醒来，最后都兜底关闭 pty 并置 _alive=False，
        否则下次 start() 会被"已在运行"守卫挡住，导致重载后子进程拉不起来。
        """
        with self._lock:
            proc = self._proc
            if proc is None or not self._alive:
                return
            self._stopping = True
        try:
            proc.write(self._graceful_key)
        except Exception:
            pass
        t0 = time.time()
        while proc.isalive() and time.time() - t0 < timeout:
            time.sleep(0.05)
        if proc.isalive():
            self._kill_tree(proc.pid)
        try:
            proc.close()  # 解除读线程可能卡住的阻塞 read()
        except Exception:
            pass
        t0 = time.time()
        while self._alive and self._reader_thread is not None \
                and self._reader_thread.is_alive() and time.time() - t0 < 2.0:
            time.sleep(0.05)
        with self._lock:
            self._alive = False
            self._proc = None

    def restart(self) -> bool:
        self.stop()
        return self.start()

    @staticmethod
    def _kill_tree(pid: int) -> None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass


class ReloadWatcher(QObject):
    """轮询播放器源码文件 mtime，变更防抖后回调（代码更新 → 自动重启加载新版）。"""

    def __init__(self, root, patterns, interval_sec: float,
                 debounce_sec: float, cooldown_sec: float, on_reload):
        super().__init__()
        self._root = str(root)
        self._patterns = list(patterns)
        self._debounce = max(0.2, debounce_sec) * 1000
        self._cooldown = max(0.0, cooldown_sec) * 1000
        self._on_reload = on_reload
        self._last_reload_ms = 0.0
        self._snapshot = self._scan()
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._fire)
        self._timer = QTimer(self)
        self._timer.setInterval(max(0.5, interval_sec) * 1000)
        self._timer.timeout.connect(self._check)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._debounce_timer.stop()

    def _scan(self) -> dict:
        out = {}
        for pat in self._patterns:
            for path in _glob.glob(os.path.join(self._root, pat), recursive=True):
                if os.path.isfile(path):
                    try:
                        out[path] = os.path.getmtime(path)
                    except OSError:
                        pass
        return out

    def _check(self):
        cur = self._scan()
        if cur != self._snapshot:
            self._snapshot = cur
            self._debounce_timer.start(self._debounce)  # 保存中的连续变更只触发一次

    def _fire(self):
        import time as _t
        now = _t.monotonic() * 1000
        if now - self._last_reload_ms < self._cooldown:
            return
        self._last_reload_ms = now
        self._on_reload()
