"""Release lifecycle and wizard contracts, without YouTube access."""

import base64
import copy
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
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
                self.answers = iter(("all", "video-audio", False, 1080, "original", True, True))
                self.rows = []

            def choose(self, *args, **kw):
                return next(self.answers)

            def checkboxes(self, label, choices, **kw):
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

    def run_wizard(self, answers, subtitles):
        class Recorder:
            def __init__(self):
                self.answers = iter(answers)
                self.prompts = []

            def choose(self, title, options, default=None, step=None, label=None):
                self.prompts.append((label, step))
                return next(self.answers)

            def checkboxes(self, label, choices, **kw):
                return []

            def say(self, *args):
                pass

            def table(self, headers, rows):
                pass

            def activity(self, *args):
                from contextlib import nullcontext

                return nullcontext()

        info = synthetic()
        info.update(id="a", title="A", webpage_url="https://youtube.com/watch?v=a")
        if subtitles:
            info["subtitles"] = {
                "en": [{"url": "https://x/?lang=en", "ext": "vtt", "name": "English"}]
            }
        recorder = Recorder()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", return_value=info),
        ):
            app.wizard(recorder, "https://youtube.com/watch?v=a", output=folder)
        return recorder.prompts

    def test_settings_prompts_are_numbered_exactly_for_each_mode(self):
        self.assertEqual(
            self.run_wizard(["video-audio", False, 1080, "original", False, True], subtitles=True),
            [
                ("Unduh apa?", None),
                ("Format file", (1, 5)),
                ("Kualitas", (2, 5)),
                ("Bahasa audio", (3, 5)),
                ("Subtitle", (4, 5)),
                ("Mulai?", (5, 5)),
            ],
        )
        self.assertEqual(
            self.run_wizard(["audio", "original", "original", False, True], subtitles=False),
            [
                ("Unduh apa?", None),
                ("Bahasa audio", (1, 4)),
                ("Format audio", (2, 4)),
                ("Kualitas audio", (3, 4)),
                ("Mulai?", (4, 4)),
            ],
        )
        self.assertEqual(
            self.run_wizard(["video", False, 720, True], subtitles=False),
            [
                ("Unduh apa?", None),
                ("Format file", (1, 3)),
                ("Kualitas", (2, 3)),
                ("Mulai?", (3, 3)),
            ],
        )

    def compatible_wizard(self, info, answers, checked=False):
        class Recorder:
            def __init__(self):
                self.answers = iter(answers)
                self.rows = []
                self.said = []
                self.offered = {}

            def choose(self, title, options, default=None, step=None, label=None):
                self.offered[label] = [text for text, _ in options]
                return next(self.answers)

            def checkboxes(self, label, choices, **kw):
                return [v for _, v in choices] if checked else []

            def say(self, *args):
                self.said += [str(a) for a in args]

            def table(self, headers, rows):
                self.rows += list(rows)

            def activity(self, *args):
                from contextlib import nullcontext

                return nullcontext()

        recorder = Recorder()
        info.update(id="a", title="A", webpage_url="https://youtube.com/watch?v=a")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", return_value=info),
        ):
            job = app.wizard(recorder, "https://youtube.com/watch?v=a", output=folder)
        return job, recorder

    def test_compatible_mp4_is_chosen_stored_and_shown_with_its_codecs(self):
        info = synthetic()
        info["subtitles"] = {"en": [{"url": "https://x/?lang=en", "ext": "vtt", "name": "English"}]}
        job, recorder = self.compatible_wizard(
            info, ["video-audio", True, 1080, "original", True, True], checked=True
        )
        choice = job["items"][0]["choice"]
        self.assertTrue(choice["compatible"])
        self.assertEqual((choice["video_format"], choice["audio_format_id"]), ("1080", "a1"))
        self.assertEqual(len(job["items"][0]["subtitles"]), 1)
        # MP4 even with a subtitle chosen; the summary says which codecs go into it.
        self.assertEqual(recorder.rows[0][2].split(" · ")[:2], ["MP4", "H.264 + AAC"])
        self.assertIn(
            "subtitle disimpan sebagai file .srt di folder subtitle", " ".join(recorder.said)
        )
        self.assertTrue(any("MP4 kompatibel" in text for text in recorder.offered["Format file"]))

    def test_automatic_format_is_unchanged_and_shows_why_a_file_becomes_mkv(self):
        job, recorder = self.compatible_wizard(
            synthetic(), ["video-audio", False, 1080, "original", True]
        )
        choice = job["items"][0]["choice"]
        self.assertNotIn("compatible", choice)
        self.assertEqual((choice["video_format"], choice["audio_format_id"]), ("1080b", "a2"))
        self.assertEqual(recorder.rows[0][2].split(" · ")[:2], ["MKV", "VP9 + Opus"])

    def test_compatible_item_without_aac_is_kept_as_mkv_and_the_user_is_told(self):
        info = synthetic()
        info["formats"] = [f for f in info["formats"] if f.get("acodec") != "mp4a"]
        job, recorder = self.compatible_wizard(info, ["video-audio", True, 1080, "original", True])
        self.assertTrue(job["items"][0]["choice"]["compatible"])
        self.assertEqual(recorder.rows[0][2].split(" · ")[:2], ["MKV", "H.264 + Opus"])
        self.assertTrue(any("tidak punya pasangan video/audio" in t for t in recorder.said))

    def test_compatible_detail_selection_only_lists_h264_av1_and_aac(self):
        job, recorder = self.compatible_wizard(
            synthetic(), ["video-audio", True, "manual", "original", "1080", "a1", True]
        )
        video = " ".join(recorder.offered["Format video"])
        audio = " ".join(recorder.offered["Format audio"])
        self.assertIn("avc1", video)
        self.assertNotIn("vp9", video)
        self.assertIn("mp4a", audio)
        self.assertNotIn("opus", audio)
        self.assertEqual(recorder.rows[0][2].split(" · ")[:2], ["MP4", "H.264 + AAC"])

    def test_compatible_with_no_h264_or_av1_stops_with_a_clear_message(self):
        info = synthetic()
        for f in info["formats"]:
            if f.get("vcodec") == "avc1":
                f["vcodec"] = "vp09.00.40.08"
        with self.assertRaisesRegex(ValueError, "H.264/AV1"):
            self.compatible_wizard(info, ["video-audio", True])

    def test_finished_menu_leaves_symbol_counter_and_answer(self):
        script = (
            "from ui import UI; u=UI(); "
            "r=u.choose('Pilih kualitas',[('A',1),('B',2)],step=(2,5),label='Kualitas'); "
            "print('RESULT',r)"
        )
        with Terminal(
            [sys.executable, "-c", script], BASE, extra={"PROMPT_TOOLKIT_NO_CPR": "1"}
        ) as term:
            data = ""
            deadline = time.monotonic() + 15
            sent = False
            while time.monotonic() < deadline and term.running():
                data += term.read(0.1)
                if "Pilih kualitas" in data and not sent:
                    time.sleep(0.3)
                    term.write("\x1b[B")
                    time.sleep(0.1)
                    term.write("\r")
                    sent = True
            data += term.drain()
            self.assertEqual(term.wait(), 0, data)
            clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", data)
            self.assertIn("RESULT 2", clean)
            self.assertIn("2/5  Pilih kualitas", clean)  # the live prompt carries the counter
            # The history line that replaces it: symbol, counter, label, answer.
            self.assertRegex(clean, r"◆ 2/5 Kualitas\s+B\r?\n")

    def test_playlist_inspection_reports_progress_item_by_item(self):
        messages = []

        class Recorder:
            def __init__(self):
                self.answers = iter(("all", "video", False, 720, True))

            def choose(self, *args, **kw):
                return next(self.answers)

            def checkboxes(self, label, choices, **kw):
                return []

            def say(self, *args):
                pass

            def table(self, headers, rows):
                pass

            def activity(self, message):
                from contextlib import contextmanager

                @contextmanager
                def spinner():
                    messages.append(message)
                    yield lambda text, step=None: messages.append(text)

                return spinner()

        entries = []
        infos = [{"playlist": True, "title": "P", "entries": entries}]
        for name in ("a", "b", "c"):
            info = synthetic()
            info.update(
                id=name, title=name.upper(), webpage_url="https://youtube.com/watch?v=" + name
            )
            entries.append({"url": info["webpage_url"], "title": name.upper()})
            infos.append(info)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", side_effect=infos),
        ):
            app.wizard(Recorder(), "https://youtube.com/playlist?list=x", output=folder)
        self.assertEqual(messages[0], "Mengambil informasi YouTube")
        progress = [m for m in messages if m.startswith("Memeriksa kualitas dan subtitle")]
        self.assertEqual(
            progress[1:],
            [f"Memeriksa kualitas dan subtitle · {n}/3" for n in (1, 2, 3)],
        )

    def test_audio_language_tag_is_converted_only_for_mp4(self):
        # The MP4 muxer drops anything but three-letter codes, Matroska takes YouTube's own.
        for code, mp4, mkv in (
            ("en", "eng", "en"),
            ("en-US", "eng", "en-US"),
            ("id", "ind", "id"),
            ("zh-Hans", "zho", "zh-Hans"),
            ("eng", "eng", "eng"),
            ("xx", None, "xx"),
        ):
            self.assertEqual(worker.audio_language(code, "mp4"), mp4, code)
            self.assertEqual(worker.audio_language(code, "mkv"), mkv, code)

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
                # A bad download is fetched once more before it is given up, so every call
                # needs a fresh response.
                patch.object(
                    maintenance.urllib.request,
                    "urlopen",
                    side_effect=lambda *args, **kwargs: Response(b"corrupt data"),
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


class RuntimeDownload(unittest.TestCase):
    """download_runtime against a local server that stands in for registry.npmjs.org."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            program = b"not a real deno " + bytes(range(256)) * 2000
            member = tarfile.TarInfo("package/deno")
            member.size = len(program)
            tar.addfile(member, io.BytesIO(program))
        self.data = archive.getvalue()
        self.wrong = bytes(reversed(self.data))
        self.plan = []  # one entry per request: "ok", "cut" (connection drops) or "wrong"
        self.ranges = []
        asset = {
            "url": "https://registry.npmjs.org/@deno/example/-/deno.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(self.data).digest()).decode(),
            "member": "package/deno",
        }
        (self.root / "runtime.json").write_text(
            json.dumps({"deno_version": "2.9.6", "assets": {platform_key(): asset}})
        )
        (self.root / ".runtime").mkdir()
        self.part = self.root / ".runtime" / ("deno-2.9.6-" + platform_key() + ".tgz.part")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(handler):
                self.ranges.append(handler.headers.get("Range"))
                action = self.plan.pop(0) if self.plan else "ok"
                data = self.wrong if action == "wrong" else self.data
                start = 0
                requested = handler.headers.get("Range")
                if requested:
                    start = int(requested.split("=")[1].split("-")[0])
                    handler.send_response(206)
                    handler.send_header(
                        "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
                    )
                else:
                    handler.send_response(200)
                body = data[start:]
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body[: len(body) * 2 // 5] if action == "cut" else body)
                handler.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.local = f"http://127.0.0.1:{server.server_port}/deno.tgz"

    def run_download(self):
        real = urllib.request.urlopen

        def local(request, *args, **kwargs):
            return real(
                urllib.request.Request(self.local, headers=dict(request.header_items())),
                *args,
                **kwargs,
            )

        output = io.StringIO()
        version = SimpleNamespace(returncode=0, stdout="deno 2.9.6 (stable)\n")
        with (
            patch.object(maintenance.urllib.request, "urlopen", local),
            patch.object(maintenance.time, "sleep"),
            patch.object(maintenance.subprocess, "run", return_value=version) as process,
            redirect_stdout(output),
        ):
            try:
                maintenance.download_runtime(self.root)
            finally:
                self.version_checks = process.call_count
        return output.getvalue()

    def test_a_connection_cut_mid_file_is_resumed_in_the_same_run(self):
        # urllib does not notice a body shorter than its Content-Length; without the length
        # check this ended in "Checksum Deno tidak cocok" and the user had to run setup again.
        self.plan = ["cut"]
        output = self.run_download()
        self.assertIn("Koneksi runtime terputus", output)
        self.assertEqual(self.ranges[0], None)
        offset = int(self.ranges[1].split("=")[1].rstrip("-"))
        self.assertTrue(0 < offset < len(self.data), self.ranges)
        self.assertTrue((self.root / ".runtime/installed.json").is_file())
        self.assertTrue(deno_path(self.root).is_file())
        self.assertFalse(self.part.exists())

    def test_a_damaged_partial_file_is_dropped_and_fetched_again_from_the_start(self):
        self.part.write_bytes(b"\0" * 1000)
        output = self.run_download()
        self.assertIn("mengunduh ulang dari awal", output)
        self.assertEqual(self.ranges, ["bytes=1000-", None])
        self.assertTrue(deno_path(self.root).is_file())
        self.assertFalse(self.part.exists())

    def test_a_download_that_stays_wrong_is_still_rejected(self):
        self.plan = ["wrong", "wrong", "wrong"]
        with self.assertRaisesRegex(ValueError, "Checksum Deno tidak cocok"):
            self.run_download()
        self.assertEqual(self.ranges, [None, None])  # one more try, from the start, then stop
        self.assertEqual(self.version_checks, 0)  # nothing from the bad archive was run
        self.assertFalse(self.part.exists())
        self.assertFalse(deno_path(self.root).exists())

    def test_a_good_download_is_one_request(self):
        output = self.run_download()
        self.assertEqual(self.ranges, [None])
        self.assertNotIn("terputus", output)
        self.assertTrue((self.root / ".runtime/installed.json").is_file())


class ReleaseList(unittest.TestCase):
    def test_which_paths_count_as_the_program(self):
        for name in (
            "app.py",
            "release.json",
            "youtube-downloader",
            "requirements.lock",
            "README.md",
            "docs.svg",
            "tests/test_one.py",
            "tests/sub/test_two.py",
            ".github/workflows/ci.yml",
            ".bootstrap/pip.whl",
            ".gitignore",
        ):
            self.assertTrue(maintenance.is_program_path(name), name)
        for name in (
            "",
            ".",
            "..",
            "../app.py",
            "tests/../../app.py",
            "/etc/passwd",
            "a\\b.py",
            "C:app.py",
            "settings.json",
            ".state/job.json",
            ".venv/bin/python",
            "tests",
            "media/video.mp4",
            "sub/app.py",
            "docs/notes.md",
        ):
            self.assertFalse(maintenance.is_program_path(name), name)

    def test_the_list_of_a_release_is_read_sorted_and_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def write(value):
                (root / "release.json").write_text(json.dumps(value))
                return maintenance.release_files(root)

            self.assertEqual(write({"files": ["b.py", "a.py", "a.py"]}), ["a.py", "b.py"])
            self.assertIsNone(write({"version": "1"}))  # a release without a list
            self.assertIsNone(write({"files": "app.py"}))
            self.assertIsNone(write({"files": ["app.py", "../outside.py"]}))
            self.assertIsNone(write({"files": ["app.py", 3]}))
            (root / "release.json").write_text("{nope")
            self.assertIsNone(maintenance.release_files(root))
            (root / "release.json").unlink()
            self.assertIsNone(maintenance.release_files(root))

    def test_program_files_are_listed_with_forward_slashes_and_without_private_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (
                "app.py",
                "tests/test_one.py",
                ".github/workflows/ci.yml",
                "settings.json",
            ):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x")
            (root / "tests/__pycache__").mkdir()
            (root / "tests/__pycache__/x.pyc").write_text("x")
            names = maintenance.program_files(root)
        self.assertEqual(names, [".github/workflows/ci.yml", "app.py", "tests/test_one.py"])


class UpdateTransaction(unittest.TestCase):
    """What `update` removes, and how an installation that died half way is put right."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old, self.new = self.root / "old", self.root / "new"
        common = (
            "model.py",
            "requirements.txt",
            "requirements.lock",
            "runtime.json",
            "youtube-downloader",
        )
        for root, version, extra in (
            (self.old, "0.9.0", ("legacy.py", "tests/test_legacy.py")),
            (self.new, "1.0.0", ("added.py",)),
        ):
            (root / ".state").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "app.py").write_text(version)
            for name in (*common, *extra):
                (root / name).write_text(version)
            (root / "model.py").write_text("VERSION='" + version + "'\n")
        (self.old / "settings.json").write_text('{"output":"custom"}')

    def meta(self, root, files=None):
        record = {"platforms": [platform_key()]}
        if files is not None:
            record["files"] = sorted({*files, "release.json"})
        (root / "release.json").write_text(json.dumps(record))

    def listed(self, root):
        return [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and ".state" not in p.parts and p.name != "settings.json"
        ]

    def update(self):
        out = io.StringIO()
        with (
            patch.object(maintenance, "setup"),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            maintenance.update(self.new, self.old)
        return out.getvalue()

    def test_a_file_the_new_release_dropped_is_removed_and_kept_in_the_backup(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, self.listed(self.new))
        said = self.update()
        self.assertFalse((self.old / "legacy.py").exists())
        self.assertFalse((self.old / "tests/test_legacy.py").exists())
        self.assertEqual((self.old / "added.py").read_text(), "1.0.0")
        self.assertEqual((self.old / "app.py").read_text(), "1.0.0")
        self.assertIn("legacy.py", said)
        backup = Path(json.loads((self.old / ".state/last-update.json").read_text())["backup"])
        self.assertEqual((backup / "legacy.py").read_text(), "0.9.0")
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(sorted(manifest["removed"]), ["legacy.py", "tests/test_legacy.py"])
        self.assertIn("added.py", manifest["added"])
        self.assertTrue((self.old / "settings.json").exists())

    def test_rolling_back_brings_the_removed_files_back_and_removes_the_added_ones(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, self.listed(self.new))
        self.update()
        with (
            patch.object(maintenance, "setup"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            maintenance.rollback(self.old)
        self.assertEqual((self.old / "legacy.py").read_text(), "0.9.0")
        self.assertEqual((self.old / "tests/test_legacy.py").read_text(), "0.9.0")
        self.assertEqual((self.old / "app.py").read_text(), "0.9.0")
        self.assertFalse((self.old / "added.py").exists())

    def test_an_installed_release_without_a_list_never_loses_a_file(self):
        self.meta(self.old)  # a release from before the list existed
        self.meta(self.new, self.listed(self.new))
        self.update()
        self.assertEqual((self.old / "legacy.py").read_text(), "0.9.0")

    def test_a_file_that_is_not_in_the_installed_list_is_not_touched(self):
        self.meta(self.old, [n for n in self.listed(self.old) if n != "legacy.py"])
        self.meta(self.new, self.listed(self.new))
        self.update()
        self.assertEqual((self.old / "legacy.py").read_text(), "0.9.0")

    def test_a_package_that_lacks_a_listed_file_is_refused_before_anything_changes(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, [*self.listed(self.new), "promised.py"])
        with self.assertRaisesRegex(ValueError, "tidak lengkap.*promised.py"):
            self.update()
        self.assertEqual((self.old / "app.py").read_text(), "0.9.0")
        self.assertFalse((self.old / maintenance.UPDATE_JOURNAL).exists())

    def test_a_failed_update_leaves_no_journal_and_the_old_program(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, self.listed(self.new))

        def setup(root, **kw):
            if not kw.get("upgrade"):
                raise ValueError("installation failure")

        with patch.object(maintenance, "setup", side_effect=setup), redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                maintenance.update(self.new, self.old)
        self.assertEqual((self.old / "app.py").read_text(), "0.9.0")
        self.assertEqual((self.old / "legacy.py").read_text(), "0.9.0")
        self.assertFalse((self.old / maintenance.UPDATE_JOURNAL).exists())

    def hard_stop(self, after):
        """Run the update in another process that dies, without any clean-up, once `after` files
        have been put in place: what a power cut or a killed window does."""
        script = (
            "import os, sys, shutil\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "import maintenance\n"
            "real, count = shutil.copy2, [0]\n"
            "def copy(src, dst, *a, **k):\n"
            "    if str(dst).endswith('.new'):\n"
            "        count[0] += 1\n"
            "        if count[0] > int(sys.argv[4]):\n"
            "            os._exit(9)\n"
            "    return real(src, dst, *a, **k)\n"
            "maintenance.shutil.copy2 = copy\n"
            "maintenance.setup = lambda *a, **k: None\n"
            "maintenance.update(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.new), str(self.old), str(BASE), str(after)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 9, result.stderr)

    def test_an_installation_killed_half_way_is_put_right_by_rollback(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, self.listed(self.new))
        self.hard_stop(after=2)
        # Some files are the new release and some the old one: the program is in pieces.
        contents = {(self.old / n).read_text() for n in ("app.py", "model.py", "runtime.json")}
        self.assertIn("1.0.0", contents)
        self.assertTrue((self.old / maintenance.UPDATE_JOURNAL).exists())
        # Another update does not build on that.
        with patch.object(maintenance, "setup"):
            with self.assertRaisesRegex(ValueError, "rollback"):
                maintenance.update(self.new, self.old)
        out = io.StringIO()
        with patch.object(maintenance, "setup"), redirect_stdout(out):
            maintenance.rollback(self.old)
        self.assertIn("terhenti", out.getvalue())
        for name in ("app.py", "model.py", "runtime.json", "legacy.py"):
            self.assertEqual(
                (self.old / name).read_text().strip().split("=")[-1].strip("'\n"), "0.9.0", name
            )
        self.assertFalse((self.old / "added.py").exists())
        self.assertFalse((self.old / maintenance.UPDATE_JOURNAL).exists())
        self.assertEqual((self.old / "settings.json").read_text(), '{"output":"custom"}')
        # The way is clear for the next update.
        self.meta(self.old, self.listed(self.old))
        self.update()
        self.assertEqual((self.old / "app.py").read_text(), "1.0.0")

    def test_a_kill_before_any_file_changes_is_recovered_just_the_same(self):
        self.meta(self.old, self.listed(self.old))
        self.meta(self.new, self.listed(self.new))
        self.hard_stop(after=0)
        with patch.object(maintenance, "setup"), redirect_stdout(io.StringIO()):
            maintenance.rollback(self.old)
        self.assertEqual((self.old / "app.py").read_text(), "0.9.0")
        self.assertFalse((self.old / maintenance.UPDATE_JOURNAL).exists())

    def test_a_journal_that_points_outside_the_backups_is_refused(self):
        journal = self.old / maintenance.UPDATE_JOURNAL
        journal.write_text(json.dumps({"backup": str(self.root)}))
        with self.assertRaisesRegex(ValueError, "Lokasi cadangan tidak valid"):
            maintenance.rollback(self.old)
        self.assertTrue(journal.exists())

    def test_a_backup_that_names_a_path_outside_the_program_is_not_restored(self):
        backup = self.old / ".state/program-backups/1"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text(
            json.dumps({"files": ["../escape.py"], "added": [], "platforms": [platform_key()]})
        )
        with patch.object(maintenance, "setup"):
            with self.assertRaisesRegex(ValueError, "jalur yang tidak valid"):
                maintenance.restore(self.old, backup)
        self.assertFalse((self.root / "escape.py").exists())

    def test_the_command_line_accepts_a_grace_period_for_the_lock(self):
        result = subprocess.run(
            [sys.executable, str(BASE / "maintenance.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--grace", result.stdout)  # hidden: it is for the updater's own use


class LockGrace(unittest.TestCase):
    def test_a_lock_released_within_the_grace_period_is_taken_and_otherwise_refused(self):
        import compat

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".state/run.lock"
            holding = threading.Event()

            def hold():
                with compat.file_lock(path):
                    holding.set()
                    time.sleep(0.6)

            thread = threading.Thread(target=hold)
            thread.start()
            try:
                self.assertTrue(holding.wait(10))
                with self.assertRaisesRegex(ValueError, "Tools masih berjalan"):
                    with compat.file_lock(path):
                        pass
                started = time.monotonic()
                with compat.file_lock(path, grace=10):
                    waited = time.monotonic() - started
                self.assertLess(waited, 8)
            finally:
                thread.join()

    def test_a_lock_that_stays_held_is_refused_when_the_grace_runs_out(self):
        import compat

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".state/run.lock"
            with compat.file_lock(path):
                started = time.monotonic()
                with self.assertRaises(ValueError):
                    with compat.file_lock(path, grace=0.3):
                        pass
                self.assertGreaterEqual(time.monotonic() - started, 0.25)


class TemporaryFolders(unittest.TestCase):
    def test_a_folder_that_cannot_be_removed_is_left_and_said_not_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder) / "variant"
            temp.mkdir()
            (temp / "video.part").write_bytes(b"x")
            for broken in (PermissionError(13, "in use"), None):
                err = io.StringIO()
                with (
                    patch.object(
                        worker.shutil,
                        "rmtree",
                        side_effect=broken,
                    ),
                    redirect_stderr(err),
                ):
                    self.assertFalse(worker.remove_temporary(temp))
                self.assertIn("jalankan clean", err.getvalue())
                self.assertTrue(temp.exists())

    def test_a_folder_that_can_be_removed_is_removed_quietly(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder) / "variant"
            (temp / "sub").mkdir(parents=True)
            (temp / "sub/video.part").write_bytes(b"x")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertTrue(worker.remove_temporary(temp))
            self.assertFalse(temp.exists())
            self.assertEqual(err.getvalue(), "")


class Windows(unittest.TestCase):
    def test_a_path_is_too_long_only_on_windows_and_only_past_the_limit(self):
        limit = worker.WINDOWS_PATH_LIMIT
        self.assertEqual(limit, 259)
        self.assertEqual(worker.path_too_long("a" * (limit + 1), windows=True), limit + 1)
        self.assertIsNone(worker.path_too_long("a" * limit, windows=True))
        self.assertIsNone(worker.path_too_long("a" * 1000, windows=False))
        self.assertEqual(worker.path_too_long("abcdef", windows=True, limit=5), 6)

    def test_a_long_destination_is_refused_before_anything_is_downloaded(self):
        item = {
            "id": "x",
            "title": "T",
            "url": "https://youtu.be/x",
            "choice": model.pick(
                synthetic(), "video-audio", "best", "en", "original", None, None, False
            ),
            "subtitles": [],
        }
        events = []

        class Events:
            def send(self, kind, **kwargs):
                events.append(kind)

            hook = pp_hook = staticmethod(lambda *a: None)

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(worker, "path_too_long", return_value=301),
            patch.object(worker.yt_dlp, "YoutubeDL") as extractor,
        ):
            with self.assertRaisesRegex(ValueError, "301 karakter.*batas Windows.*--output"):
                worker.download({"item": item, "output": folder}, Events())
            extractor.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])  # nothing was created either


class FfmpegFailure(unittest.TestCase):
    def test_a_failing_ffmpeg_is_one_sentence_without_the_command_line(self):
        secret = "/private/folder/.state/temporary/abc/video.f137.mp4"

        class Events:
            def __init__(self):
                self.sent = []

            def send(self, kind, **kwargs):
                self.sent.append((kind, kwargs))

        events = Events()
        failure = subprocess.CalledProcessError(
            1, ["ffmpeg", "-nostdin", "-i", secret, "-c", "copy", "out.mkv"]
        )
        with patch.object(worker.subprocess, "run", side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                worker.ffmpeg(["-i", secret], events, "Menggabungkan video + audio", "finish")
        message = str(caught.exception)
        self.assertIn("FFmpeg gagal (kode 1)", message)
        self.assertIn("menggabungkan video + audio", message)
        self.assertIn(".state/download.log", message)
        for leaked in (secret, "-nostdin", "CalledProcessError", "Command"):
            self.assertNotIn(leaked, message)
        self.assertEqual(events.sent[0][0], "stage")  # the stage was still announced


class DamagedArchive(unittest.TestCase):
    """A record that cannot be used is skipped and noted; the batch goes on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        patcher = patch.object(store, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archive = store.Archive()
        self.addCleanup(self.archive.close)
        self.media = self.state / "media"
        self.media.mkdir()
        self.good = self.media / "good.mkv"
        self.good.write_bytes(b"video")

    def add(self, payload, path="x"):
        self.archive.db.execute("INSERT INTO results VALUES (?,?,?)", ("v", path, payload))
        self.archive.db.commit()

    def good_payload(self):
        return json.dumps({"path": str(self.good), "mode": "video-audio", "subtitles": []})

    def log(self):
        path = self.state / "download.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_rows_that_cannot_be_read_are_skipped_and_the_next_usable_one_is_returned(self):
        self.add(self.good_payload(), "a")
        self.add("{not json", "b")
        self.add(json.dumps({"mode": "video-audio"}), "c")  # no path
        self.add(json.dumps({"path": 7}), "d")  # a path that is not text
        self.add(json.dumps(["not", "an", "object"]), "e")
        with patch.object(store, "probe"):
            result = self.archive.get("v", "video-audio")
        self.assertEqual(result["path"], str(self.good))
        self.assertEqual(self.log().count("Catatan arsip dilewati"), 4)

    def test_a_missing_ffprobe_means_download_again_not_a_failed_batch(self):
        self.add(self.good_payload())
        with patch.object(store, "probe", side_effect=FileNotFoundError("ffprobe")):
            self.assertIsNone(self.archive.get("v", "video-audio"))
        self.assertIn("FileNotFoundError", self.log())

    def test_a_record_for_a_file_that_is_gone_or_fails_the_probe_is_not_usable(self):
        gone = json.dumps({"path": str(self.media / "gone.mkv"), "mode": "video-audio"})
        self.add(gone)
        self.assertIsNone(self.archive.get("v", "video-audio"))
        self.add(self.good_payload(), "y")
        with patch.object(store, "probe", side_effect=ValueError("FFprobe menolak hasil media")):
            self.assertIsNone(self.archive.get("v", "video-audio"))

    def test_a_result_outside_the_output_folder_is_not_reused(self):
        self.add(self.good_payload())
        other = self.state / "elsewhere"
        other.mkdir()
        with patch.object(store, "probe"):
            self.assertIsNone(self.archive.get("v", "video-audio", output=other))
            self.assertIsNotNone(self.archive.get("v", "video-audio", output=self.media))

    def test_the_note_is_written_even_when_the_state_folder_is_new(self):
        shutil_target = self.state / "fresh"
        with patch.object(store, "STATE", shutil_target):
            store.log_note("satu catatan")
        self.assertIn("satu catatan", (shutil_target / "download.log").read_text(encoding="utf-8"))

    def test_a_note_that_cannot_be_written_never_raises(self):
        blocked = self.state / "file-not-folder"
        blocked.write_text("x")
        with patch.object(store, "STATE", blocked):
            store.log_note("tidak bisa ditulis")


if __name__ == "__main__":
    unittest.main()
