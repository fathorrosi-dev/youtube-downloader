"""Supply chain, log growth, buffer bounds and the contents of the release package."""

import io
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import compat
import fsutil
import release_bundle
import runner
import self_update


def pin_name(spec):
    return re.match(r"[A-Za-z0-9_.-]+", spec)[0].lower().replace("_", "-")


class LockFile(unittest.TestCase):
    def test_parser_joins_continuations_and_drops_hashes_and_comments(self):
        text = (
            "# header\n"
            "brotli==1.2.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
            'websockets==17.2 ; python_version >= "3.11" \\\n    --hash=sha256:cc\n'
            "plain==1.0  # trailing comment\n"
        )
        self.assertEqual(
            compat.lock_requirements(text),
            ["brotli==1.2.0", 'websockets==17.2 ; python_version >= "3.11"', "plain==1.0"],
        )

    def test_every_pin_carries_hashes_and_covers_the_direct_requirements(self):
        text = (BASE / "requirements.lock").read_text(encoding="utf-8")
        pins = compat.lock_requirements(text)
        entries = re.split(r"\n(?=\S)", text)
        for pin in pins:
            entry = next(x for x in entries if x.startswith(pin.split(" ;")[0].split("==")[0]))
            self.assertRegex(entry, r"--hash=sha256:[0-9a-f]{64}", pin)
        locked = {pin_name(p): p.split("==")[1].split(";")[0].strip() for p in pins}
        for line in (BASE / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if "==" in line:
                name, version = line.split("==")
                self.assertEqual(locked[pin_name(re.sub(r"\[.*\]", "", name))], version, line)
        self.assertIn("packaging", locked)

    def test_no_code_depends_on_pips_private_vendored_packages(self):
        for path in list(BASE.glob("*.py")) + list((BASE / "tests").glob("*.py")):
            if path.name != Path(__file__).name:
                self.assertNotIn("pip._vendor", path.read_text(encoding="utf-8"), path.name)


class LogsAndBuffers(unittest.TestCase):
    def test_log_rotation_keeps_one_previous_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "download.log"
            compat.rotate_log(log)  # missing file: nothing to do
            log.write_bytes(b"a" * 10)
            compat.rotate_log(log, limit=100)
            self.assertEqual(log.read_bytes(), b"a" * 10)
            log.write_bytes(b"b" * 101)
            compat.rotate_log(log, limit=100)
            self.assertFalse(log.exists())
            self.assertEqual((Path(folder) / "download.log.1").read_bytes(), b"b" * 101)
            log.write_bytes(b"c" * 101)
            compat.rotate_log(log, limit=100)
            self.assertEqual((Path(folder) / "download.log.1").read_bytes(), b"c" * 101)

    def test_oversized_event_is_rejected(self):
        class Boundary(io.BytesIO):
            pass

        messages = runner.queue.Queue()
        cancel = runner.threading.Event()
        old = runner.MAX_EVENT
        runner.MAX_EVENT = 100
        try:
            runner.read_events(
                Boundary(b'{"type":"notice","message":"' + b"x" * 200 + b'"}\n'), messages, cancel
            )
        finally:
            runner.MAX_EVENT = old
        first = messages.get_nowait()
        self.assertIsInstance(first, runner.WorkerError)
        self.assertIn("terlalu besar", str(first))
        self.assertIsNone(messages.get_nowait())

    def test_stage_phase_is_passed_on_and_must_be_text(self):
        def events(line):
            messages = runner.queue.Queue()
            runner.read_events(io.BytesIO(line), messages, runner.threading.Event())
            found = []
            while (item := messages.get_nowait()) is not None:
                found.append(item)
            return found

        ok = events(b'{"type":"stage","message":"Mengunduh video","phase":"video"}\n')
        self.assertEqual([e["phase"] for e in ok], ["video"])
        for bad in (b"5", b"null", b"[]", b'{"a":1}'):
            (error,) = events(b'{"type":"stage","phase":' + bad + b"}\n")
            self.assertIsInstance(error, runner.WorkerError)
            self.assertIn("tidak valid", str(error))

    def test_a_large_realistic_playlist_still_fits_and_memory_is_bounded(self):
        entries = [
            {
                "id": f"id{i:08d}",
                "url": f"https://www.youtube.com/watch?v=id{i:08d}",
                "title": "日本語" * 33,
            }
            for i in range(5000)
        ]
        line = json.dumps(
            {"type": "done", "result": {"playlist": True, "entries": entries}}, ensure_ascii=True
        )
        self.assertLess(len(line) + 1, runner.MAX_EVENT)
        self.assertLessEqual(runner.MAX_EVENT * runner.MAX_QUEUE, 128 * 1024**2)


class TemporaryFolders(unittest.TestCase):
    def test_remove_tree_deletes_nested_content_and_ignores_a_missing_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "YT 日本語" / ".state"
            target.mkdir(parents=True)
            (target / "download.log").write_text("log", encoding="utf-8")
            fsutil.remove_tree(Path(folder) / "YT 日本語")
            self.assertFalse((Path(folder) / "YT 日本語").exists())
            fsutil.remove_tree(Path(folder) / "YT 日本語")

    def test_cleanup_directory_leaves_nothing_for_tempfile_to_remove(self):
        directory = tempfile.TemporaryDirectory()
        (Path(directory.name) / "x").write_text("x", encoding="utf-8")
        fsutil.cleanup_directory(directory)
        self.assertFalse(Path(directory.name).exists())


class ReleasePackage(unittest.TestCase):
    def test_status_documents_stay_out_of_the_package_and_it_still_installs(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, _ = release_bundle.build(BASE, Path(folder) / "dist")
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
            leaked = [
                n
                for n in names
                if re.search(r"(INSTALL-STATUS|TEST-REPORT|PREVIEW)", n)
                or "/docs/" in n
                or "/scripts/" in n
            ]
            self.assertEqual(leaked, [])
            for expected in ("README.md", "PANDUAN.md", "MIGRASI.md", "requirements.lock"):
                self.assertIn("youtube-downloader/" + expected, names)
            version = json.loads((BASE / "release.json").read_text(encoding="utf-8"))["version"]
            root = self_update.safe_unpack(archive, Path(folder) / "unpacked", version)
            self.assertTrue((root / "app.py").is_file())


if __name__ == "__main__":
    unittest.main()
