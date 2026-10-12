"""Standard-library platform support, also usable before the virtualenv exists."""

from contextlib import contextmanager
from pathlib import Path, PureWindowsPath, PurePosixPath
import ctypes
import errno
import os
import platform
import signal
import subprocess
import sys
import time


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
def file_lock(path, wait=False):
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
                    if not wait:
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

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise ValueError("Tools masih berjalan; tunggu sampai kunci bebas.") from exc
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


def configure_terminal():
    """Enable Windows VT processing before Rich writes ANSI; restore on exit."""
    if os.name != "nt":
        return True
    import atexit
    from ctypes import wintypes as w

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetStdHandle.argtypes = [w.DWORD]
    api.GetStdHandle.restype = w.HANDLE
    api.GetConsoleMode.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    api.GetConsoleMode.restype = w.BOOL
    api.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
    api.SetConsoleMode.restype = w.BOOL
    ready = True
    for number in (-11, -12):
        handle = api.GetStdHandle(number)
        mode = w.DWORD()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)) or not api.SetConsoleMode(
            handle, mode.value | 0x5
        ):
            ready = False
            continue
        atexit.register(api.SetConsoleMode, handle, mode.value)
    return ready
