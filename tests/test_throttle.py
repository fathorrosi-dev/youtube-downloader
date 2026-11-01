"""A download that YouTube slows to a trickle: yt-dlp stops it, a new link is asked for and the
stream goes on from the bytes it already has, a limited number of times."""

import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import model
import worker
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError, ThrottledDownload
from test_tools import synthetic

PAYLOAD = bytes(number % 251 for number in range(120_000))


class Events:
    def __init__(self):
        self.sent = []
        self.timings = None

    def send(self, kind, **fields):
        self.sent.append({"type": kind, **fields})

    def of(self, kind):
        return [e for e in self.sent if e["type"] == kind]

    hook = pp_hook = staticmethod(lambda *a: None)


class Logic(unittest.TestCase):
    """download_stream, with the download and the new extraction replaced."""

    opts = {"throttledratelimit": worker.THROTTLE_RATE, "format": "v"}

    def run_stream(self, downloads, fresh=None, refreshes=3, opts=None):
        """`downloads` are what successive attempts do: an exception is raised, a string is the
        path. Returns (path, calls, events); calls are the (opts, info) of each attempt."""
        events = Events()
        calls = []
        outcomes = iter(downloads)

        def attempt(options, info, url):
            calls.append((options, info))
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        links = iter(fresh if fresh is not None else [{"link": n} for n in range(1, 10)])

        def renewed(options, url):
            outcome = next(links)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with (
            patch.object(worker, "download_once", side_effect=attempt),
            patch.object(worker, "fresh_info", side_effect=renewed) as self.renew,
            patch("sys.stderr"),
        ):
            path = worker.download_stream(
                opts or self.opts, {"link": 0}, "https://example.test/v", events, refreshes
            )
        return path, calls, events

    @property
    def renewed(self):
        """How many new links were asked for."""
        return self.renew.call_count

    def test_a_throttled_stream_gets_a_new_link_and_goes_on(self):
        path, calls, events = self.run_stream([ThrottledDownload(), "/tmp/v.mp4"])
        self.assertEqual(path, "/tmp/v.mp4")
        self.assertEqual([info["link"] for _, info in calls], [0, 1])
        self.assertEqual(self.renewed, 1)
        message = events.of("retry")[0]["message"]
        self.assertIn("100 KB/s", message)
        self.assertIn("(1/3)", message)

    def test_both_attempts_keep_the_check(self):
        _, calls, _ = self.run_stream([ThrottledDownload(), "/tmp/v.mp4"])
        self.assertEqual([c[0]["throttledratelimit"] for c in calls], [worker.THROTTLE_RATE] * 2)

    def test_after_the_last_new_link_the_stream_goes_on_without_the_check(self):
        throttled = [ThrottledDownload()] * 3
        path, calls, events = self.run_stream(throttled + ["/tmp/v.mp4"], refreshes=3)
        self.assertEqual(path, "/tmp/v.mp4")
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.renewed, 3)
        self.assertEqual(len(events.of("retry")), 3)
        self.assertEqual(
            [c[0]["throttledratelimit"] for c in calls], [worker.THROTTLE_RATE] * 3 + [None]
        )
        # The last attempt downloads from the newest link.
        self.assertEqual(calls[-1][1], {"link": 3})

    def test_no_new_link_means_finishing_slowly_not_failing(self):
        path, calls, _ = self.run_stream(
            [ThrottledDownload(), "/tmp/v.mp4"], fresh=[ExtractorError("sign in", expected=True)]
        )
        self.assertEqual(path, "/tmp/v.mp4")
        self.assertIsNone(calls[-1][0]["throttledratelimit"])
        self.assertEqual(calls[-1][1], {"link": 0})  # the old link, which is slow but alive

    def test_a_download_that_is_not_throttled_is_made_once_and_as_before(self):
        path, calls, events = self.run_stream(["/tmp/v.mp4"])
        self.assertEqual((path, len(calls), self.renewed), ("/tmp/v.mp4", 1, 0))
        self.assertEqual(events.of("retry"), [])

    def test_a_stream_without_the_option_is_never_checked(self):
        _, calls, _ = self.run_stream(["/tmp/v.mp4"], opts={"format": "v"})
        self.assertEqual(calls[0][0], {"format": "v", "throttledratelimit": None})

    def test_other_failures_are_not_mistaken_for_a_throttle(self):
        with self.assertRaises(DownloadError):
            self.run_stream([DownloadError("HTTP Error 500")])
        self.assertEqual(self.renewed, 0)

    def test_the_threshold_and_the_number_of_new_links_are_the_documented_ones(self):
        self.assertEqual(worker.THROTTLE_RATE, 100 * 1024)
        self.assertEqual(worker.THROTTLE_REFRESHES, 5)


class Wiring(unittest.TestCase):
    """The media streams of a download carry the check; nothing else does."""

    def test_the_streams_of_a_download_are_checked_and_told_where_to_report(self):
        item = {
            "id": "abcdefghijk",
            "title": "T",
            "url": "https://youtu.be/abcdefghijk",
            "choice": model.pick(synthetic(), "video-audio", "best", "en", "original"),
            "subtitles": [],
        }
        seen = {}

        class Reached(Exception):
            pass

        def stream(opts, info, url, events=None):
            seen.update(opts=opts, events=events)
            raise Reached()

        extractor = MagicMock()
        extractor.__enter__.return_value.extract_info.return_value = synthetic()
        events = Events()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(worker, "BASE", Path(folder)),
            patch.object(worker.yt_dlp, "YoutubeDL", return_value=extractor),
            patch.object(worker, "download_stream", side_effect=stream),
            patch.object(worker, "pause_before_download"),
        ):
            with self.assertRaises(Reached):
                worker.download({"item": item, "output": folder}, events)
        self.assertEqual(seen["opts"]["throttledratelimit"], worker.THROTTLE_RATE)
        self.assertIs(seen["events"], events)

    def test_the_lookups_and_the_subtitles_are_not_checked(self):
        opts = worker.options({}, Events())
        self.assertNotIn("throttledratelimit", opts)


class Slow(BaseHTTPRequestHandler):
    """/v.bin?link=1 trickles (a few KB every tenth of a second) until the client leaves;
    ?link=2 sends all that is asked for at once. Both understand a Range."""

    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        link = re.search(r"link=(\d+)", self.path)[1]
        start = 0
        found = re.match(r"bytes=(\d+)-", self.headers.get("Range", ""))
        if found:
            start = int(found[1])
        Slow.requests.append((link, start))
        body = PAYLOAD[start:]
        self.send_response(206 if found else 200)
        self.send_header("Content-Length", str(len(body)))
        if found:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        try:
            if link == "1":
                for offset in range(0, len(body), 2048):
                    self.wfile.write(body[offset : offset + 2048])
                    self.wfile.flush()
                    time.sleep(0.1)
            else:
                self.wfile.write(body)
        except OSError:
            pass  # the client stopped a download that was too slow


class RealDownload(unittest.TestCase):
    """The real yt-dlp against a server that slows the first link."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Slow.requests = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def info(self, link):
        return {
            "id": "abc",
            "title": "Fixture",
            "extractor": "fixture",
            "extractor_key": "Fixture",
            "webpage_url": "https://example.test/abc",
            "formats": [
                {
                    "format_id": "v",
                    "url": f"{self.host}/v.bin?link={link}",
                    "ext": "bin",
                    "protocol": "http",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "width": 160,
                    "height": 90,
                }
            ],
        }

    def opts(self, **extra):
        return {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "format": "v",
            "outtmpl": str(self.folder / "%(id)s-video.%(ext)s"),
            "continuedl": True,
            "retries": 0,
            "socket_timeout": 10,
            "throttledratelimit": worker.THROTTLE_RATE,
            **extra,
        }

    def test_yt_dlp_stops_a_trickle_and_the_stream_finishes_from_a_new_link(self):
        events = Events()
        started = time.monotonic()
        with (
            patch.object(worker, "fresh_info", side_effect=lambda opts, url: self.info(2)),
            patch("sys.stderr"),
        ):
            path = worker.download_stream(
                self.opts(), self.info(1), "https://example.test/abc", events, 2
            )
        elapsed = time.monotonic() - started
        # The whole file, byte for byte: what the first link gave was kept and not asked again.
        self.assertEqual(Path(path).read_bytes(), PAYLOAD)
        self.assertEqual(len(events.of("retry")), 1)
        links = [link for link, _ in Slow.requests]
        self.assertEqual(links, ["1", "2"])
        self.assertGreater(Slow.requests[1][1], 0)  # the second one asked from where it was
        # It took the three seconds below the limit, not the minute the trickle would have.
        self.assertGreater(elapsed, 3)
        self.assertLess(elapsed, 20)

    def test_a_fast_link_is_downloaded_once_with_nothing_said(self):
        events = Events()
        path = worker.download_stream(
            self.opts(), self.info(2), "https://example.test/abc", events, 2
        )
        self.assertEqual(Path(path).read_bytes(), PAYLOAD)
        self.assertEqual((events.of("retry"), len(Slow.requests)), ([], 1))

    def test_yt_dlp_raises_what_the_worker_catches(self):
        # The contract the worker relies on: process_ie_result lets ThrottledDownload through.
        with yt_dlp.YoutubeDL(self.opts()) as ydl, patch("sys.stderr"):
            with self.assertRaises(ThrottledDownload):
                ydl.process_ie_result(self.info(1), download=True)


if __name__ == "__main__":
    unittest.main()
