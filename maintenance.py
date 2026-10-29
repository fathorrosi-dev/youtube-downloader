"""Local setup, release install, backup, rollback and package migration."""

import sysconfig
import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import tarfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path, PurePosixPath
from compat import (
    clean_environment,
    file_lock,
    python_path,
    deno_path,
    platform_key,
    environment_identity,
    folder_size,
    lock_requirements,
    rotate_log,
)

BASE = Path(__file__).resolve().parent


def guard(root, wait=False, grace=0.0):
    return file_lock(Path(root) / ".state/run.lock", wait, grace)


def spinner_style(environ=None, stream=None, windows=None):
    """(frames, colour for the spinner, colour for the text, reset) of the setup spinner.

    Braille frames and the tool's pink and sky blue where the terminal can show them; the plain
    |/-\\ spinner without colour on a legacy Windows console or without UTF-8. Setup runs before
    the dependencies exist, so this writes the escape sequences itself."""
    environ = os.environ if environ is None else environ
    stream = sys.stderr if stream is None else stream
    windows = (os.name == "nt") if windows is None else windows
    modern = (not windows or bool(environ.get("WT_SESSION"))) and "utf" in (
        getattr(stream, "encoding", "") or ""
    ).lower()
    if not modern:
        return "|/-\\", "", "", ""
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    if environ.get("NO_COLOR"):
        return frames, "", "", ""
    light = (environ.get("YTDL_THEME") or "").strip().lower() == "light"
    if (environ.get("COLORTERM") or "").strip().lower() in ("truecolor", "24bit"):
        pink, blue = (
            ((201, 32, 95), (11, 111, 164)) if light else ((255, 194, 212), (168, 216, 240))
        )
        return frames, "\x1b[38;2;%d;%d;%dm" % pink, "\x1b[38;2;%d;%d;%dm" % blue, "\x1b[0m"
    pink, blue = (
        ("\x1b[38;5;161m", "\x1b[38;5;25m") if light else ("\x1b[38;5;218m", "\x1b[38;5;153m")
    )
    return frames, pink, blue, "\x1b[0m"


@contextmanager
def working(message):
    """A one-line spinner with the elapsed seconds while a long step runs, so a step that takes
    a minute does not look like a hang. Silent when stderr is not a terminal; with
    YTDL_MOTION=off it only names the step once instead of animating."""
    if not sys.stderr.isatty() or os.environ.get("TERM") == "dumb":
        yield
        return
    if (os.environ.get("YTDL_MOTION") or "").strip().lower() in ("off", "0", "false", "no"):
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        yield
        return
    stop = threading.Event()
    width = len(message) + 12
    frames, accent, text, reset = spinner_style()

    def spin():
        started, turn = time.monotonic(), 0
        while not stop.wait(0.1):
            frame = frames[turn % len(frames)]
            turn += 1
            sys.stderr.write(
                f"\r{accent}{frame}{reset} {text}{message}{reset} · {int(time.monotonic() - started)}d"
            )
            sys.stderr.flush()

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        sys.stderr.write("\r" + " " * width + "\r")
        sys.stderr.flush()


def run_logged(root, args, message=None):
    rotate_log(root / ".state/setup.log")
    with (root / ".state/setup.log").open("a", encoding="utf-8") as log:
        with working(message) if message else nullcontext():
            result = subprocess.run(
                args,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=clean_environment({"PIP_CACHE_DIR": str(root / ".state/pip-cache")}),
            )
    if result.returncode:
        raise ValueError("Pemeliharaan gagal. Periksa " + str(root / ".state/setup.log"))


def runtime_asset(runtime):
    assets = runtime["assets"]
    key = platform_key()
    return assets.get(key) or (assets.get(platform.machine()) if key.startswith("linux-") else None)


def fetch(url, archive):
    """Download `url` into `archive`, continuing a partial file after a dropped connection.

    urllib does not report a body that ends before its Content-Length: the copy simply stops and
    looks like a finished download. The length is therefore checked here, so that a cut-off body
    takes the same path as a failed connection and is resumed instead of failing the checksum."""
    for attempt in range(3):
        offset = archive.stat().st_size if archive.exists() else 0
        try:
            req = urllib.request.Request(
                url, headers={"Range": f"bytes={offset}-"} if offset else {}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                append = offset and response.status == 206
                if append and not response.headers.get("Content-Range", "").startswith(
                    f"bytes {offset}-"
                ):
                    raise ValueError("Rentang runtime tidak sesuai")
                announced = response.headers.get("Content-Length", "")
                with archive.open("ab" if append else "wb") as output:
                    shutil.copyfileobj(response, output, length=262144)
            received = archive.stat().st_size - (offset if append else 0)
            if announced.isdigit() and received != int(announced):
                raise ValueError("Unduhan runtime terpotong")
            return
        except Exception:
            if attempt == 2:
                raise
            print("Koneksi runtime terputus · mencoba melanjutkan", flush=True)
            time.sleep(attempt + 1)


def download_runtime(root):
    runtime = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    version = runtime["deno_version"]
    binary = deno_path(root)
    installed = root / ".runtime/installed.json"
    if (
        binary.exists()
        and installed.exists()
        and json.loads(installed.read_text(encoding="utf-8")).get("platform") == platform_key()
    ):
        try:
            result = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.startswith("deno " + version + " "):
                return
        except OSError:
            pass
    if (
        binary.exists()
        and not installed.exists()
        and platform_key().startswith("linux-")
        and binary.read_bytes()[:4] == b"\x7fELF"
        and int.from_bytes(binary.read_bytes()[18:20], "little")
        == ({"linux-x86_64": 62, "linux-aarch64": 183}.get(platform_key()))
    ):
        try:
            r = subprocess.run(
                [str(binary), "--version"], capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.startswith("deno " + version + " "):
                installed.write_text(
                    json.dumps({"platform": platform_key(), "version": version}), encoding="utf-8"
                )
                return
        except OSError:
            pass
    asset = runtime_asset(runtime)
    if not asset:
        raise ValueError("Platform Deno belum didukung: " + platform_key())
    url = asset["url"]
    integrity = asset["integrity"]
    if not url.startswith("https://registry.npmjs.org/@deno/") or not integrity.startswith(
        "sha512-"
    ):
        raise ValueError("Sumber runtime tidak valid")
    expected = base64.b64decode(integrity.split("-", 1)[1], validate=True)
    if len(expected) != 64:
        raise ValueError("Checksum runtime tidak valid")
    binary.parent.mkdir(parents=True, exist_ok=True)
    archive = root / ".runtime" / ("deno-" + version + "-" + platform_key() + ".tgz.part")
    print("Menyiapkan Deno lokal " + version + " · distribusi resmi npm", flush=True)
    for attempt in range(2):
        fetch(url, archive)
        with archive.open("rb") as stream:
            digest = (
                hashlib.file_digest(stream, "sha512").digest()
                if hasattr(hashlib, "file_digest")
                else hashlib.sha512(stream.read()).digest()
            )
        if digest == expected:
            break
        # Whatever is on disk is never trusted twice: a damaged partial file or a download that
        # was cut off must not decide the outcome, so the file is fetched again from the start.
        archive.unlink(missing_ok=True)
        if attempt:
            raise ValueError("Checksum Deno tidak cocok; arsip ditolak")
        print("Checksum Deno tidak cocok · mengunduh ulang dari awal", flush=True)
    with tempfile.TemporaryDirectory(dir=root / ".runtime") as folder:
        new = Path(folder) / binary.name
        with tarfile.open(archive) as source:
            member = source.getmember(asset.get("member", "package/deno"))
            if not member.isfile():
                raise ValueError("Binary runtime tidak valid")
            with source.extractfile(member) as stream, new.open("wb") as out:
                shutil.copyfileobj(stream, out)
        new.chmod(0o700)
        result = subprocess.run([str(new), "--version"], capture_output=True, text=True)
        if result.returncode or not result.stdout.startswith("deno " + version + " "):
            raise ValueError("Runtime Deno tidak dapat dijalankan")
        new.replace(binary)
    installed.write_text(
        json.dumps({"platform": platform_key(), "version": version}), encoding="utf-8"
    )
    archive.unlink(missing_ok=True)


def setup(root, check=False, upgrade=False):
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise ValueError("Gunakan Python standar, bukan build free-threaded")
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        raise ValueError(
            "Gunakan Python standar 3.11–3.14 (3.13 direkomendasikan); Python 3.10 sudah tidak didukung"
        )
    if not runtime_asset(json.loads((root / "runtime.json").read_text(encoding="utf-8"))):
        raise ValueError("OS/arsitektur belum didukung: " + platform_key())
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError(
            "FFmpeg/FFprobe diperlukan. Pasang lewat apt (Ubuntu), Homebrew (macOS), atau winget Gyan.FFmpeg (Windows), lalu ulangi setup."
        )
    env = root / ".venv"
    py = python_path(root)
    marker = env / ".tool-identity.json"
    legacy = env / ".tool-location"
    identity = environment_identity(root)
    mismatch = False
    if env.exists():
        if marker.exists():
            mismatch = json.loads(marker.read_text(encoding="utf-8")) != identity
        elif legacy.exists():
            mismatch = legacy.read_text(encoding="utf-8").strip() != str(root) or not py.exists()
        else:
            mismatch = True
    if mismatch:
        if check:
            raise ValueError(
                "Lingkungan Python perlu dibuat ulang setelah perubahan lokasi, OS, atau Python"
            )
        old = root / ".state" / ("venv-before-move-" + str(time.time_ns()))
        env.rename(old)
    if not py.exists():
        if check:
            raise ValueError("Belum ada lingkungan Python; jalankan setup.sh")
        print("Menyiapkan lingkungan Python lokal", flush=True)
        run_logged(root, [sys.executable, "-m", "venv", "--without-pip", str(env)])
    pip = subprocess.run(
        [str(py), "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if pip.returncode:
        if check:
            raise ValueError("pip belum tersedia")
        wheel = root / ".bootstrap/pip-26.2.1-py3-none-any.whl"
        expected = (root / ".bootstrap/SHA256SUMS").read_text(encoding="utf-8").split()[0]
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
            raise ValueError("Checksum bootstrap pip tidak cocok")
        script = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("pip",run_name="__main__")'
        run_logged(
            root,
            [str(py), "-c", script, str(wheel), "install", "--no-index", "--no-deps", str(wheel)],
        )
    lines = lock_requirements((root / "requirements.lock").read_text(encoding="utf-8"))
    # `packaging` is a pinned dependency of the environment, not pip's private vendored copy.
    code = (
        "import json, sys\n"
        "from importlib.metadata import version\n"
        "from packaging.requirements import Requirement\n"
        "rs = [Requirement(x) for x in json.loads(sys.argv[1])]\n"
        "assert all(version(r.name) in r.specifier for r in rs if not r.marker or r.marker.evaluate())\n"
    )
    ready = (
        subprocess.run(
            [str(py), "-c", code, json.dumps(lines)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    if check and not ready:
        raise ValueError("Dependensi tidak sesuai rilis; jalankan setup.sh")
    if not check and (upgrade or not ready):
        print("Memasang dependensi rilis yang dipatok", flush=True)
        run_logged(
            root,
            [
                str(py),
                "-m",
                "pip",
                "install",
                *(["--upgrade"] if upgrade else []),
                "--require-hashes",
                "-r",
                str(root / "requirements.lock"),
            ],
            "Memasang dependensi",
        )
    run_logged(root, [str(py), "-m", "pip", "check"])
    if check:
        binary = deno_path(root)
        if not binary.is_file():
            raise ValueError("Deno lokal belum tersedia; jalankan setup")
        stamp = root / ".runtime/installed.json"
        if (
            not stamp.exists()
            or json.loads(stamp.read_text(encoding="utf-8")).get("platform") != platform_key()
        ):
            raise ValueError("Runtime perlu diverifikasi untuk OS/arsitektur ini; jalankan setup")
        expected_runtime = json.loads((root / "runtime.json").read_text(encoding="utf-8"))[
            "deno_version"
        ]
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
        if result.returncode or not result.stdout.startswith("deno " + expected_runtime + " "):
            raise ValueError("Deno tidak sesuai rilis")
    else:
        download_runtime(root)
        marker.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
        legacy.write_text(str(root) + "\n", encoding="utf-8")
    for name in ("ffmpeg", "ffprobe", "deno"):
        binary = str(deno_path(root)) if name == "deno" else shutil.which(name)
        result = subprocess.run(
            [binary, "--version" if name == "deno" else "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode:
            raise ValueError(name + " gagal dijalankan")
        print(name + " · " + result.stdout.splitlines()[0] + " · " + binary, flush=True)
    print(platform_key() + " · Python " + platform.python_version(), flush=True)
    print("Setup siap · FFmpeg, Deno, yt-dlp/EJS, dan menu tersedia", flush=True)


PROGRAM_SUFFIXES = (".py", ".sh", ".cmd", ".md", ".svg")
PROGRAM_DIRS = (".bootstrap", "tests", ".github")
PROGRAM_NAMES = (
    "youtube-downloader",
    ".gitignore",
    "requirements.txt",
    "requirements.lock",
    "runtime.json",
    "release.json",
)


def is_program_path(name):
    """True for a relative path that is part of the program (never state, media or anything
    that climbs out of the folder)."""
    if "\\" in name or ":" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts or "." in parts:
        return False
    if parts[0] in PROGRAM_DIRS:
        return len(parts) > 1
    return len(parts) == 1 and (
        parts[0] in PROGRAM_NAMES or Path(parts[0]).suffix in PROGRAM_SUFFIXES
    )


def release_files(root):
    """The program files that the release.json of `root` lists (the package builder writes the
    list), or None for a release that has none."""
    try:
        files = json.loads((Path(root) / "release.json").read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(files, list) or not all(
        isinstance(n, str) and is_program_path(n) for n in files
    ):
        return None
    return sorted(set(files))


def program_files(source):
    names = [
        p.name
        for pattern in ("*.py", "*.sh", "*.cmd", "*.md", "*.svg")
        for p in source.glob(pattern)
    ]
    if (source / ".gitignore").exists():
        names.append(".gitignore")
    names += [
        n
        for n in (
            "youtube-downloader",
            "requirements.txt",
            "requirements.lock",
            "runtime.json",
            "release.json",
        )
        if (source / n).is_file()
    ]
    names += [
        p.relative_to(source).as_posix() for p in (source / ".github").rglob("*") if p.is_file()
    ]
    names += [
        p.relative_to(source).as_posix()
        for p in (source / "tests").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    names += [
        p.relative_to(source).as_posix() for p in (source / ".bootstrap").glob("*") if p.is_file()
    ]
    return sorted(set(names))


def supported_platforms(root):
    path = root / "release.json"
    return (
        json.loads(path.read_text(encoding="utf-8"))["platforms"]
        if path.exists()
        else ["linux-x86_64", "linux-aarch64"]
    )


UPDATE_JOURNAL = ".state/update-in-progress.json"


def update(source, dest):
    if platform_key() not in supported_platforms(source):
        raise ValueError("Paket rilis tidak mendukung OS ini")
    if dest == source or not (dest / "app.py").exists():
        raise ValueError("Gunakan folder rilis baru terpisah dan tools tujuan yang sudah terpasang")
    if (dest / UPDATE_JOURNAL).exists():
        raise ValueError(
            "Pembaruan sebelumnya terhenti di tengah pemasangan. Jalankan rollback "
            "(rollback.sh atau rollback.cmd) untuk memulihkan program dari cadangannya, "
            "lalu ulangi update."
        )
    listed = release_files(source)
    names = listed or program_files(source)
    missing = [n for n in names if not (source / n).is_file()]
    if missing:
        raise ValueError("Paket rilis tidak lengkap; tidak ada: " + ", ".join(missing[:3]))
    # Files the installed release listed and the new one dropped. Only a release with a list can
    # say so; one without a list never removes anything.
    previous = release_files(dest) or []
    stale = [n for n in previous if n not in names and (dest / n).is_file()]
    backup = dest / ".state/program-backups" / str(time.time_ns())
    backup.mkdir(parents=True)
    existing = []
    added = []
    for name in [*names, *stale]:
        if (dest / name).is_file():
            (backup / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest / name, backup / name)
            existing.append(name)
        else:
            added.append(name)
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "files": existing,
                "added": added,
                "removed": stale,
                "platforms": supported_platforms(dest),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # From here files change. A process that dies now leaves this note, and `rollback` restores
    # the backup it names even though last-update.json was never written.
    journal = dest / UPDATE_JOURNAL
    journal.write_text(
        json.dumps(
            {"backup": str(backup), "started_at": time.time(), "files": len(names)}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        for name in names:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            shutil.copy2(source / name, tmp)
            tmp.replace(target)
        for name in stale:
            (dest / name).unlink(missing_ok=True)
        setup(dest)
        record = {
            "backup": str(backup),
            "version": re.search(
                r"VERSION\s*=\s*['\"]([^'\"]+)", (source / "model.py").read_text(encoding="utf-8")
            )[1],
            "installed_at": time.time(),
        }
        (dest / ".state/last-update.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException:
        restore(dest, backup)
        journal.unlink(missing_ok=True)
        raise
    journal.unlink(missing_ok=True)
    if stale:
        print("File yang tidak ada lagi di rilis ini dihapus: " + ", ".join(stale))
    print("Rilis terpasang. Cadangan: " + str(backup))


def restore(dest, backup):
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if platform_key() not in manifest.get("platforms", ["linux-x86_64", "linux-aarch64"]):
        raise ValueError(
            "Cadangan program tidak mendukung OS ini; rollback ditolak tanpa mengubah program"
        )
    for name in manifest["files"]:
        if not is_program_path(name):
            raise ValueError("Cadangan program memuat jalur yang tidak valid")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / name, target)
    for name in manifest["added"]:
        (dest / name).unlink(missing_ok=True)
    setup(dest, upgrade=True)


def rollback(root):
    pending = root / UPDATE_JOURNAL
    if pending.exists():
        # An installation that stopped halfway: this backup, not the one of the last finished update.
        backup = Path(json.loads(pending.read_text(encoding="utf-8"))["backup"]).resolve()
        if not backup.is_relative_to((root / ".state/program-backups").resolve()):
            raise ValueError("Lokasi cadangan tidak valid")
        restore(root, backup)
        pending.unlink(missing_ok=True)
        print("Pemasangan yang terhenti dipulihkan dari cadangan " + str(backup))
        return
    journal = root / ".state/last-update.json"
    record = json.loads(journal.read_text(encoding="utf-8"))
    backup = Path(record["backup"]).resolve()
    if not backup.is_relative_to((root / ".state/program-backups").resolve()):
        raise ValueError("Lokasi cadangan tidak valid")
    if record.get("rolled_back_at"):
        raise ValueError("Cadangan ini sudah dipulihkan")
    restore(root, backup)
    record["rolled_back_at"] = time.time()
    journal.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print("Program dan dependensi rilis dipulihkan. Media dan job dipertahankan.")


# What `clean` is allowed to remove. Never in this list, so never touched: jobs, the archive,
# settings, the lock, the update journal, the logs, and of course the media.
CLEAN_CATEGORIES = {
    "venv": "Lingkungan Python lama (setelah pindah folder)",
    "backups": "Cadangan program lama (yang terbaru dan target rollback disimpan)",
    "temporary": "Folder sementara milik job yang sudah selesai",
    "media": "Cadangan jalur media yang sudah lama",
    "cache": "Cache pip dan yt-dlp (dibuat ulang otomatis)",
    "leftovers": "Sisa proses yang terhenti",
    "abandoned": "Folder sementara job yang ditinggalkan (resume mengunduh ulang)",
}
CLEAN_DAYS = 30


def completed_variants(state):
    """Variants of every item of a finished job: their temporary folders are no longer needed."""
    from model import variant  # only `clean` needs it; setup and update must not depend on it

    done = set()
    for path in (state / "jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") != "complete":
                continue
            done.update(variant(item) for item in job["items"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return done


def job_age_days(job, path, now):
    """Days since a job was last touched: its own timestamp, else the age of its file."""
    try:
        stamp = datetime.fromisoformat(job.get("updated_at") or job["created_at"])
        moment = stamp.timestamp() if stamp.tzinfo else stamp.astimezone().timestamp()
    except (KeyError, TypeError, ValueError):
        try:
            moment = path.stat().st_mtime
        except OSError:
            return 0
    return max(0, int((now - moment) // 86400))


def abandoned_jobs(root, days=CLEAN_DAYS, now=None):
    """Jobs that were never finished and have not been touched for more than `days` days, with
    the temporary folders that only they use: [(job id, status, age in days, [(path, bytes)])].

    A folder that a younger unfinished job also needs stays: both jobs resume from it."""
    from model import variant  # only `clean` needs it; setup and update must not depend on it

    state = Path(root) / ".state"
    now = time.time() if now is None else now
    jobs = []
    for path in sorted((state / "jobs").glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            variants = {variant(item) for item in job["items"]}
            jobs.append(
                (job.get("id") or path.stem, job["status"], job_age_days(job, path, now), variants)
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    in_use = set()
    for _, status, age, variants in jobs:
        if status != "complete" and age <= days:
            in_use |= variants
    found = []
    for job_id, status, age, variants in jobs:
        if status == "complete" or age <= days:
            continue
        entries = []
        for name in sorted(variants - in_use):
            folder = state / "temporary" / name
            try:
                if folder.is_dir() and not folder.is_symlink():
                    entries.append((folder, folder_size(folder)))
            except OSError:
                continue
        if entries:
            found.append((job_id, status, age, entries))
    return found


def clean_plan(root, days=CLEAN_DAYS, now=None, abandoned=False):
    """{category: [(path, bytes)]} of what `clean` may remove. Nothing is touched here.

    `abandoned` adds the temporary folders of unfinished jobs older than `days` days."""
    state = Path(root) / ".state"
    now = time.time() if now is None else now
    plan = {name: [] for name in CLEAN_CATEGORIES}

    def add(category, path):
        try:
            if not path.is_symlink():
                plan[category].append(
                    (path, folder_size(path) if path.is_dir() else path.stat().st_size)
                )
        except OSError:
            pass

    def older_than(path, seconds):
        try:
            return now - path.stat().st_mtime > seconds
        except OSError:
            return False

    for path in state.glob("venv-before-move-*"):
        add("venv", path)
    backups = sorted(
        (p for p in (state / "program-backups").glob("*") if p.is_dir()),
        key=lambda p: int(p.name) if p.name.isdigit() else 0,
    )
    keep = {backups[-1].resolve()} if backups else set()
    try:
        record = json.loads((state / "last-update.json").read_text(encoding="utf-8"))
        keep.add(Path(record["backup"]).resolve())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for path in backups:
        if path.resolve() not in keep:
            add("backups", path)
    done = completed_variants(state)
    for path in (state / "temporary").glob("*"):
        if path.name in done:
            add("temporary", path)
    if abandoned:
        listed = {path for path, _ in plan["temporary"]}
        for _, _, _, entries in abandoned_jobs(root, days, now):
            for path, _ in entries:
                if path not in listed:
                    add("abandoned", path)
                    listed.add(path)
    for path in (state / "media-path-backups").glob("*"):
        if older_than(path, days * 86400):
            add("media", path)
    for name in ("pip-cache", "cache"):
        if (state / name).is_dir():
            add("cache", state / name)
    for pattern in ("worker-*.json", "release-*"):
        for path in state.glob(pattern):
            if older_than(path, 86400):
                add("leftovers", path)
    return plan


def clean(root, apply=False, days=CLEAN_DAYS, abandoned=False):
    """Remove (or, by default, only list) what is safe to remove under .state.

    Returns [(label, how many, bytes)] for the categories that have something. Every path is
    checked to be inside .state and not a link before it is removed."""
    state = (Path(root) / ".state").resolve()
    rows = []
    for category, entries in clean_plan(root, days, abandoned=abandoned).items():
        count = freed = 0
        for path, weight in entries:
            if apply:
                if path.is_symlink() or not path.resolve().is_relative_to(state):
                    continue
                try:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                except OSError:
                    continue
            count += 1
            freed += weight
        if count:
            rows.append((CLEAN_CATEGORIES[category], count, freed))
    return rows


# What a move leaves behind. The first names are skipped wherever they are; the second ones only
# directly under .state, because a folder called "cache" elsewhere may be somebody's own.
# Setup builds a new environment, and the old ones, pip's cache and yt-dlp's cache are what
# `clean` calls leftovers. Rollback restores the program from .state/program-backups, which is
# copied, and never from them.
NOT_MOVED = (".venv", ".runtime", "__pycache__", "run.lock", ".git")
NOT_MOVED_FROM_STATE = ("venv-before-move-*", "pip-cache", "cache")


def moving_ignore(source):
    everywhere = shutil.ignore_patterns(*NOT_MOVED)
    in_state = shutil.ignore_patterns(*NOT_MOVED_FROM_STATE)
    state = Path(source) / ".state"

    def ignore(directory, names):
        skipped = set(everywhere(directory, names))
        if Path(directory) == state:
            skipped |= set(in_state(directory, names))
        return skipped

    return ignore


def migrate(source, dest):
    if dest == source or dest.is_relative_to(source) or (dest.exists() and any(dest.iterdir())):
        raise ValueError("Pilih folder kosong di luar sumber")
    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=moving_ignore(source))
    # Reuse an installed runtime; setup verifies its version/architecture before use.
    binary = deno_path(source)
    if (
        binary.is_file()
        and (source / ".runtime/installed.json").is_file()
        and json.loads((source / ".runtime/installed.json").read_text(encoding="utf-8")).get(
            "platform"
        )
        == platform_key()
    ):
        target = deno_path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        shutil.copy2(source / ".runtime/installed.json", dest / ".runtime/installed.json")
    journal = dest / ".state/last-update.json"
    if journal.exists():
        record = json.loads(journal.read_text(encoding="utf-8"))
        old = Path(record["backup"])
        if old.is_relative_to(source):
            record["backup"] = str(dest / old.relative_to(source))
            journal.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with guard(dest):
        setup(dest)
    print("Tools siap: " + str(dest))
    print("Media terpisah tetap di lokasi semula. Sumber tidak dihapus.")


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["setup", "update", "rollback", "migrate", "rebase-media"])
    p.add_argument("dest", nargs="?")
    p.add_argument("--check", action="store_true")
    p.add_argument("--upgrade", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--grace", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--from", dest="old_root")
    p.add_argument("--to", dest="new_root")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    try:
        if args.action in ("update", "migrate") and not args.dest:
            raise ValueError("Folder tujuan diperlukan")
        target = Path(args.dest).expanduser().resolve() if args.dest else BASE
        guarded = target if args.action == "update" else BASE
        if args.wait:
            print("Menunggu kunci tools bebas", flush=True)
        with guard(guarded, args.wait, args.grace):
            if args.action == "setup":
                setup(BASE, args.check, args.upgrade)
            elif args.action == "update":
                update(BASE, target)
            elif args.action == "migrate":
                migrate(BASE, target)
            elif args.action == "rebase-media":
                from media_migration import rebase_media

                if not args.old_root or not args.new_root:
                    raise ValueError("Gunakan rebase-media --from OLD --to NEW [--dry-run]")
                rebase_media(BASE, args.old_root, args.new_root, args.dry_run)
            else:
                rollback(BASE)
        return 0
    except KeyboardInterrupt:
        print("Dihentikan", file=sys.stderr)
        return 130
    except Exception as exc:
        print("! GAGAL · " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
