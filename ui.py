"""Menus and bounded terminal display; no downloader output parsing."""

from contextlib import contextmanager
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
from rich.spinner import Spinner
import questionary
from prompt_toolkit.output import ColorDepth
from compat import configure_terminal
from model import codec_name

PINK = "#FFC2D4"
RED = "#E8384F"
BLUE = "#A8D8F0"
DIM = "#94A3B8"
EMPTY = "#334155"

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


class SolidBar:
    """A one-cell-high bar measured against the space Rich allocates to it."""

    def __init__(self, completed, total, color, animation_time=0, indicator=False):
        self.completed, self.total, self.color = completed, total, color
        self.animation_time, self.indicator = animation_time, indicator

    def text(self, width):
        width = max(0, width)
        bar = Text(no_wrap=True, overflow="crop")
        if self.total is None:
            count = min(3, width) if self.indicator else 0
            travel = width - count
            step = int(self.animation_time * BAR_STEPS_PER_SECOND) % (2 * travel) if travel else 0
            start = min(step, 2 * travel - step) if travel else 0
            bar.append("░" * start, EMPTY)
            bar.append("█" * count, self.color)
            bar.append("░" * (width - start - count), EMPTY)
            return bar
        ratio = min(1.0, max(0.0, self.completed / self.total)) if self.total > 0 else 0
        eighths = int(ratio * width * 8)
        full, fraction = divmod(eighths, 8)
        bar.append("█" * full, self.color)
        if fraction:
            # A dark background keeps the fractional cell aligned with the track.
            bar.append("▏▎▍▌▋▊▉"[fraction - 1], f"{self.color} on {EMPTY}")
        bar.append("░" * (width - full - bool(fraction)), EMPTY)
        return bar

    def __rich_measure__(self, console, options):
        return Measurement(1, max(1, options.max_width))

    def __rich_console__(self, console, options):
        yield self.text(options.max_width)


class SolidBarColumn(ProgressColumn):
    """`value` replaces the task's own counter, e.g. with Smoother.value for the file bar."""

    def __init__(self, color, clock=None, value=None):
        super().__init__(table_column=Column(ratio=1, no_wrap=True))
        self.color, self.clock, self.value = color, clock, value

    def render(self, task):
        return SolidBar(
            task.completed if self.value is None else self.value(),
            task.total,
            self.color,
            self.clock() if self.clock else 0,
            task.fields.get("indicator", False),
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


class UI:
    def __init__(self, plain=False):
        vt = configure_terminal()
        # NO_COLOR counts only when it has a value (no-color.org): an empty one means "not set".
        self.no_color = bool(os.environ.get("NO_COLOR"))
        # All of this tool's display is written to stderr, so stderr decides how it looks. A
        # stdout that is captured or redirected (the saved paths go there) must not turn a
        # terminal that can draw the panel into plain text.
        self.plain = plain or not vt or not sys.stderr.isatty() or os.environ.get("TERM") == "dumb"
        self.console = Console(
            stderr=True,
            force_terminal=not self.plain,
            legacy_windows=False,
            # Detected from COLORTERM/TERM: Rich and prompt_toolkit step the palette down to
            # 256 or 16 colors by themselves when the terminal cannot show 24-bit color.
            color_system=None if self.plain else "auto",
            no_color=self.no_color,
            highlight=False,
        )
        self.depth = ColorDepth.DEPTH_1_BIT if self.no_color else None
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

    def banner(self, version):
        line = Text()
        line.append("YouTube Downloader", style=f"bold {PINK}")
        line.append("  " + version, style=DIM)
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
        """A spinner while work runs. It yields update(text) to change the message."""
        if self.plain:
            self.say(message)
            yield self.say
            return
        spinner = Spinner("dots", text=Text(message, style=BLUE), style=PINK)

        def update(text):
            spinner.update(text=Text(text, style=BLUE))

        with Live(
            spinner,
            console=self.console,
            transient=True,
            refresh_per_second=FPS,
            redirect_stdout=False,
            redirect_stderr=False,
        ):
            yield update

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
        try:
            weight = size(path.stat().st_size)
        except OSError:
            weight = "file tidak ditemukan"
        parts = []
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
        parts += [path.suffix[1:].upper(), weight]
        title = Text(str(item.get("title") or item.get("id") or shown), style="bold")
        if item["status"] == "partial":
            title.append(" · subtitle belum lengkap", style=DIM)
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
                try:
                    detail = size(path.stat().st_size)
                except OSError:
                    detail = "file tidak ditemukan"
                if status == "partial":
                    detail += " · subtitle belum lengkap"
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
        if self.plain:
            self.say(self._message(title, step), BLUE)
            for i, (text, _) in enumerate(options, 1):
                self.say(f"{i}. {text}")
            while True:
                self.say("Nomor pilihan (q untuk batal):")
                answer = input().strip()
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
        if self.plain:
            self.say(self._message(title, step), BLUE)
            return input().strip() or default
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

    def checkboxes(self, title, options, checked=()):
        if self.plain:
            self.say(title, BLUE)
            for i, (text, _) in enumerate(options, 1):
                self.say(f"{i}. {text}")
            from model import item_indices

            while True:
                answer = self.text("Nomor/rentang, all, atau none untuk tidak memilih:")
                if answer == "none":
                    return []
                try:
                    return [options[i][1] for i in item_indices(answer, len(options))]
                except ValueError as exc:
                    self.say(exc, RED)
        picked = self._ask(
            questionary.checkbox(
                title,
                choices=[
                    questionary.Choice(text, value=i, checked=value in checked)
                    for i, (text, value) in enumerate(options)
                ],
                qmark=self.glyph["step"],
                pointer=self.glyph["pointer"],
                style=self.style,
                instruction="Space pilih · Enter lanjut · Ctrl+C batal",
                color_depth=self.depth,
            )
        )
        names = [options[i][0] for i in picked]
        summary = "tidak ada" if not names else ", ".join(names) if len(names) <= 3 else None
        self._answered(title, summary or f"{len(names)} dipilih")
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
        self.overall = Progress(
            TextColumn("Item"),
            SolidBarColumn(PINK),
            TextColumn("{task.percentage:.0f}% · {task.completed:.0f}/{task.total:.0f}"),
            expand=True,
        )
        self.oid = self.overall.add_task("", total=total)
        self.file = Progress(
            TextColumn("File"),
            SolidBarColumn(RED, self.clock, self.smoother.value),
            TextColumn("{task.fields[label]}"),
            expand=True,
        )
        self.fid = self.file.add_task("", total=None, label="Menyiapkan", indicator=False)
        self.live = None
        self.last_plain = None

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

    def item_end(self, status):
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
            row.append("  " + glyph[kind] + " ", ROW_COLORS[kind])
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
                line.append(glyph[kind], ROW_COLORS[kind])
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
        rows.append(
            Text(
                "Status " + status + (f" · berjalan {elapsed}" if self.single else ""),
                style=RED if "GAGAL" in status else DIM,
                no_wrap=True,
                overflow="ellipsis",
            )
        )
        if not self.single:
            rows.append(Text("Durasi " + elapsed, style=DIM))
        yield Group(*rows)

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
            line.append(self.ui.glyph[glyph] + " ", style=color)
        elapsed = duration(time.monotonic() - self.started)
        line.append(
            label
            + (f" · {elapsed}" if self.single else f" · {self.done}/{self.total} item · {elapsed}"),
            style=BLUE,
        )
        self.ui.say(line)
        if not self.single:
            self.ui.say(
                f"{'' if self.ui.plain else '  '}"
                f"Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}"
            )
