"""How job files reach the disk: the same bytes as ever, written once, replaced atomically."""

import io
import json
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
        # A job file written before and after this change must be the same file.
        value = job(40)
        old = io.StringIO()
        json.dump(value, old, ensure_ascii=False, indent=2)
        old.write("\n")
        path = self.root / "jobs" / "abc.json"
        store.write_json(path, value)
        self.assertEqual(path.read_bytes(), old.getvalue().encode("utf-8"))
        self.assertTrue(path.read_bytes().endswith(b"}\n"))
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


if __name__ == "__main__":
    unittest.main()
