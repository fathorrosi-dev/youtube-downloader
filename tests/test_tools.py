"""Offline regression tests: real yt-dlp, FFmpeg and local HTTP media."""

import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import model
import store
import runner
import app
import ui
from rich.console import Console
from fsutil import cleanup_directory
from terminal import Terminal


def synthetic():
    def v(identifier, w, h, fps=30, codec="avc1"):
        return {
            "format_id": identifier,
            "width": w,
            "height": h,
            "fps": fps,
            "vcodec": codec,
            "acodec": "none",
        }

    return {
        "id": "x",
        "title": "Example",
        "formats": [
            {
                "format_id": "dub",
                "acodec": "mp4a",
                "vcodec": "none",
                "language": "ja",
                "language_preference": -10,
            },
            {
                "format_id": "a1",
                "acodec": "mp4a",
                "vcodec": "none",
                "language": "en",
                "language_preference": 10,
                "abr": 128,
            },
            {
                "format_id": "a2",
                "acodec": "opus",
                "vcodec": "none",
                "language": "en",
                "language_preference": 10,
                "abr": 160,
            },
            v("720", 1280, 720),
            v("1080", 1920, 1080),
            v("1080b", 1920, 1080, 60, "vp9"),
            v("1440", 2560, 1440),
            v("2160", 3840, 2160),
            v("4320", 7680, 4320),
        ],
    }


class Selection(unittest.TestCase):
    def test_quality_pairs_and_best(self):
        info = synthetic()
        for q, identifier in (
            (720, "720"),
            (1080, "1080b"),
            (1440, "1440"),
            (2160, "2160"),
            ("best", "4320"),
        ):
            c = model.pick(info, quality=q)
            self.assertEqual(c["video_format"], identifier)
            self.assertEqual(c["audio_format_id"], "a2")
            self.assertEqual(c["language"], "en")

    def test_portrait_fallback_and_no_upscale(self):
        info = synthetic()
        for f in info["formats"]:
            if f.get("width"):
                f["width"], f["height"] = f["height"], f["width"]
        self.assertEqual(model.qualities(info), [4320, 2160, 1440, 1080, 720])
        self.assertEqual(model.pick(info, quality=1200)["actual_quality"], 1080)
        with self.assertRaises(model.SelectionError):
            model.pick(info, quality=480)

    def test_modes_languages_and_containers(self):
        info = synthetic()
        c = model.pick(info, mode="video", quality=1080)
        self.assertIsNone(c["audio_format_id"])
        self.assertEqual(model.container(c, []), "mkv")
        c = model.pick(info, mode="audio", language="ja")
        self.assertIsNone(c["video_format"])
        self.assertEqual(c["audio_format_id"], "dub")
        self.assertEqual(model.container(c, []), "m4a")
        c = model.pick(info, quality=720, audio_id="a1")
        self.assertEqual(model.container(c, []), "mp4")
        self.assertEqual(model.container(c, [{}]), "mkv")

    def test_compatible_mp4_prefers_h264_and_aac_over_a_higher_ranked_vp9_and_opus(self):
        info = synthetic()
        automatic = model.pick(info, quality=1080)
        self.assertEqual((automatic["video_format"], automatic["audio_format_id"]), ("1080b", "a2"))
        self.assertNotIn("compatible", automatic)
        c = model.pick(info, quality=1080, compatible=True)
        self.assertEqual((c["video_format"], c["audio_format_id"]), ("1080", "a1"))
        self.assertIs(c["compatible"], True)
        self.assertEqual(model.container(c, []), "mp4")
        # Subtitles do not force Matroska here; they are kept as separate .srt files.
        self.assertEqual(model.container(c, [{}]), "mp4")
        self.assertEqual(model.container(automatic, [{}]), "mkv")
        # A different choice, so a different archive identity, but the old one is untouched.
        item = {"id": "x", "choice": automatic, "subtitles": []}
        self.assertNotEqual(model.variant(item), model.variant({**item, "choice": c}))

    def test_compatible_mp4_uses_av1_above_1080p_and_never_vp9(self):
        info = synthetic()
        for f in info["formats"]:
            if f.get("height") in (1440, 4320):
                f["vcodec"] = "vp09.00.50.08"
            if f.get("height") == 2160:
                f["vcodec"] = "av01.0.12M.08"
        self.assertEqual(model.qualities(info, compatible=True), [2160, 1080, 720])
        self.assertEqual(model.qualities(info), [4320, 2160, 1440, 1080, 720])
        best = model.pick(info, quality="best", compatible=True)
        self.assertEqual((best["video_format"], best["actual_quality"]), ("2160", 2160))
        self.assertEqual(model.container(best, []), "mp4")
        # 1440p exists only as VP9, so the nearest lower tier is used instead.
        self.assertEqual(model.pick(info, quality=1440, compatible=True)["actual_quality"], 1080)

    def test_compatible_mp4_prefers_h264_over_av1_at_the_same_resolution(self):
        info = synthetic()
        info["formats"].append(
            {
                "format_id": "1080av1",
                "width": 1920,
                "height": 1080,
                "fps": 60,
                "vcodec": "av01.0.09M.08",
                "acodec": "none",
            }
        )
        self.assertEqual(model.pick(info, quality=1080, compatible=True)["video_format"], "1080")

    def test_compatible_mp4_edge_cases(self):
        info = synthetic()
        only_opus = {**info, "formats": [f for f in info["formats"] if f.get("acodec") != "mp4a"]}
        c = model.pick(only_opus, quality=1080, compatible=True)
        self.assertEqual(c["audio_format_id"], "a2")  # no AAC: the best stream is kept
        self.assertEqual(model.container(c, []), "mkv")
        only_vp9 = {
            **info,
            "formats": [
                {**f, "vcodec": "vp09.00.40.08"} if f.get("vcodec") == "avc1" else f
                for f in info["formats"]
            ],
        }
        with self.assertRaisesRegex(model.SelectionError, "H.264/AV1"):
            model.pick(only_vp9, quality=1080, compatible=True)
        # A stream picked by hand is used as it is.
        manual = model.pick(info, quality=1080, video_id="1080b", audio_id="a2", compatible=True)
        self.assertEqual((manual["video_format"], manual["audio_format_id"]), ("1080b", "a2"))
        self.assertEqual(model.container(manual, [{}]), "mkv")
        audio = model.pick(info, mode="audio", compatible=True)
        self.assertNotIn("compatible", audio)
        video_only = model.pick(info, mode="video", quality=1080, compatible=True)
        self.assertEqual(model.container(video_only, []), "mp4")
        self.assertIsNone(video_only["audio_format_id"])

    def test_codec_names(self):
        for raw, name in (
            ("avc1.640028", "H.264"),
            ("vp09.00.40.08", "VP9"),
            ("av01.0.08M.08", "AV1"),
            ("hvc1.1.6.L93.B0", "H.265"),
            ("mp4a.40.2", "AAC"),
            ("opus", "Opus"),
            ("ec-3", "EC-3"),
            (None, "?"),
        ):
            self.assertEqual(model.codec_name(raw), name)

    def test_tracks_and_variants(self):
        info = {
            "subtitles": {
                "en": [{"url": "https://x/sub?lang=en", "name": "English", "ext": "vtt"}]
            },
            "automatic_captions": {
                "ja-en": [
                    {
                        "url": "https://x/sub?lang=en&tlang=ja",
                        "name": "Japanese from English",
                        "ext": "vtt",
                    }
                ],
                "en-orig": [{"url": "https://x/sub?lang=en", "name": "English", "ext": "vtt"}],
                "en": [{"url": "https://x/sub?lang=en", "name": "English", "ext": "vtt"}],
            },
        }
        ts = model.tracks(info)
        self.assertEqual(len(ts), 3)
        self.assertEqual(ts[1]["kind"], "translated")
        self.assertEqual(ts[1]["source"], "en")
        item = {
            "id": "x",
            "title": "a/b",
            "choice": model.pick(synthetic(), quality=1080),
            "subtitles": [],
        }
        key = model.variant(item)
        other = copy.deepcopy(item)
        other["choice"]["estimated_size"] = 999
        self.assertEqual(key, model.variant(other))
        other["choice"]["quality"] = 1200
        self.assertEqual(key, model.variant(other))
        other["choice"] = model.pick(synthetic(), quality=1440)
        self.assertNotEqual(key, model.variant(other))
        self.assertNotIn("/", model.basename(item))

    def test_indices_and_error_classification(self):
        self.assertEqual(model.item_indices("1,3,5-7", 7), [0, 2, 4, 5, 6])
        for bad in ("0", "8", "3-1", "x"):
            with self.assertRaises(model.SelectionError):
                model.item_indices(bad, 7)
        self.assertEqual(model.classify("Sign in to confirm your age"), "authentication")
        self.assertEqual(model.classify("HTTP 429"), "rate_limit")
        self.assertEqual(model.classify("Private video"), "unavailable")
        with self.assertRaises(model.SelectionError):
            model.valid_url("https://evil.example/video")


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        web = cls.root / "web"
        web.mkdir()

        def ff(*args):
            subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)

        ff(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-an",
            str(web / "v.mp4"),
        )
        ff(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-c:a",
            "aac",
            str(web / "a.m4a"),
        )
        (web / "en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nHello\n")
        (web / "ja.vtt").write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nこんにちは\n", encoding="utf-8"
        )

        class Handler(SimpleHTTPRequestHandler):
            flaky = 0

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(web), **kwargs)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.folder.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_directory, self.temp)
        self.root = Path(self.temp.name)
        self.state = self.root / ".state"
        self.state.mkdir()
        self.media = self.root / "media"
        fixture = {
            "duration": 1,
            "channel": "Local",
            "formats": [
                {
                    "format_id": "a",
                    "url": self.host + "/a.m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "ext": "m4a",
                    "language": "en",
                    "language_preference": 10,
                },
                {
                    "format_id": "v",
                    "url": self.host + "/v.mp4",
                    "acodec": "none",
                    "vcodec": "avc1",
                    "ext": "mp4",
                    "width": 160,
                    "height": 90,
                    "fps": 10,
                },
            ],
            "subtitles": {
                "en": [{"url": self.host + "/en.vtt?lang=en", "ext": "vtt", "name": "English"}]
            },
            "automatic_captions": {
                "ja-en": [
                    {
                        "url": self.host + "/ja.vtt?lang=en&tlang=ja",
                        "ext": "vtt",
                        "name": "Japanese from English",
                    }
                ]
            },
        }
        (self.root / "fixture.json").write_text(json.dumps(fixture))
        (self.root / "worker.py").write_text(
            "import sys,runpy\nsys.argv.insert(1,"
            + repr(str(BASE))
            + ")\nrunpy.run_path("
            + repr(str(BASE / "tests/fixture_worker.py"))
            + ',run_name="__main__")\n'
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (runner, store, app):
            self.stack.enter_context(patch.object(module, "STATE", self.state))
        self.stack.enter_context(patch.object(runner, "BASE", self.root))

    def inspect(self):
        return runner.run({"action": "inspect", "url": "fixture:sample"})

    def item(self, mode="video-audio", subs=False, audio_format="original", compatible=False):
        info = self.inspect()
        return {
            "id": "sample",
            "title": "Local fixture",
            "url": "fixture:sample",
            "choice": model.pick(info, mode, 90, audio_format=audio_format, compatible=compatible),
            "subtitles": model.tracks(info) if subs else [],
            "status": "pending",
        }

    def download(self, item, **extras):
        events = []
        result = runner.run(
            {"action": "download", "item": item, "output": str(self.media), **extras}, events.append
        )
        return result, events

    def test_real_merge_video_only_audio_and_mp3(self):
        for mode, audio_format in (
            ("video-audio", "original"),
            ("video", "original"),
            ("audio", "original"),
            ("audio", "mp3"),
        ):
            item = self.item(mode, audio_format=audio_format)
            result, events = self.download(item)
            probe = store.probe(result["path"], mode)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertTrue(any(x["type"] == "progress" for x in events))
            self.assertTrue(any(x["type"] == "stage" for x in events))
            if mode == "video-audio":
                self.assertEqual(Path(result["path"]).suffix, ".mp4")
            if mode != "audio":
                self.assertEqual(
                    next(s["codec_name"] for s in probe["streams"] if s["codec_type"] == "video"),
                    "h264",
                )
            self.assertFalse((self.root / ".state/temporary" / model.variant(item)).exists())

    def test_compatible_mp4_keeps_subtitles_outside_the_file_and_labels_the_language(self):
        item = self.item(subs=True, compatible=True)
        result, _ = self.download(item)
        path = Path(result["path"])
        self.assertEqual(path.suffix, ".mp4")
        data = store.probe(path, "video-audio")
        self.assertEqual(
            [s["codec_type"] for s in data["streams"] if s["codec_type"] == "subtitle"], []
        )
        self.assertEqual(len(result["subtitles"]), 2)
        for track in result["subtitles"]:
            self.assertGreater(Path(track["path"]).stat().st_size, 0)
            self.assertEqual(Path(track["path"]).suffix, ".srt")
        audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
        self.assertEqual((audio["codec_name"], audio["tags"]["language"]), ("aac", "eng"))
        # Index first: playback can start before the whole file has been read.
        raw = path.read_bytes()
        self.assertLess(raw.index(b"moov"), raw.index(b"mdat"))

    def test_subtitles_embedding_and_retry_without_redownload(self):
        item = self.item(subs=True)
        item["subtitles"].append(
            {
                "key": "zz",
                "origin": "subtitles",
                "language": "zz",
                "kind": "manual",
                "name": "Missing",
                "source": "",
            }
        )
        result, events = self.download(item)
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(len(result["subtitles"]), 2)
        data = store.probe(result["path"], "video-audio")
        self.assertEqual(sum(s["codec_type"] == "subtitle" for s in data["streams"]), 2)
        result2, events2 = self.download(item, existing_file=result["path"])
        self.assertFalse(any(e["type"] == "start" for e in events2))
        self.assertEqual(len(result2["missing"]), 1)

    def test_archive_skip_deleted_recovery_and_collision(self):
        item = self.item()
        result, _ = self.download(item)
        key = model.variant(item)
        archive = store.Archive()
        self.addCleanup(archive.close)
        archive.put(key, result)
        self.assertIsNotNone(archive.get(key, "video-audio"))
        alternate = self.root / "alternate"
        alternate.mkdir()
        second = alternate / Path(result["path"]).name
        import shutil

        shutil.copy2(result["path"], second)
        archive.put(key, {**result, "path": str(second)})
        self.assertEqual(archive.get(key, "video-audio", self.media)["path"], result["path"])
        self.assertEqual(archive.get(key, "video-audio", alternate)["path"], str(second))
        second.unlink()
        with self.assertRaises(runner.WorkerError):
            self.download(item)
        Path(result["path"]).unlink()
        self.assertIsNone(archive.get(key, "video-audio"))
        result, _ = self.download(item)
        archive.put(key, result)
        self.assertIsNotNone(archive.get(key, "video-audio"))

    def test_oversized_download_log_is_rotated_when_a_worker_starts(self):
        log = self.state / "download.log"
        log.write_bytes(b"x" * (5 * 1024**2 + 1))
        self.inspect()
        self.assertEqual((self.state / "download.log.1").stat().st_size, 5 * 1024**2 + 1)
        self.assertLess(log.stat().st_size, 1024**2)

    def test_worker_crash_and_auth_errors(self):
        for name, category in (
            ("auth", "authentication"),
            ("gone", "unavailable"),
            ("error", "system"),
            ("crash", "system"),
        ):
            with self.assertRaises(runner.WorkerError) as caught:
                runner.run({"action": "inspect", "url": "fixture:" + name})
            self.assertEqual(caught.exception.category, category)

    def test_batch_breaker_and_checkpoint(self):
        item = self.item()
        job = store.new_job(
            [{**copy.deepcopy(item), "id": str(i)} for i in range(5)], str(self.media)
        )
        with (
            patch.object(
                app, "run", side_effect=runner.WorkerError("API structure changed", "system")
            ) as mock,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(ValueError):
                app.execute(ui.UI(True), job)
        self.assertEqual(mock.call_count, 3)
        self.assertEqual(store.load_job(job["id"])["status"], "blocked")

    def test_interrupt_checkpoint_and_lock(self):
        item = self.item()
        job = store.new_job([item], str(self.media))
        with (
            patch.object(app, "run", side_effect=KeyboardInterrupt),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                app.execute(ui.UI(True), job)
        self.assertEqual(store.load_job(job["id"])["status"], "interrupted")
        with store.lock():
            with self.assertRaises(ValueError):
                with store.lock():
                    pass


class Display(unittest.TestCase):
    def test_widths_colors_and_fraction(self):
        for width in (40, 60, 100, 160):
            screen = ui.Session(ui.UI(True), 10)
            screen.overall.update(screen.oid, completed=5)
            screen.event({"type": "start", "name": "long " * 100, "stream": "video"})
            screen.event(
                {
                    "type": "progress",
                    "downloaded": 50 * 1024**2,
                    "total": 100 * 1024**2,
                    "speed": 1024**2,
                    "eta": 50,
                }
            )
            buffer = io.StringIO()
            Console(file=buffer, width=width, no_color=True).print(screen)
            lines = buffer.getvalue().splitlines()
            self.assertEqual(len(lines), 7)
            self.assertTrue(all(len(x) <= width for x in lines))
            self.assertIn("50%", lines[0])
            self.assertIn("█", lines[0])
        self.assertEqual(ui.SolidBar(50, 100, ui.PINK).text(20).plain, "█" * 10 + "░" * 10)

    def tty(self, no_color=False, interrupt=False):
        with Terminal(
            [sys.executable, str(BASE / "app.py"), "preview"], BASE, no_color=no_color
        ) as term:
            result = ""
            sent = False
            deadline = time.monotonic() + 15
            while term.running() and time.monotonic() < deadline:
                result += term.read(0.1)
                if interrupt and not sent and "█" in result:
                    term.interrupt()
                    sent = True
            result += term.drain()
            self.assertEqual(term.wait(), 130 if interrupt else 0, result)
            return result

    def test_tty_plain_no_color_and_interrupt(self):
        out = self.tty()
        for rgb in ("168;216;240", "255;194;212", "232;56;79"):
            self.assertIn("\x1b[38;2;" + rgb + "m", out)
        self.assertRegex(out, r"\x1b\[[1-9][0-9]*A")
        self.assertIn("\x1b[?25h", out)
        out = self.tty(no_color=True)
        self.assertNotIn("\x1b[38;", out)
        self.assertIn("█", out)
        out = self.tty(interrupt=True)
        self.assertIn("Dihentikan", out)
        self.assertIn("\x1b[?25h", out)
        result = subprocess.run(
            [sys.executable, str(BASE / "app.py"), "preview", "--plain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("█", result.stderr)
        self.assertLess(len(result.stderr.splitlines()), 25)

    def test_animation_pause_and_reset(self):
        screen = ui.Session(ui.UI(True), 1)
        screen.event({"type": "start", "name": "x"})
        self.assertIsNotNone(screen.clock.resumed_at)
        for kind in ("wait", "failure", "stage", "retry"):
            screen.event({"type": kind, "message": "test"})
            self.assertIsNone(screen.clock.resumed_at)
            screen.event({"type": "progress", "downloaded": 1})
            self.assertIsNotNone(screen.clock.resumed_at)
        screen.event({"type": "start", "name": "y"})
        self.assertEqual(screen.downloaded, 0)
        self.assertIsNone(screen.byte_total)


def rich_ui(width=80):
    """A UI that renders through rich into a buffer, whatever the test runner's terminal is."""
    screen = ui.UI(True)
    screen.plain = False
    screen.glyph = ui.GLYPHS
    buffer = io.StringIO()
    screen.console = Console(file=buffer, width=width, no_color=True, highlight=False)
    return screen, buffer


class Presentation(unittest.TestCase):
    def test_clock_size_and_home_shortening(self):
        self.assertEqual(ui.clock(213), "3:33")
        self.assertEqual(ui.clock(3723), "1:02:03")
        self.assertEqual(ui.clock(None), "0:00")
        if sys.platform != "win32":
            self.assertEqual(ui.tidy(Path.home() / "Downloads"), "~/Downloads")
            self.assertEqual(ui.tidy("/elsewhere/file"), "/elsewhere/file")

    def test_answered_line_keeps_symbol_counter_label_and_answer(self):
        screen, buffer = rich_ui()
        screen._answered("Kualitas", "1080p", (2, 5))
        screen._answered("Link", "https://youtu.be/x")
        first, second = buffer.getvalue().splitlines()
        self.assertEqual(first.split(), ["◆", "2/5", "Kualitas", "1080p"])
        self.assertEqual(second.split(), ["◆", "Link", "https://youtu.be/x"])
        self.assertEqual(first.index("Kualitas"), second.index("Link"))
        plain = ui.UI(True)
        with redirect_stderr(io.StringIO()) as captured:
            plain._answered("Kualitas", "1080p", (2, 5))
        self.assertEqual(captured.getvalue(), "[2/5] Kualitas: 1080p\n")

    def test_one_row_becomes_a_card_and_many_rows_a_truncated_table(self):
        screen, buffer = rich_ui(60)
        screen.table(("Video", "Kualitas"), [("Judul", "1080p")])
        lines = buffer.getvalue().splitlines()
        self.assertEqual([x.split() for x in lines], [["Video", "Judul"], ["Kualitas", "1080p"]])
        screen, buffer = rich_ui(60)
        long_title = "Judul yang sangat panjang " * 6
        screen.table(
            ("Video", "Kualitas", "Berkas"), [(long_title, "1080p", "MP4"), ("B", "720p", "MKV")]
        )
        lines = buffer.getvalue().splitlines()
        self.assertTrue(all(len(x) <= 60 for x in lines))
        self.assertIn("…", lines[1])
        self.assertTrue(any("720p" in x for x in lines))

    def test_report_lists_files_sizes_failures_and_technical_details(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "out"
            (root / "video").mkdir(parents=True)
            (root / "video/a.mp4").write_bytes(b"x" * 2048)
            (root / "video/b.mp4").write_bytes(b"x" * 10)
            job = {
                "id": "abc123",
                "status": "partial",
                "output": str(root),
                "items": [
                    {
                        "title": "A",
                        "status": "complete",
                        "result": {"path": str(root / "video/a.mp4")},
                    },
                    {
                        "title": "B",
                        "status": "partial",
                        "result": {"path": str(root / "video/b.mp4")},
                    },
                    {"title": "C", "status": "failed", "error": {"message": "Private video"}},
                    {"title": "D", "status": "pending"},
                ],
            }
            screen = ui.UI(True)
            with redirect_stderr(io.StringIO()) as captured:
                screen.report(job, Path(folder) / "download.log")
                screen.error("Ada item belum selesai", Path(folder) / "download.log")
            lines = captured.getvalue().splitlines()
            self.assertIn("[+] video/a.mp4 (2.0 KB)", lines)
            self.assertIn("[!] video/b.mp4 (10 B · subtitle belum lengkap)", lines)
            self.assertIn("[x] C (Private video)", lines)
            self.assertIn("[-] D (belum diproses)", lines)
            self.assertIn("Job: abc123 · partial", lines)
            self.assertEqual(sum(x.startswith("Log: ") for x in lines), 1)
            self.assertIn("! GAGAL · Ada item belum selesai", lines)
            screen, buffer = rich_ui(70)
            screen.report(job, Path(folder) / "download.log")
            text = buffer.getvalue()
            for expected in ("✓", "✗", "video/a.mp4", "Folder", "Job", "abc123", "Log"):
                self.assertIn(expected, text)

    def test_report_caps_long_batches(self):
        items = [
            {"title": f"T{i}", "status": "failed", "error": {"message": "x"}} for i in range(45)
        ]
        job = {"id": "j", "status": "partial", "output": "/tmp/none", "items": items}
        with redirect_stderr(io.StringIO()) as captured:
            ui.UI(True).report(job, "/tmp/none/log")
        lines = captured.getvalue().splitlines()
        self.assertEqual(sum(x.startswith("[x]") for x in lines), ui.MAX_REPORT_ROWS)
        self.assertTrue(any(x.startswith("+15 item lain") for x in lines))

    def test_session_exit_marks_failures_and_keeps_stop_wording(self):
        screen, buffer = rich_ui(70)
        with ui.Session(screen, 2) as session:
            session.live = None
            session.item_end("failed")
            session.item_end("complete")
        text = buffer.getvalue()
        self.assertIn("! Selesai · 2/2 item", text)
        self.assertIn("Disimpan 1 · Dilewati 0 · Gagal 1", text)

    def test_activity_updates_its_message_in_both_modes(self):
        with redirect_stderr(io.StringIO()) as captured:
            with ui.UI(True).activity("Mulai") as update:
                update("Mulai · 2/3")
        self.assertEqual(captured.getvalue().splitlines(), ["Mulai", "Mulai · 2/3"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
