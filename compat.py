"""Standard-library platform support, also usable before the virtualenv exists."""

from contextlib import contextmanager
from pathlib import Path, PureWindowsPath, PurePosixPath
import ctypes
import errno
import os
import platform
import re
import signal
import subprocess
import sys
import time


# Credentials of this tool's own updater. The processes it starts (yt-dlp, FFmpeg, pip) never need
# them, so they do not inherit them.
SECRET_ENVIRONMENT = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def clean_environment(extra=None, environ=None):
    """The environment for a child process: ours, without GitHub credentials, plus `extra`."""
    source = os.environ if environ is None else environ
    result = {k: v for k, v in source.items() if k.upper() not in SECRET_ENVIRONMENT}
    result.update(extra or {})
    return result


def folder_size(path):
    """Bytes in a folder tree. Links are counted as links and never followed."""
    total = 0
    for folder, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def platform_key():
    family = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(
        platform.system(), platform.system().lower()
    )
    arch = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(platform.machine().lower(), platform.machine().lower())
    return family + "-" + arch


def python_path(root):
    return Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def deno_path(root):
    return Path(root) / ".runtime/bin" / ("deno.exe" if os.name == "nt" else "deno")


def default_media():
    if sys.platform == "darwin":
        return str(Path.home() / "Movies/YouTube")
    if os.name == "nt":
        # Respect a relocated Windows Videos known folder.
        import uuid

        guid = (ctypes.c_byte * 16).from_buffer_copy(
            uuid.UUID("18989b1d-99b5-455b-841c-ab7c74e4ddfc").bytes_le
        )
        value = ctypes.c_wchar_p()
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        shell.SHGetKnownFolderPath.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(value)) == 0:
            try:
                return str(Path(value.value) / "YouTube")
            finally:
                ole.CoTaskMemFree(ctypes.cast(value, ctypes.c_void_p))
        return str(Path.home() / "Videos/YouTube")
    return "~/Videos/YouTube"


def foreign_path(value):
    win = PureWindowsPath(str(value))
    return (
        not win.is_absolute() and PurePosixPath(str(value)).is_absolute()
        if os.name == "nt"
        else bool(win.drive)
    )


def native_output(value):
    if foreign_path(value):
        raise ValueError(
            "Jalur media berasal dari OS lain. Salin media lalu gunakan maintenance.py rebase-media --from OLD --to NEW."
        )
    return Path(value).expanduser().resolve()


@contextmanager
def file_lock(path, wait=False, grace=0.0):
    """An exclusive lock on `path`. Without `wait` it is refused at once, or after `grace`
    seconds of trying again (for a lock that is about to be released)."""
    deadline = time.monotonic() + grace
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not wait and time.monotonic() >= deadline:
                        raise ValueError(
                            "Tools masih berjalan; tunggu sampai kunci bebas."
                        ) from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            "Tools masih berjalan; tunggu sampai kunci bebas."
                        ) from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class WindowsJob:
    """Contain the worker and every descendant; closing our handle kills leftovers."""

    def __init__(self, pid):
        from ctypes import wintypes as w

        size = ctypes.c_size_t
        u64 = ctypes.c_ulonglong

        class Basic(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_longlong),
                ("JobTime", ctypes.c_longlong),
                ("Flags", w.DWORD),
                ("MinWorking", size),
                ("MaxWorking", size),
                ("Active", w.DWORD),
                ("Affinity", size),
                ("Priority", w.DWORD),
                ("Scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, u64)
                for name in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("IO", IO),
                ("ProcessMemory", size),
                ("JobMemory", size),
                ("PeakProcess", size),
                ("PeakJob", size),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
        ]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.TerminateJobObject.restype = w.BOOL
        self.api.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_void_p,
        ]
        self.api.QueryInformationJobObject.restype = w.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = Extended()
            limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if not self.handle:
            return
        from ctypes import wintypes as w

        class Accounting(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_longlong)
                for name in ("User", "Kernel", "PeriodUser", "PeriodKernel")
            ] + [(name, w.DWORD) for name in ("Faults", "Total", "Active", "Terminated")]

        try:
            self.api.TerminateJobObject(self.handle, 130)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                info = Accounting()
                if (
                    not self.api.QueryInformationJobObject(
                        self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
                    )
                    or info.Active == 0
                ):
                    break
                time.sleep(0.02)
        finally:
            self.api.CloseHandle(self.handle)
            self.handle = None


class ProcessTree:
    def __init__(self, process):
        self.stopped = False
        self.process = process
        self.job = WindowsJob(process.pid) if os.name == "nt" else None

    def stop(self):
        if self.stopped:
            return
        p = self.process
        if os.name == "nt":
            if p.poll() is None:
                try:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.close()
            if p.poll() is None:
                p.kill()
            p.wait(timeout=3)
        else:
            # The group may outlive its original worker, so do not return on poll().
            for sig, timeout in ((signal.SIGINT, 5), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # BSD can report EPERM while the group leader is becoming a zombie.
                    try:
                        p.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        raise
                    break
                try:
                    p.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    continue
                # Kill remaining descendants even if the worker exited normally.
                if sig != signal.SIGKILL:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        if p.poll() is None:
                            raise
                break
            p.wait(timeout=3)
        self.stopped = True

    def close(self):
        if self.job:
            self.job.close()
            self.job = None


def worker_start():
    # No extraction/FFmpeg starts before the parent's process containment is ready.
    if sys.stdin.readline().strip() != "START":
        raise ValueError("Worker tidak mendapat izin mulai")
    if os.name == "nt":

        def interrupt(signum, frame):
            raise KeyboardInterrupt()

        signal.signal(signal.SIGBREAK, interrupt)


def environment_identity(root):
    return {
        "root": str(Path(root).resolve()),
        "platform": platform_key(),
        "python": ".".join(map(str, sys.version_info[:2])),
    }


def windows_build():
    """(major, minor, build) of this Windows, or None on any other system."""
    if os.name != "nt":
        return None
    version = sys.getwindowsversion()
    return (version.major, version.minor, version.build)


def _console_api():
    from ctypes import wintypes as w

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetStdHandle.argtypes = [w.DWORD]
    api.GetStdHandle.restype = w.HANDLE
    api.GetConsoleMode.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    api.GetConsoleMode.restype = w.BOOL
    api.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
    api.SetConsoleMode.restype = w.BOOL
    return api, w


def _enable_vt(api, w, number):
    """Switch VT processing on for one standard handle; False when it is not a console."""
    import atexit

    handle = api.GetStdHandle(number)
    mode = w.DWORD()
    if not api.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    if not api.SetConsoleMode(handle, mode.value | 0x5):
        return False
    atexit.register(api.SetConsoleMode, handle, mode.value)
    return True


def configure_terminal(windows=None):
    """True when the display (stderr) takes ANSI escape sequences; Windows needs VT processing
    switched on first, and it is restored on exit.

    Only stderr decides: every line of this tool's display is written there. stdout (the saved
    paths go there) is switched on too, so a menu can draw when it is a console, but a stdout
    that is redirected must not turn the display plain."""
    if not (os.name == "nt" if windows is None else windows):
        return True
    api, w = _console_api()
    _enable_vt(api, w, -11)
    return _enable_vt(api, w, -12)


def stdout_is_console(windows=None):
    """True when stdout is a terminal. On Windows the console itself is asked: isatty() also
    says yes for NUL, and the interactive menus draw through the stdout console."""
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if not (os.name == "nt" if windows is None else windows):
        return True
    api, w = _console_api()
    return bool(api.GetConsoleMode(api.GetStdHandle(-11), ctypes.byref(w.DWORD())))


MAX_LOG_BYTES = 5 * 1024**2


def rotate_log(path, limit=MAX_LOG_BYTES):
    """Keep one previous copy (path + '.1') so an append-only log cannot grow without bound."""
    path = Path(path)
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass  # a missing or busy log is not a reason to stop a download


def lock_requirements(text):
    """Pinned requirements of a pip lock file, one string per package.

    Backslash continuations are joined and --hash options and comments are dropped, so the
    same parser serves both hashed and plain locks. Environment markers are kept."""
    logical = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        logical.append(current + line)
        current = ""
    if current:
        logical.append(current)
    result = []
    for line in logical:
        line = re.sub(r"--hash=\S+", "", line.split(" #", 1)[0])
        line = " ".join(line.split())
        if line and not line.startswith("#") and "==" in line:
            result.append(line)
    return result
