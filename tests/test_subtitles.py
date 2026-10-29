"""The subtitle choice with the captions a real video has (a hundred and fifty translations a
track), not the one or two the other tests give it."""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import model
import captions


class Shape(unittest.TestCase):
    """What yt-dlp's captions become in the tool's own functions."""

    def test_the_fixture_has_a_hundred_and_fifty_translations_per_track(self):
        self.assertGreaterEqual(len(captions.TRANSLATIONS), 150)
        self.assertEqual(
            len({code for code, _ in captions.TRANSLATIONS}), len(captions.TRANSLATIONS)
        )
        subtitles, automatic = captions.captions("abc")
        self.assertEqual(list(subtitles), ["en-US"])
        self.assertIn("id-en-US", automatic)  # the translation of the creator's captions
        self.assertIn("id", automatic)  # the translation of the automatic ones
        self.assertIn("en-orig", automatic)

    def test_one_video_lists_every_track_it_has(self):
        rows = model.tracks(captions.video("abc"))
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds.count("manual"), 1)
        self.assertEqual(kinds.count("automatic"), 1)
        # The creator's captions and the automatic ones each translate into every language.
        self.assertGreaterEqual(kinds.count("translated"), 2 * 150)
        indonesian = [row for row in rows if row["language"] == "id"]
        self.assertEqual({row["source"] for row in indonesian}, {"en", "en-US"})

    def test_a_playlist_lists_a_language_once_whatever_its_source(self):
        videos = captions.playlist(3)
        merged = app.merge_tracks(videos)
        self.assertEqual(
            len(merged), len({(language, kind) for m in videos for language, kind in self.keys(m)})
        )
        indonesian = [t for identity, t in merged.items() if identity[0] == "id"]
        self.assertEqual([t["kind"] for t in indonesian], ["translated"])
        # A single video keeps each source apart, so its menu can say which is which.
        self.assertGreater(len(app.merge_tracks(videos[:1])), len(merged))

    @staticmethod
    def keys(info):
        return [(row["language"], row["kind"]) for row in model.tracks(info)]

    def test_a_requested_language_picks_the_creators_captions_before_a_translation(self):
        merged = app.merge_tracks(captions.playlist(2))
        chosen, missing = app.subtitle_identities(merged, ["id", "xx"])
        self.assertEqual(missing, ["xx"])
        self.assertEqual([merged[identity]["language"] for identity in chosen], ["id"])
        english, _ = app.subtitle_identities(merged, ["en-US"])
        self.assertEqual(merged[english[0]]["kind"], "manual")


if __name__ == "__main__":
    unittest.main()
