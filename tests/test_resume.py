"""Resuming a batch whose items are nearly all in the archive already.

Each item found in the archive used to rewrite the whole job file, so the bytes written grew with
the square of the batch. They are now noted every SKIP_CHECKPOINT_ITEMS items or seconds. What the
job file says about a skipped item is never needed to resume: execute asks the archive again."""

import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import store
import runner


def items(count):
    return [
        {
            "id": f"i{n}",
            "title": f"T{n}",
            "url": f"https://example.test/{n}",
            "choice": {"mode": "audio"},
            "status": "pending",
        }
        for n in range(count)
    ]


def archived(key, mode, output=None):
    """The archive's answer for an item it already holds."""
    return {"path": f"/media/{key}.m4a", "mode": mode, "subtitles": [], "missing": []}


class Skipping(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(store, "STATE", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        (Path(folder.name) / "jobs").mkdir()
        # The number of saves is exact only when the clock does not add its own.
        patcher = patch.object(app, "SKIP_CHECKPOINT_SECONDS", 1e9)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.saves = []

    def execute(self, job, get=archived, run=None):
        """Runs app.execute and keeps a copy of the job at every save."""
        real_save = store.save_job

        def save(value):
            self.saves.append(copy.deepcopy(value))
            real_save(value)

        with (
            patch.object(app, "save_job", side_effect=save),
            patch.object(app, "variant", side_effect=lambda item: item["id"]),
            patch.object(store.Archive, "get", side_effect=get),
            patch.object(store.Archive, "put"),
            patch.object(app, "run", side_effect=run or self.refuse),
            patch.object(app, "native_output"),
            redirect_stderr(io.StringIO()),
        ):
            app.execute(app.UI(True), job)

    @staticmethod
    def refuse(request, callback=None):
        raise AssertionError("an archived item must not be downloaded")

    def job(self, count):
        return store.new_job(items(count), "/media")

    def shape(self, job):
        return [(item["status"], item.get("result")) for item in job["items"]]

    # -- how often the file is written --------------------------------------------------------

    def test_the_saves_no_longer_grow_with_the_number_of_skipped_items(self):
        counts = {}
        for count in (120, 240, 480):
            self.saves.clear()
            self.execute(self.job(count))
            counts[count] = len(self.saves)
        # one save when it starts, one for every 50 items, one at the end
        self.assertEqual(counts, {120: 1 + 2 + 1, 240: 1 + 4 + 1, 480: 1 + 9 + 1})

    def test_the_final_job_is_what_it_was_when_every_item_was_saved(self):
        job = self.job(120)
        self.execute(job)
        saved = store.load_job(job["id"])
        self.assertEqual(saved["status"], "complete")
        self.assertEqual({i["status"] for i in saved["items"]}, {"complete"})
        self.assertEqual(
            [i["result"]["path"] for i in saved["items"]],
            [f"/media/i{n}.m4a" for n in range(120)],
        )

    def test_a_clock_that_has_run_out_saves_every_item_as_before(self):
        with patch.object(app, "SKIP_CHECKPOINT_SECONDS", 0):
            self.execute(self.job(30))
        self.assertEqual(len(self.saves), 1 + 30 + 1)

    # -- what the file says when something happens in between ---------------------------------

    def test_an_interrupted_resume_keeps_every_skipped_item_it_had_reached(self):
        job = self.job(120)

        def get(key, mode, output=None):
            if key == "i75":
                raise KeyboardInterrupt
            return archived(key, mode, output)

        with self.assertRaises(KeyboardInterrupt):
            self.execute(job, get)
        saved = store.load_job(job["id"])
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(
            [i["status"] for i in saved["items"]], ["complete"] * 75 + ["pending"] * 45
        )

    def test_a_job_file_from_the_last_checkpoint_resumes_to_the_same_result(self):
        # The process dies without running its finally (kill -9, power loss): the file is the one
        # of the last checkpoint, with only part of the skipped items noted.
        whole = self.job(120)
        self.execute(whole)
        expected = self.shape(store.load_job(whole["id"]))

        self.saves.clear()
        again = self.job(120)
        self.execute(again)
        middle = copy.deepcopy(self.saves[1])  # the first checkpoint, after 50 items
        self.assertEqual(
            [i["status"] for i in middle["items"]], ["complete"] * 50 + ["pending"] * 70
        )
        store.save_job(middle)

        self.saves.clear()
        self.execute(store.load_job(middle["id"]))
        self.assertEqual(self.shape(store.load_job(middle["id"])), expected)

    def test_a_download_after_skipped_items_finds_them_in_the_file_before_it_starts(self):
        job = self.job(4)
        seen = []

        def get(key, mode, output=None):
            return None if key == "i3" else archived(key, mode, output)

        def download(request, callback=None):
            on_disk = store.load_job(job["id"])
            seen.append([i["status"] for i in on_disk["items"]])
            return {"path": "/media/i3.m4a", "mode": "audio", "subtitles": [], "missing": []}

        self.execute(job, get, download)
        self.assertEqual(seen, [["complete", "complete", "complete", "running"]])
        self.assertEqual(store.load_job(job["id"])["status"], "complete")

    def test_a_failure_after_skipped_items_is_saved_with_them(self):
        job = self.job(4)

        def get(key, mode, output=None):
            return None if key == "i3" else archived(key, mode, output)

        def fail(request, callback=None):
            raise runner.WorkerError("Private video", "unavailable")

        with self.assertRaisesRegex(app.Stopped, "belum selesai"):
            self.execute(job, get, fail)
        saved = store.load_job(job["id"])
        self.assertEqual(
            [i["status"] for i in saved["items"]], ["complete", "complete", "complete", "failed"]
        )
        self.assertEqual(saved["status"], "partial")


if __name__ == "__main__":
    unittest.main()
