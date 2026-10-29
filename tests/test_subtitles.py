"""The subtitle choice with the captions a real video has (a hundred and fifty translations a
track), not the one or two the other tests give it."""

import json
import re
import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import model
import captions
from terminal import Terminal
from test_tools import synthetic


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


def full(video_id, **kwargs):
    """Metadata with the formats of test_tools.synthetic() and the captions of captions.video()."""
    info = synthetic()
    given = captions.video(video_id, **kwargs)
    for key in ("id", "title", "language", "subtitles", "automatic_captions"):
        info[key] = given[key]
    info["webpage_url"] = "https://www.youtube.com/watch?v=" + video_id
    return info


def answers(chosen, **kwargs):
    return {
        "mode": "video-audio",
        "quality": "best",
        "manual": False,
        "lang": "original",
        "audio_format": "original",
        "compatible": False,
        "chosen": chosen,
        **kwargs,
    }


class Summary(unittest.TestCase):
    """What the summary before "Mulai" says about subtitles."""

    def build(self, metadata, wanted, **kwargs):
        merged = app.merge_tracks(metadata)
        chosen, _ = app.subtitle_identities(merged, wanted)
        return app.build_items(None, metadata, merged, answers(chosen, **kwargs))

    def test_no_subtitles_is_said_for_a_video_that_has_them(self):
        items, rows, _, notes = self.build([full("one00000001")], [])
        self.assertIn("tanpa subtitle", rows[0][2])
        self.assertEqual(notes, [])
        self.assertEqual(items[0]["subtitles"], [])

    def test_a_video_without_any_captions_says_nothing_about_them(self):
        info = full("one00000001")
        info["subtitles"], info["automatic_captions"] = {}, {}
        _, rows, _, notes = self.build([info], [])
        self.assertNotIn("subtitle", rows[0][2])
        self.assertEqual(notes, [])

    def test_a_chosen_track_is_named_with_where_it_comes_from(self):
        _, rows, _, _ = self.build([full("one00000001")], ["id"], compatible=True)
        self.assertRegex(
            rows[0][2], r"subtitle Indonesian .*\(terjemahan dari (CC|caption otomatis)\)"
        )
        self.assertIn("(.srt terpisah)", rows[0][2])

    def test_a_playlist_says_how_many_videos_have_each_chosen_track(self):
        without = [(c, n) for c, n in captions.TRANSLATIONS if c != "id"]
        metadata = [
            full("vid00000001"),
            full("vid00000002", translations=without),
            full("vid00000003"),
        ]
        _, rows, _, notes = self.build(metadata, ["id"])
        self.assertEqual(len(notes), 1)
        self.assertRegex(notes[0], r"Indonesian.* · 2/3 item")
        self.assertIn("1 item tidak punya track ini", notes[0])
        self.assertIn("tidak ada di video ini: Indonesian", rows[1][2])

    def test_a_playlist_without_subtitles_says_so_once(self):
        metadata = [full("vid00000001"), full("vid00000002")]
        _, _, _, notes = self.build(metadata, [])
        self.assertEqual(notes, ["Subtitle · tanpa"])

    def test_the_notes_are_printed_under_the_table(self):
        said = []

        class Ui:
            def say(self, message, *rest):
                said.append(str(message))

            def table(self, headers, rows):
                said.append("table")

        app.show_summary(Ui(), [("a", "b", "c"), ("d", "e", "f")], "/tmp", 0, ["Subtitle · x"])
        self.assertLess(said.index("table"), said.index("Subtitle · x"))


ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[=>])")
DOWN, ENTER, SPACE, TAB, CTRL_A, CTRL_C = "\x1b[B", "\r", " ", "\t", "\x01", "\x03"


def screen_text(raw):
    """What was written to the terminal, without the escape sequences."""
    return ESCAPES.sub("", raw)


CPR = "\x1b[6n"


def read(term, seconds):
    """What the terminal has written. prompt_toolkit asks where the cursor is before it draws the
    line under a list (the reason Enter was refused is shown there); a terminal answers, so this
    one does, with the first row."""
    chunk = term.read(seconds)
    if CPR in chunk:
        term.write("\x1b[1;1R")
    return chunk


def ask(script, steps, timeout=30):
    """Run `script` on a real pseudo-terminal and play `steps` into it: a key to press, or
    ("shows", text) to wait until the text has appeared. Returns what the script printed."""
    raw = ""
    deadline = time.monotonic() + timeout
    with Terminal([sys.executable, "-c", script], BASE) as term:
        for step in steps:
            if isinstance(step, tuple):
                while step[1] not in screen_text(raw):
                    if time.monotonic() > deadline or not term.running():
                        raise AssertionError(f"never showed {step[1]!r}: {screen_text(raw)!r}")
                    raw += read(term, 0.1)
            else:
                # prompt_toolkit has to finish painting before it can read the key
                raw += read(term, 0.2)
                term.write(step)
        while term.running() and time.monotonic() < deadline:
            raw += read(term, 0.1)
        raw += term.drain()
        if term.running():
            raise AssertionError("the menu did not end: " + repr(screen_text(raw)))
        term.wait()
    return screen_text(raw)


def menu(required=True, bulk=False):
    """A script that shows the 156 translation languages as a list and prints what was picked."""
    return (
        "import json, sys\n"
        "sys.path.insert(0, 'tests')\n"
        "import captions\n"
        "from ui import UI, Cancelled\n"
        "options = [(name + ' · Terjemahan otomatis', code) for code, name in captions.TRANSLATIONS]\n"
        "try:\n"
        "    picked = UI().checkboxes('Track subtitle', options, step=(4, 5), "
        f"required={required}, bulk={bulk})\n"
        "    print('RESULT', json.dumps(picked))\n"
        "except KeyboardInterrupt:\n"
        "    print('CANCELLED')\n"
    )


def result(output):
    found = re.findall(r"RESULT (\[.*?\])", output)
    return json.loads(found[-1]) if found else None


class Menu(unittest.TestCase):
    """The checkbox menu on a pseudo-terminal, with a list as long as a real video's."""

    refused = "Belum ada yang dicentang"

    def test_enter_with_nothing_ticked_is_refused_and_says_how_to_tick(self):
        # The way the tool was used when it gave "Track subtitle: tidak ada": arrow to a row,
        # Enter, never Space.
        out = ask(
            menu(),
            [("shows", "Track subtitle"), DOWN, DOWN, ENTER, ("shows", self.refused), CTRL_C],
        )
        self.assertIn("CANCELLED", out)
        self.assertIsNone(result(out))
        self.assertNotIn("tidak ada", out)

    def test_the_menu_carries_the_step_number_like_every_other_prompt(self):
        out = ask(menu(), [("shows", "4/5  Track subtitle"), CTRL_C])
        self.assertIn("CANCELLED", out)

    def test_space_then_enter_returns_the_ticked_row_and_only_that(self):
        out = ask(menu(), [("shows", "Track subtitle"), DOWN, DOWN, SPACE, ENTER])
        self.assertEqual(result(out), [captions.TRANSLATIONS[2][0]])

    def test_typing_a_language_searches_the_list_and_ticks_nothing(self):
        # Typing "indonesia" used to press i (invert), a (all) and the letters between: all
        # 156 languages came back ticked.
        out = ask(
            menu(),
            [("shows", "Track subtitle")] + list("indonesia") + [SPACE, ENTER],
        )
        self.assertEqual(result(out), ["id"])

    def test_the_letters_that_used_to_select_everything_select_nothing(self):
        out = ask(
            menu(),
            [("shows", "Track subtitle"), "i", "a", ENTER, ("shows", self.refused), CTRL_C],
        )
        self.assertIsNone(result(out))
        self.assertIn("CANCELLED", out)

    def test_ctrl_a_and_tab_do_nothing_in_a_list_that_turns_them_off(self):
        out = ask(
            menu(bulk=False),
            [("shows", "Track subtitle"), CTRL_A, TAB, ENTER, ("shows", self.refused), CTRL_C],
        )
        self.assertIsNone(result(out))
        self.assertNotIn("Ctrl+A", out.split("Belum ada")[0])

    def test_ctrl_a_ticks_every_row_in_a_list_that_keeps_it(self):
        out = ask(menu(required=False, bulk=True), [("shows", "Ctrl+A semua"), CTRL_A, ENTER])
        self.assertEqual(len(result(out)), len(captions.TRANSLATIONS))

    def test_a_list_that_does_not_require_an_answer_accepts_none_ticked(self):
        out = ask(menu(required=False), [("shows", "Track subtitle"), ENTER])
        self.assertEqual(result(out), [])


if __name__ == "__main__":
    unittest.main()
