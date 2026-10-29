"""Private job checkpoints and profile-aware archive."""

from pathlib import Path
import json
import sqlite3
import uuid
import subprocess
import os
from datetime import datetime, timezone
from compat import file_lock, default_media, platform_key
from model import SelectionError, valid_browser

BASE = Path(__file__).resolve().parent
STATE = BASE / ".state"
DEFAULT = {"output": default_media(), "browser": None}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    # One dumps and one write: the same bytes as json.dump to the stream, about four times faster
    # (json.dump feeds the stream a piece at a time), and a job is saved twice per item.
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def log_note(message):
    """One line in download.log, for something that was skipped and should be findable later.
    Failing to write it is never a reason to stop."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "download.log").open("a", encoding="utf-8") as log:
            log.write(f"{now()} {message}\n")
    except OSError:
        pass


def lock(wait=False):
    return file_lock(STATE / "run.lock", wait)


def read_json(path, what):
    """A JSON file, or an error that names the file and says what is wrong with it."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{what} tidak dapat dibaca: {path} ({exc}). Perbaiki isinya atau pindahkan file itu."
        ) from exc


def settings():
    path = BASE / "settings.json"
    stored = read_json(path, "Pengaturan") if path.exists() else {}
    if not isinstance(stored, dict):
        raise ValueError(f"Pengaturan tidak valid: {path} harus berupa objek JSON")
    merged = {**DEFAULT, **stored}
    if not isinstance(merged["output"], str) or not merged["output"].strip():
        raise ValueError(f"Pengaturan tidak valid: 'output' di {path} harus berupa jalur folder")
    try:
        valid_browser(merged["browser"])
    except SelectionError as exc:
        raise ValueError(f"Pengaturan tidak valid di {path}: {exc}") from None
    return merged


def save_job(job):
    job["updated_at"] = now()
    write_json(STATE / "jobs" / f"{job['id']}.json", job)


def new_job(items, output, browser=None):
    job = {
        "id": uuid.uuid4().hex[:12],
        "created_at": now(),
        "status": "prepared",
        "platform": platform_key(),
        "output": str(Path(output).expanduser().resolve()),
        "browser": browser,
        "items": items,
    }
    save_job(job)
    return job


def load_job(identifier):
    if not identifier.isalnum():
        raise ValueError("ID job tidak valid")
    path = STATE / "jobs" / f"{identifier}.json"
    if not path.exists():
        raise ValueError("Job tidak ditemukan")
    job = read_json(path, "Job")
    if not isinstance(job, dict) or not isinstance(job.get("items", []), list):
        raise ValueError(f"Job tidak valid: {path} bukan catatan job")
    return job


# Job statuses that still have something to do. Anything else is finished.
RESUMABLE = ("prepared", "running", "interrupted", "blocked", "partial")


def list_jobs():
    """Every readable job, newest first, and the job files that could not be read.

    A damaged file is reported, never fatal: this is what `status` and `resume` rely on."""
    jobs, unreadable = [], []
    for path in (STATE / "jobs").glob("*.json"):
        try:
            jobs.append(load_job(path.stem))
        except ValueError as exc:
            unreadable.append((path.stem, str(exc)))
    jobs.sort(key=lambda j: j.get("updated_at") or j.get("created_at") or "", reverse=True)
    return jobs, unreadable


def resumable_jobs(jobs):
    return [j for j in jobs if j.get("status") in RESUMABLE]


def probe(path, mode):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise ValueError("FFprobe menolak hasil media")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = any(
        s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        for s in streams
    )
    audio = any(s.get("codec_type") == "audio" for s in streams)
    if mode == "video-audio" and not (video and audio):
        raise ValueError("Hasil harus memiliki video dan audio")
    if mode == "video" and (not video or audio):
        raise ValueError("Hasil video saja harus tanpa audio")
    if mode == "audio" and (not audio or video):
        raise ValueError("Hasil audio saja tidak valid")
    if not Path(path).stat().st_size:
        raise ValueError("Hasil media kosong")
    return data


def fingerprint(path, mode):
    """What a file looked like when ffprobe accepted it for this mode (size and modification time
    in nanoseconds), or None when it cannot be read. Not a checksum: a file replaced by another
    of the same size and the same modification time is not noticed."""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "mode": mode}


class Archive:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(STATE / "downloaded.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS results (variant TEXT NOT NULL,path TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(variant,path))"
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, mode, output=None):
        """The usable earlier result of this variant, or None. A record that cannot be read (a
        damaged row, a missing ffprobe) is noted in the log and skipped; it never stops the
        batch, because the file is simply downloaded again."""
        rows = self.db.execute(
            "SELECT rowid,payload FROM results WHERE variant=? ORDER BY rowid DESC", (key,)
        ).fetchall()
        for rowid, payload in rows:
            try:
                result, seen = self._usable(payload, mode, output)
            except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
                log_note(f"Catatan arsip dilewati ({type(exc).__name__}: {exc})")
                continue
            if result:
                if seen:
                    self._remember(rowid, payload, seen)
                return result
        return None

    @staticmethod
    def _usable(payload, mode, output):
        """(the record, None) when the file is trusted by its fingerprint, (the record, the
        fingerprint to write down) when ffprobe had to accept it, (None, None) when it is not
        usable. A record without a fingerprint (written by an earlier version) or with one that no
        longer matches the file is checked with ffprobe, as every record used to be."""
        result = json.loads(payload)
        path = Path(result["path"])
        recorded = result.pop("fingerprint", None)  # an implementation detail, not a result field
        if output and not path.resolve().is_relative_to(Path(output).resolve()):
            return None, None
        if not path.is_file():
            return None, None
        current = fingerprint(path, mode)  # before the probe: a change during it shows up later
        seen = None
        if current is None or recorded != current:
            try:
                probe(path, mode)
            except (ValueError, subprocess.SubprocessError):
                return None, None
            seen = current
        missing = [
            track for track in result.get("subtitles", []) if not Path(track["path"]).is_file()
        ]
        if missing:
            # What the record already says about a missing track (and why) is kept; a track that
            # was saved and whose file is not there now is new, and "gone" is the reason.
            known = {x["key"]: x for x in result.get("missing", [])}
            for track in missing:
                known.setdefault(track["key"], {**track, "reason": "gone"})
            result["missing"] = list(known.values())
        return result, seen

    def _remember(self, rowid, payload, seen):
        """Writes the fingerprint into a record that ffprobe has just accepted, so the next
        resume does not run it again. Best effort: the record is usable either way."""
        try:
            record = json.loads(payload)
            record["fingerprint"] = seen
            with self.db:
                self.db.execute(
                    "UPDATE results SET payload=? WHERE rowid=?",
                    (json.dumps(record, ensure_ascii=False), rowid),
                )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            log_note(f"Catatan arsip tidak diperbarui ({type(exc).__name__}: {exc})")

    def put(self, key, result):
        seen = fingerprint(result["path"], result["mode"])  # before the probe, as in _usable
        probe(result["path"], result["mode"])
        record = {**result, "fingerprint": seen} if seen else result
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO results VALUES (?,?,?)",
                (
                    key,
                    str(Path(result["path"]).resolve()),
                    json.dumps(record, ensure_ascii=False),
                ),
            )
