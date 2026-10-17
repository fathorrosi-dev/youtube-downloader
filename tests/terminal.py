"""Real POSIX PTY / Windows ConPTY adapter used only by tests."""

import codecs
import os
import signal
import subprocess
import time


class Terminal:
    def __init__(self, args, cwd, extra=None, no_color=False, width=100):
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            # A terminal that shows 24-bit color. A test of another depth overrides it in `extra`.
            "COLORTERM": "truecolor",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            **(extra or {}),
        }
        if "NO_COLOR" not in (extra or {}):
            env.pop("NO_COLOR", None)
        if no_color:
            env["NO_COLOR"] = "1"
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        if os.name == "nt":
            from winpty import PtyProcess, Backend

            self.p = PtyProcess.spawn(
                args, cwd=str(cwd), env=env, dimensions=(30, width), backend=str(Backend.ConPTY)
            )
        else:
            import pty
            import fcntl
            import termios
            import struct

            self.fd, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, width, 0, 0))
            self.p = subprocess.Popen(
                args, cwd=cwd, stdin=slave, stdout=slave, stderr=slave, env=env
            )
            os.close(slave)

    def running(self):
        return self.p.isalive() if os.name == "nt" else self.p.poll() is None

    def read(self, timeout=0.1):
        import select

        if os.name == "nt":
            if not select.select([self.p.fileobj], [], [], timeout)[0]:
                return ""
            try:
                return self.p.read(65536)
            except (EOFError, OSError):
                return ""
        if not select.select([self.fd], [], [], timeout)[0]:
            return ""
        try:
            return self.decoder.decode(os.read(self.fd, 65536))
        except OSError:
            return ""

    def write(self, value):
        if os.name == "nt":
            self.p.write(value)
        else:
            os.write(self.fd, value.encode("utf-8"))

    def interrupt(self):
        if os.name == "nt":
            self.p.sendintr()
        else:
            self.p.send_signal(signal.SIGINT)

    def wait(self):
        deadline = time.monotonic() + 5
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.running():
            raise RuntimeError("Terminal process did not exit")
        return self.p.exitstatus if os.name == "nt" else self.p.returncode

    def drain(self):
        result = ""
        for _ in range(30):
            chunk = self.read(0.1)
            if not chunk:
                break
            result += chunk
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            self.p.close(force=True)
        else:
            if self.running():
                self.p.kill()
                self.p.wait()
            os.close(self.fd)
