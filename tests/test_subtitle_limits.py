"""How the worker fetches subtitles from a server that limits it: the pause between tracks, the
longest it waits because it was told to, and when it stops asking."""

import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import model
import store
import worker
from test_tools import synthetic

VTT = b"WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nHello\n"


class Handler(BaseHTTPRequestHandler):
    """/ok.vtt is a subtitle, /limit.vtt?after=N answers 429 (with Retry-After N when given),
    /once.vtt answers 429 the first time and a subtitle after that, /gone.vtt answers 404."""

    hits = []
    served_once = set()

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        Handler.hits.append(url.path)
        if url.path == "/limit.vtt" or (
            url.path == "/once.vtt" and url.path not in self.served_once
        ):
            self.served_once.add(url.path)
            self.send_response(429)
            after = parse_qs(url.query).get("after")
            if after:
                self.send_header("Retry-After", after[0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == "/gone.vtt":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(VTT)))
            self.end_headers()
            self.wfile.write(VTT)


class Events:
    def __init__(self):
        self.sent = []
        self.timings = None

    def send(self, kind, **fields):
        self.sent.append({"type": kind, **fields})

    def of(self, kind):
        return [e for e in self.sent if e["type"] == kind]


class Fetching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.hits = []
        Handler.served_once = set()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = Events()
        self.slept = []

    def fetch(self, paths, pace=((1, 1), 0), **kwargs):
        """Run worker.subtitle_files on tracks that are served from `paths` (a path on the test
        server, or None for a track with no formats). Returns (saved, missing)."""
        info = {"subtitles": {}, "automatic_captions": {}, "http_headers": {}}
        tracks = []
        for number, path in enumerate(paths):
            key = f"t{number}"
            if path is not None:
                info["subtitles"][key] = [{"url": self.host + path, "ext": "vtt", "name": key}]
            tracks.append(
                {
                    "key": key,
                    "origin": "subtitles",
                    "language": key,
                    "kind": "manual",
                    "name": "Track " + key,
                    "source": "",
                }
            )
        item = {
            "id": "vid",
            "title": "Video",
            "choice": {"mode": "video-audio", "actual_quality": 1080, "audio_format": "original"},
            "subtitles": tracks,
        }
        (low, high), requests = pace
        pace = {"name": "test", "file": [low, high], "requests": requests}
        return self.run_worker(info, item, pace), item

    def run_worker(self, info, item, pace=None):
        # "Subtitle gagal: ..." goes to the log. FFmpeg writes to stderr's file, so it needs one.
        with open(os.devnull, "w") as log, redirect_stderr(log):
            return worker.subtitle_files(
                info, item, self.root, self.events, {"quiet": True}, pace, self.slept.append
            )

    def test_tracks_are_fetched_with_the_pause_of_the_pace_between_them(self):
        (saved, missing), _ = self.fetch(["/ok.vtt"] * 3)
        self.assertEqual((len(saved), missing), (3, []))
        # Not before the first track, then once before each of the others.
        self.assertEqual(self.slept, [1.0, 1.0])
        waits = [e["message"] for e in self.events.of("wait")]
        self.assertEqual(waits, ["Menunggu · jeda antar-subtitle"] * 2)

    def test_the_fast_profile_does_not_pause(self):
        (saved, _), _ = self.fetch(["/ok.vtt"] * 3, pace=((0, 0), 0))
        self.assertEqual(len(saved), 3)
        self.assertEqual((self.slept, self.events.of("wait")), ([], []))

    def test_a_track_that_exists_is_not_fetched_again_and_costs_no_pause(self):
        (saved, _), item = self.fetch(["/ok.vtt"] * 2)
        Handler.hits.clear()
        self.slept.clear()
        again = self.run_worker({"subtitles": {}}, item)
        self.assertEqual(len(again[0]), 2)
        self.assertEqual((Handler.hits, self.slept), ([], []))

    def test_a_retry_after_of_an_hour_is_waited_for_a_minute(self):
        (saved, missing), _ = self.fetch(["/limit.vtt?after=3600"], pace=((0, 0), 0))
        self.assertEqual(saved, [])
        self.assertEqual(self.slept, [worker.MAX_RETRY_AFTER])
        self.assertEqual(model.MISSING_REASONS[missing[0]["reason"]], "dibatasi · 429")

    def test_a_short_retry_after_is_kept_as_asked(self):
        self.fetch(["/limit.vtt?after=2"], pace=((0, 0), 0))
        self.assertEqual(self.slept, [2.0])

    def test_a_server_that_limits_gets_one_more_try_not_four(self):
        self.fetch(["/limit.vtt"], pace=((0, 0), 0))
        self.assertEqual(Handler.hits, ["/limit.vtt"] * 2)

    def test_the_second_try_after_a_429_can_succeed(self):
        (saved, missing), _ = self.fetch(["/once.vtt"], pace=((0, 0), 0))
        self.assertEqual((len(saved), missing), (1, []))
        self.assertEqual(Handler.hits, ["/once.vtt"] * 2)

    def test_two_limited_subtitles_in_a_row_stop_the_rest_of_the_video(self):
        (saved, missing), _ = self.fetch(["/limit.vtt"] * 6, pace=((0, 0), 0))
        self.assertEqual(saved, [])
        self.assertEqual({m["reason"] for m in missing}, {"limited"})
        self.assertEqual(len(missing), 6)  # every one of them is still asked for next time
        self.assertEqual(len(Handler.hits), 2 * worker.LIMITED_STOP)
        notices = " ".join(e["message"] for e in self.events.of("notice"))
        self.assertEqual(notices.count("429"), 1)
        self.assertIn("resume", notices)

    def test_a_subtitle_that_was_served_starts_the_count_again(self):
        paths = ["/limit.vtt", "/ok.vtt", "/limit.vtt", "/ok.vtt", "/limit.vtt"]
        (saved, missing), _ = self.fetch(paths, pace=((0, 0), 0))
        self.assertEqual(len(saved), 2)
        self.assertEqual([m["reason"] for m in missing], ["limited"] * 3)
        self.assertEqual(len(Handler.hits), 2 + 1 + 2 + 1 + 2)

    def test_a_subtitle_already_on_disk_is_kept_after_the_stop(self):
        (_, _), item = self.fetch(["/ok.vtt"], pace=((0, 0), 0))
        # The first track is on disk; the next three are limited; the last is on disk too.
        info = {"subtitles": {}, "automatic_captions": {}, "http_headers": {}}
        tracks = []
        for number in range(5):
            key = f"x{number}"
            info["subtitles"][key] = [{"url": self.host + "/limit.vtt", "ext": "vtt", "name": key}]
            tracks.append({**item["subtitles"][0], "key": key, "language": key})
        item = {**item, "subtitles": tracks}
        for track in (tracks[0], tracks[4]):
            path = worker.subtitle_path(self.root, model.basename(item), track)
            path.write_text("1\n00:00:00,000 --> 00:00:00,800\nHello\n")
        saved, missing = self.run_worker(info, item)
        self.assertEqual([t["key"] for t in saved], ["x0", "x4"])
        self.assertEqual([m["key"] for m in missing], ["x1", "x2", "x3"])

    def test_other_failures_keep_the_four_retries_and_say_failed(self):
        (_, missing), _ = self.fetch(["/gone.vtt"], pace=((0, 0), 0))
        self.assertEqual(Handler.hits, ["/gone.vtt"] * 5)
        self.assertEqual(self.slept, [1, 2, 4, 8])
        self.assertEqual(missing[0]["reason"], "failed")

    def test_a_track_the_video_does_not_offer_says_so(self):
        (_, missing), _ = self.fetch([None], pace=((0, 0), 0))
        self.assertEqual([m["reason"] for m in missing], ["unavailable"])
        self.assertEqual(Handler.hits, [])

    def test_the_reason_is_a_code_and_never_the_message_of_the_error(self):
        # An error message can hold the signed link the request went to.
        (_, missing), _ = self.fetch(["/limit.vtt?after=1"], pace=((0, 0), 0))
        self.assertNotIn("127.0.0.1", repr(missing[0]["reason"]))
        self.assertIn(missing[0]["reason"], model.MISSING_REASONS)

    def test_reasons_by_kind_of_failure(self):
        class Refused(Exception):
            class response:
                status = 429
                headers = {}

        self.assertEqual(worker.failure_reason(Refused()), "limited")
        self.assertEqual(
            worker.failure_reason(ValueError("HTTP Error 429: Too Many Requests")), "limited"
        )
        self.assertEqual(
            worker.failure_reason(ValueError("FFmpeg gagal (kode 1) saat x")), "ffmpeg"
        )
        self.assertEqual(worker.failure_reason(ValueError("Subtitle kosong")), "empty")
        self.assertEqual(worker.failure_reason(OSError("disk")), "failed")
        for code in ("limited", "ffmpeg", "empty", "failed", "unavailable", "path"):
            self.assertIn(code, model.MISSING_REASONS)


class Paths(unittest.TestCase):
    """The length of the paths a subtitle is written to, on Windows."""

    @staticmethod
    def item(*keys, title="T" * 100):
        tracks = [
            {
                "key": key,
                "origin": "automatic_captions",
                "language": key,
                "kind": "translated",
                "name": key,
                "source": "en-US",
            }
            for key in keys
        ]
        return {
            "id": "abcdefghijk",
            "title": title,
            "url": "https://youtu.be/abcdefghijk",
            "choice": model.pick(
                synthetic(), "video-audio", "best", "en", "original", None, None, False
            ),
            "subtitles": tracks,
        }

    def test_the_longest_path_is_the_download_of_the_longest_track(self):
        root = PureWindowsPath("C:/media")
        item = self.item("id", "zh-Hans-en-US")
        longest = worker.longest_subtitle_path(root, item)
        self.assertTrue(str(longest).endswith("automatic_captions-zh-Hans-en-US.ttml.part"))
        final = worker.subtitle_path(root, model.basename(item), item["subtitles"][1])
        self.assertGreater(len(str(longest)), len(str(final)))

    def test_an_item_without_subtitles_has_no_such_path(self):
        self.assertIsNone(worker.longest_subtitle_path(PureWindowsPath("C:/media"), self.item()))

    def test_there_are_folders_where_the_media_fits_and_its_subtitles_do_not(self):
        item = self.item("id", "zh-Hans-en-US")
        prefix = model.basename(item)
        limit = worker.WINDOWS_PATH_LIMIT
        found = []
        for depth in range(20, 140):
            root = PureWindowsPath("C:/" + "a" * depth)
            media = root / "video" / (prefix + ".mkv")
            assembling = media.with_name("." + media.stem + ".assembling.mkv")
            subtitle = worker.longest_subtitle_path(root, item)
            if len(str(assembling)) <= limit < len(str(subtitle)):
                found.append(depth)
        # The check on the media alone let these through: a window of about thirty folder
        # names deep.
        self.assertGreater(len(found), 10)

    def test_a_long_destination_for_subtitles_is_refused_before_anything_is_downloaded(self):
        item = self.item("id")
        events = []

        class Events:
            def send(self, kind, **kwargs):
                events.append(kind)

            hook = pp_hook = staticmethod(lambda *a: None)

        def too_long(path, **kwargs):
            return 270 if "subtitle" in str(path) else None

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(worker, "path_too_long", side_effect=too_long),
            patch.object(worker.yt_dlp, "YoutubeDL") as extractor,
        ):
            with self.assertRaisesRegex(
                ValueError, "subtitle 270 karakter.*batas Windows.*--output"
            ):
                worker.download({"item": item, "output": folder}, Events())
            extractor.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])  # nothing was created either

    def test_an_item_whose_paths_fit_is_not_refused(self):
        item = self.item("id")
        self.assertIsNone(
            worker.path_too_long(worker.longest_subtitle_path(Path("/media"), item), windows=True)
        )


class Batch(unittest.TestCase):
    """What a playlist does when the subtitles of its videos keep being refused."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, value in ((store, "STATE"),):
            patcher = patch.object(target, value, Path(folder.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        (Path(folder.name) / "jobs").mkdir()
        self.asked = []

    def execute(self, outcomes):
        """Run a job of one item per outcome; each is the missing list the worker returns."""
        items = [
            {
                "id": f"i{n}",
                "title": f"T{n}",
                "url": f"https://example.test/{n}",
                "choice": {"mode": "audio"},
                "status": "pending",
            }
            for n in range(len(outcomes))
        ]
        job = store.new_job(items, "/media")

        def run(request, callback=None):
            self.asked.append(request["item"]["id"])
            missing = outcomes[len(self.asked) - 1]
            return {"path": "/media/x.m4a", "mode": "audio", "subtitles": [], "missing": missing}

        with (
            patch.object(app, "variant", side_effect=lambda item: item["id"]),
            patch.object(store.Archive, "get", return_value=None),
            patch.object(store.Archive, "put"),
            patch.object(app, "run", side_effect=run),
            patch.object(app, "native_output"),
            redirect_stderr(io.StringIO()),
        ):
            try:
                app.execute(app.UI(True), job)
            except app.Stopped as stop:
                return job, stop
        return job, None

    limited = [{"key": "id", "reason": "limited"}]
    other = [{"key": "id", "reason": "unavailable"}]

    def test_two_videos_in_a_row_with_subtitles_refused_stop_the_batch(self):
        job, stop = self.execute([self.limited] * 5)
        self.assertEqual(self.asked, ["i0", "i1"])
        self.assertEqual((stop.category, job["status"]), ("rate_limit", "blocked"))
        self.assertIn("membatasi", str(stop))
        # The two videos that were saved keep their media and wait for a resume.
        self.assertEqual([i["status"] for i in job["items"][:2]], ["partial", "partial"])
        self.assertEqual([i["status"] for i in job["items"][2:]], ["pending"] * 3)

    def test_a_video_that_got_its_subtitles_starts_the_count_again(self):
        job, stop = self.execute([self.limited, [], self.limited, [], self.limited])
        # It ends as a batch with partial items ends, not as a limited one.
        self.assertIsNone(stop.category)
        self.assertEqual(len(self.asked), 5)

    def test_a_subtitle_that_the_video_does_not_have_is_not_a_limit(self):
        job, stop = self.execute([self.other] * 4)
        self.assertIsNone(stop.category)
        self.assertEqual((len(self.asked), job["status"]), (4, "partial"))


class Many(unittest.TestCase):
    """The summary of a choice of many subtitle tracks."""

    def test_more_than_ten_tracks_are_said_with_what_they_cost(self):
        from test_subtitles import answers, full

        metadata = [full(f"vid{n:08d}") for n in range(21)]
        merged = app.merge_tracks(metadata)
        chosen = list(merged)[: app.MANY_SUBTITLES + 1]
        _, _, _, notes = app.build_items(None, metadata, merged, answers(chosen))
        warning = [n for n in notes if n.startswith("!")]
        self.assertEqual(len(warning), 1)
        self.assertIn("11 track", warning[0])
        self.assertIn("231 permintaan", warning[0])

    def test_ten_are_not_a_warning(self):
        from test_subtitles import answers, full

        metadata = [full("vid00000001")]
        merged = app.merge_tracks(metadata)
        _, _, _, notes = app.build_items(
            None, metadata, merged, answers(list(merged)[: app.MANY_SUBTITLES])
        )
        self.assertEqual([n for n in notes if n.startswith("!")], [])


if __name__ == "__main__":
    unittest.main()
