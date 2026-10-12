"""Add sha256 hashes from PyPI to requirements.lock. The pinned versions are never changed.

    python scripts/lock_hashes.py          rewrite requirements.lock with hashes
    python scripts/lock_hashes.py --check  fail if the file's hashes differ from PyPI

To change a version, edit only the `==` pin (a hashed entry may be shortened to one line) and
run the script again. Every file of that release, for every platform and Python version, is
listed, so one lock serves Linux, macOS and Windows. Installation uses `pip install
--require-hashes`, which refuses any file whose hash is not in the lock.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from compat import lock_requirements  # noqa: E402

LOCK = ROOT / "requirements.lock"
HEADER = (
    "# Hash-pinned dependencies (pip install --require-hashes).\n"
    "# Regenerate after editing a pin: python scripts/lock_hashes.py\n"
)


def pypi_hashes(name, version):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "youtube-downloader-lock"})
    with urllib.request.urlopen(request, timeout=30) as response:
        files = json.load(response)["urls"]
    hashes = sorted({item["digests"]["sha256"] for item in files})
    if not hashes:
        raise SystemExit(f"PyPI lists no files for {name}=={version}")
    return hashes


def file_hashes(text):
    """Map each requirement line of a lock file to the set of hashes listed under it."""
    result = {}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            current = lock_requirements(line)[0]
            result[current] = set()
        if current:
            result[current].update(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = LOCK.read_text(encoding="utf-8")
    specs = lock_requirements(text)
    wanted = {}
    for spec in specs:
        name, version = re.match(r"([A-Za-z0-9_.-]+)==([^\s;]+)", spec).groups()
        wanted[spec] = pypi_hashes(name, version)
    if args.check:
        present = file_hashes(text)
        bad = [s for s in specs if set(wanted[s]) != present.get(s, set())]
        for spec in bad:
            print("hashes differ from PyPI:", spec)
        print("requirements.lock matches PyPI" if not bad else "run scripts/lock_hashes.py")
        return 1 if bad else 0
    lines = [HEADER.rstrip("\n")]
    for spec in specs:
        lines.append(spec + " \\")
        lines += [
            f"    --hash=sha256:{h}" + (" \\" if i < len(wanted[spec]) - 1 else "")
            for i, h in enumerate(wanted[spec])
        ]
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(specs)} pins with {sum(map(len, wanted.values()))} hashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
