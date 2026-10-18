"""Stable-release protocol, package validation and real isolated installation."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import compat
import release_bundle
import self_update as update
from terminal import Terminal


MAIN_GUARD = 'if __name__ == "__main__":\n    sys.exit(main())'


def maintenance_with(fixture):
    """The real maintenance.py with its dependency setup replaced by a local fixture."""
    source = (BASE / "maintenance.py").read_text(encoding="utf-8")
    assert MAIN_GUARD in source, "maintenance.py entry guard changed; update this fixture"
    return source.replace(MAIN_GUARD, fixture + "\n" + MAIN_GUARD)


class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "new"
        self.source.mkdir()
        self.dest = self.root / "installed"
        self.dest.mkdir()
        (self.dest / "model.py").write_text("VERSION='1.1.0'\n", encoding="utf-8")
        (self.dest / "app.py").write_text("old program", encoding="utf-8")
        (self.dest / "settings.json").write_text('{"private":true}', encoding="utf-8")
        for name in (
            "maintenance.py",
            "compat.py",
            "app.py",
            "requirements.lock",
            "runtime.json",
            "youtube-downloader",
            "youtube-downloader.cmd",
        ):
            (self.source / name).write_text("# fixture\n", encoding="utf-8")
        (self.source / "model.py").write_text("VERSION='1.2.0'\n", encoding="utf-8")
        meta = {
            "version": "1.2.0",
            "platforms": [compat.platform_key()],
            "python_min": "3.10",
            "python_max": "3.14",
        }
        (self.source / "release.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.dest / "release.json").write_text(
            json.dumps({**meta, "version": "1.1.0"}), encoding="utf-8"
        )
        self.archive, self.checksum = release_bundle.build(self.source, self.root / "packages")
        self.info = {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "id": 1,
                    "name": self.archive.name,
                    "state": "uploaded",
                    "size": self.archive.stat().st_size,
                },
                {
                    "id": 2,
                    "name": self.checksum.name,
                    "state": "uploaded",
                    "size": self.checksum.stat().st_size,
                },
            ],
        }
        self.data = {
            "/releases/latest": json.dumps(self.info).encode(),
            "/releases/assets/1": self.archive.read_bytes(),
            "/releases/assets/2": self.checksum.read_bytes(),
        }

        class Client:
            def read(inner, path, limit, asset=False):
                return self.data[path]

        self.client = Client()

    def perform(self, **kw):
        with (
            patch.object(update, "github_token", return_value="test-credential"),
            patch.object(update, "GitHub", return_value=self.client),
            # Never the real GitHub CLI from a test: verification has its own tests below.
            patch.object(update, "verify_attestation", return_value=False) as verify,
        ):
            self.verify = verify
            return update.perform(self.dest, say=lambda msg: None, **kw)

    def test_check_is_read_only(self):
        self.assertEqual(self.perform(check=True), 0)
        self.assertFalse((self.dest / ".state").exists())

    def test_equal_or_older_release_is_not_installed(self):
        for version in ("1.2.0", "1.3.0"):
            (self.dest / "model.py").write_text("VERSION='" + version + "'", encoding="utf-8")
            with patch.object(update, "install_package") as install:
                self.assertEqual(self.perform(), 0)
                install.assert_not_called()
        self.assertFalse((self.dest / ".state").exists())

    def test_package_verified_before_install_and_temporary_cleaned(self):
        def install(source, root, *args):
            self.assertEqual(update.installed_version(source), "1.2.0")
            shutil.copy2(source / "model.py", root / "model.py")
            return 0

        with patch.object(update, "install_package", side_effect=install):
            self.assertEqual(self.perform(), 0)
        self.assertEqual(
            (self.dest / "settings.json").read_text(encoding="utf-8"), '{"private":true}'
        )
        self.assertEqual(list((self.dest / ".state").glob("release-*")), [])

    def test_the_package_is_verified_before_anything_is_unpacked_or_installed(self):
        def install(source, root, *args):
            shutil.copy2(source / "model.py", root / "model.py")
            return 0

        with patch.object(update, "install_package", side_effect=install) as installer:
            self.assertEqual(self.perform(), 0)
        self.verify.assert_called_once()
        self.assertEqual(Path(self.verify.call_args.args[0]).name, self.archive.name)
        installer.assert_called_once()
        (self.dest / "model.py").write_text("VERSION='1.1.0'\n", encoding="utf-8")
        # A verification that refuses stops the update with the program untouched.
        with (
            patch.object(update, "github_token", return_value="test-credential"),
            patch.object(update, "GitHub", return_value=self.client),
            patch.object(update, "verify_attestation", side_effect=ValueError("ditolak")),
            patch.object(update, "install_package") as install,
        ):
            with self.assertRaisesRegex(ValueError, "ditolak"):
                update.perform(self.dest, say=lambda msg: None)
            install.assert_not_called()
        self.assertEqual((self.dest / "app.py").read_text(encoding="utf-8"), "old program")

    def verify_with(self, which=None, run=None, strict=False):
        said = []
        env = {"YTDL_VERIFY_ATTESTATION": "1"} if strict else {}
        with patch.dict(os.environ, env, clear=False):
            if not strict:
                os.environ.pop("YTDL_VERIFY_ATTESTATION", None)
            result = update.verify_attestation(
                self.archive, said.append, which or (lambda name: None), run
            )
        return result, said

    def test_attestation_is_reported_but_not_required_by_default(self):
        result, said = self.verify_with()  # no GitHub CLI
        self.assertFalse(result)
        self.assertRegex(said[0], "tidak diperiksa.*gh.*tidak ditemukan.*checksum paket cocok")

        class Done:
            returncode = 1
            stdout = ""
            stderr = "first line\nno attestations found\n"

        result, said = self.verify_with(lambda name: "/usr/bin/gh", lambda *a, **k: Done())
        self.assertFalse(result)
        self.assertRegex(said[0], r"verifikasi ditolak \(no attestations found\)")

        def broken(*args, **kwargs):
            raise OSError("exec failed")

        result, said = self.verify_with(lambda name: "/usr/bin/gh", broken)
        self.assertFalse(result)
        self.assertIn("gh tidak dapat dijalankan", said[0])

    def test_attestation_passes_the_archive_and_repository_to_the_cli(self):
        seen = {}

        class Ok:
            returncode = 0
            stdout = stderr = ""

        def run(command, **kwargs):
            seen["command"], seen["timeout"] = command, kwargs["timeout"]
            return Ok()

        result, said = self.verify_with(lambda name: "/usr/bin/gh", run)
        self.assertTrue(result)
        self.assertEqual(
            seen["command"],
            [
                "/usr/bin/gh",
                "attestation",
                "verify",
                str(self.archive),
                "--repo",
                update.REPOSITORY,
                "--signer-workflow",
                update.SIGNER_WORKFLOW,
            ],
        )
        self.assertLessEqual(seen["timeout"], 60)
        self.assertIn("workflow rilis", said[0])
        self.assertTrue(update.SIGNER_WORKFLOW.endswith("/.github/workflows/compatibility.yml"))

    def test_attestation_falls_back_to_the_repository_for_an_older_gh(self):
        commands = []

        class Result:
            def __init__(self, code, stderr=""):
                self.returncode, self.stdout, self.stderr = code, "", stderr

        def run(command, **kwargs):
            commands.append(command)
            if "--signer-workflow" in command:
                return Result(1, "Error: unknown flag: --signer-workflow")
            return Result(0)

        result, said = self.verify_with(lambda name: "/usr/bin/gh", run)
        self.assertTrue(result)
        self.assertEqual(len(commands), 2)
        self.assertNotIn("--signer-workflow", commands[1])
        self.assertIn("belum bisa membatasi", said[0])

    def test_a_signer_workflow_that_does_not_match_is_not_retried_without_it(self):
        commands = []

        class Refused:
            returncode = 1
            stdout = ""
            stderr = "Error: verification failed: no matching attestation"

        def run(command, **kwargs):
            commands.append(command)
            return Refused()

        result, said = self.verify_with(lambda name: "/usr/bin/gh", run)
        self.assertFalse(result)
        self.assertEqual(len(commands), 1)
        self.assertIn("verifikasi ditolak", said[0])

    def test_attestation_can_be_made_mandatory(self):
        class Refused:
            returncode = 1
            stdout = ""
            stderr = "attestation does not match"

        with self.assertRaisesRegex(ValueError, "wajib diperiksa.*gh.*tidak ditemukan"):
            self.verify_with(strict=True)
        with self.assertRaisesRegex(ValueError, "wajib diperiksa.*does not match.*belum diubah"):
            self.verify_with(lambda name: "/usr/bin/gh", lambda *a, **k: Refused(), strict=True)

    def test_shared_download_lock_refuses_installation(self):
        with (
            compat.file_lock(self.dest / ".state/run.lock"),
            patch.object(update, "install_package") as install,
        ):
            with self.assertRaisesRegex(ValueError, "berjalan"):
                self.perform()
            install.assert_not_called()
        self.assertEqual(update.installed_version(self.dest), "1.1.0")

    def test_bad_checksum_leaves_program_unchanged(self):
        self.data["/releases/assets/1"] = b"x" * self.info["assets"][0]["size"]
        with patch.object(update, "install_package") as install:
            with self.assertRaisesRegex(ValueError, "Checksum"):
                self.perform()
            install.assert_not_called()
        self.assertEqual(update.installed_version(self.dest), "1.1.0")
        self.assertFalse(list((self.dest / ".state").glob("release-*")))

    def test_checksum_filename_mismatch_rejected(self):
        self.data["/releases/assets/2"] = b"0" * 64 + b"  other.zip\n"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.perform()

    def test_unstable_invalid_and_incomplete_release_rejected(self):
        for change in (
            {"prerelease": True},
            {"draft": True},
            {"tag_name": "v1.2.0-rc1"},
            {"assets": []},
        ):
            self.data["/releases/latest"] = json.dumps({**self.info, **change}).encode()
            with self.assertRaises(ValueError):
                update.release_info(self.client)

    def test_zip_traversal_symlink_private_and_duplicate_rejected(self):
        for name in (
            "../escape",
            "youtube-downloader/../escape",
            "youtube-downloader/settings.json",
            "youtube-downloader/.state/archive",
            "youtube-downloader/tests/../../escape",
            "youtube-downloader/C:\\escape",
        ):
            archive = self.root / "invalid.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr(name, b"evil")
            with self.assertRaises(ValueError):
                update.safe_unpack(archive, self.root / "extract", "1.2.0")
        for kind in ("symlink", "duplicate"):
            with zipfile.ZipFile(self.root / "invalid.zip", "w") as out:
                member = zipfile.ZipInfo("youtube-downloader/app.py")
                member.external_attr = 0o120777 << 16 if kind == "symlink" else 0o100644 << 16
                out.writestr(member, b"target")
                if kind == "duplicate":
                    out.writestr("youtube-downloader/APP.PY", b"duplicate")
            with self.assertRaises(ValueError):
                update.safe_unpack(
                    self.root / "invalid.zip", self.root / ("extract-" + kind), "1.2.0"
                )

    def test_package_version_platform_and_python_checked(self):
        meta = json.loads((self.source / "release.json").read_text())
        for change in (
            {"version": "1.9.0"},
            {"platforms": ["unavailable"]},
            {"python_min": "9.0", "python_max": "9.1"},
        ):
            (self.source / "release.json").write_text(
                json.dumps({**meta, **change}), encoding="utf-8"
            )
            archive, _ = release_bundle.build(self.source, self.root / "packages")
            with self.assertRaises(ValueError):
                update.safe_unpack(
                    archive,
                    self.root / ("extract-" + str(len(list(self.root.glob("extract-*"))))),
                    "1.2.0",
                )

    def test_redirect_removes_credentials_and_rejects_other_hosts(self):
        handler = update.SafeRedirect()
        request = update.urllib.request.Request(
            "https://api.github.com/a", headers={"Authorization": "Bearer secret"}
        )
        redirected = handler.redirect_request(
            request, None, 302, "", {}, "https://release-assets.githubusercontent.com/a"
        )
        self.assertFalse(redirected.has_header("Authorization"))
        for url in (
            "http://github.com/a",
            "https://evil.example/a",
            "https://githubusercontent.com.evil.example/a",
        ):
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "", {}, url)

    def test_credentials_use_environment_without_spawning_or_saving(self):
        with (
            patch.dict(os.environ, {"GH_TOKEN": "test-secret"}),
            patch.object(update.subprocess, "run") as run,
        ):
            self.assertEqual(update.github_token(), "test-secret")
            run.assert_not_called()

    def test_release_bundle_excludes_private_data_and_has_executable_launcher(self):
        for name in (
            "settings.json",
            ".state/private",
            ".venv/private",
            ".runtime/private",
            "media/private",
        ):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("private")
        archive, _ = release_bundle.build(self.source, self.root / "packages")
        with zipfile.ZipFile(archive) as source:
            self.assertFalse(any("private" in x or "settings.json" in x for x in source.namelist()))
            self.assertTrue(
                source.getinfo("youtube-downloader/youtube-downloader").external_attr >> 16 & 0o111
            )

    def test_real_isolated_installer_uses_new_package_and_preserves_data(self):
        (self.source / "compat.py").write_text(
            (BASE / "compat.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        # Use the real transactional updater with dependency setup replaced by a local fixture.
        script = maintenance_with("def setup(*args, **kwargs):\n    return None")
        (self.source / "maintenance.py").write_text(script, encoding="utf-8")
        (self.dest / ".state").mkdir()
        (self.dest / ".state/archive").write_bytes(b"keep database")
        (self.dest / "media").mkdir()
        (self.dest / "media/result").write_bytes(b"keep media")
        self.assertEqual(update.install_package(self.source, self.dest, say=lambda msg: None), 0)
        self.assertEqual(update.installed_version(self.dest), "1.2.0")
        self.assertEqual((self.dest / ".state/archive").read_bytes(), b"keep database")
        self.assertEqual((self.dest / "media/result").read_bytes(), b"keep media")
        self.assertTrue((self.dest / ".state/last-update.json").exists())

    def test_real_installer_failure_restores_program(self):
        (self.source / "compat.py").write_text(
            (BASE / "compat.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        script = maintenance_with(
            "def setup(root, **kw):\n"
            "    if not kw.get('upgrade'):\n"
            "        raise ValueError('fixture failure')"
        )
        (self.source / "maintenance.py").write_text(script, encoding="utf-8")
        (self.dest / ".state").mkdir()
        with self.assertRaisesRegex(ValueError, "tidak berhasil"):
            update.install_package(self.source, self.dest, say=lambda msg: None)
        self.assertEqual(update.installed_version(self.dest), "1.1.0")
        self.assertEqual((self.dest / "app.py").read_text(encoding="utf-8"), "old program")

    def test_ctrl_c_during_install_is_deferred_and_returns_130(self):
        (self.source / "maintenance.py").write_text(
            "import time,sys\nfrom pathlib import Path\np=Path(sys.argv[2]);(p/'.state/started').write_text('started');time.sleep(1.2);(p/'.state/finished').write_text('finished')\n",
            encoding="utf-8",
        )
        (self.dest / ".state").mkdir()
        script = "from pathlib import Path; from self_update import install_package; import sys; sys.exit(install_package(Path(sys.argv[1]),Path(sys.argv[2])))"
        with Terminal(
            [sys.executable, "-c", script, str(self.source), str(self.dest)], BASE
        ) as term:
            deadline = time.monotonic() + 10
            while not (self.dest / ".state/started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.dest / ".state/started").exists())
            term.interrupt()
            data = ""
            while term.running() and time.monotonic() < deadline:
                data += term.read(0.1)
            data += term.drain()
            self.assertEqual(term.wait(), 130, data)
        self.assertTrue((self.dest / ".state/finished").exists())


class Credentials(unittest.TestCase):
    """A token with a space or a line break inside breaks the Authorization header; it is
    refused with a message that says where it came from."""

    def token(self, environment, gh=None):
        class Result:
            returncode = 0
            stderr = ""

            def __init__(self, text):
                self.stdout = text

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                update.shutil, "which", return_value="/usr/bin/gh" if gh is not None else None
            ),
            patch.object(update.subprocess, "run", return_value=Result(gh or "")),
        ):
            return update.github_token()

    def test_whitespace_around_a_token_is_dropped(self):
        self.assertEqual(self.token({"GH_TOKEN": "  abc123\n"}), "abc123")
        self.assertEqual(self.token({"GITHUB_TOKEN": "abc123\r\n"}), "abc123")

    def test_a_token_with_a_line_break_or_space_inside_names_its_source(self):
        for value in ("abc\n123", "abc 123", "abc\t123", "abc\x1b123", "abc\u00e9123"):
            with self.assertRaisesRegex(ValueError, r"GH_TOKEN.*tidak valid"):
                self.token({"GH_TOKEN": value})
        with self.assertRaisesRegex(ValueError, r"GITHUB_TOKEN.*tidak valid"):
            self.token({"GITHUB_TOKEN": "abc 123"})

    def test_a_blank_variable_is_as_good_as_not_set(self):
        self.assertEqual(self.token({"GH_TOKEN": "   ", "GITHUB_TOKEN": "second"}), "second")
        self.assertEqual(self.token({"GH_TOKEN": ""}, gh="from-gh\n"), "from-gh")

    def test_the_token_of_the_github_cli_is_checked_too(self):
        with self.assertRaisesRegex(ValueError, "gh auth token.*tidak valid"):
            self.token({}, gh="two words")

    def test_no_token_at_all_asks_for_a_login(self):
        with self.assertRaisesRegex(ValueError, "gh auth login"):
            self.token({})

    def test_the_message_never_contains_the_token(self):
        try:
            self.token({"GH_TOKEN": "secret value"})
        except ValueError as exc:
            self.assertNotIn("secret", str(exc))
        else:
            self.fail("a token with a space was accepted")


class InstallerArguments(unittest.TestCase):
    """The installer is started with a short wait for the lock the updater has just released."""

    def installer_args(self, wait):
        captured = []

        class Process:
            pid = 1

            def __init__(self, args, **kwargs):
                captured.append(args)
                self.stdin = io.BytesIO()

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        class Tree:
            def __init__(self, process):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".state").mkdir()
            with (
                patch.object(update.subprocess, "Popen", Process),
                patch.object(update, "ProcessTree", Tree),
            ):
                self.assertEqual(update.install_package(root / "source", root, wait), 0)
        return captured[0]

    def test_without_wait_the_installer_gets_five_seconds_of_grace(self):
        args = self.installer_args(wait=False)
        self.assertEqual(args[args.index("--grace") + 1], "5")
        self.assertNotIn("--wait", args)

    def test_with_wait_it_waits_for_as_long_as_it_takes(self):
        args = self.installer_args(wait=True)
        self.assertIn("--wait", args)
        self.assertNotIn("--grace", args)


class ReleaseFiles(unittest.TestCase):
    """The package says which files it holds, so an update can retire the ones a release drops."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        for name in ("tests", ".github/workflows", ".bootstrap"):
            (self.source / name).mkdir(parents=True)
        for name in (
            "maintenance.py",
            "compat.py",
            "model.py",
            "app.py",
            "README.md",
            "requirements.lock",
            "runtime.json",
            "youtube-downloader",
            "youtube-downloader.cmd",
            "tests/test_one.py",
            ".github/workflows/ci.yml",
            ".bootstrap/pip.whl",
        ):
            (self.source / name).write_text("# fixture\n", encoding="utf-8")
        (self.source / "model.py").write_text("VERSION='1.2.0'\n", encoding="utf-8")
        self.meta = {
            "version": "1.2.0",
            "platforms": [compat.platform_key()],
            "python_min": "3.11",
            "python_max": "3.14",
        }
        (self.source / "release.json").write_text(json.dumps(self.meta), encoding="utf-8")
        # Private data next to the program must never be listed.
        (self.source / "settings.json").write_text("{}", encoding="utf-8")
        (self.source / ".state").mkdir()
        (self.source / ".state/job.json").write_text("{}", encoding="utf-8")

    def build(self):
        archive, _ = release_bundle.build(self.source, self.root / "dist")
        with zipfile.ZipFile(archive) as package:
            members = sorted(
                n[len("youtube-downloader/") :] for n in package.namelist() if not n.endswith("/")
            )
            meta = json.loads(package.read("youtube-downloader/release.json"))
        return archive, members, meta

    def test_the_packaged_release_json_lists_exactly_the_files_in_the_zip(self):
        archive, members, meta = self.build()
        self.assertEqual(meta["files"], members)
        self.assertIn("release.json", meta["files"])
        self.assertIn("tests/test_one.py", meta["files"])  # forward slashes on every system
        self.assertIn(".github/workflows/ci.yml", meta["files"])
        self.assertNotIn("settings.json", meta["files"])
        self.assertFalse(any(n.startswith(".state") for n in meta["files"]))
        for key in ("version", "platforms", "python_min", "python_max"):
            self.assertEqual(meta[key], self.meta[key])
        self.assertEqual(release_bundle.verify(archive), members)

    def test_the_release_json_in_the_tree_is_left_alone(self):
        before = (self.source / "release.json").read_text(encoding="utf-8")
        self.build()
        self.assertEqual((self.source / "release.json").read_text(encoding="utf-8"), before)
        self.assertNotIn("files", json.loads(before))

    def test_building_twice_gives_the_same_package(self):
        first, _, _ = self.build()
        digest = first.read_bytes()
        again, _ = release_bundle.build(self.source, self.root / "dist2")
        self.assertEqual(again.read_bytes(), digest)

    def test_the_package_unpacks_with_the_list_inside_it(self):
        archive, members, _ = self.build()
        root = update.safe_unpack(archive, self.root / "unpacked", "1.2.0")
        listed = json.loads((root / "release.json").read_text(encoding="utf-8"))["files"]
        for name in listed:
            self.assertTrue((root / name).is_file(), name)

    def write_zip(self, name, members):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as out:
            for member, data in members.items():
                out.writestr(member, data)
        return path

    def test_a_package_whose_list_differs_from_its_contents_is_refused(self):
        listed = {**self.meta, "files": ["app.py", "release.json", "missing.py"]}
        path = self.write_zip(
            "wrong.zip",
            {
                "youtube-downloader/app.py": "x",
                "youtube-downloader/release.json": json.dumps(listed),
            },
        )
        with self.assertRaisesRegex(ValueError, "Daftar files"):
            release_bundle.verify(path)

    def test_a_package_with_data_outside_the_program_is_refused(self):
        listed = {**self.meta, "files": ["release.json", "settings.json"]}
        path = self.write_zip(
            "private.zip",
            {
                "youtube-downloader/release.json": json.dumps(listed),
                "youtube-downloader/settings.json": "{}",
            },
        )
        with self.assertRaisesRegex(ValueError, "bukan bagian program"):
            release_bundle.verify(path)
        path = self.write_zip(
            "outside.zip", {"elsewhere/app.py": "x", "youtube-downloader/release.json": "{}"}
        )
        with self.assertRaisesRegex(ValueError, "di luar folder"):
            release_bundle.verify(path)

    def test_the_command_line_checks_an_existing_package(self):
        archive, members, _ = self.build()
        result = subprocess.run(
            [sys.executable, str(BASE / "release_bundle.py"), "--verify", str(archive)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(len(members)), result.stdout)
        result = subprocess.run(
            [sys.executable, str(BASE / "release_bundle.py")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)

    def test_the_real_program_builds_and_verifies(self):
        archive, _ = release_bundle.build(BASE, self.root / "real")
        names = release_bundle.verify(archive)
        self.assertIn("release.json", names)
        self.assertIn("release_bundle.py", names)
        self.assertFalse(any(n.startswith(("scripts/", "docs/")) for n in names))


if __name__ == "__main__":
    unittest.main()
