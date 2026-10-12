"""Release lifecycle and wizard contracts, without YouTube access."""

import copy
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import maintenance
import model
import store
import app
import ui
import worker
from compat import platform_key, deno_path
from terminal import Terminal
from test_tools import synthetic


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        for root, version in ((self.old, "0.9.0"), (self.new, "1.0.0")):
            root.mkdir()
            (root / ".state").mkdir()
            (root / "app.py").write_text(version)
            (root / "model.py").write_text("VERSION='" + version + "'\n")
            for name in (
                "youtube-downloader",
                "requirements.txt",
                "requirements.lock",
                "runtime.json",
            ):
                (root / name).write_text(version)
        for root in (self.old, self.new):
            (root / "release.json").write_text(json.dumps({"platforms": [platform_key()]}))
        (self.old / "settings.json").write_text('{"output":"custom"}')
        (self.old / ".state/downloaded.sqlite3").write_bytes(b"archive unchanged")

    def test_update_rollback_preserves_state_and_config(self):
        state = (self.old / ".state/downloaded.sqlite3").read_bytes()
        settings = (self.old / "settings.json").read_text()
        with patch.object(maintenance, "setup"), redirect_stderr(io.StringIO()):
            maintenance.update(self.new, self.old)
            self.assertEqual((self.old / "app.py").read_text(), "1.0.0")
            maintenance.rollback(self.old)
            self.assertEqual((self.old / "app.py").read_text(), "0.9.0")
            with self.assertRaises(ValueError):
                maintenance.rollback(self.old)
        self.assertEqual(state, (self.old / ".state/downloaded.sqlite3").read_bytes())
        self.assertEqual(settings, (self.old / "settings.json").read_text())

    def test_failed_update_restores_program(self):
        def setup(root, **kw):
            if not kw.get("upgrade"):
                raise ValueError("installation failure")

        with patch.object(maintenance, "setup", side_effect=setup):
            with self.assertRaises(ValueError):
                maintenance.update(self.new, self.old)
        self.assertEqual((self.old / "app.py").read_text(), "0.9.0")

    def test_migrate_preserves_jobs_runtime_rebuilds_environment(self):
        (self.old / ".venv").mkdir()
        (self.old / ".venv/bad-absolute-path").write_text("old")
        (self.old / ".runtime/bin").mkdir(parents=True)
        deno_path(self.old).write_bytes(b"verified runtime")
        (self.old / ".runtime/installed.json").write_text(json.dumps({"platform": platform_key()}))
        dest = self.root / "moved"
        with patch.object(maintenance, "setup") as setup:
            maintenance.migrate(self.old, dest)
            setup.assert_called_once_with(dest)
        self.assertFalse((dest / ".venv").exists())
        self.assertEqual(deno_path(dest).read_bytes(), b"verified runtime")
        self.assertEqual((dest / ".state/downloaded.sqlite3").read_bytes(), b"archive unchanged")
        self.assertTrue(self.old.exists())
        with self.assertRaises(ValueError):
            maintenance.migrate(self.old, dest)

    def test_shared_lock_refuses_maintenance(self):
        with patch.object(store, "STATE", self.old / ".state"):
            with store.lock():
                with self.assertRaises(ValueError):
                    with maintenance.guard(self.old):
                        pass


class Wizard(unittest.TestCase):
    def test_playlist_fallback_subtitles_and_profile(self):
        a = synthetic()
        a.update(id="a", title="A", webpage_url="https://youtube.com/watch?v=a")
        b = copy.deepcopy(a)
        b.update(id="b", title="B", webpage_url="https://youtube.com/watch?v=b")
        b["formats"] = [
            f for f in b["formats"] if not model.has_video(f) or model.resolution(f) <= 720
        ]
        sub = {"en": [{"url": "https://x/?lang=en", "ext": "vtt", "name": "English"}]}
        a["subtitles"] = sub

        class Fake:
            def __init__(self):
                self.answers = iter(("all", "video-audio", 1080, "original", True, True))
                self.rows = []

            def choose(self, *args, **kw):
                return next(self.answers)

            def checkboxes(self, label, choices):
                return [v for _, v in choices]

            def say(self, *args):
                pass

            def table(self, headers, rows):
                self.rows += list(rows)

            def activity(self, *args):
                from contextlib import nullcontext

                return nullcontext()

        fake = Fake()
        playlist = {
            "playlist": True,
            "title": "Fixture",
            "entries": [
                {"url": a["webpage_url"], "title": "A"},
                {"url": b["webpage_url"], "title": "B"},
            ],
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", side_effect=[playlist, a, b]),
        ):
            job = app.wizard(fake, "https://youtube.com/playlist?list=test", output=folder)
        self.assertEqual([x["choice"]["actual_quality"] for x in job["items"]], [1080, 720])
        self.assertTrue(any("turun dari 1080p" in row[1] for row in fake.rows))
        self.assertEqual(job["items"][1]["subtitles"][0]["key"], "en")

    def test_retry_hooks_known_wait_and_original_language(self):
        class E:
            def send(self, kind, **kwargs):
                self.event = (kind, kwargs)

            def hook(self, *args):
                pass

            def pp_hook(self, *args):
                pass

        e = E()
        opts = worker.options({}, e)
        delay = opts["retry_sleep_functions"]["http"](n=2)
        self.assertEqual(delay, 4)
        self.assertEqual(e.event[0], "wait")
        self.assertAlmostEqual(e.event[1]["until"] - time.time(), 4, delta=0.1)
        info = synthetic()
        info["language"] = "ja"
        self.assertEqual(model.default_language(worker.compact(info)), "ja")
        info["formats"][1]["filesize"] = 100
        self.assertEqual(model.pick(info, quality=720)["estimated_size"], 0)

    def test_actual_menu_arrow_and_checkbox_space(self):
        script = 'from ui import UI; u=UI(); print("SELECT",u.choose("Choose",[("one",1),("two",2)])); print("CHECK",u.checkboxes("Tracks",[("en","en"),("ja","ja")]))'
        with Terminal(
            [sys.executable, "-c", script], BASE, extra={"PROMPT_TOOLKIT_NO_CPR": "1"}
        ) as term:
            data = ""
            deadline = time.monotonic() + 15
            sent_select = False
            sent_check = False
            while time.monotonic() < deadline and term.running():
                data += term.read(0.1)
                if "Choose" in data and not sent_select:
                    time.sleep(0.2)
                    term.write("\x1b[B")
                    time.sleep(0.1)
                    term.write("\r")
                    sent_select = True
                if "Tracks" in data and not sent_check:
                    time.sleep(0.2)
                    term.write(" ")
                    time.sleep(0.1)
                    term.write("\x1b[B")
                    time.sleep(0.1)
                    term.write(" ")
                    time.sleep(0.1)
                    term.write("\r")
                    sent_check = True
            data += term.drain()
            if term.running():
                raise AssertionError("Menu did not exit: " + repr(data))
            self.assertEqual(term.wait(), 0, data)
            self.assertIn("SELECT 2", data)
            self.assertIn("CHECK ['en', 'ja']", data)


class RuntimeIntegrity(unittest.TestCase):
    def test_bad_runtime_digest_is_rejected_before_install(self):
        import base64

        class Response(io.BytesIO):
            status = 200
            headers = {}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asset = {
                "url": "https://registry.npmjs.org/@deno/example.tgz",
                "integrity": "sha512-" + base64.b64encode(b"0" * 64).decode(),
            }
            (root / "runtime.json").write_text(
                json.dumps({"deno_version": "2.9.6", "assets": {platform_key(): asset}})
            )
            with (
                patch.object(
                    maintenance.urllib.request, "urlopen", return_value=Response(b"corrupt data")
                ),
                patch.object(maintenance.subprocess, "run") as process,
            ):
                with self.assertRaisesRegex(ValueError, "Checksum"):
                    maintenance.download_runtime(root)
                process.assert_not_called()
            self.assertFalse((root / ".runtime/bin/deno").exists())
            self.assertFalse(list((root / ".runtime").glob("*.part")))

    def test_finished_file_resets_percent_to_full(self):
        s = ui.Session(ui.UI(True), 1)
        with redirect_stderr(io.StringIO()):
            s.event({"type": "start", "name": "fixture"})
            s.event({"type": "progress", "downloaded": 25, "total": 100})
            s.event({"type": "file_done", "downloaded": 100, "total": 100})
        self.assertEqual(s.downloaded, s.byte_total)
        self.assertIsNone(s.eta)
        self.assertEqual(s.file.tasks[0].completed, s.file.tasks[0].total)


if __name__ == "__main__":
    unittest.main()
