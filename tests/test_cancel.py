"""Stopping the workers: Ctrl+C or a failed batch must not wait for the lookups that are running.

The tests that start real processes run on POSIX only. How the process tree is stopped on Windows
(CTRL_BREAK_EVENT) is the same code as before; what is new there is only the check of the event."""

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import runner

# A worker that follows the protocol (waits for START, announces itself) and then does nothing for
# a minute, like a lookup stuck on a slow extraction. It writes its process id next to itself.
SLOW_WORKER = """\
import json, os, sys, time
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.stdin.readline()
name = config["url"].rsplit(":", 1)[1]
Path(__file__).with_name("pid-" + name).write_text(str(os.getpid()))
print(json.dumps({"type": "stage", "message": "busy"}), flush=True)
time.sleep(60)
print(json.dumps({"type": "done", "result": {}}), flush=True)
"""


class Screen:
    """Just enough of the UI for inspect_selection: it records what the spinner was told."""

    def __init__(self):
        self.said = []

    @contextmanager
    def activity(self, message):
        yield lambda text, step=None: self.said.append(text)

    def table(self, *args, **kwargs):
        pass


class Cancelling(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "worker.py").write_text(SLOW_WORKER, encoding="utf-8")
        for name, value in (("STATE", self.root / ".state"), ("BASE", self.root)):
            patcher = patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def pid(self, name):
        path = self.root / f"pid-{name}"
        return int(path.read_text()) if path.is_file() and path.read_text() else None

    def wait_for(self, check, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = check()
            if value:
                return value
            time.sleep(0.02)
        self.fail("timed out")

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    # -- the event itself: no process is needed ------------------------------------------------

    def test_an_event_that_is_already_set_starts_no_worker(self):
        stop = threading.Event()
        stop.set()
        with patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaises(runner.WorkerCancelled):
                runner.run({"action": "inspect", "url": "fixture:a"}, cancel=stop)
        popen.assert_not_called()
        self.assertFalse((self.root / ".state").exists())  # not even a request file

    def test_cancelled_by_applies_to_this_thread_inside_the_block_only(self):
        stop = threading.Event()
        stop.set()
        request = {"action": "inspect", "url": "fixture:a"}
        with runner.cancelled_by(stop):
            with self.assertRaises(runner.WorkerCancelled):
                runner.run(request)
            seen = []
            other = threading.Thread(
                target=lambda: seen.append(getattr(runner._bound, "event", None))
            )
            other.start()
            other.join()
            self.assertEqual(seen, [None])  # another thread is not bound
        with patch.object(runner.subprocess, "Popen", side_effect=OSError("no process")):
            with self.assertRaises(OSError):
                runner.run(request)  # outside the block the event no longer matters

    def test_a_cancelled_worker_is_not_a_failure(self):
        self.assertFalse(issubclass(runner.WorkerCancelled, runner.WorkerError))

    # -- real processes ---------------------------------------------------------------------

    @unittest.skipUnless(os.name == "posix", "starts real worker processes")
    def test_a_running_worker_is_stopped_within_a_moment_and_nothing_is_left(self):
        stop = threading.Event()
        outcome = []

        def lookup():
            try:
                runner.run({"action": "inspect", "url": "slow:one"}, cancel=stop)
            except BaseException as exc:  # noqa: BLE001 - what the caller would see
                outcome.append(exc)

        thread = threading.Thread(target=lookup)
        thread.start()
        pid = self.wait_for(lambda: self.pid("one"))
        began = time.monotonic()
        stop.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 8)  # the worker would sleep for 60 s
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], runner.WorkerCancelled)
        self.assert_gone(pid)
        self.assertEqual(list((self.root / ".state").glob("worker-*.json")), [])

    @unittest.skipUnless(os.name == "posix", "starts real worker processes")
    def test_a_failed_batch_stops_the_lookups_that_are_running_and_says_so(self):
        # Item 0 is refused (login) after items 1 and 2 have started; there are as many workers
        # as items, so no other lookup starts meanwhile. Leaving inspect_selection used to wait for
        # both running lookups to finish (a minute here); now they are stopped.
        def inspect(url, browser=None, playlist=False):
            name = url.rsplit(":", 1)[1]
            if name == "0":
                self.wait_for(lambda: self.pid("1") and self.pid("2"))
                raise runner.WorkerError("YouTube meminta login", "authentication")
            return runner.run({"action": "inspect", "url": url})

        entries = [{"url": f"slow:{n}", "title": f"T{n}"} for n in range(3)]
        screen = Screen()
        began = time.monotonic()
        with (
            patch.dict(os.environ, {"YTDL_INSPECT_WORKERS": "3"}),
            patch.object(app, "inspect_url", side_effect=inspect),
        ):
            with self.assertRaises(runner.WorkerError) as caught:
                app.inspect_selection(screen, entries, None)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertLess(time.monotonic() - began, 8)
        self.assertIn("Menghentikan · menutup 2 pencarian yang berjalan", screen.said)
        for name in ("1", "2"):
            self.assert_gone(self.pid(name))


if __name__ == "__main__":
    unittest.main()
