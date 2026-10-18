"""The CI helpers and the rules that keep the workflows, the support policy and the documents in
step. The helpers live in scripts/, which is in the repository and not in the release package."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
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
        self.assertIn("ffmpeg -version", workflow)
        self.assertIn("python -VV", workflow)

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
