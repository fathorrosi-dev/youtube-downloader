"""Offline regression tests: real yt-dlp, FFmpeg and local HTTP media."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
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

    def test_phases_follow_the_mode_and_the_subtitles(self):
        info = synthetic()

        def plan(mode, subtitles=()):
            item = {"choice": model.pick(info, mode, 1080), "subtitles": list(subtitles)}
            return model.phases(item)

        self.assertEqual(plan("video-audio"), ["metadata", "video", "audio", "finish"])
        self.assertEqual(plan("video"), ["metadata", "video", "finish"])
        self.assertEqual(plan("audio"), ["metadata", "audio", "finish"])
        self.assertEqual(
            plan("video-audio", [{}]), ["metadata", "video", "audio", "subtitle", "finish"]
        )
        # Items saved by an older version have no "subtitles" key at all.
        self.assertEqual(
            model.phases({"choice": {"mode": "audio"}}), ["metadata", "audio", "finish"]
        )

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

    def test_an_empty_answer_never_selects_every_item(self):
        for empty in ("", "   ", None):
            with self.assertRaisesRegex(model.SelectionError, "all untuk semua"):
                model.item_indices(empty, 40)
        # "all" is still there, spelled out, in either language and any case.
        self.assertEqual(model.item_indices("all", 3), [0, 1, 2])
        self.assertEqual(model.item_indices(" Semua ", 2), [0, 1])

    def test_classification_reads_phrases_and_statuses_not_loose_substrings(self):
        for message, kind in (
            # The format error is its own kind: it is not about the video, so it must count
            # toward the three-failures stop and never be skipped as "unavailable".
            ("ERROR: [youtube] abc: Requested format is not available", "format"),
            ("ERROR: [youtube] abc: This video is not available", "unavailable"),
            ("Video unavailable. Blocked it on copyright grounds", "unavailable"),
            ("The uploader has not made this video available in your country", "unavailable"),
            # "429" inside a video id is not a rate limit; as a status it is.
            ("ERROR: [youtube] aB429cdEfGh: Something went wrong", "system"),
            ("HTTP Error 429: Too Many Requests", "rate_limit"),
            ("Status code: 429", "rate_limit"),
            ("HTTP Error 503: Service Unavailable", "network"),
            ("<urlopen error [Errno 111] Connection refused>", "network"),
            ("unable to download video data: HTTP Error 403: Forbidden", "system"),
            ("Unexpected API structure", "system"),
        ):
            self.assertEqual(model.classify(message), kind, message)

    def test_browser_values_and_release_age(self):
        for good in (None, "", "firefox", "firefox:/home/me/profile"):
            self.assertEqual(model.valid_browser(good), good)
        with self.assertRaises(model.SelectionError):
            model.valid_browser("chrome")
        from datetime import date

        today = date(2026, 10, 15)
        self.assertEqual(model.release_age_days("2026.8.19", today), 57)
        self.assertEqual(model.release_age_days("2026.10.15", today), 0)
        self.assertEqual(model.release_age_days("2026.10.20", today), 0)  # never negative
        for not_a_date in ("1.3.0", "", None, "2026.13.40", "x"):
            self.assertIsNone(model.release_age_days(not_a_date, today))


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

    @staticmethod
    def announced(events):
        """The stages the worker announced, in order, without repeats."""
        return list(
            dict.fromkeys(e["phase"] for e in events if e["type"] == "stage" and "phase" in e)
        )

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
            # The panel's stage row is drawn from model.phases, so the worker must follow it.
            self.assertEqual(self.announced(events), model.phases(item))
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
        self.assertEqual(self.announced(events), model.phases(item))
        data = store.probe(result["path"], "video-audio")
        self.assertEqual(sum(s["codec_type"] == "subtitle" for s in data["streams"]), 2)
        result2, events2 = self.download(item, existing_file=result["path"])
        self.assertFalse(any(e["type"] == "start" for e in events2))
        # Nothing is downloaded again: the stream stages are skipped, the order does not change.
        self.assertEqual(self.announced(events2), ["metadata", "subtitle", "finish"])
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
        # A valid file at the result path with no record to say so is adopted, not an error:
        # nothing is downloaded or rewritten, and the same result comes back.
        adopted, events = self.download(item)
        self.assertEqual(adopted["path"], result["path"])
        self.assertFalse(any(e["type"] in ("start", "progress") for e in events))
        # A file that does not pass the media check is never touched or adopted.
        original = Path(result["path"]).read_bytes()
        Path(result["path"]).write_bytes(b"not media")
        with self.assertRaisesRegex(runner.WorkerError, "Benturan hasil"):
            self.download(item)
        self.assertEqual(Path(result["path"]).read_bytes(), b"not media")
        self.assertNotEqual(original, b"not media")
        Path(result["path"]).unlink()
        self.assertIsNone(archive.get(key, "video-audio"))
        result, _ = self.download(item)
        archive.put(key, result)
        self.assertIsNotNone(archive.get(key, "video-audio"))

    def extractions(self):
        log = self.root / "extractions.log"
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def test_one_extraction_serves_every_stream_of_an_item(self):
        item = self.item()
        before = len(self.extractions())
        result, _ = self.download(item)
        self.assertTrue(Path(result["path"]).is_file())
        # The metadata check, and nothing more: the video and the audio stream reuse it.
        self.assertEqual(len(self.extractions()) - before, 1)

    def scripted(self, *flags):
        """app.main() on the fixture link: the real worker and FFmpeg, no menu and no terminal."""
        out, err = io.StringIO(), io.StringIO()
        settings = {"output": str(self.media), "browser": None}
        with (
            patch.object(app, "valid_url", side_effect=lambda url: url),
            patch.object(app, "settings", return_value=settings),
            patch.object(app.sys.stdin, "isatty", return_value=False, create=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = app.main(["fixture:sample", "--plain", "--yes", *flags])
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_a_link_with_flags_downloads_with_no_menu_and_prints_the_path(self):
        code, paths, err = self.scripted("--subs", "en")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(paths), 1)
        video = Path(paths[0])
        self.assertEqual(video.suffix, ".mkv")  # embedded subtitles need MKV in Otomatis mode
        store.probe(video, "video-audio")  # a real, valid file
        self.assertEqual(len(list((self.media / "subtitle").glob("*.srt"))), 1)
        self.assertIn("Selesai", err)
        # The same link again changes nothing and still reports the file, from the archive.
        code, again, err = self.scripted("--subs", "en")
        self.assertEqual((code, again), (0, paths), err)

    def test_scripted_audio_as_mp3_and_a_refused_flag(self):
        code, paths, err = self.scripted("--mode", "audio", "--audio-format", "mp3")
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(paths[0]).suffix, ".mp3")
        code, paths, err = self.scripted("--quality", "9999")
        self.assertEqual((code, paths), (1, []))
        self.assertIn("--quality 9999", err)
        self.assertNotIn("Traceback", err)

    def test_a_finished_file_with_no_record_is_adopted_with_its_subtitles(self):
        item = self.item(subs=True)
        result, _ = self.download(item)
        before = len(self.extractions())
        again, events = self.download(item)
        self.assertEqual((again["path"], len(again["subtitles"])), (result["path"], 2))
        self.assertEqual(again["missing"], [])
        self.assertEqual(self.announced(events), ["finish"])
        # No extraction, no stream, no FFmpeg: the file is exactly what it was.
        self.assertEqual(len(self.extractions()), before)
        self.assertFalse((self.root / ".state/temporary" / model.variant(item)).exists())

    def test_adopting_a_file_reports_the_subtitles_that_are_gone(self):
        item = self.item(subs=True)
        result, _ = self.download(item)
        Path(result["subtitles"][0]["path"]).unlink()
        again, _ = self.download(item)
        self.assertEqual((len(again["subtitles"]), len(again["missing"])), (1, 1))

    def test_a_result_that_cannot_be_recorded_fails_one_item_not_the_batch(self):
        item = self.item()
        job = store.new_job(
            [{**copy.deepcopy(item), "id": "a"}, {**copy.deepcopy(item), "id": "b"}],
            str(self.media),
        )
        results = []

        def fake_run(request, callback=None):
            path = self.media / "video" / (request["item"]["id"] + ".mp4")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            results.append(path)
            return {"path": str(path), "mode": "video-audio", "subtitles": [], "missing": []}

        with (
            patch.object(app, "run", side_effect=fake_run),
            patch.object(store.Archive, "put", side_effect=ValueError("FFprobe menolak")),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, "belum selesai"):
                app.execute(ui.UI(True), job)
        saved = store.load_job(job["id"])
        # Both items were tried: the first failure did not stop the batch.
        self.assertEqual(len(results), 2)
        self.assertEqual([i["status"] for i in saved["items"]], ["failed", "failed"])
        self.assertIn("belum tercatat", saved["items"][0]["error"]["message"])

    def test_a_download_reports_where_its_time_went_and_shows_its_pauses(self):
        item = self.item(subs=True)
        pace = {"name": "test", "file": [0.2, 0.2], "requests": 0}
        result, events = self.download(item, pace=pace)
        timings = result["timings"]
        for phase in model.TIMING_PHASES:
            self.assertIn(phase, timings)
            self.assertGreaterEqual(timings[phase], 0)
        self.assertLessEqual(sum(timings[p] for p in model.TIMING_PHASES), timings["total"] + 0.01)
        self.assertEqual(
            model.clean_timings(timings).keys() - {"speed"}, timings.keys() - {"speed"}
        )
        self.assertGreater(timings["bytes"], 0)
        # One pause of 0.2 s before each of the two streams, announced before the stream starts,
        # and one between the two subtitle tracks (none before the first).
        waits = [i for i, e in enumerate(events) if e["type"] == "wait"]
        pauses = [i for i in waits if "antar-unduhan" in events[i]["message"]]
        between_subtitles = [i for i in waits if "antar-subtitle" in events[i]["message"]]
        self.assertEqual((len(pauses), len(between_subtitles)), (2, 1))
        self.assertAlmostEqual(timings["waited"], 0.6, delta=0.05)
        for index in pauses:
            self.assertIn("jeda antar-unduhan", events[index]["message"])
            self.assertEqual(events[index - 1]["type"], "stage")
            self.assertIn(events[index - 1]["phase"], ("video", "audio"))
            self.assertEqual(events[index + 1]["type"], "start")

    def test_a_profile_without_a_pause_sends_no_wait(self):
        pace = {"name": "none", "file": [0, 0], "requests": 0}
        result, events = self.download(self.item(), pace=pace)
        self.assertEqual([e for e in events if e["type"] == "wait"], [])
        self.assertEqual(result["timings"]["waited"], 0)

    def run_item_with(self, result_extra, item_extra=None):
        item = {**copy.deepcopy(self.item()), **(item_extra or {})}
        job = store.new_job([item], str(self.media))
        sent = []

        def fake_run(request, callback=None):
            sent.append(request)
            path = self.media / "video" / "a.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            return {
                "path": str(path),
                "mode": "video-audio",
                "subtitles": [],
                "missing": [],
                **result_extra,
            }

        with (
            patch.object(app, "run", side_effect=fake_run),
            patch.object(store.Archive, "put") as put,
            redirect_stderr(io.StringIO()),
        ):
            app.execute(ui.UI(True), job)
        return store.load_job(job["id"]), put, sent

    def test_the_timings_are_kept_with_the_item_and_not_in_the_archive(self):
        reported = {
            "total": 5.0,
            "video": 2.0,
            "waited": 1.0,
            "bytes": 100,
            "speed": 50.0,
            "url": "https://example.test/?sig=secret",  # not a timing: must not be kept
        }
        saved, put, sent = self.run_item_with({"timings": reported})
        timings = saved["items"][0]["timings"]
        self.assertEqual(set(timings) - set(model.TIMING_KEYS), set())
        self.assertIn("wall", timings)  # measured by the app around the worker
        self.assertGreaterEqual(timings["wall"], 0)
        self.assertEqual((timings["total"], timings["video"]), (5.0, 2.0))
        self.assertNotIn("timings", put.call_args[0][1])
        self.assertNotIn("timings", saved["items"][0]["result"])
        self.assertNotIn("secret", json.dumps(saved))
        self.assertEqual(sent[0]["pace"], model.pace_profile())

    def test_an_item_run_again_without_timings_loses_the_old_ones(self):
        saved, _, _ = self.run_item_with({}, {"timings": {"total": 99.0}})
        self.assertNotIn("timings", saved["items"][0])

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

    def tty(self, no_color=False, interrupt=False, extra=None, shell=None):
        command = [sys.executable, str(BASE / "app.py"), "preview"]
        if shell:  # for example a redirection: a shell command line that wraps the preview
            command = ["sh", "-c", shell.format(" ".join(command))]
        with Terminal(command, BASE, extra=extra, no_color=no_color) as term:
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
        # Sky blue and pink carry the preview; the dark red of a failure is not in it (nothing
        # fails), and the active bar is no longer red.
        for rgb in ("168;216;240", "255;194;212"):
            self.assertIn("\x1b[38;2;" + rgb + "m", out)
        self.assertNotIn("\x1b[38;2;255;84;112m", out)
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
        lines = result.stderr.splitlines()
        # Plain output is a log, not a byte counter: a progress line about every tenth of each
        # stream (the preview has three items of two streams), and nothing per update.
        beats = [x for x in lines if x.startswith(("  video ", "  audio "))]
        self.assertTrue(beats)
        self.assertLessEqual(len(beats), 3 * 2 * 10)
        self.assertLess(len(lines), 100)

    @unittest.skipIf(os.name == "nt", "Windows takes no hint from TERM; see the next test")
    def test_color_depth_follows_the_terminal(self):
        # No COLORTERM and a 256-color TERM: the palette steps down to the 256-color table
        # instead of sending 24-bit sequences the terminal would show wrongly.
        out = self.tty(extra={"COLORTERM": ""})
        self.assertNotIn("\x1b[38;2;", out)
        self.assertIn("\x1b[38;5;", out)
        self.assertIn("█", out)
        # A 16-color terminal gets the basic colors.
        out = self.tty(extra={"COLORTERM": "", "TERM": "xterm"})
        self.assertNotIn("\x1b[38;2;", out)
        self.assertNotIn("\x1b[38;5;", out)
        self.assertRegex(out, r"\x1b\[(?:[0-9];)*(?:3[0-7]|9[0-7])m")

    @unittest.skipUnless(os.name == "nt", "the Windows rule, in a real console")
    def test_a_windows_console_shows_24_bits_whatever_the_environment_says(self):
        if sys.getwindowsversion().build < 15063:
            self.skipTest("older than the first Windows build with 24-bit console colour")
        for extra in ({"COLORTERM": ""}, {"COLORTERM": "", "TERM": "xterm"}):
            out = self.tty(extra=extra)
            self.assertIn("\x1b[38;2;", out)
            self.assertIn("█", out)

    def test_an_empty_no_color_leaves_the_colors_on(self):
        out = self.tty(extra={"NO_COLOR": ""})
        self.assertIn("\x1b[38;2;", out)

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell for the redirection")
    def test_a_redirected_stdout_does_not_turn_the_display_plain(self):
        # Everything is drawn on stderr, which is still the terminal here.
        out = self.tty(shell="{} > /dev/null")
        self.assertIn("█", out)
        self.assertRegex(out, r"\x1b\[[1-9][0-9]*A")

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

    def test_smoother_fills_the_gap_between_reports_and_never_goes_back(self):
        now = [0.0]
        smooth = ui.Smoother(lambda: now[0])
        self.assertEqual(smooth.value(), 0)
        smooth.update(1000, 1000)
        shown = []
        for moment in (0.0, 0.1, 0.2, 0.3, 5.0):
            now[0] = moment
            shown.append(round(smooth.value()))
        # It advances at the reported speed, and stops one HORIZON after the report.
        self.assertEqual(shown, [1000, 1100, 1200, 1300, 1300])
        # The next report is lower than the estimate: the bar holds instead of stepping back.
        now[0] = 0.3
        smooth.update(1100, 1000)
        held = []
        for moment in (0.3, 0.4, 0.5, 0.6):
            now[0] = moment
            held.append(round(smooth.value()))
        self.assertEqual(held, [1300, 1300, 1300, 1400])
        # A pause freezes the bar where it is; finish() fills it; reset() empties it.
        smooth.freeze()
        now[0] = 9.0
        self.assertEqual(round(smooth.value()), 1400)
        smooth.finish(2000)
        self.assertEqual(smooth.value(), 2000)
        smooth.reset()
        self.assertEqual(smooth.value(), 0)
        # A missing speed (the worker sends null before it has measured one) adds nothing.
        smooth.update(10, None)
        now[0] = 9.1
        self.assertEqual(smooth.value(), 10)

    def test_file_bar_moves_between_worker_reports(self):
        now = [0.0]
        screen = ui.Session(ui.UI(True), 1)
        screen.smoother.now = lambda: now[0]
        screen.smoother.reset()
        screen.event({"type": "start", "name": "x", "stream": "video"})
        screen.event({"type": "progress", "downloaded": 0, "total": 1000, "speed": 1000, "eta": 1})
        column = screen.file.columns[1]

        def empty_cells():
            return column.render(screen.file.tasks[0]).text(40).plain.count("░")

        cells = []
        for moment in (0.0, 0.1, 0.2, 0.3, 0.6):
            now[0] = moment
            cells.append(empty_cells())
        self.assertEqual(cells[0], 40)
        self.assertEqual(cells[:4], sorted(set(cells[:4]), reverse=True))
        self.assertEqual(cells[3], cells[4])
        # A report that comes in below the estimate does not move the bar back.
        now[0] = 0.3
        screen.event({"type": "progress", "downloaded": 150, "total": 1000, "speed": 1000})
        self.assertEqual(empty_cells(), cells[3])
        # The end of the file fills the bar, and the next file starts empty.
        screen.event({"type": "file_done", "downloaded": 1000, "total": 1000})
        self.assertEqual(empty_cells(), 0)
        screen.event({"type": "start", "name": "y", "stream": "audio"})
        # No size yet: the moving block, not a filled bar.
        bar = column.render(screen.file.tasks[0]).text(40).plain
        self.assertEqual((bar.count("█"), bar.count("░")), (3, 37))

    def test_unknown_size_block_moves_a_cell_per_frame(self):
        def start(moment):
            bar = ui.SolidBar(0, None, ui.RED, moment, indicator=True)
            return bar.text(20).plain.index("█")

        frame = 1 / ui.BAR_STEPS_PER_SECOND
        self.assertEqual([start(frame * (n + 0.5)) for n in range(4)], [0, 1, 2, 3])
        self.assertGreaterEqual(ui.FPS, ui.BAR_STEPS_PER_SECOND)

    def test_every_live_display_redraws_at_the_shared_rate(self):
        screen, _ = rich_ui()
        screen.motion = True
        with patch.object(ui, "Live") as live, patch.object(ui.time, "sleep"):
            with ui.Session(screen, 1):  # the panel, then the flourish of a clean finish
                pass
            with screen.activity("x"):
                pass
        rates = [call.kwargs["refresh_per_second"] for call in live.call_args_list]
        self.assertEqual(rates, [ui.FPS, ui.FPS, ui.FPS])
        self.assertGreaterEqual(ui.FPS, 12)

    def test_a_still_activity_does_not_redraw_at_the_animation_rate(self):
        screen, _ = rich_ui()
        screen.motion = False
        with patch.object(ui, "Live") as live:
            with screen.activity("x"):
                pass
        self.assertLess(live.call_args.kwargs["refresh_per_second"], ui.FPS)


def panel(session, width=100, height=30):
    """The lines the panel draws, at a fixed terminal size."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, height=height, no_color=True, highlight=False)
    console.print(session)
    return buffer.getvalue().splitlines()


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

    def test_stage_row_follows_the_worker_and_only_moves_forward(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 1, [{"title": "A"}])
        # One video has no batch to count: file bar, file name, speed and status only.
        self.assertEqual(len(panel(session)), 4)  # no item has started: no stage row yet

        def row():
            return next(x for x in panel(session) if x.startswith("Tahap")).split()

        session.item_start(["metadata", "video", "audio", "finish"])
        self.assertEqual(len(panel(session)), 5)
        self.assertEqual(row()[1:], "● Metadata → ○ Video → ○ Audio → ○ Gabung".split())
        session.event({"type": "stage", "message": "x", "phase": "video"})
        self.assertEqual(row()[1:], "✓ Metadata → ● Video → ○ Audio → ○ Gabung".split())
        # The audio stage is announced by its download, even if the stage event was missed.
        session.event({"type": "start", "name": "n", "stream": "audio"})
        self.assertEqual(row()[1:], "✓ Metadata → ✓ Video → ● Audio → ○ Gabung".split())
        for event in (
            {"type": "stage", "phase": "metadata"},  # never backwards
            {"type": "stage", "phase": "bogus"},  # unknown stages are ignored
            {"type": "stage", "message": "no phase at all"},
            {"type": "notice", "message": "n", "phase": "finish"},  # only stage events count
        ):
            session.event(event)
            self.assertEqual(row()[1:], "✓ Metadata → ✓ Video → ● Audio → ○ Gabung".split())
        session.item_end("complete")
        self.assertEqual(row()[1:], "✓ Metadata → ✓ Video → ✓ Audio → ✓ Gabung".split())

    def test_stage_row_marks_the_failed_stage_and_names_the_last_stage(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 2, [{"title": "A"}, {"title": "B"}])
        session.item_start(["metadata", "video", "audio", "subtitle", "finish"])
        session.event({"type": "stage", "message": "x", "phase": "video"})
        session.item_end("failed")
        line = next(x for x in panel(session) if x.startswith("Tahap"))
        self.assertEqual(
            line.split()[1:], "✓ Metadata → ✗ Video → ○ Audio → ○ Subtitle → ○ Gabung".split()
        )
        # One stream only: nothing is merged, the last stage is just saving.
        session.item_start(["metadata", "audio", "finish"])
        line = next(x for x in panel(session) if x.startswith("Tahap"))
        self.assertEqual(line.split()[1:], "● Metadata → ○ Audio → ○ Simpan".split())
        # A skipped item never ran, so it leaves no stages behind.
        skipped = ui.Session(screen, 1, [{"title": "A"}])
        skipped.item_end("skipped")
        self.assertFalse(any(x.startswith("Tahap") for x in panel(skipped)))

    def test_stage_row_gives_up_names_before_it_overflows(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 1, [{"title": "A"}])
        session.item_start(["metadata", "video", "audio", "subtitle", "finish"])
        session.event({"type": "stage", "message": "x", "phase": "video"})
        full = next(x for x in panel(session, 100) if x.startswith("Tahap"))
        self.assertIn("Metadata", full)
        for width in (60, 40, 30, 20):
            lines = panel(session, width)
            self.assertTrue(all(len(x) <= width for x in lines), (width, lines))
            row = next(x for x in lines if x.startswith("Tahap"))
            self.assertIn("●", row)
            if width <= 40:
                self.assertNotIn("Metadata", row)
                self.assertIn("● Video", row)

    def test_playlist_rows_show_every_state_and_slide_with_the_current_item(self):
        screen, _ = rich_ui()
        items = [{"title": f"Video {n}"} for n in range(1, 6)]
        session = ui.Session(screen, 5, items)

        def glyphs():
            lines = panel(session)
            return [x.split()[0] for x in lines if x.lstrip().startswith(tuple("○●✓✗–!"))]

        self.assertEqual(glyphs(), ["○"] * 5)
        for status in ("complete", "failed", "skipped", "partial"):
            session.item_start(["metadata", "finish"])
            session.item_end(status)
        session.item_start(["metadata", "finish"])
        self.assertEqual(glyphs(), ["✓", "✗", "–", "!", "●"])
        rows = [x for x in panel(session) if "Video" in x]
        self.assertEqual([x.split()[1] for x in rows], ["1", "2", "3", "4", "5"])
        self.assertIn("Video 5", rows[-1])

        many = ui.Session(screen, 12, [{"title": f"Video {n}"} for n in range(1, 13)])

        def titles():
            return [" ".join(x.split()[2:]) for x in panel(many) if "Video" in x]

        self.assertEqual(titles(), [f"Video {n}" for n in range(1, 7)])
        for _ in range(4):
            many.item_start(["metadata", "finish"])
            many.item_end("complete")
        # One finished item stays above the current one; the rest of the window is what is next.
        self.assertEqual(titles(), [f"Video {n}" for n in range(4, 10)])
        for _ in range(7):
            many.item_start(["metadata", "finish"])
            many.item_end("complete")
        self.assertEqual(titles(), [f"Video {n}" for n in range(7, 13)])
        self.assertEqual(len(panel(many)), 7 + 1 + ui.WINDOW_ROWS)

    def test_playlist_rows_are_left_out_when_they_add_nothing_or_do_not_fit(self):
        screen, _ = rich_ui()
        one = ui.Session(screen, 1, [{"title": "Only video"}])
        self.assertFalse(any("Only video" in x for x in panel(one)))
        many = ui.Session(screen, 8, [{"title": f"Video {n}"} for n in range(8)])
        self.assertTrue(any("Video 0" in x for x in panel(many, height=30)))
        # A terminal this short cannot hold the rows and the rest of the panel: Rich would cut
        # the bottom of the panel off, so the rows go.
        short = panel(many, height=12)
        self.assertFalse(any("Video" in x for x in short))
        self.assertEqual(len(short), 7)
        # Items without a title fall back to their id, and never break the panel.
        odd = ui.Session(screen, 3, [{"id": "abc"}, {}, {"title": None, "id": None}])
        self.assertTrue(any("abc" in x for x in panel(odd)))

    def test_panel_fits_every_width_with_plan_and_rows_in_both_glyph_sets(self):
        for plain in (False, True):
            for width in (20, 40, 60, 100, 160):
                screen, _ = rich_ui()
                if plain:
                    screen.glyph = ui.PLAIN_GLYPHS
                items = [{"title": "long " * 40, "id": str(n)} for n in range(9)]
                session = ui.Session(screen, 9, items)
                session.item_start(["metadata", "video", "audio", "subtitle", "finish"])
                session.event({"type": "start", "name": "x" * 300, "stream": "video"})
                session.event({"type": "progress", "downloaded": 5, "total": 10, "speed": 1})
                lines = panel(session, width)
                self.assertTrue(all(len(x) <= width for x in lines), (plain, width, lines))
                self.assertEqual(len(lines), 7 + 1 + ui.WINDOW_ROWS)

    def test_redrawing_while_events_arrive_never_fails(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 30, [{"title": f"Video {n}"} for n in range(30)])
        failures = []

        def worker():
            try:
                for item in range(30):
                    session.item_start(["metadata", "video", "audio", "finish"])
                    session.event({"type": "stage", "message": "x", "phase": "video"})
                    session.event({"type": "start", "name": "n", "stream": "video"})
                    for step in range(40):
                        session.event(
                            {"type": "progress", "downloaded": step, "total": 40, "speed": 5}
                        )
                    session.event({"type": "file_done"})
                    session.item_end("failed" if item % 7 == 3 else "complete")
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        draws = 0
        while thread.is_alive() or draws < 20:
            lines = panel(session)
            self.assertTrue(7 <= len(lines) <= 7 + 1 + ui.WINDOW_ROWS)
            draws += 1
        thread.join()
        self.assertEqual(failures, [])

    def test_a_single_video_panel_leaves_out_the_batch_rows(self):
        screen, _ = rich_ui()
        one = ui.Session(screen, 1, [{"title": "A"}])
        lines = panel(one)
        self.assertFalse(any(x.startswith(("Item", "Hasil", "Durasi")) for x in lines))
        self.assertTrue(any(x.startswith("Status") and "berjalan" in x for x in lines))
        # A batch keeps all of them.
        lines = panel(ui.Session(screen, 2, [{"title": "A"}, {"title": "B"}]))
        for row in ("Item", "Hasil", "Status", "Durasi"):
            self.assertTrue(any(x.startswith(row) for x in lines), row)

    def test_a_single_video_ends_without_a_tally_of_one(self):
        screen, buffer = rich_ui(70)
        with ui.Session(screen, 1) as session:
            session.live = None
            session.item_end("complete")
        text = buffer.getvalue()
        self.assertRegex(text, r"Selesai · \d+d")
        self.assertNotIn("1/1 item", text)
        self.assertNotIn("Disimpan", text)

    def test_a_single_finished_video_is_a_card_not_a_log_line(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "video" / "Judul [abc] - 1080p.mkv"
            video.parent.mkdir()
            video.write_bytes(b"x" * 2048)
            job = {
                "id": "abc123",
                "status": "complete",
                "output": folder,
                "items": [
                    {
                        "title": "Judul",
                        "status": "complete",
                        "result": {"path": str(video)},
                        "choice": {
                            "mode": "video-audio",
                            "actual_quality": 1080,
                            "vcodec": "vp9",
                            "acodec": "opus",
                        },
                    }
                ],
            }
            screen, buffer = rich_ui(80)
            screen.report(job, Path(folder) / "download.log")
            text = buffer.getvalue()
            self.assertIn("Judul", text)
            self.assertIn("1080p · VP9 + Opus · MKV · 2.0 KB", text)
            self.assertIn("video/Judul [abc] - 1080p.mkv", text)
            self.assertIn("abc123", text)
            # Plain output keeps the line per file that scripts and logs already read.
            with redirect_stderr(io.StringIO()) as captured:
                ui.UI(True).report(job, Path(folder) / "download.log")
            self.assertIn("[+] video/Judul [abc] - 1080p.mkv", captured.getvalue())

    def test_plain_progress_is_reported_about_every_tenth_of_a_file(self):
        now = [0.0]
        with redirect_stderr(io.StringIO()) as captured:
            screen = ui.Session(ui.UI(True), 1)
            screen.now = lambda: now[0]
            screen.event({"type": "start", "name": "x", "stream": "video"})
            for got in range(0, 1001, 25):
                screen.event({"type": "progress", "downloaded": got, "total": 1000, "speed": 500})
        lines = [x for x in captured.getvalue().splitlines() if x.startswith("  video")]
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0], "  video 10% · 100 B/1000 B · 500 B/s")
        self.assertTrue(lines[-1].startswith("  video 100%"))
        self.assertTrue(all(line.isascii() or "·" in line for line in lines))
        # The next file counts from zero again.
        with redirect_stderr(io.StringIO()) as captured:
            screen.event({"type": "start", "name": "y", "stream": "audio"})
            screen.event({"type": "progress", "downloaded": 300, "total": 1000, "speed": 1})
        self.assertIn("  audio 30%", captured.getvalue())

    def test_plain_progress_without_a_size_is_reported_once_a_minute(self):
        now = [0.0]
        with redirect_stderr(io.StringIO()) as captured:
            screen = ui.Session(ui.UI(True), 1)
            screen.now = lambda: now[0]
            screen.event({"type": "start", "name": "x", "stream": "video"})
            for moment in (10, 59, 61, 100, 121, 125):
                now[0] = moment
                screen.event({"type": "progress", "downloaded": moment * 10, "speed": 10})
        lines = [x for x in captured.getvalue().splitlines() if x.startswith("  video")]
        self.assertEqual(len(lines), 2)  # at 61 s and at 121 s

    def test_the_live_display_writes_no_plain_progress_lines(self):
        screen, buffer = rich_ui()
        session = ui.Session(screen, 1)
        session.event({"type": "start", "name": "x", "stream": "video"})
        session.event({"type": "progress", "downloaded": 500, "total": 1000, "speed": 1})
        self.assertNotIn("video 50%", buffer.getvalue())

    def test_error_ends_with_the_command_that_carries_on(self):
        with redirect_stderr(io.StringIO()) as captured:
            ui.UI(True).error(
                "Perlu login. Lalu lanjutkan:", None, "./youtube-downloader resume abc"
            )
        lines = captured.getvalue().splitlines()
        self.assertEqual(lines[0], "! GAGAL · Perlu login. Lalu lanjutkan:")
        self.assertEqual(lines[-1], "  ./youtube-downloader resume abc")
        screen, buffer = rich_ui()
        screen.error("Perlu login. Lalu lanjutkan:", None, "./youtube-downloader resume abc")
        self.assertIn("    ./youtube-downloader resume abc", buffer.getvalue())

    def test_the_display_follows_stderr_and_the_environment(self):
        def build(stderr_is_tty, windows=None, vt=True, **environment):
            # `windows` is the Windows version tuple, or None for every other system: the rule
            # that applies is the one asked for, whichever system runs this test.
            fake = io.StringIO()
            fake.isatty = lambda: stderr_is_tty
            env = {"TERM": "xterm-256color", **environment}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(ui, "configure_terminal", return_value=vt),
                patch.object(ui, "windows_build", return_value=windows),
                patch.object(ui.sys, "stderr", fake),
                # stdout is captured or redirected, as it is when the saved paths are collected.
                patch.object(ui.sys.stdout, "isatty", return_value=False, create=True),
            ):
                return ui.UI(False)

        self.assertFalse(build(True).plain)  # a redirected stdout does not make it plain
        self.assertTrue(build(False).plain)
        self.assertTrue(build(True, TERM="dumb").plain)
        self.assertFalse(build(True, NO_COLOR="").no_color)  # empty means not set
        self.assertTrue(build(True, NO_COLOR="1").no_color)
        # One decision for Rich and prompt_toolkit, taken from the terminal and not forced to 24 bits.
        self.assertIs(build(True).depth, ui.ColorDepth.DEPTH_8_BIT)
        self.assertIs(build(True, COLORTERM="truecolor").depth, ui.ColorDepth.DEPTH_24_BIT)
        self.assertIs(build(True, TERM="xterm").depth, ui.ColorDepth.DEPTH_4_BIT)
        self.assertIs(build(True, NO_COLOR="1").depth, ui.ColorDepth.DEPTH_1_BIT)
        self.assertEqual(build(True, COLORTERM="truecolor").console.color_system, "truecolor")
        self.assertEqual(build(True).console.color_system, "256")
        self.assertEqual(build(True, TERM="xterm").console.color_system, "standard")
        # A gradient needs 256 colours; a basic terminal gets the flat bar with a warm head.
        self.assertTrue(build(True, COLORTERM="truecolor").gradient)
        self.assertTrue(build(True).gradient)
        self.assertFalse(build(True, TERM="xterm").gradient)
        self.assertFalse(build(False).gradient)
        # Windows takes no hint from TERM or COLORTERM: the console shows 24 bits from build
        # 10.0.15063 on and 256 colours before, and VT processing decides whether it is a display.
        modern, old = (10, 0, 19045), (10, 0, 14393)
        for term in ("xterm", "xterm-256color"):
            screen = build(True, windows=modern, TERM=term)
            self.assertEqual(screen.console.color_system, "truecolor")
            self.assertIs(screen.depth, ui.ColorDepth.DEPTH_24_BIT)
            self.assertTrue(screen.gradient)
            screen = build(True, windows=old, TERM=term, COLORTERM="truecolor")
            self.assertEqual(screen.console.color_system, "256")
            self.assertIs(screen.depth, ui.ColorDepth.DEPTH_8_BIT)
        self.assertTrue(build(True, windows=modern, vt=False).plain)
        self.assertIs(build(True, windows=modern, NO_COLOR="1").depth, ui.ColorDepth.DEPTH_1_BIT)

    def test_activity_updates_its_message_in_both_modes(self):
        with redirect_stderr(io.StringIO()) as captured:
            with ui.UI(True).activity("Mulai") as update:
                update("Mulai · 2/3")
        self.assertEqual(captured.getvalue().splitlines(), ["Mulai", "Mulai · 2/3"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
