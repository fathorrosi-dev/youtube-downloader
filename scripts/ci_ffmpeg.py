"""Put ffmpeg and ffprobe on PATH for the Windows jobs of compatibility.yml.

    python scripts/ci_ffmpeg.py

1. If both tools are already on PATH, nothing is installed.
2. Otherwise Chocolatey installs them, up to ATTEMPTS times. An attempt counts as successful
   only when ffmpeg and ffprobe can then be found, never because choco exited with 0: when the
   community feed answers "503 Service Unavailable", choco prints "Unable to find package",
   installs 0 of 0 packages and still exits with 0 (a job failed that way).
3. If Chocolatey did not deliver, a fixed FFmpeg release is downloaded and its SHA-256 is checked
   against the value written in this file before anything is unpacked.
4. The folder of each tool that was found is appended to $GITHUB_PATH, so that the next steps of
   the job see the same tools. The job fails here, naming what is missing, if that is not possible.

Standard library only. The pieces that touch the network or start programs are parameters, so
tests/test_ci.py runs the flow without either.
"""

import hashlib
import http.client
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

TOOLS = ("ffmpeg", "ffprobe")
CHOCO_BIN = r"C:\ProgramData\chocolatey\bin"
ATTEMPTS = 4
RETRY_SECONDS = 10  # the wait after attempt n is n times this

# The release the fallback unpacks: the "essentials" build 9.0.2 of gyan.dev, published as a
# GitHub release of GyanD/codexffmpeg (the same version the Chocolatey package ffmpeg 9.0.2
# carries). SHA256 is the hash gyan.dev publishes in ffmpeg-9.0.2-essentials_build.zip.sha256, and
# it equals the SHA-256 of the file downloaded from the first address. A download whose hash
# differs is rejected, so a wrong address or a changed file ends in an error, not in an unknown
# binary.
VERSION = "9.0.2"
ARCHIVE = f"ffmpeg-{VERSION}-essentials_build.zip"
SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
URLS = (
    f"https://github.com/GyanD/codexffmpeg/releases/download/{VERSION}/{ARCHIVE}",
    f"https://www.gyan.dev/ffmpeg/builds/packages/{ARCHIVE}",
)
DOWNLOAD_ATTEMPTS = 2  # per address


def with_folder(folder, path):
    return os.pathsep.join([str(folder), path]) if path else str(folder)


def find_tools(path):
    """{'ffmpeg': 'C:\\...\\ffmpeg.EXE', ...} for each tool found when searching `path`."""
    found = {}
    for tool in TOOLS:
        where = shutil.which(tool, path=path)
        if where:
            found[tool] = where
    return found


def complete(found):
    return all(tool in found for tool in TOOLS)


def from_chocolatey(path, find=find_tools, run=subprocess.run, sleep=time.sleep):
    """Run `choco install ffmpeg` until the tools can be found. True if they can."""
    choco = shutil.which("choco", path=path)
    if not choco:
        print("choco is not on PATH")
        return False
    for attempt in range(1, ATTEMPTS + 1):
        result = run([choco, "install", "ffmpeg", "--yes", "--no-progress"], check=False)
        if complete(find(path)):
            return True
        print(
            f"choco attempt {attempt}/{ATTEMPTS}: exit code {result.returncode}, "
            "and ffmpeg/ffprobe are still missing"
        )
        if attempt < ATTEMPTS:
            sleep(RETRY_SECONDS * attempt)
    return False


def download(url, destination):
    """Fetch `url` into `destination` and return the SHA-256 of what arrived."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "youtube-downloader-ci"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as out:
        for block in iter(lambda: response.read(1 << 20), b""):
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def unpack(archive, target):
    """Extract the archive; return the folder that holds ffmpeg.exe and ffprobe.exe, or None."""
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    for first in Path(target).rglob("ffmpeg.exe"):
        if (first.parent / "ffprobe.exe").is_file():
            return first.parent
    return None


def from_download(
    target,
    urls=URLS,
    expected=SHA256,
    fetch=download,
    sleep=time.sleep,
    attempts=DOWNLOAD_ATTEMPTS,
):
    """Download, verify and unpack the pinned release into `target`. The tools' folder, or None."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / ARCHIVE
    for url in urls:
        for attempt in range(1, attempts + 1):
            try:
                actual = fetch(url, archive)
            except (OSError, http.client.HTTPException) as error:
                print(f"download {attempt}/{attempts} of {url} failed: {error}")
                sleep(RETRY_SECONDS)
                continue
            if actual != expected:
                # A cut-off transfer also lands here, so this is retried like a failed one.
                print(
                    f"download {attempt}/{attempts} of {url}: SHA-256 {actual}, expected {expected}"
                )
                continue
            try:
                folder = unpack(archive, target / "unpacked")
            except (OSError, zipfile.BadZipFile) as error:
                print(f"{ARCHIVE} matched its checksum but cannot be unpacked: {error}")
                return None
            if folder is None:
                print(f"{ARCHIVE} has no folder with both ffmpeg.exe and ffprobe.exe")
            return folder
    return None


def hand_on(folders, github_path):
    """Append each folder once to the file $GITHUB_PATH names, so later steps put it on PATH."""
    if not github_path:
        print("GITHUB_PATH is not set; the next steps will not see these folders")
        return
    with open(github_path, "a", encoding="utf-8") as out:
        for folder in dict.fromkeys(folders):
            out.write(f"{folder}\n")


def runs(program):
    """True when `program -version` starts and exits with 0."""
    try:
        return (
            subprocess.run([program, "-version"], capture_output=True, timeout=60).returncode == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def main(
    env=None,
    find=find_tools,
    via_chocolatey=from_chocolatey,
    via_download=from_download,
    check=runs,
):
    env = os.environ if env is None else env
    path = with_folder(CHOCO_BIN, env.get("PATH", ""))
    found = find(path)
    if not complete(found):
        print("ffmpeg and ffprobe are not on PATH; installing with Chocolatey")
        via_chocolatey(path)
        found = find(path)
    if not complete(found):
        print("Chocolatey did not deliver; downloading FFmpeg " + VERSION)
        base = env.get("RUNNER_TEMP") or tempfile.gettempdir()
        folder = via_download(Path(base) / "ffmpeg")
        if folder:
            path = with_folder(folder, path)
            found = find(path)
    missing = [tool for tool in TOOLS if tool not in found]
    if missing:
        print("::error::not found after every way of installing them: " + ", ".join(missing))
        shown = Path(CHOCO_BIN)
        if shown.is_dir():
            print(f"{CHOCO_BIN} holds: " + ", ".join(sorted(p.name for p in shown.iterdir())))
        return 1
    for tool in TOOLS:
        print(f"{tool} -> {found[tool]}")
        if not check(found[tool]):
            print(f"::error::{found[tool]} -version did not run")
            return 1
    hand_on([str(Path(found[tool]).parent) for tool in TOOLS], env.get("GITHUB_PATH"))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # keep choco's output and ours in order
    sys.exit(main())
