"""Menus and bounded terminal display; no downloader output parsing."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import groupby
import math
import os
import sys
import time
from pathlib import Path
from rich.console import Console, Group
from rich.live import Live
from rich.measure import Measurement
from rich.padding import Padding
from rich.progress import Progress, ProgressColumn, TextColumn
from rich.table import Table, Column
from rich.text import Text
import questionary
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth
from compat import configure_terminal, stdout_is_console, windows_build
from model import MISSING_REASONS, codec_name

# Three colours carry the look: pink for what you chose and for success, sky blue for what is
# going on (labels, the item bar, hints), red only for what failed. Grey is for secondary text and
# the empty track of a bar. Each theme has a set tuned for its background: the text colours reach
# 4.5:1 against PALETTE_GROUND, and the bar colours 3:1, the least for something that is not text
# (the tests check both).
#
# The file bar is the one place that mixes colours: it is pink at its tail and warms to a scarlet of
# its own (bar_red) at its leading edge. That scarlet is a different shade from the red of a
# failure on purpose, so a running bar is never mistaken for a failed one. bar_pink is the pink
# the bar starts from: in the light theme the text pink is too dark to read as pink in a bar.
PALETTES = {
    "dark": {
        "pink": "#FFC2D4",
        "blue": "#A8D8F0",
        "red": "#FF5470",
        "dim": "#94A3B8",
        "track": "#334155",
        "bar_pink": "#FFC2D4",
        "bar_red": "#FF4433",
    },
    "light": {
        "pink": "#C9205F",
        "blue": "#0B6FA4",
        "red": "#D1122F",
        "dim": "#566577",
        "track": "#CBD5E1",
        "bar_pink": "#CC3D7B",
        "bar_red": "#D6361F",
    },
}
PALETTE_GROUND = {"dark": "#1E1E1E", "light": "#EEEEEE"}


def detect_theme(environ):
    """YTDL_THEME=dark or light, or auto (the default). Auto reads COLORFGBG ("fg;bg", set by
    some terminals): a background of 7 or 15 is a light one. Anything else is taken as dark."""
    choice = (environ.get("YTDL_THEME") or "auto").strip().lower()
    if choice in PALETTES:
        return choice
    background = (environ.get("COLORFGBG") or "").rpartition(";")[2].strip()
    return "light" if background in ("7", "15") else "dark"


THEME = detect_theme(os.environ)
PINK, BLUE, RED, DIM, EMPTY, BAR_PINK, BAR_RED = (
    PALETTES[THEME][name] for name in ("pink", "blue", "red", "dim", "track", "bar_pink", "bar_red")
)

_TERM_COLORS = {"kitty": "256", "256color": "256", "16color": "standard"}


def detect_color_system(environ, vt=True, windows=None):
    """The colour depth of the display, "truecolor", "256" or "standard", from one rule for every
    system. The display is stderr, so `vt` is whether stderr takes escape sequences.

    POSIX reads COLORTERM and TERM. Windows ignores both: a console with VT processing shows 24
    bits from build 10.0.15063 on (`windows` is the version tuple), 256 colours before."""
    if windows is not None:
        if not vt:
            return None
        return "truecolor" if tuple(windows) >= (10, 0, 15063) else "256"
    if (environ.get("COLORTERM") or "").strip().lower() in ("truecolor", "24bit"):
        return "truecolor"
    term = (environ.get("TERM") or "").strip().lower()
    return _TERM_COLORS.get(term.rpartition("-")[2], "standard")


def menus_numbered(plain, windows, stdout_is_console):
    """Whether menus are numbered lines instead of arrow menus: always in plain output, and on
    Windows when stdout is not a console (prompt_toolkit draws through the console of stdout).
    `stdout_is_console` is a function, asked only on Windows."""
    return bool(plain or (windows and not stdout_is_console()))


def motion_enabled(environ, plain):
    """Animation is part of the live display only: never in plain output, and YTDL_MOTION=off
    turns it off for anyone who prefers a still screen."""
    return not plain and (environ.get("YTDL_MOTION") or "auto").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
    )


def mix(first, second, weight):
    """The colour between two #RRGGBB colours; weight 0 is the first, 1 the second."""
    weight = min(1.0, max(0.0, weight))
    a, b = (tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in (first, second))
    return "#" + "".join(f"{round(x + (y - x) * weight):02X}" for x, y in zip(a, b, strict=True))


def pulse(moment, first, second, period=1.6):
    """A colour that breathes between two over `period` seconds."""
    return mix(first, second, (1 - math.cos(2 * math.pi * moment / period)) / 2)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spin(moment):
    return SPINNER[int(moment * 12) % len(SPINNER)]


def shimmer(text, moment, base, peak, still=False):
    """A message with a bright band that sweeps over it, one pass in about two seconds."""
    line = Text(no_wrap=True, overflow="ellipsis")
    if still:
        line.append(text, base)
        return line
    reach = 5.0
    position = (moment * 16) % (len(text) + 2 * reach) - reach
    for index, char in enumerate(text):
        line.append(char, mix(base, peak, 1 - abs(index - position) / reach))
    return line


# Redraws per second of every live panel. The worker reports progress only about four times a
# second; Smoother fills the frames in between, so a higher rate costs no extra events.
FPS = 12
# The unknown-size indicator moves one cell per frame.
BAR_STEPS_PER_SECOND = 12
# Rows of the playlist list, and the stages an item goes through (see model.phases).
WINDOW_ROWS = 6
PHASE_LABELS = {
    "metadata": "Metadata",
    "video": "Video",
    "audio": "Audio",
    "subtitle": "Subtitle",
    "finish": "Gabung",
}


class AnimationClock:
    """Animation time advances only while a file is actively downloading."""

    def __init__(self):
        self.elapsed = 0.0
        self.resumed_at = None

    def __call__(self):
        return self.elapsed + (
            time.monotonic() - self.resumed_at if self.resumed_at is not None else 0
        )

    @property
    def running(self):
        return self.resumed_at is not None

    def active(self, value, reset=False):
        self.elapsed = 0.0 if reset else self()
        self.resumed_at = time.monotonic() if value else None


class Smoother:
    """A byte counter that moves between worker reports instead of jumping at each one.

    Between two reports the counter keeps advancing at the last reported speed, but for no longer
    than HORIZON seconds, so a stalled connection does not keep a bar crawling. It never moves
    backwards within one file: a report below the estimate holds the bar until the real count
    catches up. The state is one immutable tuple that is replaced as a whole, so the thread that
    redraws the panel reads a consistent snapshot without a lock."""

    HORIZON = 0.3

    def __init__(self, now=time.monotonic):
        self.now = now
        self.state = (0.0, 0.0, now(), 0.0)

    def value(self):
        base, speed, stamp, floor = self.state
        ahead = min(max(0.0, self.now() - stamp), self.HORIZON)
        return max(floor, base + speed * ahead)

    def update(self, downloaded, speed):
        """A report from the worker. What is on screen right now becomes the new floor."""
        self.state = (float(downloaded), max(0.0, float(speed or 0)), self.now(), self.value())

    def freeze(self):
        """Stop where the bar is, for a pause that has no byte counter (waiting, merging)."""
        shown = self.value()
        self.state = (shown, 0.0, self.now(), shown)

    def finish(self, total):
        self.state = (float(total), 0.0, self.now(), float(total))

    def reset(self):
        self.state = (0.0, 0.0, self.now(), 0.0)


# The fill of the file bar breathes between its two colours once in BREATH_PERIOD seconds, and the
# tail of the fill gets BREATH_DEPTH of the way to the head colour at the top of a breath (the
# head is already there). Below 1 the gradient never goes away, even at the top of a breath.
BREATH_PERIOD = 1.6
BREATH_DEPTH = 0.6
# The gradient is spread over at least this many cells. A bar that has just begun is then still
# mostly its own colour, instead of one red cell that turns pink as soon as a second one appears.
HEAT_RAMP = 8


@dataclass(frozen=True)
class Heat:
    """A bar that warms from its own colour at the tail to `head` at its leading edge.

    `smooth` is a true gradient, for terminals with 256 or more colours. Without it (16 colours,
    where a gradient would fall into a few unrelated colours) only the last two cells take the
    head colour. `breathe` makes the whole fill pulse toward the head colour and back."""

    head: str
    smooth: bool = True
    breathe: bool = False


def heat_colors(count, tail, heat, moment=None):
    """The colour of each of the `count` filled cells, tail first.

    The cells are spread between the tail colour and the head colour, so the leading cell is the
    warmest; it is the head colour itself once the bar is HEAT_RAMP + 1 cells long. With a `moment`
    (seconds) and a breathing `heat`, the fill also swells toward the head colour and back, so the
    gradient moves as a whole instead of travelling along the bar."""
    if not heat.smooth:
        return [heat.head if index >= max(count - 2, 2) else tail for index in range(count)]
    swell = 0.0
    if heat.breathe and moment is not None:
        swell = BREATH_DEPTH * (1 - math.cos(2 * math.pi * moment / BREATH_PERIOD)) / 2
    reach = max(count - 1, HEAT_RAMP)
    return [mix(tail, heat.head, (i / reach) + (1 - i / reach) * swell) for i in range(count)]


class SolidBar:
    """A one-cell-high bar measured against the space Rich allocates to it."""

    def __init__(self, completed, total, color, animation_time=0, indicator=False, heat=None):
        self.completed, self.total, self.color = completed, total, color
        self.animation_time, self.indicator, self.heat = animation_time, indicator, heat

    def text(self, width):
        width = max(0, width)
        bar = Text(no_wrap=True, overflow="crop")
        if self.total is None:
            count = min(3, width) if self.indicator else 0
            travel = width - count
            step = int(self.animation_time * BAR_STEPS_PER_SECOND) % (2 * travel) if travel else 0
            start = min(step, 2 * travel - step) if travel else 0
            bar.append("░" * start, EMPTY)
            cells = [self.color] * count
            if self.heat and count == 3:
                cells[1] = self.heat.head  # the block is lit from its centre
            for cell in cells:
                bar.append("█", cell)
            bar.append("░" * (width - start - count), EMPTY)
            return bar
        ratio = min(1.0, max(0.0, self.completed / self.total)) if self.total > 0 else 0
        eighths = int(ratio * width * 8)
        full, fraction = divmod(eighths, 8)
        cells = full + bool(fraction)
        colors = (
            heat_colors(cells, self.color, self.heat, self.animation_time)
            if self.heat
            else [self.color] * cells
        )
        for color, run in groupby(colors[:full]):
            bar.append("█" * len(list(run)), color)
        if fraction:
            # A dark background keeps the fractional cell aligned with the track.
            bar.append("▏▎▍▌▋▊▉"[fraction - 1], f"{colors[-1]} on {EMPTY}")
        bar.append("░" * (width - cells), EMPTY)
        return bar

    def __rich_measure__(self, console, options):
        return Measurement(1, max(1, options.max_width))

    def __rich_console__(self, console, options):
        yield self.text(options.max_width)


class SolidBarColumn(ProgressColumn):
    """`value` replaces the task's own counter, e.g. with Smoother.value for the file bar.

    A `heat` that breathes does so only while the clock runs, that is, while bytes arrive. A bar
    that waits, merges, or has failed rests on its plain gradient instead of freezing at some
    point of a breath."""

    def __init__(self, color, clock=None, value=None, heat=None):
        super().__init__(table_column=Column(ratio=1, no_wrap=True))
        self.color, self.clock, self.value, self.heat = color, clock, value, heat

    def render(self, task):
        heat = self.heat
        if heat and heat.breathe and not (self.clock and self.clock.running):
            heat = replace(heat, breathe=False)
        return SolidBar(
            task.completed if self.value is None else self.value(),
            task.total,
            self.color,
            self.clock() if self.clock else 0,
            task.fields.get("indicator", False),
            heat,
        )


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}j {minutes}m" if hours else f"{minutes}m {seconds}d" if minutes else f"{seconds}d"
    )


def clock(seconds):
    """Length of a video as 3:42 or 1:02:03."""
    seconds = max(0, int(seconds or 0))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def size(number):
    number = max(0, number or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.1f} {unit}" if unit != "B" else f"{number:.0f} B"
        number /= 1024


def tidy(path):
    """A path for display: the home folder is shortened to ~ on POSIX systems."""
    text = str(path)
    home = str(Path.home())
    if os.name != "nt" and home not in ("", "/") and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home) :]
    return text


class Busy:
    """One line for work that has no byte counter: a spinner, the message with a light that
    sweeps over it, a small bar when the number of steps is known, and the time once it takes
    more than a few seconds. Without motion the same line stands still."""

    SLOW = 3

    def __init__(self, message, motion=True, now=time.monotonic):
        self.message, self.step = message, None
        self.motion, self.now = motion, now
        self.started = now()

    def set(self, message, step=None):
        self.message, self.step = message, step

    def __rich_console__(self, console, options):
        moment = self.now() - self.started
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append((spin(moment) if self.motion else "●") + " ", PINK)
        line.append_text(shimmer(self.message, moment, BLUE, PINK, still=not self.motion))
        if self.step:
            done, total = self.step
            line.append("  ")
            line.append_text(
                SolidBar(done, total, PINK).text(min(16, max(4, options.max_width // 4)))
            )
        if moment >= self.SLOW:
            line.append(" · " + duration(moment), DIM)
        yield line


class Cancelled(Exception):
    pass


GLYPHS = {
    "step": "◆",
    "pointer": "›",
    "ok": "✓",
    "warn": "!",
    "fail": "✗",
    "skip": "–",
    "stop": "■",
    "arrow": "→",
    "pending": "○",
    "active": "●",
}
# --plain output stays 7-bit so logs and legacy consoles never see box or symbol characters.
PLAIN_GLYPHS = {
    "step": "*",
    "pointer": ">",
    "ok": "+",
    "warn": "!",
    "fail": "x",
    "skip": "-",
    "stop": "#",
    "arrow": "->",
    "pending": ".",
    "active": "*",
}
# How a playlist row and a stage are drawn: glyph colour, then the style of the name next to it.
ROW_COLORS = {"pending": DIM, "active": BLUE, "ok": PINK, "warn": BLUE, "fail": RED, "skip": DIM}
ROW_TITLES = {
    "pending": DIM,
    "active": f"bold {BLUE}",
    "ok": DIM,
    "warn": BLUE,
    "fail": RED,
    "skip": DIM,
}
MAX_REPORT_ROWS = 30
# The line under a list when Enter was pressed with nothing ticked.
EMPTY_CHOICE = (
    "Belum ada yang dicentang · Space mencentang baris yang disorot, lalu Enter · Ctrl+C batal"
)


def missing_note(missing, limit=3):
    """The subtitles that were not saved and why, for the card and the report: "Indonesian
    (dibatasi · 429), English (tidak tersedia) +2 lagi". A record from before the reasons were kept
    gives the names alone."""
    parts = []
    for track in missing[:limit]:
        name = str(track.get("name") or track.get("key") or "?")
        reason = MISSING_REASONS.get(track.get("reason"))
        parts.append(f"{name} ({reason})" if reason else name)
    text = ", ".join(parts)
    if len(missing) > limit:
        text += f" +{len(missing) - limit} lagi"
    return text


def incomplete(result):
    """ "subtitle belum lengkap: ..." with the tracks that are missing from a result, if it says."""
    note = missing_note(result.get("missing") or [])
    return "subtitle belum lengkap" + (": " + note if note else "")


def picked_summary(names):
    """What the history line says of a checkbox answer: the names, or how many when there are many."""
    if not names:
        return "tidak ada"
    return ", ".join(names) if len(names) <= 3 else f"{len(names)} dipilih"


def subtitle_figures(result):
    """For a result of the subtitle-only mode: how many .srt files it holds and their size."""
    saved = result.get("subtitles") or []
    total = 0
    for track in saved:
        try:
            total += Path(track["path"]).stat().st_size
        except OSError:
            pass
    return len(saved), total


def without_bulk_keys(question):
    """Make Ctrl+A (tick every row) and Tab (invert) do nothing in a questionary checkbox.

    questionary binds them itself and offers no switch to remove them. A binding added later wins
    over an earlier one for the same key."""
    bindings = question.application.key_bindings
    for key in (Keys.ControlA, Keys.ControlI):
        bindings.add(key, eager=True)(lambda event: None)


class UI:
    def __init__(self, plain=False):
        vt = configure_terminal()
        # NO_COLOR counts only when it has a value (no-color.org): an empty one means "not set".
        self.no_color = bool(os.environ.get("NO_COLOR"))
        # All of this tool's display is written to stderr, so stderr decides how it looks. A
        # stdout that is captured or redirected (the saved paths go there) must not turn a
        # terminal that can draw the panel into plain text.
        self.plain = plain or not vt or not sys.stderr.isatty() or os.environ.get("TERM") == "dumb"
        # Menus are the one part that also needs stdout on Windows (prompt_toolkit draws through
        # the console of stdout). Behind a redirected stdout they are numbered lines instead,
        # and the panel and the colours stay.
        windows = windows_build()  # None on every other system; the tests set it to try Windows
        self.numbered = menus_numbered(self.plain, windows is not None, stdout_is_console)
        self.motion = motion_enabled(os.environ, self.plain)
        # One decision for Rich and prompt_toolkit alike, the same on every system; both step the
        # palette down to what the terminal shows, so neither guesses on its own.
        system = None if self.plain else detect_color_system(os.environ, vt, windows)
        self.console = Console(
            stderr=True,
            force_terminal=not self.plain,
            legacy_windows=False,
            color_system=system,
            no_color=self.no_color,
            highlight=False,
        )
        # A gradient needs more than the 16 colours of a basic terminal to look like one.
        self.gradient = system in ("truecolor", "256")
        self.depth = (
            ColorDepth.DEPTH_1_BIT
            if self.no_color
            else {"truecolor": ColorDepth.DEPTH_24_BIT, "256": ColorDepth.DEPTH_8_BIT}.get(
                system, ColorDepth.DEPTH_4_BIT if system else None
            )
        )
        self.glyph = PLAIN_GLYPHS if self.plain else GLYPHS
        self._reported_log = None
        self.style = (
            questionary.Style(
                [
                    ("qmark", PINK),
                    ("question", BLUE),
                    ("answer", PINK),
                    ("pointer", BLUE),
                    ("highlighted", BLUE),
                    ("selected", PINK),
                    ("instruction", DIM),
                    # What is typed into a list to search it, and the line under the list when
                    # Enter was refused. questionary's own colours (green, red on white) are not
                    # the palette's; refusing is not a failure of the download, so it is blue.
                    ("search_success", BLUE),
                    ("search_none", RED),
                    ("validation-toolbar", BLUE),
                ]
            )
            if not self.no_color
            else questionary.Style([])
        )

    # Output ---------------------------------------------------------------------------------

    def say(self, message, color=None):
        """Print a line. A rich Text keeps its own styling; --plain prints its plain content."""
        if self.plain:
            sys.stderr.write((message.plain if isinstance(message, Text) else str(message)) + "\n")
            sys.stderr.flush()
        elif isinstance(message, Text):
            message.overflow = "fold"
            self.console.print(message)
        else:
            self.console.print(Text(str(message), style=color, overflow="fold"))

    def banner(self, version, animate=False):
        """The title line. In an interactive run it is typed out with a light on the newest
        letter; the finished line is what stays on the screen."""
        title = "YouTube Downloader"
        line = Text()
        line.append(title, style=f"bold {PINK}")
        line.append("  " + version, style=DIM)
        if animate and self.motion and not self.numbered:
            with Live(
                console=self.console,
                transient=True,
                refresh_per_second=FPS,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                for shown in range(1, len(title) + 1):
                    typed = Text(no_wrap=True)
                    typed.append(title[: shown - 1], style=f"bold {PINK}")
                    typed.append(title[shown - 1], style=f"bold {BLUE}")
                    live.update(typed, refresh=True)
                    time.sleep(0.022)
        self.say(line)

    def error(self, message, log=None, command=None):
        """The failure line, the log if it was not already shown, and the exact command that
        carries on (a copy-paste line, so nobody has to remember the syntax)."""
        if self.plain:
            self.say("! GAGAL · " + str(message))
        else:
            line = Text()
            line.append(self.glyph["fail"] + " Gagal", style=f"bold {RED}")
            line.append(" · " + str(message), style=RED)
            self.say(line)
        if log and str(log) != self._reported_log:
            self.facts([("Log", tidy(log))])
        if command:
            self.say(Text("    " + command, style=BLUE) if not self.plain else "  " + command)

    def facts(self, rows):
        """Technical references (folder, job, log) as dim label/value pairs."""
        if self.plain:
            for label, value in rows:
                self.say(f"{label}: {value}")
            return
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=DIM, no_wrap=True, min_width=6)
        grid.add_column(overflow="fold")
        for label, value in rows:
            grid.add_row(Text(label, style=DIM), Text(str(value)))
        self.console.print(Padding(grid, (0, 0, 0, 2)))

    def table(self, headers, rows):
        rows = [tuple(str(value) for value in row) for row in rows]
        if self.plain:
            self.say(" | ".join(headers), BLUE)
            for row in rows:
                self.say(" | ".join(row))
            return
        if len(rows) == 1:
            # One item reads better as a card than as a one-row table that wraps.
            grid = Table.grid(padding=(0, 2))
            grid.add_column(no_wrap=True)
            grid.add_column(overflow="fold")
            for header, value in zip(headers, rows[0], strict=True):
                grid.add_row(Text(header, style=BLUE), Text(value))
            self.console.print(Padding(grid, (0, 0, 0, 2)))
            return
        table = Table(box=None, show_edge=False, padding=(0, 1), pad_edge=False, expand=True)
        ratios = {2: (2, 3), 3: (3, 3, 4)}.get(len(headers), (1,) * len(headers))
        for index, header in enumerate(headers):
            # The first column (a video title) is cut with an ellipsis; the others wrap.
            table.add_column(
                header,
                header_style=BLUE,
                overflow="ellipsis" if index == 0 else "fold",
                no_wrap=index == 0,
                ratio=ratios[index],
            )
        for row in rows:
            table.add_row(*(Text(value) for value in row))
        self.console.print(table)

    @contextmanager
    def activity(self, message):
        """Work with no byte counter (fetching metadata, checking for a release). It yields
        update(text, step=None): a new message and, when the number of steps is known, the pair
        (done, total) that draws a small bar. Plain output prints each message as a line."""
        if self.plain:
            self.say(message)
            yield lambda text, step=None: self.say(text)
            return
        busy = Busy(message, self.motion)
        with Live(
            busy,
            console=self.console,
            transient=True,
            refresh_per_second=FPS if self.motion else 4,
            redirect_stdout=False,
            redirect_stderr=False,
        ):
            yield busy.set

    def card(self, item, root):
        """A single finished video as a card: title, what it is, and where it is."""
        result, choice = item.get("result"), item.get("choice")
        if item.get("status") not in ("complete", "partial") or not result or not choice:
            return None
        path = Path(result["path"])
        try:
            shown = path.relative_to(root).as_posix()
        except ValueError:
            shown = str(path)
        parts = []
        if choice.get("mode") == "subtitle":
            # A set of files in one folder, not a file: no codec, no size of its own.
            count, total = subtitle_figures(result)
            shown += "/"
            parts = ["SRT", f"{count} subtitle", size(total)]
        else:
            if choice.get("mode") == "audio":
                parts.append("MP3" if choice.get("audio_format") == "mp3" else "audio asli")
                if choice.get("audio_format") != "mp3" and choice.get("acodec"):
                    parts.append(codec_name(choice["acodec"]))
            else:
                if choice.get("actual_quality"):
                    parts.append(f"{choice['actual_quality']}p")
                codecs = codec_name(choice.get("vcodec"))
                if choice.get("mode") == "video-audio":
                    codecs += " + " + codec_name(choice.get("acodec"))
                parts.append(codecs)
            try:
                weight = size(path.stat().st_size)
            except OSError:
                weight = "file tidak ditemukan"
            parts += [path.suffix[1:].upper(), weight]
        title = Text(str(item.get("title") or item.get("id") or shown), style="bold")
        if item["status"] == "partial":
            title.append(" · " + incomplete(result), style=DIM)
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(overflow="fold")
        kind = "warn" if item["status"] == "partial" else "ok"
        grid.add_row(
            Text(self.glyph[kind], style=BLUE if kind == "warn" else PINK),
            Group(title, Text(" · ".join(parts), style=DIM), Text(shown, style=DIM)),
        )
        return grid

    def report(self, job, log):
        """What was saved where, then folder, job and log, once the batch has ended."""
        root = Path(job["output"])
        card = (
            self.card(job["items"][0], root) if len(job["items"]) == 1 and not self.plain else None
        )
        rows = []
        for item in job["items"]:
            status = item.get("status")
            if status in ("complete", "partial") and item.get("result"):
                path = Path(item["result"]["path"])
                try:
                    shown = path.relative_to(root).as_posix()
                except ValueError:
                    shown = str(path)
                if item["result"].get("mode") == "subtitle":
                    count, total = subtitle_figures(item["result"])
                    shown = f"{item.get('title') or item.get('id') or shown}"
                    detail = f"{count} subtitle · {size(total)}"
                else:
                    try:
                        detail = size(path.stat().st_size)
                    except OSError:
                        detail = "file tidak ditemukan"
                if status == "partial":
                    detail += " · " + incomplete(item["result"])
                rows.append(("warn" if status == "partial" else "ok", shown, detail))
            elif status == "failed":
                reason = (item.get("error") or {}).get("message") or "gagal"
                rows.append(("fail", item["title"], reason))
            else:
                rows.append(("skip", item["title"], "belum diproses"))
        hidden = max(0, len(rows) - MAX_REPORT_ROWS)
        colors = {"ok": PINK, "warn": BLUE, "fail": RED, "skip": DIM}
        if rows and self.plain:
            for kind, name, detail in rows[:MAX_REPORT_ROWS]:
                self.say(f"[{self.glyph[kind]}] {name} ({detail})")
        elif card is not None:
            self.console.print(Padding(card, (1, 0, 1, 2)))
        elif rows:
            grid = Table.grid(padding=(0, 1))
            grid.add_column(width=1, no_wrap=True)
            grid.add_column(overflow="fold")
            for kind, name, detail in rows[:MAX_REPORT_ROWS]:
                line = Text(name)
                line.append(" · " + detail, style=DIM)
                grid.add_row(Text(self.glyph[kind], style=colors[kind]), line)
            self.console.print(Padding(grid, (1, 0, 1, 2)))
        if hidden:
            self.say(f"+{hidden} item lain; lihat file job untuk daftar lengkap.")
        self._reported_log = str(log)
        self.facts(
            [
                ("Folder", tidy(root)),
                ("Job", f"{job['id']} · {job['status']}"),
                ("Log", tidy(log)),
            ]
        )

    # Prompts --------------------------------------------------------------------------------

    def _answered(self, label, answer, step=None):
        """The line a finished prompt leaves behind, so the screen reads as a history."""
        if self.plain:
            prefix = f"[{step[0]}/{step[1]}] " if step else ""
            self.say(f"{prefix}{label}: {answer}")
            return
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(min_width=3, no_wrap=True)
        grid.add_column(min_width=15, no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(
            Text(self.glyph["step"], style=PINK),
            Text(f"{step[0]}/{step[1]}" if step else "", style=DIM),
            Text(label, style=BLUE),
            Text(str(answer), style=PINK),
        )
        self.console.print(grid)

    def answered(self, label, answer, step=None):
        """Leave the history line of a prompt that was answered without asking (a command-line
        flag, or --yes), so the screen reads the same as when it was asked."""
        self._answered(label, answer, step)

    def _message(self, title, step):
        return f"{step[0]}/{step[1]}  {title}" if step else title

    def _ask(self, question):
        # The live prompt is erased once answered; _answered() prints the compact line instead.
        question.application.erase_when_done = True
        result = question.unsafe_ask()
        if result is None:
            raise Cancelled()
        return result

    @staticmethod
    def _read():
        """One typed line. Ctrl+D (end of input) cancels, the same as q."""
        try:
            return input().strip()
        except EOFError:
            raise Cancelled() from None

    @staticmethod
    def _index(options, value):
        for index, (_, candidate) in enumerate(options):
            if candidate == value and type(candidate) is type(value):
                return index
        return None

    def choose(self, title, options, default=None, step=None, label=None):
        if not sys.stdin.isatty():
            raise ValueError(
                "Menu membutuhkan input terminal. Gunakan terminal interaktif atau resume ID_JOB."
            )
        label = label or title
        if self.numbered:
            self.say(self._message(title, step), BLUE)
            for i, (text, _) in enumerate(options, 1):
                self.say(f"{i}. {text}")
            while True:
                self.say("Nomor pilihan (q untuk batal):")
                answer = self._read()
                if answer.lower() == "q":
                    raise Cancelled()
                if answer.isdigit() and 1 <= int(answer) <= len(options):
                    index = int(answer) - 1
                    break
                self.say("Pilihan tidak valid", RED)
        else:
            index = self._ask(
                questionary.select(
                    self._message(title, step),
                    choices=[
                        questionary.Choice(text, value=i) for i, (text, _) in enumerate(options)
                    ],
                    default=self._index(options, default),
                    qmark=self.glyph["step"],
                    pointer=self.glyph["pointer"],
                    instruction="↑↓ pilih · Enter lanjut · Ctrl+C batal",
                    style=self.style,
                    color_depth=self.depth,
                )
            )
        self._answered(label, options[index][0], step)
        return options[index][1]

    def text(self, title, default="", step=None, label=None):
        if not sys.stdin.isatty():
            raise ValueError("Masukkan link melalui terminal interaktif.")
        if self.numbered:
            self.say(self._message(title, step), BLUE)
            return self._read() or default
        result = self._ask(
            questionary.text(
                self._message(title, step),
                default=default,
                qmark=self.glyph["step"],
                style=self.style,
                color_depth=self.depth,
            )
        ).strip()
        self._answered(label or title, result, step)
        return result

    def checkboxes(
        self, title, options, checked=(), step=None, required=False, bulk=True, record=True
    ):
        """Pick any number of `options` (pairs of text and value); the values picked, in list order.

        Typing filters the list and never changes what is ticked: Space ticks the highlighted
        row. `required` refuses Enter while nothing is ticked, because an empty answer to a list
        that was asked for is a missed Space and not a choice; the numbered form still takes
        `none`. `bulk` keeps Ctrl+A (every row) and Tab (invert) for a list where that is a
        sensible thing to do; for a list of 150 languages it is a way to ask for all of them by
        accident. `record=False` leaves the history line to the caller, for a choice that
        is asked in more than one list."""
        if self.numbered:
            self.say(self._message(title, step), BLUE)
            kept = [value for _, value in options if value in checked]
            for i, (text, value) in enumerate(options, 1):
                self.say(f"{i}. {'[x] ' if value in kept else ''}{text}")
            from model import item_indices

            hint = "Nomor/rentang, all, atau none untuk tidak memilih"
            if kept:
                hint += "; Enter tanpa mengetik: tetap ([x])"
            while True:
                answer = self.text(hint + ":")
                if answer == "none":
                    return []
                if not answer and kept:
                    return kept
                try:
                    return [options[i][1] for i in item_indices(answer, len(options))]
                except ValueError as exc:
                    self.say(exc, RED)
        keys = "↑↓ pindah · Space centang · ketik untuk mencari"
        keys += " · Ctrl+A semua · Tab balik" if bulk else ""
        question = questionary.checkbox(
            self._message(title, step),
            choices=[
                questionary.Choice(text, value=i, checked=value in checked)
                for i, (text, value) in enumerate(options)
            ],
            qmark=self.glyph["step"],
            pointer=self.glyph["pointer"],
            style=self.style,
            instruction=keys + " · Enter lanjut · Ctrl+C batal",
            validate=(lambda picked: True if picked or not required else EMPTY_CHOICE),
            # Typing searches. Without it questionary reads `i` and `a` as "invert" and "select
            # all", and typing a language into a list of 150 ticks every one of them. The search
            # cannot share its letters with j/k, so those no longer move the pointer.
            use_search_filter=True,
            use_jk_keys=False,
            color_depth=self.depth,
        )
        if not bulk:
            without_bulk_keys(question)
        picked = self._ask(question)
        if record:
            self._answered(title, picked_summary([options[i][0] for i in picked]), step)
        return [options[i][1] for i in picked]


class Session:
    """The live panel of one batch.

    Worker events arrive on the thread that runs the batch, while Rich redraws the panel from its
    own refresh thread. Everything the panel draws from more than one value (the list of item
    states, the stage pipeline, the smoothed counter) is an immutable tuple that is replaced as
    a whole, never changed in place, so a redraw always sees a consistent snapshot."""

    def __init__(self, ui, total, items=None):
        self.ui = ui
        self.total = total
        # One video has no batch to count: no item bar, no tally, and one line for status and time.
        self.single = total == 1
        self.now = time.monotonic
        self.stream = "media"
        self.beat = 0
        self.beat_at = None
        self.done = 0
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.started = time.monotonic()
        self.clock = AnimationClock()
        self.smoother = Smoother()
        # One row per item of a playlist; see item_start() and item_end().
        self.titles = tuple(str(x.get("title") or x.get("id") or "") for x in items or ())
        self.kinds = ("pending",) * len(self.titles)
        # (stages of the current item, index of the current one, "run" | "ok" | "fail")
        self.pipeline = ((), 0, "run")
        self.message = "Menyiapkan"
        self.current = ""
        self.downloaded = 0
        self.byte_total = None
        self.speed = 0
        self.eta = None
        self.until = None
        # The item bar is sky blue. The file bar is pink at its tail and warms to scarlet at the
        # head; it breathes while bytes arrive, when the terminal can show it and motion is on.
        heat = Heat(BAR_RED, smooth=ui.gradient, breathe=ui.gradient and ui.motion)
        self.overall = Progress(
            TextColumn("Item"),
            SolidBarColumn(BLUE),
            TextColumn("{task.percentage:.0f}% · {task.completed:.0f}/{task.total:.0f}"),
            expand=True,
        )
        self.oid = self.overall.add_task("", total=total)
        self.file = Progress(
            TextColumn("File"),
            SolidBarColumn(BAR_PINK, self.clock, self.smoother.value, heat),
            TextColumn("{task.fields[label]}"),
            expand=True,
        )
        self.fid = self.file.add_task("", total=None, label="Menyiapkan", indicator=False)
        self.live = None
        self.last_plain = None
        # The timings that the downloads of this run reported, for the closing line.
        self.timed = []

    def __enter__(self):
        if not self.ui.plain:
            self.live = Live(
                self,
                console=self.ui.console,
                transient=True,
                refresh_per_second=FPS,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start()
        else:
            self.ui.say(f"Download · {self.total} item")
        return self

    def item_start(self, phases=()):
        """The next item begins: light its row and its first stage, and clear the file bar.

        `phases` is the plan from model.phases(); the worker then announces each stage."""
        self.mark(self.done, "active")
        self.pipeline = (tuple(phases), 0, "run")
        self.clock.active(False, reset=True)
        self.smoother.reset()
        self.current = ""
        self.downloaded = 0
        self.byte_total = None
        self.speed = 0
        self.eta = None
        self.until = None
        self.empty_file_bar(indicator=False)
        self.message = "Menyiapkan"

    def empty_file_bar(self, indicator):
        """Back to an empty bar of unknown size.

        Progress.reset() would do, but it replaces every field of the task, and the panel is
        redrawn from Rich's own thread: for a moment the label would be missing and the redraw
        would raise. Updating single fields never leaves the task incomplete."""
        self.file.update(self.fid, completed=0, indicator=indicator)
        self.file.tasks[0].total = None

    def mark(self, index, kind):
        if 0 <= index < len(self.kinds):
            self.kinds = self.kinds[:index] + (kind,) + self.kinds[index + 1 :]

    def advance(self, phase):
        """Move the pipeline to `phase`. Stages only move forward: one that is skipped (a stream
        that was already downloaded, subtitles that were already saved) counts as done."""
        plan, index, outcome = self.pipeline
        if phase in plan and plan.index(phase) > index:
            self.pipeline = (plan, plan.index(phase), outcome)

    def event(self, event):
        kind = event.get("type")
        if kind == "start":
            self.stream = event.get("stream") or "media"
            self.beat = 0
            self.beat_at = self.now()
            self.current = event.get("name", "")
            self.downloaded = 0
            self.byte_total = None
            self.speed = 0
            self.eta = None
            self.until = None
            self.clock.active(True, reset=True)
            self.smoother.reset()
            self.empty_file_bar(indicator=True)
            self.message = "Mengunduh " + event.get("stream", "media")
            self.advance(event.get("stream"))
        elif kind == "progress":
            self.clock.active(True)
            self.until = None
            self.message = "Mengunduh media"
            self.downloaded = event.get("downloaded") or 0
            self.byte_total = event.get("total") or None
            self.speed = event.get("speed") or 0
            self.eta = event.get("eta")
            self.smoother.update(self.downloaded, self.speed)
            self.file.update(self.fid, completed=self.downloaded, indicator=True)
            self.file.tasks[0].total = self.byte_total
            if self.ui.plain:
                self.heartbeat()
        elif kind == "file_done":
            self.downloaded = (
                event.get("downloaded") or event.get("total") or self.byte_total or self.downloaded
            )
            self.byte_total = event.get("total") or self.downloaded or None
            self.eta = None
            self.until = None
            self.clock.active(False)
            self.smoother.finish(max(1, self.downloaded))
            self.file.update(
                self.fid, total=max(1, self.downloaded), completed=max(1, self.downloaded)
            )
            self.message = "File sementara selesai"
        elif kind in ("stage", "retry", "wait", "failure", "notice"):
            self.clock.active(False)
            self.smoother.freeze()
            self.until = event.get("until") if kind == "wait" else None
            if kind == "stage":
                self.advance(event.get("phase"))
            self.message = event.get("message") or (
                "Mencoba kembali"
                if kind == "retry"
                else "! GAGAL"
                if kind == "failure"
                else "Menunggu"
            )
        if (
            self.ui.plain
            and kind in ("start", "stage", "retry", "wait", "failure", "notice")
            and self.message != self.last_plain
        ):
            self.ui.say(self.message, RED if kind == "failure" else None)
            self.last_plain = self.message

    def heartbeat(self):
        """Plain mode has no live bar, so a log would stay silent for the whole download. One
        line is written for every tenth of the file (every minute while the size is unknown),
        about ten lines per file at most, enough to see that it is alive and how fast."""
        total, got = self.byte_total, self.downloaded
        if total:
            step = min(10, int(got * 10 // total))
            if step < 1 or step <= self.beat:
                return
            self.beat = step
            line = f"  {self.stream} {int(got * 100 // total)}% · {size(got)}/{size(total)}"
        else:
            if self.now() - (self.beat_at or 0) < 60:
                return
            line = f"  {self.stream} {size(got)}"
        self.beat_at = self.now()
        self.ui.say(line + f" · {size(self.speed)}/s")

    def item_end(self, status, timings=None):
        if timings:
            self.timed.append(timings)
        self.clock.active(False)
        self.smoother.freeze()
        index = self.done
        self.done += 1
        if status in ("complete", "partial"):
            self.saved += 1
            kind = "ok" if status == "complete" else "warn"
        elif status == "skipped":
            self.skipped += 1
            kind = "skip"
        else:
            self.failed += 1
            kind = "fail"
        self.mark(index, kind)
        plan, position, _ = self.pipeline
        # A skipped item never ran, so no stages are left to show; a failed one keeps its stages
        # with the stage that failed marked.
        self.pipeline = (
            ((), 0, "run")
            if kind == "skip"
            else (plan, position, "fail" if kind == "fail" else "ok")
        )
        self.overall.update(self.oid, completed=self.done)
        if self.ui.plain:
            self.ui.say(f"Item {self.done}/{self.total} · {status}")

    def tint(self, kind):
        """The glyph colour of a row or stage. The one in progress breathes between sky blue and
        pink while there is motion."""
        if kind == "active" and self.ui.motion:
            return pulse(self.now(), BLUE, PINK)
        return ROW_COLORS[kind]

    def window(self, height):
        """The playlist rows around the current item: one finished item above it, the rest below.

        Nothing is drawn for a single video, or when the terminal is too short to hold the rest
        of the panel as well (Rich would cut the bottom of the panel off instead)."""
        titles, kinds = self.titles, self.kinds
        room = min(WINDOW_ROWS, (height or 0) - 10)
        if len(titles) < 2 or room < 3:
            return []
        current = min(self.done, len(titles) - 1)
        start = max(0, min(current - 1, len(titles) - room))
        digits = len(str(len(titles)))
        glyph = self.ui.glyph
        rows = []
        for index in range(start, min(len(titles), start + room)):
            kind = kinds[index]
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append("  " + glyph[kind] + " ", self.tint(kind))
            row.append(str(index + 1).rjust(digits) + " ", DIM)
            row.append(titles[index], ROW_TITLES[kind])
            rows.append(row)
        return rows

    def stages(self, width):
        """`Tahap ✓ Metadata → ● Video → ○ Audio → ○ Gabung`; None until an item has started.

        When that does not fit, only the stage in progress keeps its name."""
        plan, position, outcome = self.pipeline
        if not plan:
            return None
        glyph = self.ui.glyph
        finish = "Gabung" if "video" in plan and "audio" in plan else "Simpan"
        full = Text("Tahap ", no_wrap=True, overflow="ellipsis")
        short = Text("Tahap ", no_wrap=True, overflow="ellipsis")
        for index, phase in enumerate(plan):
            if outcome == "ok" or index < position:
                kind = "ok"
            elif index == position:
                kind = "active" if outcome == "run" else "fail"
            else:
                kind = "pending"
            label = finish if phase == "finish" else PHASE_LABELS.get(phase, phase)
            if index:
                for line in (full, short):
                    line.append(" " + glyph["arrow"] + " ", DIM)
            for line, shown in ((full, True), (short, kind in ("active", "fail"))):
                line.append(glyph[kind], self.tint(kind))
                if shown:
                    line.append(" " + label, ROW_TITLES[kind])
        return full if full.cell_len <= width else short

    def __rich_console__(self, console, options):
        compact = options.max_width < 60
        percent = (
            f"{min(100, max(0, self.downloaded / self.byte_total * 100)):.0f}%"
            if self.byte_total
            else "—%"
        )
        label = (
            percent
            if compact
            else percent
            + " · "
            + size(self.downloaded)
            + (f"/{size(self.byte_total)}" if self.byte_total else "")
        )
        self.file.update(self.fid, label=label)
        rate = size(self.speed) + "/s"
        if compact:
            rate += (
                " · "
                + size(self.downloaded)
                + (f"/{size(self.byte_total)}" if self.byte_total else "")
            )
        if self.eta is not None:
            rate += " · sisa " + duration(self.eta)
        status = self.message + (
            f" · sisa {duration(self.until - time.time())}" if self.until else ""
        )
        stages = self.stages(options.max_width)
        elapsed = duration(time.monotonic() - self.started)
        rows = [] if self.single else [self.overall]
        rows += self.window(options.max_height)
        rows += [self.file, *([stages] if stages else [])]
        rows += [
            Text(self.current, style=BLUE, no_wrap=True, overflow="ellipsis"),
            Text(rate, style=BLUE, no_wrap=True, overflow="ellipsis"),
        ]
        if not self.single:
            rows.append(
                Text(
                    f"Hasil  Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}",
                    no_wrap=True,
                    overflow="ellipsis",
                )
            )
        working = self.ui.motion and bool(stages) and self.pipeline[2] == "run"
        rows.append(
            Text(
                "Status "
                + (spin(self.now()) + " " if working and "GAGAL" not in status else "")
                + status
                + (f" · berjalan {elapsed}" if self.single else ""),
                style=RED if "GAGAL" in status else DIM,
                no_wrap=True,
                overflow="ellipsis",
            )
        )
        if not self.single:
            rows.append(Text("Durasi " + elapsed, style=DIM))
        yield Group(*rows)

    def settle(self):
        """A short flourish when a batch finishes without a failure: a ring that fills in pink and
        closes into the check mark that the final line shows. It lasts about a third of a second."""
        frames = ("◌", "◔", "◑", "◕", "●")
        with Live(
            console=self.ui.console,
            transient=True,
            refresh_per_second=FPS,
            redirect_stdout=False,
            redirect_stderr=False,
        ) as live:
            for index, frame in enumerate(frames):
                tone = mix(BLUE, PINK, index / (len(frames) - 1))
                live.update(Text(frame + " Menyelesaikan", style=tone), refresh=True)
                time.sleep(0.07)

    def timing_line(self):
        """One quiet line on where the time went in this run: the average speed of what was
        downloaded and the time spent waiting on purpose (the pause between downloads and before
        a retry). None when no download of this run reported timings, so an old job or a run of
        skipped items says nothing."""
        parts = []
        speeds = [(t["bytes"], t["speed"]) for t in self.timed if t.get("bytes") and t.get("speed")]
        if speeds:
            busy = sum(got / speed for got, speed in speeds)
            parts.append(f"Rata-rata {size(sum(got for got, _ in speeds) / busy)}/s")
        waited = sum(t.get("waited") or 0 for t in self.timed)
        if waited >= 1:
            parts.append(f"menunggu {duration(waited)}")
        return " · ".join(parts)

    def __exit__(self, exc_type, exc, tb):
        self.clock.active(False)
        if self.live:
            self.live.stop()
        interrupted = exc_type in (KeyboardInterrupt, Cancelled)
        label = "Dihentikan" if interrupted else "Terhenti" if exc_type else "Selesai"
        glyph, color = (
            ("stop", BLUE)
            if interrupted
            else ("fail", RED)
            if exc_type
            else ("warn", RED)
            if self.failed
            else ("ok", PINK)
        )
        line = Text()
        if not self.ui.plain:
            if glyph == "ok" and self.live and self.ui.motion:
                self.settle()
            line.append(self.ui.glyph[glyph] + " ", style=color)
        elapsed = duration(time.monotonic() - self.started)
        line.append(
            label
            + (f" · {elapsed}" if self.single else f" · {self.done}/{self.total} item · {elapsed}"),
            style=BLUE,
        )
        self.ui.say(line)
        indent = "" if self.ui.plain else "  "
        if not self.single:
            self.ui.say(
                f"{indent}Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}"
            )
        where = self.timing_line()
        if where:
            self.ui.say(Text(indent + where, style=DIM))
