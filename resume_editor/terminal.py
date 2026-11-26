"""PTY-backed shell session for the integrated terminal."""

import fcntl
import os
import pty
import queue
import shlex
import signal
import struct
import subprocess
import termios
import threading


class PtySession:
    def __init__(self, cwd, cmd=None, cols=80, rows=24):
        self.out = queue.Queue()
        self.exited = False
        master, slave = pty.openpty()
        self.master = master
        self._set_winsize(slave, rows, cols)
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        if cmd:
            argv = shlex.split(cmd)
        else:
            shell = os.environ.get("SHELL") or "/bin/bash"
            argv = [shell, "-l"]
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(cwd),
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _set_winsize(fd, rows, cols):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols, rows):
        try:
            self._set_winsize(self.master, rows, cols)
        except OSError:
            pass

    def write(self, data: str):
        try:
            os.write(self.master, data.encode("utf-8", "replace"))
        except OSError:
            pass

    def _read_loop(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            self.out.put(data)
        self.exited = True
        self.out.put(None)

    def drain(self):
        items = []
        try:
            while True:
                items.append(self.out.get_nowait())
        except queue.Empty:
            pass
        return items

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.close(self.master)
        except OSError:
            pass
