"""Subtitles that YouTube gives only to what looks like a browser. yt-dlp marks them
`impersonate: True`; the worker asks for them with that look when curl_cffi can give it, and when
it cannot, a refusal is reported as that (waiting does not help) and not as a rate limit."""

import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import model
import ui
import worker
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

VTT = b"WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nHello\n"


class Handler(BaseHTTPRequestHandler):
    """/browser.vtt is a subtitle for a request that looks like a browser's (Chrome sends
    sec-ch-ua, which yt-dlp's plain request does not) and a 429 for any other."""

    hits = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits.append(self.path)
        if "sec-ch-ua" in {key.lower() for key in self.headers}:
            self.send_response(200)
            self.send_header("Content-Length", str(len(VTT)))
            self.end_headers()
            self.wfile.write(VTT)
        else:
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()


class Events:
    def __init__(self):
        self.sent = []
        self.timings = None

    def send(self, kind, **fields):
        self.sent.append({"type": kind, **fields})

    def of(self, kind):
        return [e for e in self.sent if e["type"] == kind]


def real_target():
    """What this machine's yt-dlp can impersonate (None without curl_cffi)."""
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        return worker.impersonation_target(ydl, True)


class Refused(Exception):
    """The error of a request that the server answered with 429."""

    class response:
        status = 429
        headers = {}

        @staticmethod
        def close():
            pass


class Stub:
    """A YoutubeDL whose requests are answered by `outcome(request)`; `target` is what it can
    impersonate."""

    calls = []
    outcome = None
    target = None

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def urlopen(self, request):
        Stub.calls.append(request)
        return Stub.outcome(request)

    def _parse_impersonate_targets(self, wanted):
        return (Stub.target if wanted else None), []


class Case(unittest.TestCase):
    def setUp(self):
        Handler.hits = []
        Stub.calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = Events()
        self.slept = []

    def subtitles(self, url, tracks=1, impersonate=True):
        """(info, item) for `tracks` subtitle tracks served from `url`."""
        info = {"subtitles": {}, "automatic_captions": {}, "http_headers": {}}
        listed = []
        for number in range(tracks):
            key = f"t{number}"
            fmt = {"url": url, "ext": "vtt", "name": key}
            if impersonate:
                fmt["impersonate"] = True
            info["subtitles"][key] = [fmt]
            listed.append(
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
            "subtitles": listed,
        }
        return info, item

    def fetch(self, info, item):
        pace = {"name": "test", "file": [0, 0], "requests": 0}
        # "Subtitle gagal: ..." goes to the log; FFmpeg writes to stderr's file, so it needs one.
        with open(os.devnull, "w") as log, redirect_stderr(log):
            return worker.subtitle_files(
                info, item, self.root, self.events, {"quiet": True}, pace, self.slept.append
            )


class WithAServer(Case):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port) + "/browser.vtt"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    @unittest.skipUnless(real_target(), "curl_cffi is not installed")
    def test_with_curl_cffi_the_subtitle_is_asked_for_like_a_browser_and_served(self):
        saved, missing = self.fetch(*self.subtitles(self.url))
        self.assertEqual((len(saved), missing), (1, []))
        self.assertEqual(len(Handler.hits), 1)

    def test_a_refusal_without_the_look_is_blamed_on_curl_cffi_and_asked_once(self):
        info, item = self.subtitles(self.url, tracks=3)
        with patch.object(worker, "impersonation_target", return_value=None):
            saved, missing = self.fetch(info, item)
        self.assertEqual(saved, [])
        self.assertEqual([m["reason"] for m in missing], ["impersonation"] * 3)
        # One request: not a second after a pause, and none for the tracks that follow.
        self.assertEqual(len(Handler.hits), 1)
        self.assertEqual(self.slept, [])
        notices = " ".join(e["message"] for e in self.events.of("notice"))
        self.assertIn("curl_cffi", notices)
        self.assertNotIn("429", notices)

    def test_the_same_refusal_of_a_format_that_never_asked_stays_a_rate_limit(self):
        info, item = self.subtitles(self.url, impersonate=False)
        saved, missing = self.fetch(info, item)
        self.assertEqual([m["reason"] for m in missing], ["limited"])
        self.assertEqual(len(Handler.hits), 2)  # the one more try after the pause, as before

    def test_a_subtitle_that_is_served_to_the_plain_request_needs_nothing(self):
        # Not every session is refused: when the plain request is served, the subtitle is saved.
        class Open(Handler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(VTT)))
                self.end_headers()
                self.wfile.write(VTT)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Open)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:" + str(server.server_port) + "/x.vtt"
        with patch.object(worker, "impersonation_target", return_value=None):
            saved, missing = self.fetch(*self.subtitles(url))
        self.assertEqual((len(saved), missing), (1, []))


class WithAStub(Case):
    def fetch_stubbed(self, outcome, target, **kwargs):
        Stub.outcome, Stub.target = outcome, target
        info, item = self.subtitles("https://example.test/x.vtt", **kwargs)
        with patch.object(worker.yt_dlp, "YoutubeDL", Stub):
            return self.fetch(info, item)

    def test_the_target_is_put_on_the_request(self):
        target = ImpersonateTarget("chrome")
        saved, missing = self.fetch_stubbed(lambda request: io.BytesIO(VTT), target)
        self.assertEqual((len(saved), missing), (1, []))
        self.assertEqual(len(Stub.calls), 1)
        self.assertIs(Stub.calls[0].extensions["impersonate"], target)

    def test_a_format_that_did_not_ask_gets_no_target(self):
        target = ImpersonateTarget("chrome")
        self.fetch_stubbed(lambda request: io.BytesIO(VTT), target, impersonate=False)
        self.assertNotIn("impersonate", Stub.calls[0].extensions)

    def test_a_429_with_the_look_is_still_a_rate_limit_with_its_one_more_try(self):
        def refuse(request):
            raise Refused()

        saved, missing = self.fetch_stubbed(refuse, ImpersonateTarget("chrome"))
        self.assertEqual([m["reason"] for m in missing], ["limited"])
        self.assertEqual(len(Stub.calls), 2)


class Target(unittest.TestCase):
    def test_nothing_is_asked_when_the_format_does_not_ask(self):
        self.assertIsNone(worker.impersonation_target(object(), None))
        self.assertIsNone(worker.impersonation_target(object(), False))

    def test_a_yt_dlp_without_the_lookup_gives_none_instead_of_an_error(self):
        self.assertIsNone(worker.impersonation_target(object(), True))

    def test_the_real_yt_dlp_answers_with_a_target_or_none(self):
        # The lookup is yt-dlp's own, not a public API: this fails if a new release changes it.
        target = real_target()
        self.assertTrue(target is None or isinstance(target, ImpersonateTarget))

    def test_when_curl_cffi_is_installed_yt_dlp_can_impersonate(self):
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            self.skipTest("curl_cffi is not installed")
        self.assertIsInstance(real_target(), ImpersonateTarget)


class Words(unittest.TestCase):
    def test_the_reason_is_in_the_vocabulary_and_says_what_to_do(self):
        text = model.MISSING_REASONS["impersonation"]
        self.assertIn("curl_cffi", text)
        self.assertIn("setup", text)

    def test_the_card_shows_it_with_the_track(self):
        note = ui.missing_note([{"name": "Indonesian", "reason": "impersonation"}])
        self.assertEqual(note, f"Indonesian ({text_of('impersonation')})")

    def test_curl_cffi_is_a_direct_requirement_with_a_pin_in_the_lock(self):
        requirements = (BASE / "requirements.txt").read_text(encoding="utf-8").split()
        self.assertIn("curl-cffi==0.16.0", requirements)
        lock = (BASE / "requirements.lock").read_text(encoding="utf-8")
        for pin in ("curl-cffi==0.16.0", "cffi==2.1.1", "pycparser==3.0"):
            self.assertRegex(lock, rf"(?m)^{pin} \\$")


def text_of(reason):
    return model.MISSING_REASONS[reason]


if __name__ == "__main__":
    unittest.main()
