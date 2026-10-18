"""Compare the yt-dlp pin with PyPI, and prepare the pin change.

    python scripts/pin_status.py --check   exit 1 only when the pin is older than PIN_WARN_DAYS
                                           AND PyPI has a newer release
    python scripts/pin_status.py --bump    rewrite the pin in requirements.txt and requirements.lock
                                           (yt-dlp and the EJS version it requires), then run
                                           scripts/lock_hashes.py to write the hashes

Both need the network. They are for CI (dependencies.yml, bump-ytdlp.yml); the tool itself
stays offline. Nothing is changed unless --bump is given.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import PIN_WARN_DAYS, release_age_days  # noqa: E402

REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"
PIN = re.compile(r"(yt-dlp\[default\]==)([0-9][0-9.]*)")


def pypi(path):
    request = urllib.request.Request(
        "https://pypi.org/pypi/" + path, headers={"User-Agent": "youtube-downloader-pin"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def current_pin(text):
    match = PIN.search(text)
    if not match:
        raise SystemExit("requirements.txt has no yt-dlp[default]== pin")
    return match[2]


def newer(latest, pin):
    try:
        return version_key(latest) > version_key(pin)
    except ValueError:
        return False


def ejs_requirement(requires_dist):
    """The yt-dlp-ejs version that this yt-dlp release asks for in its `default` extra."""
    for entry in requires_dist or []:
        match = re.match(r"yt-dlp-ejs==([0-9][0-9.]*)\s*;.*extra\s*==\s*['\"]default['\"]", entry)
        if match:
            return match[1]
    return None


def check(pin, latest, today=None):
    """(exit code, message): worth a warning only when a newer release exists."""
    age = release_age_days(pin, today)
    if not newer(latest, pin):
        return 0, f"yt-dlp {pin} is the newest release on PyPI (released {age} days ago)"
    if age is not None and age > PIN_WARN_DAYS:
        return 1, f"yt-dlp {pin} is {age} days old (limit {PIN_WARN_DAYS}); PyPI has {latest}"
    return (
        0,
        f"yt-dlp {pin} is {age} days old; PyPI has {latest}, within the {PIN_WARN_DAYS}-day limit",
    )


def bump(latest, ejs):
    """New text of requirements.txt and requirements.lock with the pin set to `latest`."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    lock = LOCK.read_text(encoding="utf-8")
    text = PIN.sub(lambda m: m[1] + latest, text, count=1)
    lock, count = re.subn(r"(?m)^yt-dlp==[0-9][0-9.]*", "yt-dlp==" + latest, lock, count=1)
    if count != 1:
        raise SystemExit("requirements.lock has no yt-dlp== line")
    if ejs:
        lock, count = re.subn(r"(?m)^yt-dlp-ejs==[0-9][0-9.]*", "yt-dlp-ejs==" + ejs, lock, count=1)
        if count != 1:
            raise SystemExit("requirements.lock has no yt-dlp-ejs== line")
    return text, lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--bump", action="store_true")
    args = parser.parse_args(argv)
    pin = current_pin(REQUIREMENTS.read_text(encoding="utf-8"))
    latest = pypi("yt-dlp/json")["info"]["version"]
    if args.check:
        code, message = check(pin, latest)
        print(message)
        return code
    if not newer(latest, pin):
        print(f"yt-dlp {pin} is already the newest release; nothing to change")
        return 0
    ejs = ejs_requirement(pypi(f"yt-dlp/{latest}/json")["info"].get("requires_dist"))
    text, lock = bump(latest, ejs)
    REQUIREMENTS.write_text(text, encoding="utf-8")
    LOCK.write_text(lock, encoding="utf-8")
    print(f"yt-dlp {pin} -> {latest}" + (f", yt-dlp-ejs -> {ejs}" if ejs else ""))
    print("now run: python scripts/lock_hashes.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
