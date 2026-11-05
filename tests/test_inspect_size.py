"""The inspection of a video that has captions in many languages.

yt-dlp lists a caption track once per language it can be translated into and once per format,
each with a signed address. A video with two dozen tracks written by its creator made the answer
of the worker's inspection 13 MiB, and the reader of the worker's channel refuses an event above
8 MiB (runner.MAX_EVENT): the download of such a video failed with "Kanal worker terlalu besar"
before the person had chosen anything."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import captions
import model
import runner
import store
import worker
from fsutil import cleanup_directory


def event_size(result):
    """The bytes the worker's `done` event takes on the channel, as Events.send writes it."""
    return len(json.dumps({"type": "done", "result": result}, ensure_ascii=True)) + 1


class Reduction(unittest.TestCase):
    """worker.compact on the captions of a video with twenty-four written tracks."""

    @classmethod
    def setUpClass(cls):
        cls.raw = captions.heavy_video("7wAJ5olZW40")
        cls.compact = worker.compact(cls.raw)

    def test_the_fixture_is_too_large_for_the_channel_as_it_comes_from_yt_dlp(self):
        # What makes the other tests here mean something: without the reduction this is the event.
        self.assertGreater(event_size(self.raw), runner.MAX_EVENT)

    def test_the_inspection_of_such_a_video_fits_the_channel_with_room_to_spare(self):
        self.assertLess(event_size(self.compact), runner.MAX_EVENT // 4)

    def test_the_tracks_are_the_same(self):
        before = model.tracks(self.raw)
        self.assertGreater(len(before), 3000)
        self.assertEqual(model.tracks(self.compact), before)

    def test_the_subtitle_menu_is_the_same(self):
        self.assertEqual(app.merge_tracks([self.compact]), app.merge_tracks([self.raw]))
        self.assertEqual(app.track_coverage([self.compact]), app.track_coverage([self.raw]))

    def test_where_a_translation_comes_from_is_the_same(self):
        for track in model.tracks(self.raw):
            self.assertEqual(
                app.track_source(self.compact, track), app.track_source(self.raw, track)
            )

    def test_the_keys_are_the_same(self):
        for origin in ("subtitles", "automatic_captions"):
            self.assertEqual(list(self.compact[origin]), list(self.raw[origin]))

    def test_one_entry_per_key_with_what_the_menus_read(self):
        for origin in ("subtitles", "automatic_captions"):
            for key, formats in self.compact[origin].items():
                self.assertEqual(len(formats), 1, key)
                self.assertEqual(set(formats[0]), {"ext", "name", "url"}, key)
                self.assertNotIn("signature", formats[0]["url"])

    def test_the_other_fields_of_the_inspection_are_untouched(self):
        for key in ("id", "title", "language"):
            self.assertEqual(self.compact[key], self.raw[key])
        self.assertEqual(self.compact["formats"], [])


class Edges(unittest.TestCase):
    """What slim_captions does with the shapes that yt-dlp (and other extractors) can give."""

    URL = "https://www.youtube.com/api/timedtext?v=a&ei=x&signature=s&lang={}&fmt=vtt{}"

    def slim(self, captions_):
        return model.slim_captions(captions_)

    def same_tracks(self, subtitles=None, automatic=None):
        info = {"subtitles": subtitles or {}, "automatic_captions": automatic or {}}
        slim = {
            "subtitles": self.slim(info["subtitles"]),
            "automatic_captions": self.slim(info["automatic_captions"]),
        }
        self.assertEqual(model.tracks(slim), model.tracks(info))
        return slim

    def test_nothing_gives_nothing(self):
        self.assertEqual(self.slim(None), {})
        self.assertEqual(self.slim({}), {})

    def test_an_empty_key_stays_because_a_translation_asks_whether_its_source_is_there(self):
        slim = self.slim({"en": [], "id": [{"ext": "vtt", "name": "Indonesian", "url": "x"}]})
        self.assertEqual(slim["en"], [])
        self.assertIn("en", slim)

    def test_live_chat_is_not_a_track_before_or_after(self):
        subtitles = {
            "live_chat": [
                {"ext": "json", "url": "https://www.youtube.com/live_chat?continuation=a"}
            ],
            "en": [{"ext": "vtt", "name": "English", "url": self.URL.format("en", "")}],
        }
        slim = self.same_tracks(subtitles)
        self.assertEqual([t["key"] for t in model.tracks(slim)], ["en"])

    def test_an_entry_without_a_name_or_an_address_reads_as_before(self):
        self.same_tracks(
            {"en": [{"ext": "vtt", "url": self.URL.format("en", "")}], "fr": [{"ext": "vtt"}]}
        )
        slim = self.slim({"fr": [{"ext": "vtt"}]})
        self.assertEqual(slim["fr"][0]["url"], "")
        self.assertNotIn("name", slim["fr"][0])

    def test_languages_with_dashes_and_escapes_read_as_before(self):
        entry = lambda url: [{"ext": "vtt", "name": "N", "url": url}]  # noqa: E731
        self.same_tracks(
            {"zh-Hans": entry(self.URL.format("zh-Hans", ""))},
            {
                "pt-BR-zh-Hans": entry(self.URL.format("zh-Hans", "&tlang=pt-BR")),
                "en-orig": entry(self.URL.format("en", "&kind=asr")),
                "es": entry(self.URL.format("es%2FMX", "&tlang=es")),
                "same": entry(self.URL.format("en", "&tlang=en")),
            },
        )

    def test_an_address_without_a_query_or_with_a_fragment_stays_usable(self):
        slim = self.slim(
            {
                "a": [{"url": "https://example.com/a.vtt"}],
                "b": [{"url": "https://example.com/b.vtt?lang=b#frag"}],
            }
        )
        self.assertEqual(slim["a"][0]["url"], "https://example.com/a.vtt")
        self.assertEqual(slim["b"][0]["url"], "https://example.com/b.vtt?lang=b")

    def test_the_input_is_not_changed(self):
        subtitles = captions.captions("abc", signed=True)[0]
        before = json.dumps(subtitles, sort_keys=True)
        self.slim(subtitles)
        self.assertEqual(json.dumps(subtitles, sort_keys=True), before)


class Pipe(unittest.TestCase):
    """The inspection of the same video through the real worker, process and channel."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_directory, self.temp)
        self.root = Path(self.temp.name)
        self.state = self.root / ".state"
        self.state.mkdir()
        self.raw = captions.heavy_video("7wAJ5olZW40")
        fixture = {
            "duration": 1,
            "channel": "Local",
            "formats": [
                {
                    "format_id": "a",
                    "url": "http://127.0.0.1:9/a.m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "ext": "m4a",
                }
            ],
            "subtitles": self.raw["subtitles"],
            "automatic_captions": self.raw["automatic_captions"],
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

    def test_a_video_with_captions_in_many_languages_can_be_inspected(self):
        # Before the captions were reduced this raised WorkerError("Kanal worker terlalu besar").
        self.assertGreater((self.root / "fixture.json").stat().st_size, runner.MAX_EVENT)
        info = runner.run({"action": "inspect", "url": "fixture:heavy"})
        self.assertEqual(info["id"], "heavy")
        self.assertEqual(model.tracks(info), model.tracks(self.raw))
        self.assertLess(event_size(info), runner.MAX_EVENT // 4)


if __name__ == "__main__":
    unittest.main()
