"""How job files reach the disk (the same bytes as ever, written once, replaced atomically) and how
the archive decides that a file it recorded is still the one it accepted."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import store


def job(items=3):
    return {
        "id": "abc123def456",
        "status": "running",
        "output": "/media/Vidéo – 日本語",
        "browser": None,
        "items": [
            {"url": f"https://example.test/{n}", "title": f"Judul {n} · é", "state": "pending"}
            for n in range(items)
        ],
    }


class WriteJson(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_the_bytes_are_those_of_the_json_dump_it_replaces(self):
        # A job file written before and after this change must be the same file. The old way is
        # written to a real text-mode file, not to a StringIO, so that the newline translation of
        # the platform (CRLF on Windows) applies to the expectation as it does to write_json.
        value = job(40)
        expected = self.root / "expected.json"
        with expected.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        path = self.root / "jobs" / "abc.json"
        store.write_json(path, value)
        self.assertEqual(path.read_bytes(), expected.read_bytes())
        # read_text turns CRLF back into LF, so this holds on every platform.
        self.assertTrue(path.read_text(encoding="utf-8").endswith("}\n"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)

    def test_the_file_is_flushed_to_disk_and_replaced_in_one_step(self):
        path = self.root / "job.json"
        path.write_text("old", encoding="utf-8")
        with patch.object(store.os, "fsync", wraps=store.os.fsync) as fsync:
            store.write_json(path, job())
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["id"], "abc123def456")
        self.assertEqual([p.name for p in self.root.iterdir()], ["job.json"])  # no .tmp left

    def test_a_value_that_cannot_be_written_leaves_the_old_file_and_no_temporary(self):
        path = self.root / "job.json"
        store.write_json(path, {"keep": True})
        with self.assertRaises(TypeError):
            store.write_json(path, {"bad": object()})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"keep": True})
        self.assertEqual([p.name for p in self.root.iterdir()], ["job.json"])


class ArchiveFingerprints(unittest.TestCase):
    """A recorded file is checked with ffprobe once; after that its size and modification time
    stand for it, unless an earlier version wrote the record or the file has changed."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = patch.object(store, "STATE", self.root / "state")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archive = store.Archive()
        self.addCleanup(self.archive.close)
        self.media = self.root / "media"
        self.media.mkdir()
        self.file = self.media / "a.m4a"
        self.file.write_bytes(b"audio" * 100)
        self.result = {"path": str(self.file), "mode": "audio", "subtitles": [], "missing": []}

    def probed(self):
        """Patches ffprobe with one that accepts everything and counts its calls."""
        patcher = patch.object(store, "probe")
        probe = patcher.start()
        self.addCleanup(patcher.stop)
        return probe

    def stored(self):
        rows = self.archive.db.execute("SELECT payload FROM results").fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_old_record(self, **extra):
        """A record as an earlier version wrote it: the result and nothing else."""
        payload = json.dumps({**self.result, **extra}, ensure_ascii=False)
        self.archive.db.execute(
            "INSERT INTO results VALUES (?,?,?)", ("v", str(self.file.resolve()), payload)
        )
        self.archive.db.commit()

    # -- writing -----------------------------------------------------------------------------

    def test_put_records_what_the_file_looked_like_and_leaves_the_result_alone(self):
        probe = self.probed()
        before = dict(self.result)
        self.archive.put("v", self.result)
        info = self.file.stat()
        self.assertEqual(self.result, before)  # the caller's dict is not touched
        probe.assert_called_once()  # a file that was just written is still checked
        (record,) = self.stored()
        self.assertEqual(
            record["fingerprint"],
            {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "mode": "audio"},
        )
        self.assertEqual({k: v for k, v in record.items() if k != "fingerprint"}, before)

    # -- reading -----------------------------------------------------------------------------

    def test_a_file_that_has_not_changed_is_not_probed_again(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        probe.reset_mock()
        for _ in range(3):
            self.assertEqual(self.archive.get("v", "audio", self.media), self.result)
        probe.assert_not_called()

    def test_the_fingerprint_is_not_part_of_what_get_returns(self):
        self.probed()
        self.archive.put("v", self.result)
        self.assertNotIn("fingerprint", self.archive.get("v", "audio"))

    def test_a_record_from_an_earlier_version_is_probed_once_and_then_trusted(self):
        probe = self.probed()
        self.add_old_record()
        self.assertEqual(self.archive.get("v", "audio"), self.result)
        probe.assert_called_once()
        (record,) = self.stored()
        self.assertEqual(record["fingerprint"]["size"], self.file.stat().st_size)
        self.assertEqual(self.archive.get("v", "audio"), self.result)
        probe.assert_called_once()  # not again

    def test_a_file_of_another_size_is_probed_again_and_recorded_anew(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        probe.reset_mock()
        self.file.write_bytes(b"audio" * 101)
        self.assertEqual(self.archive.get("v", "audio"), self.result)
        probe.assert_called_once()
        self.assertEqual(self.stored()[0]["fingerprint"]["size"], 505)
        probe.reset_mock()
        self.archive.get("v", "audio")
        probe.assert_not_called()

    def test_a_file_with_another_modification_time_is_probed_again(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        probe.reset_mock()
        info = self.file.stat()
        os.utime(self.file, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        self.assertIsNotNone(self.archive.get("v", "audio"))
        probe.assert_called_once()

    def test_a_changed_file_that_ffprobe_refuses_is_not_usable_and_the_record_stays_as_it_was(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        recorded = self.stored()
        self.file.write_bytes(b"not audio at all")
        probe.side_effect = ValueError("FFprobe menolak hasil media")
        self.assertIsNone(self.archive.get("v", "audio"))
        self.assertEqual(self.stored(), recorded)

    def test_a_deleted_file_is_not_usable_and_is_not_probed(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        probe.reset_mock()
        self.file.unlink()
        self.assertIsNone(self.archive.get("v", "audio"))
        probe.assert_not_called()

    def test_the_fingerprint_of_one_mode_does_not_vouch_for_another(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        probe.reset_mock()
        self.archive.get("v", "video-audio")
        probe.assert_called_once_with(self.file, "video-audio")

    def test_a_fingerprint_that_is_not_one_is_ignored(self):
        probe = self.probed()
        for junk in ("x", 7, [], {"size": "a"}, {"size": 500, "mtime_ns": None, "mode": "audio"}):
            self.archive.db.execute("DELETE FROM results")
            self.add_old_record(fingerprint=junk)
            probe.reset_mock()
            self.assertEqual(self.archive.get("v", "audio"), self.result, junk)
            probe.assert_called_once()

    def test_a_record_that_cannot_be_updated_is_still_returned(self):
        self.probed()
        self.add_old_record()
        self.archive.db.execute("PRAGMA query_only=ON")  # every write is refused
        self.assertEqual(self.archive.get("v", "audio"), self.result)
        log = (self.root / "state" / "download.log").read_text(encoding="utf-8")
        self.assertIn("Catatan arsip tidak diperbarui", log)

    def test_missing_subtitles_are_still_noticed_when_the_media_is_trusted(self):
        probe = self.probed()
        self.result["subtitles"] = [{"key": "id", "path": str(self.media / "a.id.srt")}]
        self.archive.put("v", self.result)
        probe.reset_mock()
        found = self.archive.get("v", "audio")
        probe.assert_not_called()
        self.assertEqual([x["key"] for x in found["missing"]], ["id"])

    def test_the_folder_check_still_comes_first(self):
        probe = self.probed()
        self.archive.put("v", self.result)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.assertIsNone(self.archive.get("v", "audio", output=elsewhere))
        self.assertIsNotNone(self.archive.get("v", "audio", output=self.media))
        self.assertEqual(probe.call_count, 1)  # only the one of put

    # -- with the real ffprobe ----------------------------------------------------------------

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs FFmpeg")
    def test_with_a_real_file_the_second_check_runs_no_process_and_damage_is_still_found(self):
        real = self.media / "tone.m4a"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.2", "-y", str(real)],
            check=True,
            timeout=60,
        )
        result = {"path": str(real), "mode": "audio", "subtitles": [], "missing": []}
        self.archive.put("v", result)
        with patch.object(store.subprocess, "run", side_effect=AssertionError("no process")):
            self.assertEqual(self.archive.get("v", "audio"), result)
        real.write_bytes(b"this was audio once")  # another size: the real ffprobe refuses it
        self.assertIsNone(self.archive.get("v", "audio"))


if __name__ == "__main__":
    unittest.main()
