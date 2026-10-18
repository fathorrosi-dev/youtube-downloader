"""Build the program-only release ZIP and its checksum on any supported OS."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from maintenance import is_program_path, program_files

ROOT = "youtube-downloader/"


def packaged_meta(source, names):
    """release.json as it ships: the one in the tree plus the exact list of program files, so an
    update can tell which installed files the new release no longer has."""
    meta = json.loads((source / "release.json").read_text(encoding="utf-8"))
    meta["files"] = sorted(set(names) | {"release.json"})
    return meta


def build(source, dest):
    source = Path(source).resolve()
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    names = program_files(source)
    meta = packaged_meta(source, names)
    version = meta["version"]
    archive = dest / ("youtube-downloader-" + version + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            path = source / name
            info = zipfile.ZipInfo(ROOT + Path(name).as_posix(), (2026, 1, 1, 0, 0, 0))
            mode = 0o755 if path.name == "youtube-downloader" or path.suffix == ".sh" else 0o644
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = (
                (json.dumps(meta, indent=2) + "\n").encode("utf-8")
                if name == "release.json"
                else path.read_bytes()
            )
            out.writestr(info, data)
    verify(archive)
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n",
        encoding="utf-8",
    )
    return archive, checksum


def verify(archive):
    """Open the package the way an installer would and refuse one that is inconsistent: the file
    list in release.json must match what is in the ZIP, and every path must be a program path."""
    with zipfile.ZipFile(archive) as package:
        members = [m.filename for m in package.infolist() if not m.is_dir()]
        if any(not name.startswith(ROOT) for name in members):
            raise ValueError("Paket memuat file di luar folder youtube-downloader/")
        names = sorted(name[len(ROOT) :] for name in members)
        if len(set(names)) != len(names):
            raise ValueError("Paket memuat nama file ganda")
        bad = [n for n in names if not is_program_path(n) or PurePosixPath(n).is_absolute()]
        if bad:
            raise ValueError("Paket memuat jalur yang bukan bagian program: " + ", ".join(bad[:3]))
        meta = json.loads(package.read(ROOT + "release.json").decode("utf-8"))
    if meta.get("files") != names:
        raise ValueError("Daftar files di release.json tidak sama dengan isi paket")
    return names


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("dest", nargs="?", help="folder for the package and its checksum")
    p.add_argument("--verify", metavar="ZIP", help="only check an existing package")
    args = p.parse_args()
    if args.verify:
        print(len(verify(args.verify)), "file, daftar release.json cocok")
    elif args.dest:
        for path in build(Path(__file__).resolve().parent, args.dest):
            print(path)
    else:
        p.error("beri folder tujuan atau --verify ZIP")
