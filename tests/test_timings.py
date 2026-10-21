"""Where the time of a download goes (FEAT-202), the pause before each file as the panel shows it
(UX-201), the pacing profiles (FEAT-201) and the closing line (UX-202)."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import model
import store
import ui
import worker
from rich.console import Console


class Clock:
    """A clock that only moves when a test says so."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Stream(io.StringIO):
    def sent(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]


class WorkerTimings(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timings = worker.Timings(self.clock)

    def test_each_phase_is_timed_and_a_phase_that_did_not_run_is_absent(self):
        with self.timings.phase("metadata"):
            self.clock.advance(3.25)
        with self.timings.phase("video"):
            self.clock.advance(40)
        self.clock.advance(0.5)  # between phases
        report = self.timings.report()
        self.assertEqual(report["metadata"], 3.25)
        self.assertEqual(report["video"], 40)
        self.assertEqual(report["total"], 43.75)
        for absent in ("audio", "subtitle", "finish"):
            self.assertNotIn(absent, report)

    def test_a_phase_that_runs_twice_adds_up_and_one_that_fails_is_still_counted(self):
        for _ in range(2):
            with self.timings.phase("subtitle"):
                self.clock.advance(1.5)
        with self.assertRaises(RuntimeError):
            with self.timings.phase("finish"):
                self.clock.advance(2)
                raise RuntimeError("ffmpeg")
        report = self.timings.report()
        self.assertEqual((report["subtitle"], report["finish"]), (3.0, 2))

    def test_waits_are_counted_inside_the_phase_they_happened_in(self):
        with self.timings.phase("video"):
            self.timings.waited(1.5)
            self.clock.advance(1.5)
            self.timings.bytes = 10 * 1024**2
            self.clock.advance(8.5)
        report = self.timings.report()
        self.assertEqual(report["waited"], 1.5)
        # 10 MiB over the 8.5 s of the stream that were not waiting.
        self.assertAlmostEqual(report["speed"], 10 * 1024**2 / 8.5, delta=1)

    def test_the_speed_is_unknown_without_bytes_or_without_time(self):
        with self.timings.phase("video"):
            self.clock.advance(5)
        self.assertIsNone(self.timings.report()["speed"])  # nothing was downloaded
        self.timings.bytes = 1000
        with self.timings.phase("audio"):
            self.clock.advance(0.01)
        with self.timings.phase("video"):
            self.timings.waited(5)
        self.assertIsNone(self.timings.report()["speed"])  # next to no time spent downloading

    def test_every_number_is_plain_and_not_negative(self):
        with self.timings.phase("metadata"):
            self.clock.advance(1)
        report = self.timings.report()
        for key, value in report.items():
            self.assertIsInstance(value, (int, float, type(None)), key)
            self.assertFalse(value is not None and value < 0, key)
        self.assertEqual(
            model.clean_timings(report), {k: v for k, v in report.items() if v is not None}
        )

    def test_the_report_holds_nothing_but_numbers_no_urls_or_paths(self):
        with self.timings.phase("video"):
            self.clock.advance(1)
        text = json.dumps(self.timings.report())
        self.assertNotIn("http", text)
        self.assertNotIn("/", text)
        self.assertEqual(set(self.timings.report()) - set(model.TIMING_KEYS), set())


class EventsAndWaits(unittest.TestCase):
    def setUp(self):
        self.stream = Stream()
        self.events = worker.Events(self.stream)
        self.events.timings = worker.Timings()

    def test_a_wait_is_sent_to_the_panel_and_counted_in_the_phase(self):
        with self.events.timings.phase("video"):
            worker.wait(self.events, 2.5, "Menunggu · jeda antar-unduhan")
        (sent,) = self.stream.sent()
        self.assertEqual(sent["type"], "wait")
        self.assertEqual(sent["message"], "Menunggu · jeda antar-unduhan")
        self.assertGreater(sent["until"], 0)
        self.assertEqual(self.events.timings.waits, {"video": 2.5})

    def test_a_wait_works_without_timings(self):
        self.events.timings = None
        worker.wait(self.events, 1, "x")
        self.assertEqual(self.stream.sent()[0]["type"], "wait")

    def hook(self, *statuses):
        for status in statuses:
            self.events.hook(
                {"status": status, "filename": "/t/a-video.mp4", "downloaded_bytes": 500}
            )

    def test_bytes_count_only_for_a_file_that_was_really_downloaded(self):
        self.hook("downloading", "finished")
        self.assertEqual(self.events.timings.bytes, 500)
        self.hook("finished")  # a file that was already there: finished with no download first
        self.assertEqual(self.events.timings.bytes, 500)


class Pause(unittest.TestCase):
    def test_the_pause_is_a_random_time_in_the_profile_range_shown_as_a_wait(self):
        stream, slept, asked = Stream(), [], []
        events = worker.Events(stream)

        def uniform(low, high):
            asked.append((low, high))
            return 1.5

        seconds = worker.pause_before_download(
            events, model.pace_profile("normal"), sleep=slept.append, uniform=uniform
        )
        self.assertEqual((seconds, slept, asked), (1.5, [1.5], [(1, 2)]))
        (sent,) = stream.sent()
        self.assertEqual((sent["type"], sent["message"]), ("wait", "Menunggu · jeda antar-unduhan"))

    def test_no_pause_is_no_event(self):
        stream, slept = Stream(), []
        seconds = worker.pause_before_download(
            worker.Events(stream), {"file": [0, 0]}, sleep=slept.append
        )
        self.assertEqual((seconds, slept, stream.getvalue()), (0.0, [], ""))

    def test_a_finished_stream_in_the_temporary_folder_is_found_a_partial_one_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse(worker.stream_present(root, "video"))
            (root / "abc-video.mp4.part").write_bytes(b"x")
            (root / "abc-video.mp4.ytdl").write_bytes(b"x")
            self.assertFalse(worker.stream_present(root, "video"))
            (root / "abc-video.mp4").write_bytes(b"x")
            self.assertTrue(worker.stream_present(root, "video"))
            self.assertFalse(worker.stream_present(root, "audio"))


class PaceProfiles(unittest.TestCase):
    def test_the_default_is_what_the_tool_has_always_done(self):
        normal = model.pace_profile()
        self.assertEqual(
            (normal["name"], normal["file"], normal["requests"]), ("normal", [1, 2], 1)
        )
        self.assertEqual(model.pace_profile(""), normal)
        self.assertEqual(model.pace_profile(" NORMAL "), normal)

    def test_the_three_profiles_are_ordered_from_hasty_to_careful(self):
        fast, normal, careful = (model.pace_profile(n) for n in ("fast", "normal", "careful"))
        self.assertLess(fast["file"][1], normal["file"][1])
        self.assertLess(normal["file"][1], careful["file"][1])
        self.assertLessEqual(fast["requests"], normal["requests"])
        self.assertLessEqual(normal["requests"], careful["requests"])
        for profile in (fast, normal, careful):
            self.assertLessEqual(profile["file"][0], profile["file"][1])

    def test_an_unknown_name_is_refused_with_the_accepted_ones(self):
        with self.assertRaises(model.SelectionError) as caught:
            model.pace_profile("slow")
        message = str(caught.exception)
        for word in ("YTDL_PACE=slow", "normal", "careful", "fast"):
            self.assertIn(word, message)

    def test_a_request_without_usable_pacing_gets_the_default(self):
        default = model.pace_profile()
        for request in (
            {},
            {"pace": None},
            {"pace": {}},
            {"pace": {"file": [3, 1], "requests": 1}},
            {"pace": {"file": ["a", 2], "requests": 1}},
            {"pace": {"file": [1, 2], "requests": -1}},
            {"pace": {"file": [1, float("inf")], "requests": 1}},
        ):
            self.assertEqual(model.pace_of(request), default, request)
        mine = {"name": "x", "file": [0.2, 0.4], "requests": 0}
        self.assertEqual(
            model.pace_of({"pace": mine}), {"name": "x", "file": [0.2, 0.4], "requests": 0.0}
        )

    def test_the_worker_gives_yt_dlp_only_the_pause_between_requests(self):
        for request, requests in (({}, 1), ({"pace": model.pace_profile("careful")}, 2)):
            opts = worker.options(request, worker.Events(Stream()))
            self.assertEqual(opts["sleep_interval_requests"], requests)
            # The pause before a file is the worker's own, so yt-dlp must not add a second one.
            self.assertNotIn("sleep_interval", opts)
            self.assertNotIn("max_sleep_interval", opts)

    def test_the_requests_the_app_sends_carry_the_profile_of_the_environment(self):
        sent = []
        with (
            patch.dict(os.environ, {"YTDL_PACE": "careful"}),
            patch.object(app, "run", side_effect=lambda request, *a, **k: sent.append(request)),
        ):
            app.inspect_url("https://youtu.be/a")
        self.assertEqual(sent[0]["pace"], model.pace_profile("careful"))
        sent.clear()
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(app, "run", side_effect=lambda request, *a, **k: sent.append(request)),
        ):
            os.environ.pop("YTDL_PACE", None)
            app.inspect_url("https://youtu.be/a")
        self.assertEqual(sent[0]["pace"], model.pace_profile())

    def test_an_unknown_profile_stops_the_command_before_anything_is_asked(self):
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"YTDL_PACE": "slow"}),
            patch.object(app, "inspect_url", side_effect=AssertionError("looked up")),
            patch("sys.stderr", err),
        ):
            code = app.main(["https://youtu.be/a", "--yes", "--plain"])
        self.assertEqual(code, 1)
        self.assertIn("normal, careful, fast", err.getvalue())


class CleanTimings(unittest.TestCase):
    def test_only_known_numeric_fields_survive(self):
        raw = {
            "total": 12.5,
            "video": 4,
            "audio": -1,
            "waited": float("nan"),
            "bytes": True,
            "speed": None,
            "secret": "https://x/?sig=1",
        }
        self.assertEqual(model.clean_timings(raw), {"total": 12.5, "video": 4})

    def test_without_a_total_there_is_nothing_to_keep(self):
        for raw in (None, [], "x", {}, {"video": 1}):
            self.assertIsNone(model.clean_timings(raw), raw)


def job_with(*timings):
    items = [
        {
            "id": f"v{n}",
            "title": f"Video {n}",
            "status": "complete",
            **({"timings": t} if t else {}),
        }
        for n, t in enumerate(timings)
    ]
    return {"id": "j1", "status": "complete", "output": "/m", "items": items}


FIRST = {
    "metadata": 3.0, "video": 40.0, "audio": 6.0, "subtitle": 1.0, "finish": 2.0,
    "total": 52.5, "waited": 3.0, "bytes": 92 * 1024**2, "speed": 2 * 1024**2, "wall": 53.5,
}  # fmt: skip
SECOND = {
    "metadata": 2.0, "video": 20.0, "audio": 4.0, "finish": 1.0,
    "total": 27.5, "waited": 2.0, "bytes": 36 * 1024**2, "speed": 2 * 1024**2, "wall": 28.0,
}  # fmt: skip


class JobTimings(unittest.TestCase):
    def test_a_job_without_timings_has_none(self):
        self.assertIsNone(app.job_timings(job_with(None, None)))
        self.assertIsNone(app.job_timings({"id": "x", "items": []}))
        self.assertIsNone(app.job_timings({"id": "x", "items": [{"timings": "broken"}, None]}))

    def test_the_totals_add_up_over_the_items_that_have_them(self):
        data = app.job_timings(job_with(FIRST, None, SECOND))
        self.assertEqual(data["items"], 2)
        self.assertEqual(data["wall"], 81.5)
        self.assertEqual(data["worker"], 80.0)
        self.assertEqual(data["overhead"], 1.5)  # 1.0 + 0.5 outside the workers
        self.assertEqual(data["waited"], 5.0)
        self.assertEqual(data["phases"]["video"], 60.0)
        self.assertEqual(data["phases"]["subtitle"], 1.0)
        self.assertAlmostEqual(data["speed"], 2 * 1024**2, delta=1)
        self.assertNotIn("per_item", data)

    def test_the_phases_come_close_to_the_worker_total(self):
        for timings in (FIRST, SECOND):
            phases = sum(timings.get(name, 0) for name in model.TIMING_PHASES)
            self.assertLessEqual(phases, timings["total"])
            self.assertGreater(phases, timings["total"] - 5)

    def test_the_detail_names_each_item(self):
        data = app.job_timings(job_with(FIRST, SECOND), detail=True)
        self.assertEqual([e["id"] for e in data["per_item"]], ["v0", "v1"])
        self.assertEqual(data["per_item"][0]["video"], 40.0)


class Reading(unittest.TestCase):
    """status --json and the table."""

    def run_status(self, job, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / ".state"
            (state / "jobs").mkdir(parents=True)
            (state / "jobs" / "j1.json").write_text(json.dumps(job), encoding="utf-8")
            screen = Table()
            out = io.StringIO()
            with (
                patch.object(store, "STATE", state),
                patch.object(app, "STATE", state),
                redirect_stdout(out),
            ):
                app.status(screen, **kwargs)
        return screen, out.getvalue()

    def test_status_json_carries_the_totals_and_with_timings_each_item(self):
        _, printed = self.run_status(job_with(FIRST, SECOND), as_json=True)
        entry = json.loads(printed)["jobs"][0]
        self.assertEqual(entry["timings"]["items"], 2)
        self.assertNotIn("per_item", entry["timings"])
        _, printed = self.run_status(job_with(FIRST, SECOND), as_json=True, timings=True)
        self.assertEqual(len(json.loads(printed)["jobs"][0]["timings"]["per_item"]), 2)

    def test_an_old_job_is_listed_as_before(self):
        _, printed = self.run_status(job_with(None, None), as_json=True, timings=True)
        data = json.loads(printed)
        self.assertEqual(data["jobs"], [{"id": "j1", "status": "complete", "items": 2}])

    def test_the_table_has_a_row_per_item_and_a_sum_and_says_so_when_there_is_nothing(self):
        screen, _ = self.run_status(job_with(FIRST, SECOND), timings=True)
        titles = [rows[0] for rows in screen.rows]
        self.assertIn("Video 0", titles)
        self.assertIn("Video 1", titles)
        self.assertEqual(titles[-1], "Jumlah")
        said_screen, _ = self.run_status(job_with(None), timings=True)
        self.assertIn("Belum ada job dengan catatan waktu.", said_screen.said)

    def test_status_shows_the_pace_and_does_not_fail_over_a_wrong_one(self):
        for value, shown in (("careful", "careful"), (None, "normal")):
            env = {"YTDL_PACE": value} if value else {}
            with patch.dict(os.environ, env):
                if not value:
                    os.environ.pop("YTDL_PACE", None)
                screen, printed = self.run_status(job_with(None), as_json=True)
            self.assertEqual(json.loads(printed)["pace"]["name"], shown)
        with patch.dict(os.environ, {"YTDL_PACE": "slow"}):
            screen, printed = self.run_status(job_with(None), as_json=True)
        self.assertIn("error", json.loads(printed)["pace"])
        with patch.dict(os.environ, {"YTDL_PACE": "slow"}):
            screen, _ = self.run_status(job_with(None))
        pace_row = [row for row in screen.rows if row[0] == "Jeda"]
        self.assertIn("tidak dikenal", pace_row[0][1])


class Table:
    """A UI for status: it keeps the rows and what was said."""

    def __init__(self):
        self.rows, self.said = [], []

    def table(self, headers, rows):
        self.rows += [tuple(map(str, row)) for row in rows]

    def say(self, *args):
        self.said.append(str(args[0]))


def rich_ui(width=80):
    screen = ui.UI(True)
    screen.plain = False
    screen.glyph = ui.GLYPHS
    buffer = io.StringIO()
    screen.console = Console(file=buffer, width=width, no_color=True, highlight=False)
    return screen, buffer


class ClosingLine(unittest.TestCase):
    def test_the_line_gives_the_average_speed_and_the_time_spent_waiting(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 2)
        session.item_end("complete", FIRST)
        session.item_end("complete", SECOND)
        # 128 MiB over 64 s of streaming each at 2 MiB/s, and 5 s of waiting.
        self.assertEqual(session.timing_line(), "Rata-rata 2.0 MB/s · menunggu 5d")

    def test_a_pause_of_less_than_a_second_or_an_unknown_speed_leaves_that_part_out(self):
        screen, _ = rich_ui()
        session = ui.Session(screen, 1)
        session.item_end("complete", {"total": 5, "waited": 0.4, "bytes": 0})
        self.assertEqual(session.timing_line(), "")
        session = ui.Session(screen, 1)
        session.item_end("complete", {"total": 5, "waited": 9, "bytes": 0})
        self.assertEqual(session.timing_line(), "menunggu 9d")

    def test_the_closing_shows_the_line_only_when_something_was_timed(self):
        for timed, shown in ((FIRST, True), (None, False)):
            screen, buffer = rich_ui(70)
            with ui.Session(screen, 2) as session:
                session.live = None
                session.item_end("complete", timed)
                session.item_end("skipped")
            text = buffer.getvalue()
            self.assertIn("Disimpan 1 · Dilewati 1 · Gagal 0", text)
            self.assertEqual("Rata-rata" in text, shown, text)

    def test_a_single_video_gets_the_line_without_a_tally(self):
        screen, buffer = rich_ui(70)
        with ui.Session(screen, 1) as session:
            session.live = None
            session.item_end("complete", FIRST)
        text = buffer.getvalue()
        self.assertIn("Rata-rata 2.0 MB/s · menunggu 3d", text)
        self.assertNotIn("Disimpan", text)

    def test_plain_output_stays_free_of_symbols_and_goes_to_stderr(self):
        screen = ui.UI(True)
        err, out = io.StringIO(), io.StringIO()
        with patch("sys.stderr", err), patch("sys.stdout", out):
            with ui.Session(screen, 1) as session:
                session.item_end("complete", FIRST)
        self.assertEqual(out.getvalue(), "")
        line = [x for x in err.getvalue().splitlines() if "Rata-rata" in x]
        self.assertEqual(len(line), 1)
        self.assertTrue(all(c.isascii() or c == "·" for c in line[0]), line[0])


if __name__ == "__main__":
    unittest.main()
