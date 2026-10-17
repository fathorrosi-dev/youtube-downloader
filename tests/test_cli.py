"""The command line around the downloader: flags, resume, status, clean and what they refuse."""

import argparse
import copy
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import app
import compat
import maintenance
import model
import runner
import store
from test_tools import synthetic


def metadata(identifier="a", subtitles=False):
    info = synthetic()
    info.update(id=identifier, title="Video " + identifier)
    info["webpage_url"] = "https://youtube.com/watch?v=" + identifier
    if subtitles:
        info["subtitles"] = {
            "en": [{"url": "https://x/?lang=en", "ext": "vtt", "name": "English"}],
            "id": [{"url": "https://x/?lang=id", "ext": "vtt", "name": "Indonesian"}],
        }
    return info


class Silent:
    """A UI that must never ask: every answer has to come from a flag or from --yes."""

    def __init__(self):
        self.answered_lines = []
        self.said = []
        self.rows = []

    def choose(self, title, *args, **kwargs):
        raise AssertionError("asked: " + title)

    checkboxes = text = choose

    def answered(self, label, answer, step=None):
        self.answered_lines.append((label, answer, step))

    def say(self, *args):
        self.said.append(str(args[0]))

    def table(self, headers, rows):
        self.rows += list(rows)

    def activity(self, *args):
        return nullcontext()


class Scripted(unittest.TestCase):
    """A job built from flags, with no menu and no terminal."""

    def wizard(self, preset, info=None, url="https://youtube.com/watch?v=a", ui=None):
        ui = ui or Silent()
        info = info or metadata(subtitles=True)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", return_value=info),
        ):
            return app.wizard(ui, url, output=folder, preset=preset), ui

    def test_yes_alone_answers_every_prompt_with_the_documented_defaults(self):
        job, ui = self.wizard({"yes": True})
        choice = job["items"][0]["choice"]
        self.assertEqual((choice["mode"], choice["actual_quality"]), ("video-audio", 4320))
        self.assertEqual(choice["audio_format"], "original")
        self.assertEqual(job["items"][0]["subtitles"], [])
        labels = [x[0] for x in ui.answered_lines]
        self.assertEqual(
            labels,
            ["Unduh apa?", "Format file", "Kualitas", "Bahasa audio", "Subtitle", "Mulai?"],
        )
        # The history is numbered like the menus are: 5 steps, the last one is the start.
        self.assertEqual(
            [x[2] for x in ui.answered_lines][1:], [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
        )

    def test_flags_answer_their_prompts_and_yes_the_rest(self):
        job, ui = self.wizard(
            {"yes": True, "quality": 720, "compatible": True, "lang": "ja", "subs": ["en"]}
        )
        item = job["items"][0]
        self.assertEqual(item["choice"]["actual_quality"], 720)
        self.assertEqual(item["choice"]["language"], "ja")
        self.assertEqual([t["language"] for t in item["subtitles"]], ["en"])
        self.assertIn(
            ("Format file", "MP4 kompatibel · H.264/AV1 + AAC · iPhone, Mac, editor", (1, 5)),
            ui.answered_lines,
        )

    def test_subtitles_missing_for_a_requested_language_are_said_not_dropped_silently(self):
        job, ui = self.wizard({"yes": True, "subs": ["en", "xx"]})
        self.assertEqual([t["language"] for t in job["items"][0]["subtitles"]], ["en"])
        self.assertTrue(any("xx" in line for line in ui.said))

    def test_original_captions_are_taken_before_automatic_ones(self):
        info = metadata(subtitles=True)
        info["automatic_captions"] = {
            "en": [{"url": "https://x/?lang=en&auto", "ext": "vtt", "name": "English (auto)"}]
        }
        job, _ = self.wizard({"yes": True, "subs": ["en"]}, info)
        self.assertEqual(len(job["items"][0]["subtitles"]), 1)
        self.assertEqual(job["items"][0]["subtitles"][0]["kind"], "manual")

    def test_none_means_no_subtitles_even_when_there_are_some(self):
        job, _ = self.wizard({"yes": True, "subs": []})
        self.assertEqual(job["items"][0]["subtitles"], [])

    def test_a_value_the_video_does_not_offer_is_refused_with_the_choices(self):
        with self.assertRaisesRegex(model.SelectionError, "--quality 999.*tidak tersedia.*1080"):
            self.wizard({"yes": True, "quality": 999})
        with self.assertRaisesRegex(model.SelectionError, "--lang fr.*tidak tersedia.*ja"):
            self.wizard({"yes": True, "lang": "fr"})

    def test_a_flag_that_does_not_apply_is_refused_not_ignored(self):
        with self.assertRaisesRegex(model.SelectionError, "--quality"):
            self.wizard({"yes": True, "mode": "audio", "quality": 720})
        with self.assertRaisesRegex(model.SelectionError, "--lang"):
            self.wizard({"yes": True, "mode": "video", "lang": "en"})
        with self.assertRaisesRegex(model.SelectionError, "--audio-format"):
            self.wizard({"yes": True, "audio_format": "mp3"})

    def test_audio_flags(self):
        job, _ = self.wizard({"yes": True, "mode": "audio", "audio_format": "mp3", "lang": "en"})
        choice = job["items"][0]["choice"]
        self.assertEqual((choice["mode"], choice["audio_format"]), ("audio", "mp3"))

    def test_items_and_playlist_need_a_playlist(self):
        with self.assertRaisesRegex(model.SelectionError, "--items hanya berlaku untuk playlist"):
            self.wizard({"yes": True, "items": "1"})
        with self.assertRaisesRegex(model.SelectionError, "--playlist memerlukan"):
            self.wizard({"yes": True, "playlist": True})

    def playlist(self, preset, url="https://youtube.com/playlist?list=PLx"):
        a, b, c = (metadata(x) for x in "abc")
        entries = [{"url": m["webpage_url"], "title": m["title"]} for m in (a, b, c)]
        initial = {"playlist": True, "title": "Lista", "entries": entries}
        ui = Silent()
        by_url = {m["webpage_url"]: m for m in (a, b, c)}

        def fake(link, browser=None, is_playlist=False):
            return by_url.get(link, initial)

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", side_effect=fake) as inspect,
        ):
            job = app.wizard(ui, url, output=folder, preset=preset)
        return job, ui, inspect

    def test_a_playlist_takes_all_items_under_yes_or_the_ones_named(self):
        job, ui, _ = self.playlist({"yes": True})
        self.assertEqual([i["id"] for i in job["items"]], ["a", "b", "c"])
        self.assertIn(("Item", "semua item", None), ui.answered_lines)
        job, ui, inspect = self.playlist({"yes": True, "items": "1,3"})
        self.assertEqual([i["id"] for i in job["items"]], ["a", "c"])
        self.assertEqual(inspect.call_count, 3)  # the list, and the two chosen items
        with self.assertRaisesRegex(model.SelectionError, "di luar daftar"):
            self.playlist({"yes": True, "items": "9"})
        # An empty value is not "everything": it is refused, like at the prompt.
        with self.assertRaisesRegex(model.SelectionError, "all untuk semua"):
            self.playlist({"yes": True, "items": " "})

    def test_a_link_with_a_video_and_a_playlist_is_the_video_unless_told_otherwise(self):
        url = "https://youtube.com/watch?v=a&list=PLx"
        info = metadata()
        job, ui = self.wizard({"yes": True}, info, url)
        self.assertEqual(len(job["items"]), 1)
        self.assertIn(("Sumber", "Video ini saja", None), ui.answered_lines)
        a, b = metadata("a"), metadata("b")
        initial = {
            "playlist": True,
            "title": "Lista",
            "entries": [{"url": m["webpage_url"], "title": m["title"]} for m in (a, b)],
        }
        ui = Silent()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", side_effect=[initial, a, b]),
        ):
            job = app.wizard(ui, url, output=folder, preset={"yes": True, "playlist": True})
        self.assertEqual(len(job["items"]), 2)

    def test_the_preset_of_flags_comes_from_the_command_line(self):
        parse = app.build_parser().parse_args
        args = parse(
            [
                "LINK",
                "--mode",
                "audio",
                "--quality",
                "1080",
                "--format",
                "mp4",
                "--lang",
                "en",
                "--audio-format",
                "mp3",
                "--subs",
                "en, id",
                "--items",
                "1,3",
                "--playlist",
                "--yes",
            ]
        )
        self.assertEqual(
            app.scripted_preset(args),
            {
                "mode": "audio",
                "quality": 1080,
                "compatible": True,
                "lang": "en",
                "audio_format": "mp3",
                "subs": ["en", "id"],
                "items": "1,3",
                "playlist": True,
                "yes": True,
            },
        )
        self.assertEqual(app.scripted_preset(parse(["LINK"])), {})
        self.assertEqual(app.scripted_preset(parse(["LINK", "--subs", "NONE"])), {"subs": []})
        self.assertEqual(
            app.scripted_preset(parse(["LINK", "--quality", "best"])), {"quality": "best"}
        )
        for bad in (
            ["--quality", "high"],
            ["--quality", "5"],
            ["--mode", "x"],
            ["--format", "avi"],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(["LINK", *bad])

    def test_help_lists_the_commands_and_the_version_is_printed(self):
        parser = app.build_parser()
        text = parser.format_help()
        for expected in (
            "status",
            "resume",
            "clean",
            "update",
            "rebase-media",
            "--yes",
            "setup.sh",
        ):
            self.assertIn(expected, text)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            parser.parse_args(["--version"])
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "youtube-downloader " + model.VERSION)


class Changing(unittest.TestCase):
    """The confirmation can send the person back to the questions without asking YouTube again."""

    def test_changing_the_answers_reuses_the_metadata_and_preselects_the_earlier_ones(self):
        seen = []
        plan = {
            "Unduh apa?": ["video-audio", "audio"],
            "Format file": [False],
            "Kualitas": [1080],
            "Bahasa audio": ["ja", "en"],
            "Format audio": ["mp3"],
            "Kualitas audio": [False],
            "Mulai?": ["edit", True],
        }
        queues = {k: list(v) for k, v in plan.items()}

        class Menu(Silent):
            def choose(self, title, options, default=None, step=None, label=None):
                seen.append((label, default, step))
                return queues[label].pop(0)

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", return_value=metadata()) as inspect,
        ):
            job = app.wizard(Menu(), "https://youtube.com/watch?v=a", output=folder)
        self.assertEqual(inspect.call_count, 1)  # not fetched again after "Ubah pilihan"
        self.assertEqual(job["items"][0]["choice"]["mode"], "audio")
        self.assertEqual(job["items"][0]["choice"]["language"], "en")
        first, second = seen[:5], seen[5:]
        self.assertEqual(
            [x[0] for x in first],
            ["Unduh apa?", "Format file", "Kualitas", "Bahasa audio", "Mulai?"],
        )
        # The second pass opens each question on what was answered the first time.
        defaults = {label: default for label, default, _ in second}
        self.assertEqual(defaults["Unduh apa?"], "video-audio")
        self.assertEqual(defaults["Bahasa audio"], "ja")
        self.assertEqual([q for q in queues.values() if q], [])  # every planned answer was used

    def test_cancelling_at_the_confirmation_saves_nothing(self):
        class Menu(Silent):
            def choose(self, title, options, default=None, step=None, label=None):
                return False if label == "Mulai?" else options[0][1]

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app, "inspect_url", return_value=metadata()),
        ):
            with self.assertRaises(app.Cancelled):
                app.wizard(Menu(), "https://youtube.com/watch?v=a", output=folder)
            self.assertEqual(list(Path(folder).glob("jobs/*.json")), [])


def write_job(folder, identifier, status, updated, done=0, total=2):
    path = Path(folder) / "jobs"
    path.mkdir(exist_ok=True)
    items = [{"status": "complete" if n < done else "failed"} for n in range(total)]
    job = {"id": identifier, "status": status, "updated_at": updated, "items": items}
    (path / (identifier + ".json")).write_text(json.dumps(job), encoding="utf-8")


class Resuming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        patcher = patch.object(store, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        write_job(self.state, "done", "complete", "2026-10-14T10:00:00+00:00", 2)
        write_job(self.state, "oldest", "interrupted", "2026-10-01T08:00:00+00:00")
        write_job(self.state, "newest", "blocked", "2026-10-14T21:07:00+00:00", 1)
        write_job(self.state, "middle", "partial", "2026-10-10T12:00:00+00:00", 1)
        (self.state / "jobs/broken.json").write_text("{not json", encoding="utf-8")

    def args(self, **kw):
        return SimpleNamespace(**{"job": None, "last": False, **kw})

    def test_jobs_are_listed_newest_first_and_a_broken_file_is_reported_not_fatal(self):
        jobs, unreadable = store.list_jobs()
        self.assertEqual([j["id"] for j in jobs], ["newest", "done", "middle", "oldest"])
        self.assertEqual([name for name, _ in unreadable], ["broken"])
        self.assertIn("broken.json", unreadable[0][1])
        self.assertEqual(
            [j["id"] for j in store.resumable_jobs(jobs)], ["newest", "middle", "oldest"]
        )

    def test_a_job_without_a_status_or_time_is_tolerated(self):
        # The shape the clean-install test leaves behind: nothing but an id and its items.
        (self.state / "jobs/sentinel.json").write_text('{"id":"sentinel","items":[]}')
        jobs, _ = store.list_jobs()
        self.assertEqual(jobs[-1]["id"], "sentinel")
        self.assertNotIn("sentinel", [j["id"] for j in store.resumable_jobs(jobs)])
        self.assertEqual(app.job_label(jobs[-1]), "sentinel · None · 0/0 selesai · -")

    def test_last_takes_the_newest_job_that_has_something_left(self):
        self.assertEqual(app.choose_job(Silent(), self.args(last=True))["id"], "newest")

    def test_without_a_terminal_an_id_or_last_is_required(self):
        with patch.object(app.sys.stdin, "isatty", return_value=False, create=True):
            with self.assertRaisesRegex(ValueError, "resume ID_JOB atau resume --last"):
                app.choose_job(Silent(), self.args())
        with self.assertRaisesRegex(ValueError, "bukan keduanya"):
            app.choose_job(Silent(), self.args(last=True, job="x"))

    def test_the_menu_offers_only_what_can_be_resumed_and_returns_the_pick(self):
        offered = []

        class Menu(Silent):
            def choose(self, title, options, label=None, **kw):
                offered.extend(options)
                return "middle"

        with patch.object(app.sys.stdin, "isatty", return_value=True, create=True):
            job = app.choose_job(Menu(), self.args())
        self.assertEqual(job["id"], "middle")
        self.assertEqual([value for _, value in offered], ["newest", "middle", "oldest"])
        text = offered[0][0]
        self.assertRegex(text, r"^newest · blocked · 1/2 selesai · \d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_nothing_to_resume_says_so(self):
        for name in ("newest", "middle", "oldest"):
            (self.state / "jobs" / (name + ".json")).unlink()
        with patch.object(app.sys.stdin, "isatty", return_value=True, create=True):
            with self.assertRaisesRegex(ValueError, "Tidak ada job yang bisa dilanjutkan"):
                app.choose_job(Silent(), self.args())
        with self.assertRaisesRegex(ValueError, "Tidak ada job yang bisa dilanjutkan"):
            app.choose_job(Silent(), self.args(last=True))


class Stopping(unittest.TestCase):
    def test_every_stop_ends_with_a_reason_and_the_way_on(self):
        self.assertIn("login di firefox", app.stop_message("authentication").lower())
        self.assertIn("Tunggu beberapa menit", app.stop_message("rate_limit"))
        self.assertIn("koneksi", app.stop_message("network"))
        for message in map(app.stop_message, ("authentication", "rate_limit", "network", "system")):
            self.assertTrue(message.endswith("lanjutkan:"), message)

    def test_an_old_yt_dlp_is_named_only_when_it_could_be_the_cause(self):
        with patch.object(app, "ytdlp_age", return_value=57):
            self.assertIn("yt-dlp rilis 57 hari lalu", app.stop_message("system"))
            self.assertIn("yt-dlp rilis 57 hari lalu", app.stop_message("format"))
            self.assertNotIn("yt-dlp", app.stop_message("authentication"))
            self.assertNotIn("yt-dlp", app.stop_message("network"))
        with patch.object(app, "ytdlp_age", return_value=model.PIN_WARN_DAYS):
            self.assertNotIn("yt-dlp", app.stop_message("system"))
        with patch.object(app, "ytdlp_age", return_value=None):
            self.assertNotIn("yt-dlp", app.stop_message("system"))

    def test_the_resume_command_works_from_anywhere(self):
        with patch.object(app.os, "getcwd", return_value=str(app.BASE)):
            self.assertEqual(app.resume_command("abc123"), "./youtube-downloader resume abc123")
            self.assertEqual(
                app.resume_command("abc123", login=True),
                "./youtube-downloader resume abc123 --browser firefox",
            )
        with tempfile.TemporaryDirectory() as elsewhere:
            with patch.object(app.os, "getcwd", return_value=elsewhere):
                command = app.resume_command("abc123")
        self.assertTrue(command.endswith(" resume abc123"))
        self.assertIn(str(app.BASE), command)

    def run_stopped(self, categories):
        """A batch whose items fail with the given categories, in order."""
        items = [
            {
                "id": f"i{n}",
                "title": f"T{n}",
                "url": "u",
                "choice": {"mode": "audio"},
                "status": "pending",
            }
            for n in range(len(categories))
        ]
        job = store.new_job([copy.deepcopy(i) for i in items], "/tmp/none")
        failures = iter(categories)

        def fail(request, callback=None):
            raise runner.WorkerError("pesan", next(failures))

        with (
            patch.object(app, "run", side_effect=fail),
            patch.object(app, "variant", side_effect=lambda item: item["id"]),
            patch.object(store.Archive, "get", return_value=None),
            patch.object(app, "native_output"),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(app.Stopped) as stop:
                app.execute(app.UI(True), job)
        return stop.exception, store.load_job(job["id"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(store, "STATE", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        (Path(self.tmp.name) / "jobs").mkdir()

    def test_a_login_request_stops_at_once_and_asks_for_the_browser_flag(self):
        stop, saved = self.run_stopped(["authentication", "system", "system"])
        self.assertEqual((stop.category, stop.login), ("authentication", True))
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual([i["status"] for i in saved["items"]], ["failed", "pending", "pending"])

    def test_a_rate_limit_stops_at_once_instead_of_asking_again(self):
        stop, saved = self.run_stopped(["rate_limit", "system", "system"])
        self.assertEqual((stop.category, stop.login), ("rate_limit", False))
        self.assertIn("Tunggu", str(stop))
        self.assertEqual(saved["items"][1]["status"], "pending")

    def test_three_in_a_row_stop_and_a_format_error_counts_but_unavailable_does_not(self):
        stop, _ = self.run_stopped(["format", "format", "format"])
        self.assertEqual(stop.category, "format")
        self.assertIn("Tiga kegagalan", str(stop))
        # Unavailable videos are skipped without counting: the batch runs to its end.
        job = store.new_job(
            [
                {
                    "id": f"i{n}",
                    "title": "T",
                    "url": "u",
                    "choice": {"mode": "audio"},
                    "status": "pending",
                }
                for n in range(5)
            ],
            "/tmp/none",
        )

        def gone(request, callback=None):
            raise runner.WorkerError("Private video", "unavailable")

        with (
            patch.object(app, "run", side_effect=gone),
            patch.object(app, "variant", side_effect=lambda item: item["id"]),
            patch.object(store.Archive, "get", return_value=None),
            patch.object(app, "native_output"),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(app.Stopped, "belum selesai") as stop:
                app.execute(app.UI(True), job)
        self.assertIsNone(stop.exception.category)
        self.assertEqual(
            sum(i["status"] == "failed" for i in store.load_job(job["id"])["items"]), 5
        )

    def test_saved_paths_go_to_stdout_only_when_it_is_collected(self):
        job = {
            "items": [
                {"status": "complete", "result": {"path": "/m/a.mp4"}},
                {"status": "partial", "result": {"path": "/m/b.mkv"}},
                {"status": "failed", "error": {"message": "x"}},
                {"status": "pending"},
            ]
        }
        for scripted, tty, expected in (
            (True, True, "/m/a.mp4\n/m/b.mkv\n"),
            (False, False, "/m/a.mp4\n/m/b.mkv\n"),
            (False, True, ""),
        ):
            out = io.StringIO()
            with (
                redirect_stdout(out),
                patch.object(out, "isatty", return_value=tty, create=True),
            ):
                app.emit_paths(job, scripted)
            self.assertEqual(out.getvalue(), expected, (scripted, tty))


class Status(unittest.TestCase):
    def test_the_pin_note_names_the_age_of_yt_dlp_only(self):
        with patch.object(app, "release_age_days", return_value=57):
            self.assertIn("rilis 57 hari lalu", app.pin_note("yt-dlp", "2026.8.19"))
            self.assertIn("sudah lama", app.pin_note("yt-dlp", "2026.8.19"))
            self.assertEqual(app.pin_note("rich", "14.3.4"), "")
        with patch.object(app, "release_age_days", return_value=10):
            note = app.pin_note("YT-DLP", "2026.10.5")
            self.assertIn("rilis 10 hari lalu", note)
            self.assertNotIn("sudah lama", note)
        with patch.object(app, "release_age_days", return_value=None):
            self.assertEqual(app.pin_note("yt-dlp", "weird"), "")

    def test_status_shows_age_state_size_and_unreadable_jobs_without_failing(self):
        tables = []

        class Capture(Silent):
            def table(self, headers, rows):
                tables.append((headers, list(rows)))

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / ".state"
            state.mkdir()
            (state / "venv-before-move-1").mkdir()
            (state / "venv-before-move-1/big").write_bytes(b"x" * 4096)
            write_job(state, "fine", "complete", "2026-10-14T10:00:00+00:00", 2)
            (state / "jobs/broken.json").write_text("nope")
            with patch.object(store, "STATE", state), patch.object(app, "STATE", state):
                app.status(Capture())
        info = dict(tables[0][1])
        self.assertRegex(info["Catatan"], r"\d+(\.\d)? (KB|B|MB).*dapat dibersihkan.*clean")
        self.assertRegex(info["yt-dlp"], r"rilis \d+ hari lalu")
        jobs = dict((row[0], row[1:]) for row in tables[1][1])
        self.assertEqual(jobs["fine"], ("complete", 2))
        self.assertEqual(jobs["broken"], ("tidak terbaca", "-"))


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("BASE", self.root), ("STATE", self.root / ".state")):
            patcher = patch.object(store, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / ".state/jobs").mkdir(parents=True)

    def write(self, text):
        (self.root / "settings.json").write_text(text, encoding="utf-8")

    def test_missing_valid_and_partial_settings(self):
        self.assertEqual(store.settings()["browser"], None)
        self.write('{"output": "/media/x", "browser": "firefox:/p"}')
        self.assertEqual(store.settings(), {"output": "/media/x", "browser": "firefox:/p"})
        self.write('{"output": "/media/x"}')
        self.assertIsNone(store.settings()["browser"])

    def test_damaged_settings_name_the_file_and_what_is_wrong(self):
        for text, expected in (
            ("{oops", r"Pengaturan tidak dapat dibaca:.*settings\.json.*Perbaiki"),
            ("[1, 2]", r"harus berupa objek JSON"),
            ('{"output": ""}', r"'output'.*jalur folder"),
            ('{"output": 5}', r"'output'.*jalur folder"),
            ('{"browser": "chrome"}', r"settings\.json.*hanya firefox"),
        ):
            self.write(text)
            with self.assertRaisesRegex(ValueError, expected):
                store.settings()

    def test_damaged_jobs_name_the_file(self):
        path = self.root / ".state/jobs/bad.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"Job tidak dapat dibaca:.*bad\.json"):
            store.load_job("bad")
        path.write_text('["not", "a", "job"]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bukan catatan job"):
            store.load_job("bad")
        with self.assertRaisesRegex(ValueError, "tidak ditemukan"):
            store.load_job("nothing")
        with self.assertRaisesRegex(ValueError, "tidak valid"):
            store.load_job("../escape")

    def test_main_reports_a_damaged_settings_file_instead_of_a_traceback(self):
        self.write("{oops")
        err = io.StringIO()
        with (
            patch.object(app, "STATE", self.root / ".state"),
            patch.object(app.sys.stdin, "isatty", return_value=True, create=True),
            redirect_stderr(err),
        ):
            code = app.main(["https://youtu.be/abc", "--plain"])
        self.assertEqual(code, 1)
        self.assertIn("Pengaturan tidak dapat dibaca", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_an_invalid_browser_flag_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = app.main(["https://youtu.be/abc", "--browser", "chrome", "--plain"])
        self.assertEqual(code, 1)
        self.assertIn("hanya firefox", err.getvalue())


class Environment(unittest.TestCase):
    def test_credentials_of_the_updater_are_not_passed_on(self):
        source = {
            "PATH": "/bin",
            "GH_TOKEN": "a",
            "github_token": "b",
            "GITHUB_TOKEN": "c",
            "GH_ENTERPRISE_TOKEN": "d",
            "HOME": "/h",
        }
        env = compat.clean_environment({"PYTHONUTF8": "1"}, source)
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "PYTHONUTF8": "1"})
        self.assertIn("GH_TOKEN", source)  # the caller's own environment is left alone

    def test_the_worker_starts_without_them(self):
        seen = {}

        def refuse(command, **kwargs):
            seen.update(kwargs["env"])
            raise OSError("stop here")

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(runner, "STATE", Path(folder)),
            patch.dict(os.environ, {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret"}),
            patch.object(runner.subprocess, "Popen", side_effect=refuse),
        ):
            with self.assertRaises(OSError):
                runner.run({"action": "inspect", "url": "x"})
        self.assertTrue(seen)
        self.assertNotIn("GH_TOKEN", seen)
        self.assertNotIn("GITHUB_TOKEN", seen)
        self.assertEqual(seen["PYTHONUTF8"], "1")
        self.assertIn("PATH", seen)

    def test_pip_and_the_installer_do_not_get_them_either(self):
        for module in ("maintenance", "self_update"):
            source = (BASE / (module + ".py")).read_text(encoding="utf-8")
            self.assertIn("clean_environment(", source)
            self.assertNotIn("**os.environ", source)


class Cleaning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "tools"
        self.state = self.root / ".state"
        self.state.mkdir(parents=True)
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "precious").write_text("keep")

        def put(path, size=100, age_days=0):
            path = self.state / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
            if age_days:
                old = path.stat().st_mtime - age_days * 86400
                os.utime(path, (old, old))
            return path

        self.put = put
        # Things that may go.
        put("venv-before-move-1/lib/x", 500)
        put("venv-before-move-2/lib/y", 300)
        for stamp in ("100", "200", "300"):
            put(f"program-backups/{stamp}/app.py", 50)
        (self.state / "last-update.json").write_text(
            json.dumps({"backup": str(self.state / "program-backups/100")})
        )
        put("media-path-backups/old/x", 40)
        old = time.time() - 40 * 86400
        os.utime(self.state / "media-path-backups/old", (old, old))
        put("media-path-backups/new/x", 40)
        put("pip-cache/http/x", 700)
        put("cache/yt/x", 200)
        put("worker-abc.json", 10, age_days=3)
        put("worker-fresh.json", 10)
        put("release-old/x", 10)
        stale = time.time() - 3 * 86400
        os.utime(self.state / "release-old", (stale, stale))
        # Temporary folders: the job that owns one decides.
        info = synthetic()
        choice = model.pick(info, "video-audio", "best", "en", "original", None, None, False)
        done = {"id": "done", "choice": choice, "subtitles": []}
        kept = {"id": "kept", "choice": choice, "subtitles": []}
        put(f"temporary/{model.variant(done)}/a.part", 60)
        put(f"temporary/{model.variant(kept)}/b.part", 60)
        put("temporary/unknown/c.part", 60)
        for name, status, item in (("finished", "complete", done), ("open", "blocked", kept)):
            put(f"jobs/{name}.json").write_text(
                json.dumps({"id": name, "status": status, "items": [item]})
            )
        # Things that must never go.
        self.protected = [
            put("downloaded.sqlite3"),
            put("run.lock"),
            put("download.log"),
            put("update.log"),
            self.state / "jobs/finished.json",
            self.state / "last-update.json",
            self.state / "program-backups/300/app.py",
            self.state / "program-backups/100/app.py",
        ]
        (self.root / "settings.json").write_text("{}")
        (self.root / "media").mkdir()
        (self.root / "media/film.mkv").write_bytes(b"film")
        self.protected += [self.root / "settings.json", self.root / "media/film.mkv"]
        # A link that points out of .state is never followed or removed.
        link = self.state / "venv-before-move-evil"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
            self.link = link
        except OSError:  # Windows without the privilege
            self.link = None

    def plan(self, **kw):
        return {
            k: sorted(p.name for p, _ in v)
            for k, v in maintenance.clean_plan(self.root, **kw).items()
        }

    def test_the_plan_lists_only_what_is_safe_to_remove(self):
        plan = self.plan()
        self.assertEqual(plan["venv"], ["venv-before-move-1", "venv-before-move-2"])
        # The newest backup and the one rollback would use stay; the middle one goes.
        self.assertEqual(plan["backups"], ["200"])
        self.assertEqual(plan["media"], ["old"])
        self.assertEqual(plan["cache"], ["cache", "pip-cache"])
        self.assertEqual(plan["leftovers"], ["release-old", "worker-abc.json"])
        self.assertEqual(len(plan["temporary"]), 1)  # the finished job's, not the open one's

    def test_the_age_of_media_backups_is_a_parameter(self):
        self.assertEqual(self.plan(days=60)["media"], [])
        self.assertEqual(self.plan(days=1)["media"], ["old"])
        # Judged a week from now, both have passed a week-old limit.
        self.assertEqual(self.plan(days=1, now=time.time() + 7 * 86400)["media"], ["new", "old"])

    def test_a_listing_removes_nothing_and_reports_sizes(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        rows = maintenance.clean(self.root)
        self.assertEqual(
            before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        )
        counts = {label: (count, size) for label, count, size in rows}
        self.assertEqual(counts[maintenance.CLEAN_CATEGORIES["venv"]], (2, 800))
        self.assertEqual(counts[maintenance.CLEAN_CATEGORIES["cache"]][1], 900)

    def test_applying_frees_exactly_what_was_listed_and_keeps_everything_else(self):
        listed = sum(size for _, _, size in maintenance.clean(self.root))
        before = {p: p.read_bytes() for p in self.protected if p.is_file()}
        rows = maintenance.clean(self.root, apply=True)
        self.assertEqual(sum(size for _, _, size in rows), listed)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.assertFalse((self.state / "venv-before-move-1").exists())
        self.assertFalse((self.state / "pip-cache").exists())
        self.assertFalse((self.state / "program-backups/200").exists())
        self.assertTrue((self.state / "program-backups/300").exists())
        self.assertEqual(maintenance.clean(self.root), [])  # nothing left to do

    def test_a_job_that_is_not_finished_keeps_its_temporary_folder(self):
        maintenance.clean(self.root, apply=True)
        kept = sorted(p.name for p in (self.state / "temporary").iterdir())
        self.assertEqual(len(kept), 2)
        self.assertIn("unknown", kept)

    def test_a_link_out_of_state_is_never_followed_or_removed(self):
        if not self.link:
            self.skipTest("symbolic links are not available")
        maintenance.clean(self.root, apply=True)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual((self.outside / "precious").read_text(), "keep")

    def test_an_unreadable_job_does_not_stop_the_plan(self):
        self.put("jobs/broken.json").write_text("{nope")
        self.put("jobs/odd.json").write_text('{"status": "complete", "items": [1, 2]}')
        self.assertEqual(len(self.plan()["temporary"]), 1)

    def test_cleaning_a_tool_folder_that_has_no_state_is_not_an_error(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(maintenance.clean(empty, apply=True), [])

    def test_the_command_lists_by_default_and_applies_on_request(self):
        lock_taken = []

        class Capture(Silent):
            def table(self, headers, rows):
                self.rows += list(rows)

        def command(**kw):
            screen = Capture()
            args = SimpleNamespace(**{"apply": False, "days": 30, **kw})
            with (
                patch.object(app, "BASE", self.root),
                patch.object(
                    app, "lock", side_effect=lambda: lock_taken.append(1) or nullcontext()
                ),
            ):
                self.assertEqual(app.clean_command(screen, args), 0)
            return screen

        listing = command()
        self.assertEqual(lock_taken, [])  # a listing changes nothing, so it needs no lock
        self.assertTrue(
            any("Dapat dibebaskan" in line and "--apply" in line for line in listing.said)
        )
        applied = command(apply=True)
        self.assertEqual(lock_taken, [1])
        self.assertTrue(any(line.startswith("Dibebaskan") for line in applied.said))
        again = command()
        self.assertEqual(again.said, ["Tidak ada yang perlu dibersihkan."])


class Commands(unittest.TestCase):
    def test_commands_that_change_the_python_environment_point_to_their_scripts(self):
        for word in ("setup", "migrate", "rollback"):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(app.main([word, "--plain"]), 1)
            self.assertRegex(err.getvalue(), rf"{word}\.(sh|cmd)")
            self.assertIn("lingkungan Python", err.getvalue())

    def test_flags_without_a_menu_are_for_links_only(self):
        for command in ("status", "resume", "clean"):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(app.main([command, "--yes", "--plain"]), 1)
            self.assertIn("hanya untuk membuat job dari link", err.getvalue())

    def test_a_wizard_without_a_terminal_needs_yes(self):
        err = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(store, "STATE", Path(folder)),
            patch.object(app.sys.stdin, "isatty", return_value=False, create=True),
            redirect_stderr(err),
        ):
            self.assertEqual(app.main(["https://youtu.be/abc", "--plain"]), 1)
        self.assertIn("--yes", err.getvalue())

    def test_rebase_media_needs_both_ends(self):
        with self.assertRaisesRegex(ValueError, "rebase-media --from LAMA --to BARU"):
            app.rebase_command(argparse.Namespace(old_root=None, new_root="/b", dry_run=False))

    def test_rebase_media_runs_under_the_lock(self):
        calls = []
        with (
            patch.object(app, "lock", return_value=nullcontext()),
            patch("media_migration.rebase_media", side_effect=lambda *a: calls.append(a)),
        ):
            args = argparse.Namespace(old_root="/a", new_root="/b", dry_run=True)
            self.assertEqual(app.rebase_command(args), 0)
        self.assertEqual(calls, [(app.BASE, "/a", "/b", True)])


class Documents(unittest.TestCase):
    """The documents name the version the code is, so they cannot quietly fall behind."""

    def test_the_guides_and_the_preview_name_the_current_version(self):
        for name in ("README.md", "PANDUAN.md", "MIGRASI.md"):
            first = (BASE / name).read_text(encoding="utf-8").splitlines()[0]
            self.assertIn(model.VERSION, first, name)
        preview = BASE / "docs/PREVIEW.svg"
        if preview.exists():  # docs/ is not part of the installed program
            named = model.VERSION in preview.read_text(encoding="utf-8")
            self.assertTrue(named, "docs/PREVIEW.svg does not show version " + model.VERSION)

    def test_no_guide_describes_an_older_release_as_the_current_one(self):
        # "Rilis X memakai yt-dlp ..." states what the current release ships.
        for name in ("PANDUAN.md", "README.md", "MIGRASI.md"):
            text = (BASE / name).read_text(encoding="utf-8")
            for found in re.findall(r"Rilis (\d+\.\d+\.\d+) memakai", text):
                self.assertEqual(found, model.VERSION, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
