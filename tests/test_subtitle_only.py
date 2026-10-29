"""The subtitle-only mode: the subtitles of a video as .srt files, and nothing else."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import app
import captions
import model
import store
import ui
import worker
from test_subtitle_limits import Events as Recorder, Handler
from test_subtitles import full
from test_tools import rich_ui, synthetic


class Events(Recorder):
    """The events of the worker, with the hooks that yt-dlp is given."""

    hook = pp_hook = staticmethod(lambda *args: None)


TRACK = {
    "key": "id",
    "origin": "automatic_captions",
    "language": "id",
    "kind": "translated",
    "name": "Indonesian",
    "source": "en",
}


def media_item(mode):
    choice = {
        "mode": mode,
        "quality": "best",
        "actual_quality": 1080 if mode != "audio" else None,
        "language": "en",
        "audio_format": "original",
        "video_format": "137",
        "audio_format_id": "140",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "vcodec": "avc1.640028",
        "dynamic_range": "SDR",
        "acodec": "mp4a.40.2",
        "estimated_size": 5,
    }
    return {"id": "abcdefghijk", "title": "Judul: tes / 1", "choice": choice, "subtitles": [TRACK]}


def only_subtitles(tracks=(TRACK,), title="Judul: tes / 1"):
    choice = model.pick(synthetic(), "subtitle")
    return {
        "id": "abcdefghijk",
        "title": title,
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "choice": choice,
        "subtitles": list(tracks),
        "status": "pending",
    }


class Identity(unittest.TestCase):
    """What the new mode is, and that the others are what they were."""

    def test_the_other_modes_have_the_same_variant_and_name_as_before(self):
        # Worked out with the model as it was before this mode existed.
        before = {
            "video-audio": (
                "5c3895c29fed20c86dddbdcc6aed682e68535c96e3532dc7fceff94eb08faff5",
                "Judul_ tes _ 1 [abcdefghijk] - 1080p - video-audio - 5c3895c29f",
            ),
            "audio": (
                "2c0f136d83d1328a6e390d39fca89729e1965871db21d0c4bc19db7ee86b2904",
                "Judul_ tes _ 1 [abcdefghijk] - original - audio - 2c0f136d83",
            ),
            "video": (
                "ed7c262a2eaa5c67a7f31ae273232896646f3bbac027bc964ed23750aec21b77",
                "Judul_ tes _ 1 [abcdefghijk] - 1080p - video - ed7c262a2e",
            ),
        }
        for mode, (variant, name) in before.items():
            item = media_item(mode)
            self.assertEqual(model.variant(item), variant, mode)
            self.assertEqual(model.basename(item), name, mode)
            self.assertEqual(model.subtitle_prefix(item), name, mode)

    def test_the_choice_of_this_mode_names_nothing_of_the_media(self):
        choice = model.pick(synthetic(), "subtitle")
        self.assertEqual(choice["mode"], "subtitle")
        for key in ("actual_quality", "video_format", "audio_format_id", "vcodec", "acodec"):
            self.assertIsNone(choice[key], key)
        self.assertEqual(model.container(choice, [TRACK]), "srt")
        self.assertEqual(model.phases(only_subtitles()), ["metadata", "subtitle"])
        self.assertIn("subtitle", model.MODES)

    def test_a_video_with_no_formats_at_all_can_still_have_its_subtitles_saved(self):
        # The choice is made without looking at the formats.
        self.assertEqual(model.pick({"formats": []}, "subtitle")["mode"], "subtitle")
        with self.assertRaises(model.SelectionError):
            model.pick({"formats": []}, "audio")

    def test_the_name_of_a_subtitle_does_not_depend_on_what_was_chosen_for_media(self):
        item = only_subtitles()
        self.assertEqual(model.subtitle_prefix(item), "Judul_ tes _ 1 [abcdefghijk]")
        # Another track list, the same video: the same start of the name.
        other = only_subtitles(tracks=[{**TRACK, "key": "en"}])
        self.assertEqual(model.subtitle_prefix(other), model.subtitle_prefix(item))
        self.assertNotIn(model.variant(item)[:10], model.subtitle_prefix(item))

    def test_the_name_of_a_subtitle_stays_inside_the_subtitle_folder(self):
        evil = {**TRACK, "key": "../../etc/passwd", "origin": "subtitles/../x"}
        path = model.subtitle_path(Path("/media"), "Judul [abc]", evil)
        self.assertEqual(path.parent, Path("/media/subtitle"))
        self.assertEqual(path.suffix, ".srt")
        self.assertNotIn("/", path.name)
        self.assertTrue(set(path.name.split("].")[1][:-4]) <= set("abcdefghijklmnopqrstuvwxyz-_."))


class Wizard(unittest.TestCase):
    """The questions, the summary and the flags of this mode."""

    class Screen:
        def __init__(self):
            self.asked, self.answered_lines, self.said = [], [], []

        def choose(self, title, options, **kwargs):
            self.asked.append((title, [text for text, _ in options]))
            return options[0][1]

        def checkboxes(self, title, options, **kwargs):
            self.asked.append((title, [text for text, _ in options]))
            return [options[0][1]]

        def answered(self, label, answer, step=None):
            self.answered_lines.append((label, answer, step))

        def say(self, message, *rest):
            self.said.append(str(message))

    def settings(self, metadata, preset):
        screen = self.Screen()
        merged = app.merge_tracks(metadata)
        preset = {"_used": set(), **preset}
        return screen, merged, app.ask_settings(screen, metadata, merged, preset)

    def test_the_mode_is_offered_when_the_video_has_subtitles(self):
        metadata = [full("one00000001")]
        screen = self.Screen()
        app.ask_settings(screen, metadata, app.merge_tracks(metadata), {"_used": set()})
        self.assertIn("Subtitle saja — tanpa video dan audio", screen.asked[0][1])

    def test_the_mode_is_not_offered_when_there_is_nothing_to_save(self):
        info = full("one00000001")
        info["subtitles"], info["automatic_captions"] = {}, {}
        screen = self.Screen()
        app.ask_settings(screen, [info], {}, {"_used": set()})
        self.assertFalse(any("Subtitle saja" in text for text in screen.asked[0][1]))

    def test_it_asks_for_the_subtitles_and_for_the_start_and_nothing_else(self):
        metadata = [full("one00000001")]
        screen, merged, settings = self.settings(
            metadata, {"mode": "subtitle", "subs": ["id"], "yes": True}
        )
        self.assertEqual(settings["mode"], "subtitle")
        self.assertEqual([merged[i]["language"] for i in settings["chosen"]], ["id"])
        # Two numbered prompts: the subtitles (answered by the flag) and the start.
        self.assertEqual(settings["steps"].total, 2)
        # The mode is asked before the count and carries no number.
        self.assertEqual(
            [step for _, _, step in screen.answered_lines if step is not None], [(1, 2)]
        )
        self.assertEqual(screen.asked, [])

    def test_the_menu_goes_straight_to_the_tracks(self):
        metadata = [full("one00000001")]
        screen, _, settings = self.settings(metadata, {"mode": "subtitle"})
        self.assertEqual([title for title, _ in screen.asked], ["Track subtitle"])
        self.assertNotIn("Tambahan subtitle", [title for title, _ in screen.asked])
        self.assertEqual(len(settings["chosen"]), 1)
        self.assertTrue(any("Subtitle saja" in line for line in screen.said))

    def test_no_subtitle_chosen_is_refused_with_the_flag_that_is_missing(self):
        metadata = [full("one00000001")]
        for preset in ({"mode": "subtitle", "yes": True}, {"mode": "subtitle", "subs": []}):
            with self.assertRaisesRegex(model.SelectionError, "--subs"):
                self.settings(metadata, preset)

    def test_the_flags_of_a_media_download_do_not_apply(self):
        metadata = [full("one00000001")]
        with self.assertRaisesRegex(model.SelectionError, "--quality"):
            self.settings(metadata, {"mode": "subtitle", "subs": ["id"], "quality": 1080})

    def test_the_flag_is_refused_when_nothing_can_be_saved(self):
        info = full("one00000001")
        info["subtitles"], info["automatic_captions"] = {}, {}
        with self.assertRaisesRegex(model.SelectionError, "--mode subtitle"):
            app.ask_settings(
                self.Screen(), [info], {}, {"_used": set(), "mode": "subtitle", "subs": ["id"]}
            )

    def test_the_summary_says_what_is_saved_and_that_it_is_not_media(self):
        metadata = captions.playlist(2)
        for info in metadata:
            info.update(synthetic())
            info.update(id="x" + str(metadata.index(info)), title="V")
        merged = app.merge_tracks(metadata)
        chosen, _ = app.subtitle_identities(merged, ["id"])
        settings = {
            "mode": "subtitle",
            "quality": "best",
            "manual": False,
            "lang": "original",
            "audio_format": "original",
            "compatible": False,
            "chosen": chosen,
        }
        # synthetic() replaced the captions, so give them back
        for number, info in enumerate(metadata):
            given = captions.video(f"vid{number:08d}")
            info["subtitles"], info["automatic_captions"] = (
                given["subtitles"],
                given["automatic_captions"],
            )
        items, rows, not_mp4, notes = app.build_items(None, metadata, merged, settings)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["choice"]["mode"], "subtitle")
        self.assertEqual(rows[0][1], "Subtitle saja")
        self.assertIn("SRT", rows[0][2])
        self.assertIn("subtitle Indonesian", rows[0][2])
        self.assertNotIn("MKV", rows[0][2])
        self.assertNotIn("MP4", rows[0][2])
        self.assertEqual(not_mp4, 0)


class Fetch(unittest.TestCase):
    """The worker, with the real subtitle code and a server of our own, and no media anywhere."""

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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.looked_up = 0
        self.media = []
        host = self.host
        test = self

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                test.looked_up += 1
                if download:
                    test.media.append(url)
                return {
                    "id": "abcdefghijk",
                    "subtitles": {
                        "id": [{"url": host + "/ok.vtt", "ext": "vtt", "name": "Indonesian"}]
                    },
                    "automatic_captions": {
                        "en": [{"url": host + "/ok.vtt", "ext": "vtt", "name": "English"}]
                    },
                    "formats": [],
                    "http_headers": {},
                }

            def urlopen(self, request):
                return urllib.request.urlopen(request.url)

            def process_ie_result(self, *args, **kwargs):
                test.media.append("process_ie_result")
                raise AssertionError("a stream was downloaded")

        patcher = patch.object(worker.yt_dlp, "YoutubeDL", YoutubeDL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tracks = [
            {
                "key": "id",
                "origin": "subtitles",
                "language": "id",
                "kind": "manual",
                "name": "Indonesian",
                "source": "",
            },
            {
                "key": "en",
                "origin": "automatic_captions",
                "language": "en",
                "kind": "automatic",
                "name": "English",
                "source": "",
            },
        ]

    def run_item(self, item=None):
        request = {
            "action": "download",
            "item": item or only_subtitles(self.tracks),
            "output": str(self.root),
            "pace": {"name": "test", "file": [0, 0], "requests": 0},
        }
        events = Events()
        with open(os.devnull, "w") as log, redirect_stderr(log):
            return worker.download(request, events), events

    def test_the_subtitles_are_saved_and_nothing_else_is_written(self):
        result, events = self.run_item()
        names = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(
            names,
            [
                "Judul_ tes _ 1 [abcdefghijk].automatic_captions-en.srt",
                "Judul_ tes _ 1 [abcdefghijk].subtitles-id.srt",
            ],
        )
        self.assertEqual({p.parent.name for p in self.root.rglob("*.srt")}, {"subtitle"})
        self.assertEqual([p.name for p in self.root.iterdir()], ["subtitle"])
        self.assertEqual((result["mode"], result["missing"]), ("subtitle", []))
        self.assertEqual(result["path"], str(self.root / "subtitle"))
        self.assertEqual(len(result["subtitles"]), 2)
        self.assertEqual(self.media, [])
        # Only the page of the video was looked up, once.
        self.assertEqual(self.looked_up, 1)
        # No media phase is announced.
        phases = {e.get("phase") for e in events.of("stage")}
        self.assertTrue(phases <= {"metadata", "subtitle", None}, phases)

    def test_it_writes_no_temporary_folder_for_the_media(self):
        before = set((BASE / ".state").glob("temporary/*")) if (BASE / ".state").exists() else set()
        self.run_item()
        after = set((BASE / ".state").glob("temporary/*")) if (BASE / ".state").exists() else set()
        self.assertEqual(before, after)

    def test_a_file_that_is_there_is_not_fetched_again(self):
        self.run_item()
        first = len(Handler.hits)
        self.assertEqual(first, 2)
        result, _ = self.run_item()
        # The page is looked up again, but no subtitle is requested.
        self.assertEqual(len(Handler.hits), first)
        self.assertEqual(len(result["subtitles"]), 2)

    def test_a_track_the_video_lacks_is_said_as_missing(self):
        lacking = {**self.tracks[0], "key": "ja", "language": "ja", "name": "Japanese"}
        result, _ = self.run_item(only_subtitles([self.tracks[0], lacking]))
        self.assertEqual([m["key"] for m in result["missing"]], ["ja"])
        self.assertEqual(result["missing"][0]["reason"], "unavailable")
        self.assertEqual(len(result["subtitles"]), 1)

    def test_the_media_of_the_same_video_is_not_touched_by_it(self):
        # A download of the video from before is left exactly as it was.
        media = self.root / "video" / "Judul_ tes _ 1 [abcdefghijk] - 1080p - video-audio - x.mkv"
        media.parent.mkdir()
        media.write_bytes(b"media")
        self.run_item()
        self.assertEqual(media.read_bytes(), b"media")
        self.assertEqual(sorted(p.name for p in (self.root / "video").iterdir()), [media.name])

    def test_a_destination_too_long_for_windows_is_refused_before_the_page_is_looked_up(self):
        with patch.object(worker, "path_too_long", return_value=270):
            with self.assertRaisesRegex(ValueError, "subtitle 270 karakter"):
                self.run_item()
        self.assertEqual(self.looked_up, 0)


class Execute(unittest.TestCase):
    """The job: what is skipped, what is run, and what is recorded."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / "state"
        (self.state / "jobs").mkdir(parents=True)
        self.root = Path(folder.name) / "media"
        self.root.mkdir()
        patcher = patch.object(store, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asked = []

    def job(self, item=None):
        return store.new_job([item or only_subtitles()], str(self.root))

    def write_subtitles(self, item):
        prefix = model.subtitle_prefix(item)
        for track in item["subtitles"]:
            path = model.subtitle_path(self.root, prefix, track)
            path.parent.mkdir(exist_ok=True)
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHalo\n", encoding="utf-8")

    def execute(self, job, saved=None):
        def run(request, callback=None):
            self.asked.append(request)
            files = saved if saved is not None else request["item"]["subtitles"]
            return {
                "path": str(self.root / "subtitle"),
                "mode": "subtitle",
                "subtitles": [{**t, "path": "/x/" + t["key"]} for t in files],
                "missing": [],
            }

        with (
            patch.object(app, "run", side_effect=run),
            patch.object(store.Archive, "get", side_effect=AssertionError("the archive")),
            patch.object(store.Archive, "put", side_effect=AssertionError("the archive")),
            patch.object(app, "native_output"),
            redirect_stderr(io.StringIO()),
        ):
            app.execute(app.UI(True), job)
        return job

    def test_the_archive_of_downloads_is_never_asked_and_never_written(self):
        job = self.job()
        self.execute(job)  # the fakes of the archive raise if they are called
        self.assertEqual(job["status"], "complete")
        self.assertEqual(len(self.asked), 1)
        self.assertNotIn("existing_file", self.asked[0])

    def test_an_item_whose_files_are_all_there_is_skipped_without_the_worker(self):
        item = only_subtitles()
        self.write_subtitles(item)
        job = self.job(item)
        self.execute(job)
        self.assertEqual(self.asked, [])
        self.assertEqual(job["status"], "complete")
        result = job["items"][0]["result"]
        self.assertEqual((result["mode"], result["missing"]), ("subtitle", []))
        self.assertEqual(len(result["subtitles"]), 1)
        self.assertTrue(Path(result["subtitles"][0]["path"]).is_file())

    def test_an_item_with_a_file_missing_is_run_again_for_the_rest(self):
        other = {**TRACK, "key": "en", "language": "en", "name": "English"}
        item = only_subtitles([TRACK, other])
        self.write_subtitles(only_subtitles([TRACK]))
        job = self.job(item)
        self.execute(job)
        self.assertEqual(len(self.asked), 1)

    def test_an_empty_file_does_not_count_as_a_subtitle(self):
        item = only_subtitles()
        self.write_subtitles(item)
        for path in (self.root / "subtitle").glob("*.srt"):
            path.write_bytes(b"")
        self.assertIsNone(app.saved_subtitles(item, self.root))

    def test_an_item_without_tracks_is_never_complete_by_itself(self):
        self.assertIsNone(app.saved_subtitles(only_subtitles([]), self.root))

    def test_the_paths_are_printed_one_per_subtitle(self):
        job = self.job()
        self.execute(job)
        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            app.emit_paths(job, True)
        self.assertEqual(output.getvalue().split(), ["/x/id"])
        # A media item still prints its one file.
        job["items"][0]["result"] = {"path": "/media/a.mkv", "mode": "video-audio"}
        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            app.emit_paths(job, True)
        self.assertEqual(output.getvalue().strip(), "/media/a.mkv")


class Show(unittest.TestCase):
    """What the card and the report say of a result that is a set of subtitle files."""

    def job(self, folder, missing=()):
        root = Path(folder) / "out"
        (root / "subtitle").mkdir(parents=True)
        files = []
        for key in ("id", "en"):
            path = root / "subtitle" / f"V [abc].subtitles-{key}.srt"
            path.write_bytes(b"x" * 100)
            files.append({"key": key, "name": key, "path": str(path)})
        return {
            "id": "abc123",
            "status": "partial" if missing else "complete",
            "output": str(root),
            "items": [
                {
                    "id": "abc",
                    "title": "V",
                    "status": "partial" if missing else "complete",
                    "choice": model.pick(synthetic(), "subtitle"),
                    "result": {
                        "path": str(root / "subtitle"),
                        "mode": "subtitle",
                        "subtitles": files,
                        "missing": list(missing),
                    },
                }
            ],
        }

    def test_the_plain_report_counts_the_files(self):
        with tempfile.TemporaryDirectory() as folder:
            screen = ui.UI(True)
            with redirect_stderr(io.StringIO()) as captured:
                screen.report(self.job(folder), Path(folder) / "download.log")
            self.assertIn("[+] V (2 subtitle · 200 B)", captured.getvalue().splitlines())

    def test_the_card_names_the_folder_and_not_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            screen, buffer = rich_ui(80)
            screen.report(self.job(folder), Path(folder) / "download.log")
            text = " ".join(buffer.getvalue().split())
            self.assertIn("SRT · 2 subtitle · 200 B", text)
            self.assertIn("subtitle/", text)
            self.assertNotIn("file tidak ditemukan", text)

    def test_a_partial_result_says_what_is_missing(self):
        lost = [{"key": "ja", "name": "Japanese", "reason": "unavailable"}]
        with tempfile.TemporaryDirectory() as folder:
            screen = ui.UI(True)
            with redirect_stderr(io.StringIO()) as captured:
                screen.report(self.job(folder, lost), Path(folder) / "download.log")
            self.assertIn(
                "[!] V (2 subtitle · 200 B · subtitle belum lengkap: Japanese (tidak tersedia))",
                captured.getvalue().splitlines(),
            )

    def test_the_job_file_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            job = self.job(folder)
            self.assertEqual(json.loads(json.dumps(job)), job)


if __name__ == "__main__":
    unittest.main()
