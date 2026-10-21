"""The CI helpers and the rules that keep the workflows, the support policy and the documents in
step. The helpers live in scripts/, which is in the repository and not in the release package."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import model

SCRIPTS = BASE / "scripts"
WORKFLOWS = BASE / ".github/workflows"


def text(path):
    return path.read_text(encoding="utf-8")


@unittest.skipUnless((SCRIPTS / "ci_summary.py").is_file(), "scripts/ is not part of the package")
class Summary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        import ci_summary

        cls.ci = ci_summary

    def test_failing_tests_are_found_once_each(self):
        log = [
            "test_a (tests.test_x.A.test_a) ... ok\n",
            "FAIL: test_b (tests.test_x.A.test_b)\n",
            "ERROR: test_c (tests.test_y.B.test_c)\n",
            "FAIL: test_b (tests.test_x.A.test_b)\n",  # repeated in the final summary
            "FAILED (failures=1, errors=1)\n",
        ]
        self.assertEqual(
            self.ci.failures(log),
            [
                ("FAIL", "test_b (tests.test_x.A.test_b)"),
                ("ERROR", "test_c (tests.test_y.B.test_c)"),
            ],
        )
        self.assertEqual(self.ci.failures(["OK\n"]), [])

    def test_an_annotation_is_one_line_with_the_workflow_command_escapes(self):
        line = self.ci.annotation("FAIL", "test_x (m.C.test_x)\nsecond 100%", "Tests linux")
        self.assertTrue(line.startswith("::error title=Tests linux::FAIL: test_x"))
        self.assertNotIn("\n", line)
        self.assertIn("%0A", line)
        self.assertIn("100%25", line)

    def test_the_summary_names_the_tests_and_keeps_the_tail_of_the_output(self):
        lines = [f"line {n}\n" for n in range(200)]
        report = self.ci.summary("Tests linux", 1, [("FAIL", "test_b (m.A.test_b)")], lines)
        self.assertIn("1 failing test(s)", report)
        self.assertIn("- `FAIL` test_b (m.A.test_b)", report)
        self.assertIn("line 199", report)
        self.assertNotIn("line 100", report)
        self.assertIn("All tests passed", self.ci.summary("Tests linux", 0, [], lines))

    def test_result_lines_of_every_kind_name_their_test(self):
        for tail in ("ok", "FAIL", "ERROR", "skipped 'needs a console'", "expected failure"):
            line = f"test_a (tests.test_x.A.test_a) ... {tail}\n"
            self.assertEqual(self.ci.result_name(line), "test_a (tests.test_x.A.test_a)", tail)
        for other in ("FAIL: test_a (t.A.test_a)\n", "Ran 3 tests in 1.0s\n", "OK\n", "\n"):
            self.assertIsNone(self.ci.result_name(other), other)

    def test_a_test_lasts_from_the_result_line_before_it_to_its_own(self):
        stamped = [("test_a", 10.0), ("test_b", 10.5), ("test_c", 14.0), ("test_d", 14.25)]
        # The first test has no line before it, so it has no time.
        self.assertEqual(
            self.ci.durations(stamped), [("test_b", 0.5), ("test_c", 3.5), ("test_d", 0.25)]
        )
        self.assertEqual(self.ci.durations([("test_a", 1.0)]), [])
        self.assertEqual(self.ci.durations([]), [])

    def test_the_ten_slowest_are_listed_longest_first(self):
        timed = [(f"test_{n}", float(n % 7) + n / 100) for n in range(30)]
        found = self.ci.slowest(timed)
        self.assertEqual(len(found), 10)
        self.assertEqual([s for _, s in found], sorted((s for _, s in found), reverse=True))
        self.assertEqual(found[0][1], max(s for _, s in timed))
        self.assertEqual(self.ci.slowest([("only", 1.0)]), [("only", 1.0)])
        lines = self.ci.slow_lines([("test_slow (m.A.test_slow)", 12.8), ("test_fast", 0.5)])
        self.assertEqual(lines[0], "  12.80 s  test_slow (m.A.test_slow)")
        self.assertEqual(lines[1], "   0.50 s  test_fast")

    def test_the_summary_lists_the_slowest_tests_whether_the_run_passed_or_not(self):
        timed = [("test_slow (m.A.test_slow)", 12.8), ("test_fast (m.A.test_fast)", 0.5)]
        passed = self.ci.summary("Tests linux", 0, [], [], timed)
        self.assertIn("All tests passed", passed)
        self.assertIn("Slowest 2 tests", passed)
        self.assertLess(passed.index("test_slow"), passed.index("test_fast"))
        failed = self.ci.summary(
            "Tests linux", 1, [("FAIL", "test_b (m.A.test_b)")], ["x\n"], timed
        )
        self.assertIn("1 failing test(s)", failed)
        self.assertIn("12.80 s  test_slow", failed)
        self.assertNotIn("Slowest", self.ci.summary("Tests linux", 0, [], []))  # nothing timed

    def run_script(self, code, **env):
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "ci_summary.py"),
                    "--title",
                    "demo",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary), **env},
            )
            return result, summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_a_failing_run_keeps_its_exit_code_and_annotates_each_failure(self):
        code = "import sys; print('FAIL: test_x (t.A.test_x)'); print('ERROR: test_y (t.B.test_y)'); sys.exit(1)"
        result, summary = self.run_script(code)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL: test_x (t.A.test_x)", result.stdout)  # the output is still shown
        self.assertIn("::error title=demo::FAIL: test_x (t.A.test_x)", result.stdout)
        self.assertIn("::error title=demo::ERROR: test_y (t.B.test_y)", result.stdout)
        self.assertIn("2 failing test(s)", summary)

    def test_a_crash_without_test_names_still_says_the_exit_code(self):
        result, summary = self.run_script("import sys; sys.exit(3)")
        self.assertEqual(result.returncode, 3)
        self.assertIn("::error title=demo::the command exited with code 3", result.stdout)
        self.assertIn("Exit code 3", summary)

    def test_a_run_names_its_slowest_test_in_the_log_and_in_the_summary(self):
        code = (
            "import time\n"
            "for name, wait in (('a', 0), ('b', 0), ('slow', 0.4), ('c', 0)):\n"
            "    time.sleep(wait)\n"
            "    print(f'test_{name} (t.A.test_{name}) ... ok', flush=True)\n"
        )
        result, summary = self.run_script(code)
        self.assertEqual(result.returncode, 0)
        for shown in (result.stdout, summary):
            listed = shown[shown.index("Slowest 3 tests") :]
            self.assertLess(listed.index("test_slow"), listed.index("test_c"), shown)
            self.assertIn("test_b", listed)
            self.assertNotIn("test_a (", listed)  # the first test has no time
        self.assertNotIn("::error", result.stdout)

    def test_a_passing_run_is_quiet_and_exits_zero(self):
        result, summary = self.run_script("print('ok')")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("::error", result.stdout)
        self.assertIn("All tests passed", summary)

    def test_it_works_without_a_summary_file(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            out = io.StringIO()
            with redirect_stdout(out):
                code = self.ci.main(["--", sys.executable, "-c", "print('x')"])
        self.assertEqual(code, 0)


@unittest.skipUnless((SCRIPTS / "pin_status.py").is_file(), "scripts/ is not part of the package")
class PinStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        import pin_status

        cls.pins = pin_status

    def test_a_new_release_is_compared_numerically(self):
        newer = self.pins.newer
        self.assertTrue(newer("2026.10.5", "2026.8.19"))
        self.assertTrue(newer("2026.8.20", "2026.8.19"))
        self.assertFalse(newer("2026.8.19", "2026.8.19"))
        self.assertFalse(newer("2026.8.2", "2026.8.19"))  # 2 is not later than 19
        self.assertFalse(newer("weird", "2026.8.19"))

    def test_the_pin_is_old_only_when_a_newer_release_exists(self):
        today = date(2026, 10, 17)
        old = "2026.8.19"  # 59 days
        code, message = self.pins.check(old, old, today)
        self.assertEqual(code, 0)
        self.assertIn("newest release", message)
        code, message = self.pins.check(old, "2026.10.1", today)
        self.assertEqual(code, 1)
        self.assertIn("59 days", message)
        self.assertIn("2026.10.1", message)
        # A newer release exists but the pin is young: not worth a warning yet.
        code, message = self.pins.check("2026.10.1", "2026.10.15", today)
        self.assertEqual(code, 0)
        self.assertIn("within", message)
        self.assertEqual(self.pins.check("2026.8.19", "2026.8.19", None)[0], 0)

    def test_the_age_limit_is_the_one_the_tool_uses(self):
        today = date(2026, 10, 17)
        at_limit = date.fromordinal(today.toordinal() - model.PIN_WARN_DAYS)
        pin = f"{at_limit.year}.{at_limit.month}.{at_limit.day}"
        self.assertEqual(self.pins.check(pin, "2099.1.1", today)[0], 0)  # not older than the limit
        past = date.fromordinal(today.toordinal() - model.PIN_WARN_DAYS - 1)
        pin = f"{past.year}.{past.month}.{past.day}"
        self.assertEqual(self.pins.check(pin, "2099.1.1", today)[0], 1)

    def test_the_ejs_version_comes_from_the_default_extra(self):
        requires = [
            "brotli; implementation_name == 'cpython' and extra == 'default'",
            "yt-dlp-ejs==0.9.1; extra == 'default'",
            "pytest>=8; extra == 'dev'",
        ]
        self.assertEqual(self.pins.ejs_requirement(requires), "0.9.1")
        self.assertEqual(self.pins.ejs_requirement(['yt-dlp-ejs==1.0; extra == "default"']), "1.0")
        self.assertIsNone(self.pins.ejs_requirement(["yt-dlp-ejs==1.0; extra == 'dev'"]))
        self.assertIsNone(self.pins.ejs_requirement(None))

    def test_bumping_changes_only_the_two_pins_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as folder:
            requirements = Path(folder) / "requirements.txt"
            lock = Path(folder) / "requirements.lock"
            requirements.write_text(text(BASE / "requirements.txt"), encoding="utf-8")
            lock.write_text(text(BASE / "requirements.lock"), encoding="utf-8")
            with (
                patch.object(self.pins, "REQUIREMENTS", requirements),
                patch.object(self.pins, "LOCK", lock),
            ):
                new_text, new_lock = self.pins.bump("2026.10.1", "0.9.0")
        before, after = text(BASE / "requirements.txt").splitlines(), new_text.splitlines()
        self.assertEqual(
            [(a, b) for a, b in zip(before, after, strict=True) if a != b],
            [("yt-dlp[default]==2026.8.19", "yt-dlp[default]==2026.10.1")],
        )
        old_lock = text(BASE / "requirements.lock").splitlines()
        changed = [(a, b) for a, b in zip(old_lock, new_lock.splitlines(), strict=True) if a != b]
        self.assertEqual(
            [b.split(" ")[0] for _, b in changed], ["yt-dlp==2026.10.1", "yt-dlp-ejs==0.9.0"]
        )

    def test_the_lock_must_have_the_line_to_change(self):
        with tempfile.TemporaryDirectory() as folder:
            requirements = Path(folder) / "requirements.txt"
            lock = Path(folder) / "requirements.lock"
            requirements.write_text("yt-dlp[default]==2026.8.19\n", encoding="utf-8")
            lock.write_text(
                "rich==14.3.4 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
            )
            with (
                patch.object(self.pins, "REQUIREMENTS", requirements),
                patch.object(self.pins, "LOCK", lock),
                self.assertRaises(SystemExit),
            ):
                self.pins.bump("2026.10.1", None)

    def test_the_current_pin_is_read_from_requirements(self):
        self.assertEqual(
            self.pins.current_pin(text(BASE / "requirements.txt")),
            re.search(r"yt-dlp\[default\]==([0-9.]+)", text(BASE / "requirements.txt"))[1],
        )
        with self.assertRaises(SystemExit):
            self.pins.current_pin("rich==14.3.4\n")


@unittest.skipUnless((SCRIPTS / "ci_ffmpeg.py").is_file(), "scripts/ is not part of the package")
class FfmpegForWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        import ci_ffmpeg

        cls.ff = ci_ffmpeg

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        # main() puts Chocolatey's folder first on PATH before it looks. On a Windows runner that
        # folder already holds the tools the "FFmpeg Windows" step installed, so a test that meant
        # to look only at its own folder found those instead. Every test here gets an empty one.
        chocolatey = patch.object(self.ff, "CHOCO_BIN", str(self.root / "chocolatey-bin"))
        chocolatey.start()
        self.addCleanup(chocolatey.stop)

    def handed_on(self):
        """The folders that main() wrote to $GITHUB_PATH, as paths."""
        lines = (self.root / "gh").read_text(encoding="utf-8").splitlines()
        return [Path(line) for line in lines]

    def program(self, folder, name):
        """An executable stand-in that shutil.which finds on Windows and elsewhere."""
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / (name + (".exe" if os.name == "nt" else ""))
        file.write_text("")
        file.chmod(0o755)
        return file

    def tools_in(self, folder):
        for tool in self.ff.TOOLS:
            self.program(folder, tool)

    def release(self, name="release.zip", members=("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")):
        """A zip laid out like gyan.dev's essentials build, and the SHA-256 of that file."""
        archive = self.root / name
        with zipfile.ZipFile(archive, "w") as bundle:
            for member in members:
                bundle.writestr(f"ffmpeg-9.0.2-essentials_build/bin/{member}", "binary")
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def quiet(self, function, *args, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            result = function(*args, **kwargs)
        return result, out.getvalue()

    # -- Chocolatey ---------------------------------------------------------------------------

    def test_an_exit_code_of_zero_is_not_taken_for_an_installation(self):
        # The 503 case: choco printed "installed 0/0 packages" and exited with 0, every time.
        bin_folder = self.root / "choco-bin"
        self.program(bin_folder, "choco")
        shims = self.root / "shims"
        calls, sleeps = [], []

        def run(command, check):
            calls.append(command[1:])
            if len(calls) == 3:  # the feed answers on the third attempt
                self.tools_in(shims)
            return SimpleNamespace(returncode=0)

        path = os.pathsep.join([str(bin_folder), str(shims)])
        installed, _ = self.quiet(self.ff.from_chocolatey, path, run=run, sleep=sleeps.append)
        self.assertTrue(installed)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ["install", "ffmpeg", "--yes", "--no-progress"])
        wait = self.ff.RETRY_SECONDS
        self.assertEqual(sleeps, [wait, wait * 2])  # waits longer each time, not after success

    def test_chocolatey_gives_up_after_its_attempts(self):
        bin_folder = self.root / "choco-bin"
        self.program(bin_folder, "choco")
        calls, sleeps = [], []

        def run(command, check):
            calls.append(command)
            return SimpleNamespace(returncode=0)

        installed, shown = self.quiet(
            self.ff.from_chocolatey, str(bin_folder), run=run, sleep=sleeps.append
        )
        self.assertFalse(installed)
        self.assertEqual(len(calls), self.ff.ATTEMPTS)
        self.assertEqual(len(sleeps), self.ff.ATTEMPTS - 1)
        self.assertIn("still missing", shown)

    def test_without_choco_nothing_is_started(self):
        calls = []
        installed, shown = self.quiet(
            self.ff.from_chocolatey, str(self.root), run=lambda *a, **k: calls.append(a)
        )
        self.assertFalse(installed)
        self.assertEqual(calls, [])
        self.assertIn("choco is not on PATH", shown)

    # -- the pinned download ------------------------------------------------------------------

    def test_the_pinned_release_is_written_down_completely(self):
        self.assertRegex(self.ff.SHA256, r"^[0-9a-f]{64}$")
        self.assertIn(self.ff.VERSION, self.ff.ARCHIVE)
        for url in self.ff.URLS:
            self.assertTrue(url.startswith("https://"), url)
            self.assertTrue(url.endswith("/" + self.ff.ARCHIVE), url)

    def test_a_download_with_the_expected_hash_is_unpacked(self):
        archive, digest = self.release()
        folder, _ = self.quiet(
            self.ff.from_download,
            self.root / "target",
            urls=[archive.as_uri()],
            expected=digest,
        )
        self.assertEqual(folder.name, "bin")
        self.assertTrue((folder / "ffmpeg.exe").is_file())
        self.assertTrue((folder / "ffprobe.exe").is_file())

    def test_a_download_with_another_hash_is_not_unpacked(self):
        archive, _ = self.release()
        target = self.root / "target"
        folder, shown = self.quiet(
            self.ff.from_download,
            target,
            urls=[archive.as_uri()],
            expected="0" * 64,
            sleep=lambda seconds: None,
        )
        self.assertIsNone(folder)
        self.assertIn("SHA-256", shown)
        self.assertFalse((target / "unpacked").exists())

    def test_the_next_address_is_tried_when_one_does_not_answer(self):
        archive, digest = self.release()
        missing = (self.root / "absent.zip").as_uri()
        folder, shown = self.quiet(
            self.ff.from_download,
            self.root / "target",
            urls=[missing, archive.as_uri()],
            expected=digest,
            sleep=lambda seconds: None,
        )
        self.assertEqual(folder.name, "bin")
        self.assertIn("absent.zip", shown)

    def test_a_transfer_that_arrives_wrong_is_fetched_again(self):
        archive, digest = self.release()
        answers = []

        def fetch(url, destination):
            answers.append(url)
            if len(answers) == 1:
                return "f" * 64  # cut off on the way
            return self.ff.download(url, destination)

        folder, _ = self.quiet(
            self.ff.from_download,
            self.root / "target",
            urls=[archive.as_uri()],
            expected=digest,
            fetch=fetch,
        )
        self.assertEqual(len(answers), 2)
        self.assertEqual(folder.name, "bin")

    def test_an_archive_without_both_tools_is_refused(self):
        archive, digest = self.release(members=("ffmpeg.exe",))
        folder, shown = self.quiet(
            self.ff.from_download,
            self.root / "target",
            urls=[archive.as_uri()],
            expected=digest,
        )
        self.assertIsNone(folder)
        self.assertIn("ffprobe.exe", shown)

    # -- the whole flow -----------------------------------------------------------------------

    def environment(self, **extra):
        return {
            "PATH": "",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_PATH": str(self.root / "gh"),
            **extra,
        }

    def found_in(self, folder):
        """A stand-in for find_tools that finds the tools whenever `folder` is on the path."""

        def find(path):
            if str(folder) not in path.split(os.pathsep):
                return {}
            return {tool: str(Path(folder) / tool) for tool in self.ff.TOOLS}

        return find

    def test_tools_that_are_already_there_are_not_installed(self):
        folder = self.root / "tools"
        self.tools_in(folder)
        calls = []
        code, _ = self.quiet(
            self.ff.main,
            self.environment(PATH=str(folder)),
            via_chocolatey=lambda path: calls.append("choco"),
            via_download=lambda target: calls.append("download"),
            check=lambda program: True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.handed_on(), [folder])

    def test_tools_that_chocolatey_already_put_in_its_folder_are_found_there(self):
        # The state of a Windows runner when the tests run: the "FFmpeg Windows" step has filled
        # Chocolatey's folder, and PATH of this step does not have to name it.
        chocolatey = Path(self.ff.CHOCO_BIN)
        self.tools_in(chocolatey)
        calls = []
        code, _ = self.quiet(
            self.ff.main,
            self.environment(),
            via_chocolatey=lambda path: calls.append("choco"),
            via_download=lambda target: calls.append("download"),
            check=lambda program: True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.handed_on(), [chocolatey])

    def test_a_download_is_the_fallback_and_its_folder_reaches_the_next_steps(self):
        folder = self.root / "unpacked" / "bin"
        targets = []

        def via_download(target):
            targets.append(target)
            return folder

        code, shown = self.quiet(
            self.ff.main,
            self.environment(),
            find=self.found_in(folder),
            via_chocolatey=lambda path: False,
            via_download=via_download,
            check=lambda program: True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(targets, [self.root / "ffmpeg"])
        self.assertIn("Chocolatey did not deliver", shown)
        self.assertEqual((self.root / "gh").read_text(encoding="utf-8"), f"{folder}\n")  # once

    def test_the_job_fails_naming_what_is_missing_when_nothing_worked(self):
        code, shown = self.quiet(
            self.ff.main,
            self.environment(),
            find=lambda path: {},
            via_chocolatey=lambda path: False,
            via_download=lambda target: None,
            check=lambda program: True,
        )
        self.assertEqual(code, 1)
        self.assertIn("::error::", shown)
        self.assertIn("ffmpeg, ffprobe", shown)
        self.assertFalse((self.root / "gh").exists())

    def test_a_tool_that_does_not_run_fails_the_job_and_is_not_handed_on(self):
        folder = self.root / "tools"
        self.tools_in(folder)
        code, shown = self.quiet(
            self.ff.main,
            self.environment(PATH=str(folder)),
            via_chocolatey=lambda path: False,
            via_download=lambda target: None,
            check=lambda program: False,
        )
        self.assertEqual(code, 1)
        self.assertIn("-version did not run", shown)
        self.assertIn(str(folder), shown)  # the tool tried is the one in this test's folder
        self.assertFalse((self.root / "gh").exists())

    def test_without_github_path_the_folders_are_only_named(self):
        _, shown = self.quiet(self.ff.hand_on, ["C:\\tools"], None)
        self.assertIn("GITHUB_PATH is not set", shown)


@unittest.skipUnless(WORKFLOWS.is_dir(), ".github/ is not present")
class Workflows(unittest.TestCase):
    def test_the_python_floor_is_the_same_everywhere(self):
        floor = "3.11"
        meta = json.loads(text(BASE / "release.json"))
        self.assertEqual(meta["python_min"], floor)
        self.assertIn("(3, 11) <= sys.version_info", text(BASE / "maintenance.py"))
        matrix = re.search(r"python: \[([^\]]*)\]", text(WORKFLOWS / "compatibility.yml"))[1]
        versions = re.findall(r"'([0-9.]+)'", matrix)
        self.assertEqual(versions[0], floor)
        self.assertNotIn("3.10", versions)
        self.assertEqual(versions[-1], meta["python_max"])
        if (BASE / "ruff.toml").is_file():
            self.assertIn('target-version = "py311"', text(BASE / "ruff.toml"))
        self.assertIn("3.11–3.14", text(BASE / "PANDUAN.md"))
        self.assertIn("3.11–3.14", text(BASE / "README.md"))

    def test_the_lock_has_one_pin_per_package_and_no_python_marker_left(self):
        lock = text(BASE / "requirements.lock")
        self.assertNotIn("python_version", lock)
        names = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==", lock)
        self.assertEqual(len(names), len(set(names)))

    def test_the_weekly_age_check_cannot_lose_its_exit_code(self):
        workflow = text(WORKFLOWS / "dependencies.yml")
        step = workflow[workflow.index("id: age") : workflow.index("Open an issue")]
        self.assertIn("set -o pipefail", step)
        self.assertIn("pin_status.py --check", step)
        self.assertLess(step.index("set -o pipefail"), step.index("| tee"))

    def test_tests_run_through_the_summary_helper_on_every_system(self):
        workflow = text(WORKFLOWS / "compatibility.yml")
        self.assertEqual(workflow.count("scripts/ci_summary.py"), 2)  # POSIX and Windows
        self.assertNotIn("run: .venv/bin/python -m unittest", workflow)
        self.assertNotIn("run: .venv/Scripts/python.exe -m unittest", workflow)

    def step(self, name):
        """The text of one step of the native job, from its name to the next step."""
        workflow = text(WORKFLOWS / "compatibility.yml")
        start = workflow.index(f"      - name: {name}\n")
        end = workflow.find("\n      - ", start + 1)
        return workflow[start : end if end >= 0 else len(workflow)]

    def test_the_tool_versions_are_found_the_way_the_program_finds_them(self):
        # A bash on Windows may not see the PATH that the later steps and the tests see (a job
        # failed there with "ffmpeg: command not found"), so Python looks the tools up itself.
        step = self.step("Tool versions")
        self.assertIn("shell: python", step)
        self.assertIn("shutil.which(tool)", step)
        self.assertIn("sys.version", step)
        self.assertIn("'-version'", step)
        self.assertIn("sys.exit('not on PATH", step)  # a missing tool fails here, naming itself
        self.assertNotIn("shell: bash", step)

    def test_the_windows_ffmpeg_step_runs_the_script_that_judges_by_what_it_finds(self):
        # A job failed because choco exited with 0 after a 503 from its feed and installed
        # nothing; an exit code in the workflow is what let that pass the install step.
        step = self.step("FFmpeg Windows")
        self.assertIn("if: runner.os == 'Windows'", step)
        self.assertIn("run: python scripts/ci_ffmpeg.py", step)
        self.assertNotIn("LASTEXITCODE", step)
        self.assertNotIn("choco install", step)
        workflow = text(WORKFLOWS / "compatibility.yml")
        self.assertLess(workflow.index("actions/setup-python"), workflow.index(step))

    def test_the_release_waits_for_lint_and_every_native_job(self):
        workflow = text(WORKFLOWS / "compatibility.yml")
        self.assertIn("needs: [lint, native]", workflow)
        self.assertIn("release_bundle.py dist", workflow)

    def test_the_bump_workflow_is_limited_to_the_pin_files_and_to_what_it_needs(self):
        workflow = text(WORKFLOWS / "bump-ytdlp.yml")
        self.assertRegex(
            workflow,
            r"(?m)^permissions:\n  contents: write\n  pull-requests: write\n  actions: write\n",
        )
        self.assertIn(r"requirements\.(txt|lock)", workflow)
        self.assertIn("git add requirements.txt requirements.lock", workflow)
        self.assertIn("gh workflow run compatibility.yml", workflow)
        for other in ("compatibility.yml", "dependencies.yml"):
            self.assertNotIn("contents: write", text(WORKFLOWS / other).split("jobs:")[0])

    def test_every_workflow_pins_its_actions_by_commit(self):
        for path in WORKFLOWS.glob("*.yml"):
            for use in re.findall(r"uses:\s*(\S+)", text(path)):
                self.assertRegex(use, r"@[0-9a-f]{40}$", f"{path.name}: {use}")

    def test_build_output_stays_out_of_git(self):
        ignore = text(BASE / ".gitignore").splitlines()
        self.assertIn("dist/", ignore)
        self.assertIn("*.zip.sha256", ignore)

    def test_the_documents_describe_the_new_behaviour(self):
        guide = text(BASE / "PANDUAN.md")
        for word in (
            "YTDL_THEME",
            "YTDL_MOTION",
            "YTDL_INSPECT_WORKERS",
            "YTDL_PACE",
            "--timings",
            "--links-file",
            "--abandoned",
            "status --json",
            "--signer-workflow",
        ):
            self.assertIn(word, guide, word)
        self.assertIsNone(re.search(r"\blo\b", guide))  # one register in the guide
        self.assertNotIn("E8384F", guide)  # the bar is no longer red


if __name__ == "__main__":
    unittest.main(verbosity=2)
