"""Authenticated stable-release updates; no credentials are saved in the package."""

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from compat import file_lock, platform_key, ProcessTree, worker_start

REPOSITORY = "fathorrosi-dev/youtube-downloader"
API = "https://api.github.com/repos/" + REPOSITORY
MAX_ARCHIVE = 100 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Versi rilis tidak valid")
    return tuple(map(int, value.split(".")))


def installed_version(root):
    match = re.search(
        r"VERSION\s*=\s*['\"]([^'\"]+)", (Path(root) / "model.py").read_text(encoding="utf-8")
    )
    if not match:
        raise ValueError("Versi program terpasang tidak terbaca")
    version_tuple(match[1])
    return match[1]


def github_token():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name].strip()
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise ValueError(
        "Repo rilis privat membutuhkan login GitHub. Pasang GitHub CLI lalu jalankan gh auth login; gunakan akun yang punya akses repo."
    )


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        host = target.hostname or ""
        if target.scheme != "https" or not (
            host in ("api.github.com", "github.com") or host.endswith(".githubusercontent.com")
        ):
            raise ValueError("Pengalihan unduhan rilis ditolak")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if target.netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(SafeRedirect())

    def read(self, path, limit, asset=False):
        # Only a fixed GitHub API endpoint receives the credential.
        if not path.startswith("/releases/"):
            raise ValueError("Endpoint rilis tidak valid")
        request = urllib.request.Request(
            API + path,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/octet-stream" if asset else "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "youtube-downloader-self-update",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Paket/metadata rilis melebihi batas ukuran")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ValueError(
                    "Rilis stabil belum tersedia atau akun GitHub tidak punya akses repo privat."
                ) from None
            if exc.code in (401, 403):
                raise ValueError(
                    "Akses GitHub ditolak; periksa login, izin Contents: read, atau batas permintaan."
                ) from None
            raise ValueError(
                "GitHub tidak dapat melayani pembaruan (HTTP "
                + str(exc.code)
                + "). Coba kembali nanti."
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError(
                "Koneksi ke GitHub terputus; program belum diubah. Coba update kembali."
            ) from None


def release_info(client):
    info = json.loads(client.read("/releases/latest", 2 * 1024 * 1024).decode("utf-8"))
    if not isinstance(info, dict) or info.get("draft") or info.get("prerelease"):
        raise ValueError("Hanya rilis stabil yang dapat dipasang")
    tag = info.get("tag_name", "")
    if not tag.startswith("v"):
        raise ValueError("Tag rilis tidak valid")
    version = tag[1:]
    version_tuple(version)
    name = "youtube-downloader-" + version + ".zip"
    assets = info.get("assets", [])
    selected = []
    for expected in (name, name + ".sha256"):
        matches = [x for x in assets if x.get("name") == expected and x.get("state") == "uploaded"]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
            raise ValueError("Paket rilis atau checksum belum lengkap")
        if type(matches[0].get("size")) is not int or not 0 < matches[0]["size"] <= (
            MAX_ARCHIVE if expected == name else 4096
        ):
            raise ValueError("Ukuran aset rilis tidak valid")
        selected.append(matches[0])
    return version, name, selected


def safe_unpack(archive, directory, version):
    allowed_roots = {".bootstrap", "tests", ".github"}
    allowed_names = {
        "youtube-downloader",
        ".gitignore",
        "requirements.txt",
        "requirements.lock",
        "runtime.json",
        "release.json",
    }
    seen = set()
    total = 0
    with zipfile.ZipFile(archive) as source:
        if len(source.infolist()) > 1000:
            raise ValueError("Terlalu banyak file dalam paket rilis")
        for member in source.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            if (
                "\\" in name
                or ":" in name
                or not parts
                or parts[0] != "youtube-downloader"
                or any(p in (".", "..") for p in name.split("/"))
            ):
                raise ValueError("Jalur paket rilis tidak aman")
            relative = parts[1:]
            if not relative:
                continue
            identity = "/".join(relative).casefold()
            if identity in seen:
                raise ValueError("Nama file bentrok dalam paket rilis")
            seen.add(identity)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Paket rilis berisi tautan/file khusus")
            if relative[0] not in allowed_roots and (
                len(relative) != 1
                or (
                    relative[0] not in allowed_names
                    and Path(relative[0]).suffix not in (".py", ".sh", ".cmd", ".md", ".svg")
                )
            ):
                raise ValueError("Paket rilis berisi data di luar program")
            if any(p in (".state", ".venv", ".runtime", ".git", "__pycache__") for p in relative):
                raise ValueError("Paket rilis berisi data pribadi/runtime")
            total += member.file_size
            if total > 50 * 1024 * 1024 or member.file_size > 20 * 1024 * 1024:
                raise ValueError("Isi paket rilis melebihi batas ukuran")
            target = Path(directory).joinpath(*relative)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as src, target.open("xb") as dest:
                shutil.copyfileobj(src, dest)
            target.chmod(0o700 if mode & 0o111 else 0o600)
    root = Path(directory)
    required = (
        "maintenance.py",
        "compat.py",
        "model.py",
        "app.py",
        "release.json",
        "requirements.lock",
        "runtime.json",
        "youtube-downloader",
        "youtube-downloader.cmd",
    )
    if any(not (root / name).is_file() for name in required):
        raise ValueError("Paket rilis tidak lengkap")
    meta = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if meta.get("version") != version or installed_version(root) != version:
        raise ValueError("Versi paket tidak cocok dengan tag rilis")
    if platform_key() not in meta.get("platforms", []):
        raise ValueError("Rilis tidak mendukung OS/arsitektur ini")
    try:
        low = tuple(map(int, meta["python_min"].split(".")))
        high = tuple(map(int, meta["python_max"].split(".")))
    except (KeyError, ValueError, AttributeError):
        raise ValueError("Metadata Python rilis tidak valid") from None
    if not low <= sys.version_info[:2] <= high:
        raise ValueError("Versi Python perangkat ini tidak didukung rilis terbaru")
    return root


def fetch_package(client, version, name, assets, temporary):
    checksum = (
        client.read("/releases/assets/" + str(assets[1]["id"]), 4096, True).decode("utf-8").strip()
    )
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?" + re.escape(name), checksum)
    if not match:
        raise ValueError("File checksum rilis tidak valid")
    data = client.read("/releases/assets/" + str(assets[0]["id"]), MAX_ARCHIVE, True)
    if len(data) != assets[0]["size"] or hashlib.sha256(data).hexdigest() != match[1].lower():
        raise ValueError("Checksum paket tidak cocok; program belum diubah")
    archive = Path(temporary) / name
    archive.write_bytes(data)
    return safe_unpack(archive, Path(temporary) / "package", version)


def install_package(source, root, wait=False, say=print):
    """Isolate the installer. Ctrl+C is deferred during its protected transaction."""
    logpath = Path(root) / ".state/update.log"
    interrupted = False
    tree = None
    process = None
    announced = False

    def defer(signum, frame):
        nonlocal interrupted
        interrupted = True

    with logpath.open("a", encoding="utf-8") as log:
        log.write("\nUpdate dimulai " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        log.flush()
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--installer",
            str(source),
            str(root),
            *(["--wait"] if wait else []),
        ]
        previous = signal.signal(signal.SIGINT, defer)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                **(
                    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if os.name == "nt"
                    else {"start_new_session": True}
                ),
            )
            tree = ProcessTree(process)
            process.stdin.write(b"START\n")
            process.stdin.flush()
            process.stdin.close()
            while True:
                if interrupted and not announced:
                    say(
                        "Pemasangan sedang berlangsung; menyelesaikan transaksi aman sebelum keluar."
                    )
                    announced = True
                try:
                    code = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except KeyboardInterrupt:
                    if not interrupted:
                        say(
                            "Pemasangan sedang berlangsung; menyelesaikan transaksi aman sebelum keluar."
                        )
                    interrupted = True
        finally:
            try:
                if tree:
                    tree.stop()
                    tree.close()
                elif process:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
            finally:
                signal.signal(signal.SIGINT, previous)
    if code:
        raise ValueError(
            "Pembaruan tidak berhasil. Periksa "
            + str(logpath)
            + "; pemulihan program dilakukan oleh installer."
        )
    return 130 if interrupted else 0


def perform(root, check=False, wait=False, say=print, activity=None):
    from contextlib import nullcontext

    root = Path(root).resolve()
    client = GitHub(github_token())

    def phase(message):
        say(message)
        return activity(message) if activity else nullcontext()

    with phase("Memeriksa rilis stabil GitHub"):
        version, name, assets = release_info(client)
    current = installed_version(root)
    say("Terpasang " + current + " · rilis stabil " + version)
    if version_tuple(version) <= version_tuple(current):
        say("Program sudah mutakhir; tidak ada perubahan.")
        return 0
    if check:
        say("Pembaruan tersedia. Jalankan youtube-downloader update untuk memasangnya.")
        return 0
    if wait:
        say("Menunggu kunci tools bebas")
    with file_lock(root / ".state/run.lock", wait):
        # Another updater may have completed while this request was waiting.
        if version_tuple(version) <= version_tuple(installed_version(root)):
            say("Program sudah mutakhir.")
            return 0
        temporary = tempfile.TemporaryDirectory(prefix="release-", dir=root / ".state")
        try:
            with phase("Mengunduh dan memverifikasi paket " + version):
                source = fetch_package(client, version, name, assets, temporary.name)
        except BaseException:
            temporary.cleanup()
            raise
    # The new installer acquires the same lock again before replacing any file.
    try:
        with phase("Memasang " + version + " dan mencadangkan program sebelumnya"):
            code = install_package(source, root, wait, say)
        if installed_version(root) != version:
            raise ValueError(
                "Installer selesai tanpa versi yang diminta. Periksa .state/update.log."
            )
        say(
            "Pembaruan selesai · "
            + installed_version(root)
            + " · backup di "
            + str(root / ".state/program-backups")
        )
        say("Log: " + str(root / ".state/update.log"))
        return code
    finally:
        temporary.cleanup()


def installer_entry():
    worker_start()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    return subprocess.call(
        [
            sys.executable,
            str(Path(sys.argv[2]) / "maintenance.py"),
            "update",
            sys.argv[3],
            *sys.argv[4:],
        ]
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--installer":
        sys.exit(installer_entry())
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--wait", action="store_true")
    args = p.parse_args()
    try:
        sys.exit(perform(Path(__file__).resolve().parent, args.check, args.wait))
    except KeyboardInterrupt:
        print("Pembaruan dihentikan sebelum pemasangan.")
        sys.exit(130)
    except Exception as exc:
        print("! GAGAL · " + str(exc), file=sys.stderr)
        sys.exit(1)
