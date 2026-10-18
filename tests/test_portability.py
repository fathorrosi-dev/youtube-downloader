"""Native process containment, event protocol, migration and installation tests."""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import ExitStack, redirect_stdout, closing
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import compat
import maintenance
import runner
import media_migration
import store
import model
from fsutil import cleanup_directory
from terminal import Terminal
from test_tools import synthetic


class Portable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="YT 日本語 ")
        self.addCleanup(cleanup_directory, self.tmp)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / ".state"
        self.state.mkdir()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (store, runner):
            stack.enter_context(patch.object(module, "STATE", self.state))
        stack.enter_context(patch.object(runner, "BASE", self.root))

    def worker(self, body):
        (self.root / "worker.py").write_text(
            "import sys,os,json,time,subprocess\nsys.path.insert(0,"
            + repr(str(BASE))
            + ")\nfrom compat import worker_start\nworker_start()\n"
            + body,
            encoding="utf-8",
        )

    def test_corrupt_protocol_and_missing_final(self):
        for body in (
            'print("broken",flush=True)',
            'print("[]",flush=True)',
            'print(json.dumps({"type":"unexpected"}),flush=True)',
            'print(json.dumps({"type":"progress","downloaded":"bad"}),flush=True)',
            'sys.stdout.write("{\\"type\\":\\"done\\"}");sys.stdout.flush()',
            'print(json.dumps({"type":"start"}),flush=True)',
            'print(json.dumps({"type":"done"}),flush=True);print(json.dumps({"type":"done"}),flush=True)',
        ):
            with self.subTest(body=body):
                self.worker(body)
                with self.assertRaises(runner.WorkerError):
                    runner.run({})
        self.assertFalse(list(self.state.glob("worker-*.json")))

    def test_maximum_event_size(self):
        # One byte over the limit, as before. Writing far more would leave the worker blocked on a
        # full pipe once the reader stops, so it would have to be killed instead of exiting.
        self.worker('sys.stdout.write("x"*(%d));sys.stdout.flush()' % (runner.MAX_EVENT + 1))
        with self.assertRaisesRegex(runner.WorkerError, "besar"):
            runner.run({})

    def test_worker_waits_for_start(self):
        self.worker('(Path if False else open)(sys.argv[1]+".started","w").close()')
        p = subprocess.Popen(
            [sys.executable, str(self.root / "worker.py"), str(self.root / "request")],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.2)
            self.assertFalse((self.root / "request.started").exists())
            p.stdin.write(b"START\n")
            p.stdin.close()
            p.wait(timeout=5)
            self.assertTrue((self.root / "request.started").exists())
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_cross_process_lock_and_release(self):
        code = 'import sys,time;sys.path.insert(0,sys.argv[1]);from compat import file_lock\nwith file_lock(sys.argv[2]):print("ready",flush=True);sys.stdin.readline()'
        p = subprocess.Popen(
            [sys.executable, "-c", code, str(BASE), str(self.state / "run.lock")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            self.assertEqual(p.stdout.readline().strip(), b"ready")
            with self.assertRaises(ValueError):
                with compat.file_lock(self.state / "run.lock"):
                    pass
            p.stdin.write(b"END\n")
            p.stdin.close()
            p.wait(timeout=5)
            with compat.file_lock(self.state / "run.lock"):
                pass
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()

    def alive(self, pid):
        if os.name == "nt":
            import ctypes

            k = ctypes.WinDLL("kernel32")
            k.OpenProcess.restype = ctypes.c_void_p
            k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            h = k.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            value = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(value))
            k.CloseHandle(h)
            return value.value == 259
        try:
            os.kill(pid, 0)
            if sys.platform.startswith("linux") and ") Z" in Path(f"/proc/{pid}/stat").read_text():
                return False
            r = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
        except (ProcessLookupError, FileNotFoundError):
            return False

    def test_crash_and_interrupt_kill_ffmpeg_descendant(self):
        # Real FFmpeg processes emulate an in-flight download/merge child.
        for crash in (False, True):
            self.worker(
                'p=subprocess.Popen(["ffmpeg","-v","error","-re","-f","lavfi","-i","testsrc2=size=32x32:rate=1","-f","null","-"],stdout=sys.stderr,stderr=sys.stderr)\nopen(os.path.join(os.path.dirname(sys.argv[1]),"child.pid"),"w").write(str(p.pid))\n'
                + (
                    "os._exit(7)"
                    if crash
                    else 'print(json.dumps({"type":"stage","message":"merge"}),flush=True)\ntime.sleep(60)'
                )
            )

            def interrupt(event):
                raise KeyboardInterrupt()

            with self.assertRaises(runner.WorkerError if crash else KeyboardInterrupt):
                runner.run({}, interrupt)
            pid = int((self.state / "child.pid").read_text())
            deadline = time.monotonic() + 5
            while self.alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(self.alive(pid), "FFmpeg left running")

    def test_cli_interrupt_during_download_and_merge(self):
        for stage in ("Mengunduh video", "Menggabungkan video + audio"):
            self.worker(
                'p=subprocess.Popen(["ffmpeg","-v","error","-re","-f","lavfi","-i","testsrc2=size=32x32:rate=1","-f","null","-"],stdout=sys.stderr,stderr=sys.stderr)\nopen(os.path.join(os.path.dirname(sys.argv[1]),"child.pid"),"w").write(str(p.pid))\nprint(json.dumps({"type":"stage","message":'
                + repr(stage)
                + "}),flush=True)\ntime.sleep(60)"
            )
            item = {
                "id": "x",
                "title": "Fixture",
                "url": "fixture:x",
                "choice": model.pick(synthetic()),
                "subtitles": [],
                "status": "pending",
            }
            script = (
                "import sys;sys.path.insert(0,"
                + repr(str(BASE))
                + ");import runner,store,app;from pathlib import Path;from ui import UI\nroot=Path("
                + repr(str(self.root))
                + ');runner.BASE=root;runner.STATE=store.STATE=app.STATE=root/".state"\njob=store.new_job('
                + repr([item])
                + ',str(root/"media"))\ntry:app.execute(UI(True),job)\nexcept KeyboardInterrupt:sys.exit(130)'
            )
            with Terminal([sys.executable, "-c", script], BASE) as terminal:
                data = ""
                deadline = time.monotonic() + 15
                sent = False
                while terminal.running() and time.monotonic() < deadline:
                    data += terminal.read(0.1)
                    if stage in data and not sent:
                        terminal.interrupt()
                        sent = True
                data += terminal.drain()
                self.assertTrue(sent, data)
                self.assertEqual(terminal.wait(), 130, data)
            latest = max((self.state / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
            self.assertEqual(
                json.loads(latest.read_text(encoding="utf-8"))["status"], "interrupted"
            )
            pid = int((self.state / "child.pid").read_text())
            deadline = time.monotonic() + 5
            while self.alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(self.alive(pid))

    def test_filename_unicode_and_windows_reserved_characters(self):
        item = {
            "title": "CON<>:/\\|?* 日本語😀" + ("a" * 400),
            "id": "x",
            "choice": {"mode": "audio", "actual_quality": None, "audio_format": "original"},
            "subtitles": [],
        }
        name = model.basename(item)
        self.assertLess(len(name.encode("utf-8")), 220)
        for c in "<>:/\\|?*":
            self.assertNotIn(c, name)
        self.assertIn("日本語", name)

    def prepare_rebase(self, old):
        self.new = self.root / "new media 日本語"
        (self.new / "video").mkdir(parents=True)
        (self.new / "video/a.mp4").write_bytes(b"media")
        source = str(media_migration.source_path(old) / "video/a.mp4")
        job = {
            "id": "abc",
            "output": old,
            "browser": "firefox:old-profile",
            "items": [{"result": {"path": source}}],
        }
        store.write_json(self.state / "jobs/abc.json", job)
        store.write_json(self.root / "settings.json", {"output": old, "browser": "firefox:old"})
        db = sqlite3.connect(self.state / "downloaded.sqlite3")
        db.execute(
            "CREATE TABLE results (variant TEXT,path TEXT,payload TEXT,PRIMARY KEY(variant,path))"
        )
        db.execute(
            "INSERT INTO results VALUES (?,?,?)",
            ("v", source, json.dumps({"path": source, "mode": "video", "subtitles": []})),
        )
        db.commit()
        db.close()

    def test_foreign_rebase_dry_run_idempotence_and_browser_reset(self):
        old = r"C:\Users\old\Videos\YouTube" if os.name != "nt" else "/home/old/Videos/YouTube"
        self.prepare_rebase(old)
        before = (self.state / "jobs/abc.json").read_bytes()
        with redirect_stdout(io.StringIO()):
            preview = media_migration.rebase_media(self.root, old, str(self.new), True)
            self.assertGreater(preview["changed"], 0)
            self.assertEqual(before, (self.state / "jobs/abc.json").read_bytes())
            media_migration.rebase_media(self.root, old, str(self.new))
            again = media_migration.rebase_media(self.root, old, str(self.new), True)
        self.assertEqual(again["changed"], 0)
        job = store.load_job("abc")
        self.assertEqual(job["output"], str(self.new))
        self.assertIsNone(job["browser"])
        self.assertTrue(list((self.state / "media-path-backups").iterdir()))
        with closing(sqlite3.connect(self.state / "downloaded.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT path FROM results").fetchone()[0], str(self.new / "video/a.mp4")
            )

    def test_rebase_archive_collision_is_rejected_without_changes(self):
        old = "/old/YouTube"
        self.prepare_rebase(old)
        with closing(sqlite3.connect(self.state / "downloaded.sqlite3")) as db:
            target = str(self.new / "video/a.mp4")
            db.execute(
                "INSERT INTO results VALUES (?,?,?)",
                ("v", target, json.dumps({"path": target, "mode": "video", "subtitles": []})),
            )
            db.commit()
        before = (self.state / "downloaded.sqlite3").read_bytes()
        with self.assertRaisesRegex(ValueError, "Benturan"):
            media_migration.rebase_media(self.root, old, str(self.new), True)
        self.assertEqual(before, (self.state / "downloaded.sqlite3").read_bytes())

    def test_rebase_archive_is_skipped_and_deleted_file_recovers(self):
        old = "/old/YouTube"
        self.prepare_rebase(old)
        with redirect_stdout(io.StringIO()):
            media_migration.rebase_media(self.root, old, str(self.new))
        archive = store.Archive()
        self.addCleanup(archive.close)
        with patch.object(store, "probe"):
            self.assertIsNotNone(archive.get("v", "video", self.new))
            (self.new / "video/a.mp4").unlink()
            self.assertIsNone(archive.get("v", "video", self.new))

    def test_rebase_missing_file_preserves_records(self):
        old = "/old/YouTube"
        self.prepare_rebase(old)
        (self.new / "video/a.mp4").unlink()
        before = (self.state / "downloaded.sqlite3").read_bytes()
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "belum lengkap"):
            media_migration.rebase_media(self.root, old, str(self.new))
        self.assertEqual(before, (self.state / "downloaded.sqlite3").read_bytes())

    def test_rebase_failure_restores_job_and_database(self):
        old = "/old/YouTube"
        self.prepare_rebase(old)
        job = (self.state / "jobs/abc.json").read_bytes()
        db = (self.state / "downloaded.sqlite3").read_bytes()
        with (
            patch.object(store, "write_json", side_effect=OSError("disk failure")),
            redirect_stdout(io.StringIO()),
            self.assertRaises(OSError),
        ):
            media_migration.rebase_media(self.root, old, str(self.new))
        self.assertEqual(job, (self.state / "jobs/abc.json").read_bytes())
        self.assertEqual(db, (self.state / "downloaded.sqlite3").read_bytes())

    def test_foreign_output_refused_and_linux_rollback_restricted(self):
        foreign = r"C:\Users\old\Videos" if os.name != "nt" else "/home/old/Videos"
        with self.assertRaisesRegex(ValueError, "rebase-media"):
            compat.native_output(foreign)
        backup = self.root / "backup"
        backup.mkdir()
        (backup / "manifest.json").write_text(json.dumps({"files": [], "added": []}))
        with (
            patch.object(maintenance, "platform_key", return_value="windows-x86_64"),
            self.assertRaisesRegex(ValueError, "ditolak"),
        ):
            maintenance.restore(self.root, backup)


class NativeInstaller(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("YT_TEST_INSTALL") == "1", "CI enables clean dependency installation"
    )
    def test_clean_install_unicode_update_migrate_rollback(self):
        with tempfile.TemporaryDirectory(prefix="YT setup 日本語 ") as folder:
            root = Path(folder) / "fresh tool"
            root.mkdir()
            for name in maintenance.program_files(BASE):
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BASE / name, dest)
            media = Path(folder) / "media"
            media.mkdir()
            (media / "sentinel.mp4").write_bytes(b"unchanged media")
            (root / ".state/jobs").mkdir(parents=True)
            (root / ".state/jobs/sentinel.json").write_bytes(b'{"id":"sentinel","items":[]}')
            (root / "settings.json").write_text(
                json.dumps({"output": str(media), "browser": None}), encoding="utf-8"
            )
            (root / ".state/downloaded.sqlite3").write_bytes(b"unchanged archive")
            saved = {
                n: (root / n).read_bytes()
                for n in ("settings.json", ".state/jobs/sentinel.json", ".state/downloaded.sqlite3")
            }
            # Installer must choose its own verified OS asset, never reuse another OS runtime.
            for args in (["setup"], ["setup", "--check"]):
                subprocess.run(
                    [sys.executable, str(root / "maintenance.py"), *args], check=True, timeout=300
                )
            launcher = root / (
                "youtube-downloader.cmd" if os.name == "nt" else "youtube-downloader"
            )
            launcher_args = (
                ["cmd.exe", "/d", "/c", "call", str(launcher)]
                if os.name == "nt"
                else [str(launcher)]
            )
            result = subprocess.run(
                [*launcher_args, "preview", "--plain"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Preview selesai", result.stderr)
            result = subprocess.run(
                [*launcher_args, "resume"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            py = compat.python_path(root)
            subprocess.run(
                [str(py), str(root / "app.py"), "preview", "--plain"], check=True, timeout=15
            )
            moved = Path(folder) / "moved tools"
            subprocess.run(
                [sys.executable, str(root / "maintenance.py"), "migrate", str(moved)],
                check=True,
                timeout=300,
            )
            subprocess.run(
                [sys.executable, str(root / "maintenance.py"), "update", str(moved)],
                check=True,
                timeout=300,
            )
            subprocess.run(
                [sys.executable, str(moved / "maintenance.py"), "rollback"], check=True, timeout=300
            )
            subprocess.run(
                [sys.executable, str(moved / "maintenance.py"), "setup", "--check"],
                check=True,
                timeout=60,
            )
            for name, data in saved.items():
                self.assertEqual((moved / name).read_bytes(), data)
            self.assertEqual((media / "sentinel.mp4").read_bytes(), b"unchanged media")
