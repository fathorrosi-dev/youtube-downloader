"""The look of the display: colour rules and themes, what each colour means, motion, the plain
menus, and how the Windows console is told apart from a redirected stdout."""

import ctypes
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import testenv  # noqa: F401
import compat
import ui
from rich.console import Console
from terminal import Terminal

BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def luminance(color):
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def rgb(color):
    return ";".join(str(int(color[i : i + 2], 16)) for i in (1, 3, 5))


class ColourRules(unittest.TestCase):
    def test_one_rule_names_the_colour_depth_on_posix(self):
        table = [
            ({"COLORTERM": "truecolor"}, "truecolor"),
            ({"COLORTERM": "24bit", "TERM": "xterm"}, "truecolor"),
            ({"COLORTERM": " TrueColor "}, "truecolor"),
            ({"TERM": "xterm-256color"}, "256"),
            ({"TERM": "xterm-kitty"}, "256"),
            ({"TERM": "screen-256color", "COLORTERM": ""}, "256"),
            ({"TERM": "xterm-16color"}, "standard"),
            ({"TERM": "xterm"}, "standard"),
            ({"TERM": "linux"}, "standard"),
            ({}, "standard"),
        ]
        for environment, expected in table:
            self.assertEqual(ui.detect_color_system(environment), expected, environment)

    def test_windows_goes_by_the_console_and_ignores_term_and_colorterm(self):
        modern, old = (10, 0, 22631), (10, 0, 14393)
        for environment in ({}, {"TERM": "dumb"}, {"TERM": "xterm"}, {"COLORTERM": "truecolor"}):
            self.assertEqual(ui.detect_color_system(environment, True, modern), "truecolor")
            self.assertEqual(ui.detect_color_system(environment, True, old), "256")
            self.assertIsNone(ui.detect_color_system(environment, False, modern))
        # The first build that shows 24 bits in a console with VT processing.
        self.assertEqual(ui.detect_color_system({}, True, (10, 0, 15063)), "truecolor")
        self.assertEqual(ui.detect_color_system({}, True, (10, 0, 15062)), "256")

    def test_the_theme_follows_the_setting_then_the_terminal_then_dark(self):
        light_background = {"COLORFGBG": "0;15"}
        self.assertEqual(ui.detect_theme({}), "dark")
        self.assertEqual(ui.detect_theme({"YTDL_THEME": "light"}), "light")
        self.assertEqual(ui.detect_theme({"YTDL_THEME": " DARK "}), "dark")
        # An explicit choice beats what the terminal reports.
        self.assertEqual(ui.detect_theme({"YTDL_THEME": "dark", **light_background}), "dark")
        self.assertEqual(ui.detect_theme({"YTDL_THEME": "auto", **light_background}), "light")
        self.assertEqual(ui.detect_theme(light_background), "light")
        self.assertEqual(ui.detect_theme({"COLORFGBG": "0;7"}), "light")
        self.assertEqual(ui.detect_theme({"COLORFGBG": "15;0"}), "dark")
        self.assertEqual(ui.detect_theme({"COLORFGBG": "default;default"}), "dark")
        self.assertEqual(ui.detect_theme({"YTDL_THEME": "bogus", **light_background}), "light")

    def test_every_text_colour_of_each_theme_reads_on_its_own_background(self):
        self.assertEqual(set(ui.PALETTES), {"dark", "light"})
        for theme, palette in ui.PALETTES.items():
            ground = ui.PALETTE_GROUND[theme]
            for name in ("pink", "blue", "red", "dim"):
                ratio = contrast(palette[name], ground)
                self.assertGreaterEqual(ratio, 4.5, f"{theme} {name} {palette[name]}: {ratio:.2f}")

    def test_the_two_themes_define_the_same_roles(self):
        self.assertEqual(set(ui.PALETTES["dark"]), set(ui.PALETTES["light"]))
        for theme in ui.PALETTES.values():
            self.assertEqual(len({theme["pink"], theme["blue"], theme["red"]}), 3)

    def test_the_three_colours_are_sky_blue_pink_and_red(self):
        # Hue, not exact value, so that tuning a shade does not break the meaning.
        for palette in ui.PALETTES.values():

            def channels(name, palette=palette):
                return [int(palette[name][i : i + 2], 16) for i in (1, 3, 5)]

            red, green, blue = channels("blue")
            self.assertGreater(blue, red)  # sky blue: blue leads
            red, green, blue = channels("pink")
            self.assertGreater(red, green)  # pink: red over green...
            self.assertGreater(blue, green)  # ...and blue over green
            red, green, blue = channels("red")
            self.assertGreater(red, 2 * max(green, blue) * 0.9)  # red: red dominates

    def test_motion_can_be_switched_off_and_never_runs_in_plain_output(self):
        self.assertTrue(ui.motion_enabled({}, False))
        self.assertTrue(ui.motion_enabled({"YTDL_MOTION": "auto"}, False))
        self.assertTrue(ui.motion_enabled({"YTDL_MOTION": "on"}, False))
        for word in ("off", "OFF", "0", "false", "no", " off "):
            self.assertFalse(ui.motion_enabled({"YTDL_MOTION": word}, False), word)
        self.assertFalse(ui.motion_enabled({}, True))

    def test_mixing_and_pulsing_stay_between_their_two_colours(self):
        self.assertEqual(ui.mix("#000000", "#FFFFFF", 0), "#000000")
        self.assertEqual(ui.mix("#000000", "#FFFFFF", 1), "#FFFFFF")
        self.assertEqual(ui.mix("#000000", "#FFFFFF", 5), "#FFFFFF")  # clamped
        self.assertEqual(ui.mix("#000000", "#FFFFFF", 0.5), "#808080")
        self.assertEqual(ui.pulse(0, "#102030", "#FFFFFF"), "#102030")
        self.assertEqual(ui.pulse(0.8, "#102030", "#FFFFFF", period=1.6), "#FFFFFF")
        for moment in (0.1, 0.4, 0.9, 1.3):
            self.assertRegex(ui.pulse(moment, ui.BLUE, ui.PINK), r"^#[0-9A-F]{6}$")


def render(renderable, width=100):
    """What Rich writes for `renderable` on a terminal with 24-bit colour."""
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=width,
        height=30,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        highlight=False,
    )
    console.print(renderable)
    return buffer.getvalue()


class Roles(unittest.TestCase):
    def session(self, motion=False):
        screen = ui.UI(True)
        screen.plain = False
        screen.motion = motion
        screen.glyph = ui.GLYPHS
        session = ui.Session(screen, 1)
        session.event({"type": "start", "name": "video.f137.mp4", "stream": "video"})
        session.event({"type": "progress", "downloaded": 40, "total": 100, "speed": 10})
        return session

    def test_the_active_download_bar_is_sky_blue_and_not_red(self):
        out = render(self.session())
        self.assertIn("\x1b[38;2;" + rgb(ui.BLUE) + "m", out)
        self.assertNotIn("\x1b[38;2;" + rgb(ui.RED) + "m", out)

    def test_red_appears_only_when_something_failed(self):
        session = self.session()
        session.event({"type": "failure", "message": "! GAGAL · contoh"})
        self.assertIn("\x1b[38;2;" + rgb(ui.RED) + "m", render(session))

    def test_a_finished_item_is_pink(self):
        screen = ui.UI(True)
        screen.plain = False
        screen.glyph = ui.GLYPHS
        session = ui.Session(screen, 2, [{"title": "a"}, {"title": "b"}])
        session.item_start(("metadata", "finish"))
        session.item_end("complete")
        self.assertIn("\x1b[38;2;" + rgb(ui.PINK) + "m", render(session))


class Moving(unittest.TestCase):
    def test_a_busy_line_turns_a_spinner_with_motion_and_stands_still_without(self):
        clock = [0.0]
        moving = ui.Busy("Memeriksa", True, now=lambda: clock[0])
        still = ui.Busy("Memeriksa", False, now=lambda: clock[0])
        seen = set()
        for moment in (0.0, 0.1, 0.2, 0.3):
            clock[0] = moment
            first = render(moving).replace("\x1b", "")
            seen.add(next(c for c in first if c in BRAILLE))
            self.assertNotIn("●", first)
            self.assertIn("●", render(still))
            self.assertFalse(any(c in render(still) for c in BRAILLE))
        self.assertGreater(len(seen), 1)

    def test_a_busy_line_shows_the_time_only_once_it_is_slow(self):
        clock = [0.0]
        busy = ui.Busy("Memeriksa", False, now=lambda: clock[0])
        clock[0] = 1.0
        self.assertNotIn("·", render(busy))
        clock[0] = 7.0
        self.assertIn("7d", render(busy))

    def test_a_busy_line_draws_a_bar_when_the_steps_are_known(self):
        busy = ui.Busy("Memeriksa", False)
        busy.set("Memeriksa · 2/4", (2, 4))
        self.assertIn("█", render(busy))
        busy.set("Memeriksa", None)
        self.assertNotIn("█", render(busy))

    def test_the_spinner_frame_follows_the_clock(self):
        self.assertEqual(ui.spin(0), ui.SPINNER[0])
        self.assertEqual(ui.spin(1 / 12 + 0.001), ui.SPINNER[1])
        self.assertEqual(ui.spin(len(ui.SPINNER) / 12 + 0.001), ui.SPINNER[0])

    def test_the_status_line_spins_only_while_a_stage_runs_with_motion(self):
        screen = ui.UI(True)
        screen.plain = False
        screen.glyph = ui.GLYPHS
        for motion, expected in ((True, True), (False, False)):
            screen.motion = motion
            session = ui.Session(screen, 1)
            session.item_start(("metadata", "video", "finish"))
            session.event({"type": "stage", "message": "Memeriksa", "phase": "metadata"})
            out = render(session)
            self.assertEqual(any(c in out for c in BRAILLE), expected, motion)

    def test_a_clean_finish_closes_with_a_short_flourish_and_a_failure_does_not(self):
        screen = ui.UI(True)
        screen.plain = False
        screen.motion = True
        screen.glyph = ui.GLYPHS
        screen.console = Console(file=io.StringIO(), width=80, no_color=True)
        with patch.object(ui, "Live"), patch.object(ui.time, "sleep"):
            session = ui.Session(screen, 1)
            with patch.object(ui.Session, "settle") as settle:
                with session:
                    pass
                settle.assert_called_once()
            failing = ui.Session(screen, 1)
            with patch.object(ui.Session, "settle") as settle:
                with failing:
                    failing.item_end("failed")
                settle.assert_not_called()
            still = ui.UI(True)
            still.plain = False
            still.motion = False
            still.glyph = ui.GLYPHS
            still.console = screen.console
            quiet = ui.Session(still, 1)
            with patch.object(ui.Session, "settle") as settle:
                with quiet:
                    pass
                settle.assert_not_called()

    def test_the_banner_is_typed_only_in_an_interactive_run_with_motion(self):
        screen = ui.UI(True)
        screen.plain = False
        screen.numbered = False
        screen.glyph = ui.GLYPHS
        screen.console = Console(file=io.StringIO(), width=80, no_color=True)
        for motion, animate, typed in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
        ):
            screen.motion = motion
            with patch.object(ui, "Live") as live, patch.object(ui.time, "sleep"):
                screen.banner("1.3.0", animate=animate)
            self.assertEqual(live.called, typed, (motion, animate))
        buffer = screen.console.file
        self.assertIn("YouTube Downloader", buffer.getvalue())
        self.assertIn("1.3.0", buffer.getvalue())


class NumberedMenus(unittest.TestCase):
    def screen(self):
        screen = ui.UI(True)  # plain output: numbered menus
        self.assertTrue(screen.numbered)
        return screen

    def ask(self, screen, answers, call):
        feed = iter(answers)
        out = io.StringIO()
        with (
            patch.object(ui.sys.stdin, "isatty", return_value=True, create=True),
            patch("builtins.input", side_effect=lambda *a: next(feed)),
            redirect_stderr(out),
        ):
            return call(screen), out.getvalue()

    def test_a_checkbox_list_marks_the_earlier_answer_and_enter_keeps_it(self):
        options = [("A", 1), ("B", 2), ("C", 3)]
        result, shown = self.ask(
            self.screen(), [""], lambda s: s.checkboxes("Subtitle", options, checked=(1, 3))
        )
        self.assertEqual(result, [1, 3])
        self.assertIn("1. [x] A", shown)
        self.assertIn("2. B", shown)
        self.assertNotIn("[x] B", shown)
        self.assertIn("3. [x] C", shown)
        self.assertIn("Enter tanpa mengetik: tetap", shown)

    def test_a_checkbox_list_takes_numbers_ranges_and_none(self):
        options = [("A", 1), ("B", 2), ("C", 3)]
        for answer, expected in (("2", [2]), ("1,3", [1, 3]), ("1-2", [1, 2]), ("none", [])):
            result, _ = self.ask(
                self.screen(), [answer], lambda s: s.checkboxes("Subtitle", options, checked=(1, 3))
            )
            self.assertEqual(result, expected, answer)

    def test_a_checkbox_list_without_an_earlier_answer_does_not_offer_to_keep_it(self):
        options = [("A", 1), ("B", 2)]
        result, shown = self.ask(self.screen(), ["2"], lambda s: s.checkboxes("Subtitle", options))
        self.assertEqual(result, [2])
        self.assertNotIn("[x]", shown)
        self.assertNotIn("tetap", shown)

    def test_end_of_input_cancels_like_q_in_every_numbered_prompt(self):
        screen = self.screen()
        for call in (
            lambda s: s.choose("Pilih", [("satu", 1), ("dua", 2)]),
            lambda s: s.text("Link:"),
            lambda s: s.checkboxes("Pilih", [("satu", 1)]),
        ):
            with (
                patch.object(ui.sys.stdin, "isatty", return_value=True, create=True),
                patch("builtins.input", side_effect=EOFError),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(ui.Cancelled):
                    call(screen)

    def test_q_cancels_and_a_wrong_number_asks_again(self):
        screen = self.screen()
        result, shown = self.ask(
            screen, ["9", "x", "2"], lambda s: s.choose("Pilih", [("satu", 1), ("dua", 2)])
        )
        self.assertEqual(result, 2)
        self.assertEqual(shown.count("Pilihan tidak valid"), 2)
        with self.assertRaises(ui.Cancelled):
            self.ask(screen, ["q"], lambda s: s.choose("Pilih", [("satu", 1)]))

    def test_menus_are_numbered_on_windows_only_when_stdout_is_not_a_console(self):
        def asked(answer):
            calls = []

            def console():
                calls.append(1)
                return answer

            return console, calls

        console, calls = asked(False)
        self.assertTrue(ui.menus_numbered(False, True, console))
        console, calls = asked(True)
        self.assertFalse(ui.menus_numbered(False, True, console))
        # Elsewhere stdout is never asked: a redirected stdout does not change the menus.
        console, calls = asked(False)
        self.assertFalse(ui.menus_numbered(False, False, console))
        self.assertEqual(calls, [])
        self.assertTrue(ui.menus_numbered(True, False, console))


class WindowsConsole(unittest.TestCase):
    """The Windows rules, checked on every system with the console API replaced."""

    def switch(self, results, windows=True):
        calls = []

        def enable(api, w, number):
            calls.append(number)
            return results[number]

        with (
            patch.object(compat, "_console_api", return_value=(None, None)),
            patch.object(compat, "_enable_vt", side_effect=enable),
        ):
            return compat.configure_terminal(windows=windows), calls

    def test_stderr_alone_decides_whether_the_display_takes_escape_sequences(self):
        # stdout redirected to a file or NUL, stderr a console: the display stays.
        self.assertEqual(self.switch({-11: False, -12: True}), (True, [-11, -12]))
        self.assertEqual(self.switch({-11: True, -12: True}), (True, [-11, -12]))
        # stderr is what is not a console: the display is plain whatever stdout is.
        self.assertEqual(self.switch({-11: True, -12: False}), (False, [-11, -12]))
        self.assertEqual(self.switch({-11: False, -12: False}), (False, [-11, -12]))

    def test_other_systems_need_no_switch(self):
        self.assertEqual(self.switch({}, windows=False), (True, []))

    def test_stdout_is_a_console_only_when_the_console_says_so(self):
        class Api:
            def __init__(self, answer):
                self.answer, self.asked = answer, 0

            def GetStdHandle(self, number):
                return number

            def GetConsoleMode(self, handle, mode):
                self.asked += 1
                return self.answer

        class Types:
            DWORD = ctypes.c_ulong

        for answer in (True, False):
            api = Api(answer)
            with (
                patch.object(ui.sys.stdout, "isatty", return_value=True, create=True),
                patch.object(compat, "_console_api", return_value=(api, Types)),
            ):
                # isatty() says yes for NUL as well; the console is the one asked.
                self.assertEqual(compat.stdout_is_console(windows=True), answer)
            self.assertEqual(api.asked, 1)

    def test_a_stdout_that_is_not_a_terminal_is_never_a_console(self):
        with patch.object(ui.sys.stdout, "isatty", return_value=False, create=True):
            self.assertFalse(compat.stdout_is_console(windows=True))
            self.assertFalse(compat.stdout_is_console(windows=False))

    def test_the_ui_reports_the_display_not_stdout(self):
        def build(windows=None, vt=True, console=False):
            # `windows` is the Windows version tuple or None; `console` is whether stdout is a
            # console. Both are set here, so the same test checks every rule on every system.
            fake = io.StringIO()
            fake.isatty = lambda: True
            env = {"TERM": "xterm-256color"}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(ui, "configure_terminal", return_value=vt),
                patch.object(ui, "windows_build", return_value=windows),
                patch.object(ui, "stdout_is_console", return_value=console),
                patch.object(ui.sys, "stderr", fake),
                patch.object(ui.sys.stdout, "isatty", return_value=False, create=True),
            ):
                return ui.UI(False)

        # POSIX: stdout does not matter; only Windows draws menus through it.
        redirected = build()
        self.assertFalse(redirected.plain)
        self.assertFalse(redirected.numbered)
        self.assertTrue(build(vt=False).plain)
        # Windows: a redirected stdout keeps the panel and the colours, and numbers the menus.
        modern = (10, 0, 19045)
        redirected = build(windows=modern)
        self.assertFalse(redirected.plain)
        self.assertTrue(redirected.numbered)
        self.assertFalse(build(windows=modern, console=True).numbered)
        screen = build(windows=modern, vt=False)
        self.assertTrue(screen.plain)
        self.assertTrue(screen.numbered)


@unittest.skipIf(os.name == "nt", "ConPTY and Ctrl+D behave differently; see the Windows tests")
class Terminals(unittest.TestCase):
    def run_preview(self, extra=None):
        command = [sys.executable, str(BASE / "app.py"), "preview"]
        with Terminal(command, BASE, extra=extra) as term:
            result = ""
            import time

            deadline = time.monotonic() + 30
            while term.running() and time.monotonic() < deadline:
                result += term.read(0.1)
            result += term.drain()
            self.assertEqual(term.wait(), 0, result)
            return result

    def test_motion_on_draws_the_spinner_and_motion_off_draws_none(self):
        moving = self.run_preview({"YTDL_MOTION": "on"})
        self.assertTrue(any(c in moving for c in BRAILLE), "no spinner frame was drawn")
        still = self.run_preview({"YTDL_MOTION": "off"})
        self.assertFalse(any(c in still for c in BRAILLE), "a spinner was drawn without motion")
        self.assertIn("█", still)

    def test_the_theme_changes_the_colours_that_reach_the_terminal(self):
        dark = self.run_preview({"YTDL_THEME": "dark"})
        light = self.run_preview({"YTDL_THEME": "light"})
        self.assertIn("\x1b[38;2;" + rgb(ui.PALETTES["dark"]["blue"]) + "m", dark)
        self.assertNotIn("\x1b[38;2;" + rgb(ui.PALETTES["dark"]["blue"]) + "m", light)
        self.assertIn("\x1b[38;2;" + rgb(ui.PALETTES["light"]["blue"]) + "m", light)

    def test_ctrl_d_at_a_plain_prompt_stops_cleanly_with_130(self):
        import time

        command = [sys.executable, str(BASE / "app.py"), "--plain"]
        with Terminal(command, BASE) as term:
            result = ""
            deadline = time.monotonic() + 20
            while "Tempel link" not in result and time.monotonic() < deadline:
                result += term.read(0.1)
            self.assertIn("Tempel link", result)
            term.write("\x04")
            deadline = time.monotonic() + 20
            while term.running() and time.monotonic() < deadline:
                result += term.read(0.1)
            result += term.drain()
            self.assertEqual(term.wait(), 130, result)
        self.assertIn("Dihentikan", result)
        self.assertNotIn("GAGAL", result)


class SetupSpinner(unittest.TestCase):
    """The setup spinner runs before any dependency exists, so it writes its own escapes."""

    class Stream:
        def __init__(self, encoding):
            self.encoding = encoding

    def style(self, environ, encoding="utf-8", windows=False):
        import maintenance

        return maintenance.spinner_style(environ, self.Stream(encoding), windows)

    def test_a_modern_terminal_gets_braille_and_the_two_colours(self):
        frames, accent, text, reset = self.style({"COLORTERM": "truecolor"})
        self.assertEqual(frames, BRAILLE)
        self.assertEqual(accent, "\x1b[38;2;255;194;212m")
        self.assertEqual(text, "\x1b[38;2;168;216;240m")
        self.assertEqual(reset, "\x1b[0m")

    def test_the_light_theme_uses_its_own_shades(self):
        _, accent, text, _ = self.style({"COLORTERM": "truecolor", "YTDL_THEME": "light"})
        self.assertEqual(accent, "\x1b[38;2;201;32;95m")
        self.assertEqual(text, "\x1b[38;2;11;111;164m")

    def test_without_truecolor_the_256_colour_codes_are_used(self):
        _, accent, text, _ = self.style({"TERM": "xterm-256color"})
        self.assertTrue(accent.startswith("\x1b[38;5;") and text.startswith("\x1b[38;5;"))

    def test_no_colour_keeps_the_frames_and_drops_the_colour(self):
        self.assertEqual(self.style({"NO_COLOR": "1"}), (BRAILLE, "", "", ""))

    def test_a_legacy_console_or_a_non_utf8_stream_keeps_the_ascii_spinner(self):
        self.assertEqual(self.style({}, windows=True), ("|/-\\", "", "", ""))
        self.assertEqual(self.style({}, encoding="cp1252"), ("|/-\\", "", "", ""))
        # Windows Terminal is a modern terminal.
        self.assertEqual(self.style({"WT_SESSION": "x"}, windows=True)[0], BRAILLE)

    def test_the_spinner_is_silent_without_a_terminal_and_names_the_step_without_motion(self):
        import maintenance

        out = io.StringIO()
        out.isatty = lambda: False
        with patch.object(maintenance.sys, "stderr", out):
            with maintenance.working("Memasang"):
                pass
        self.assertEqual(out.getvalue(), "")
        out = io.StringIO()
        out.isatty = lambda: True
        with (
            patch.object(maintenance.sys, "stderr", out),
            patch.dict(os.environ, {"YTDL_MOTION": "off", "TERM": "xterm"}),
        ):
            with maintenance.working("Memasang"):
                pass
        self.assertEqual(out.getvalue(), "Memasang\n")


class Subprocess(unittest.TestCase):
    def test_the_tools_import_cleanly_in_a_fresh_interpreter(self):
        result = subprocess.run(
            [sys.executable, "-c", "import ui, app, maintenance; print(ui.THEME)"],
            cwd=BASE,
            capture_output=True,
            text=True,
            env={**os.environ, "YTDL_THEME": "light"},
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "light")


if __name__ == "__main__":
    unittest.main(verbosity=2)
