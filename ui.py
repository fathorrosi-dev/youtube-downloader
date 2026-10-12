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

PINK = "#FFC2D4"
RED = "#E8384F"
BLUE = "#A8D8F0"
DIM = "#94A3B8"
EMPTY = "#334155"


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
            step = int(self.animation_time * 8) % (2 * travel) if travel else 0
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
    def __init__(self, color, clock=None):
        super().__init__(table_column=Column(ratio=1, no_wrap=True))
        self.color, self.clock = color, clock

    def render(self, task):
        return SolidBar(
            task.completed,
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
}
MAX_REPORT_ROWS = 30


class UI:
    def __init__(self, plain=False):
        vt = configure_terminal()
        self.plain = (
            plain
            or not vt
            or not (sys.stdout.isatty() and sys.stderr.isatty())
            or os.environ.get("TERM") == "dumb"
        )
        self.console = Console(
            stderr=True,
            force_terminal=not self.plain,
            legacy_windows=False,
            color_system="truecolor" if not self.plain else None,
            no_color="NO_COLOR" in os.environ,
            highlight=False,
        )
        self.depth = ColorDepth.DEPTH_1_BIT if "NO_COLOR" in os.environ else ColorDepth.DEPTH_24_BIT
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
            if "NO_COLOR" not in os.environ
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

    def error(self, message, log=None):
        if self.plain:
            self.say("! GAGAL · " + str(message))
        else:
            line = Text()
            line.append(self.glyph["fail"] + " Gagal", style=f"bold {RED}")
            line.append(" · " + str(message), style=RED)
            self.say(line)
        if log and str(log) != self._reported_log:
            self.facts([("Log", tidy(log))])

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
            refresh_per_second=4,
            redirect_stdout=False,
            redirect_stderr=False,
        ):
            yield update

    def report(self, job, log):
        """What was saved where, then folder, job and log, once the batch has ended."""
        root = Path(job["output"])
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

    def checkboxes(self, title, options):
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
                choices=[questionary.Choice(text, value=i) for i, (text, _) in enumerate(options)],
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
    def __init__(self, ui, total):
        self.ui = ui
        self.total = total
        self.done = 0
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.started = time.monotonic()
        self.clock = AnimationClock()
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
            SolidBarColumn(RED, self.clock),
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
                refresh_per_second=4,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start()
        else:
            self.ui.say(f"Download · {self.total} item")
        return self

    def event(self, event):
        kind = event.get("type")
        if kind == "start":
            self.current = event.get("name", "")
            self.downloaded = 0
            self.byte_total = None
            self.speed = 0
            self.eta = None
            self.until = None
            self.clock.active(True, reset=True)
            self.file.reset(self.fid, total=None, indicator=True)
            self.message = "Mengunduh " + event.get("stream", "media")
        elif kind == "progress":
            self.clock.active(True)
            self.until = None
            self.message = "Mengunduh media"
            self.downloaded = event.get("downloaded") or 0
            self.byte_total = event.get("total") or None
            self.speed = event.get("speed") or 0
            self.eta = event.get("eta")
            self.file.update(
                self.fid, completed=self.downloaded, total=self.byte_total, indicator=True
            )
            self.file.tasks[0].total = self.byte_total
        elif kind == "file_done":
            self.downloaded = (
                event.get("downloaded") or event.get("total") or self.byte_total or self.downloaded
            )
            self.byte_total = event.get("total") or self.downloaded or None
            self.eta = None
            self.until = None
            self.clock.active(False)
            self.file.update(
                self.fid, total=max(1, self.downloaded), completed=max(1, self.downloaded)
            )
            self.message = "File sementara selesai"
        elif kind in ("stage", "retry", "wait", "failure", "notice"):
            self.clock.active(False)
            self.until = event.get("until") if kind == "wait" else None
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

    def item_end(self, status):
        self.clock.active(False)
        self.done += 1
        if status in ("complete", "partial"):
            self.saved += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
        self.overall.update(self.oid, completed=self.done)
        if self.ui.plain:
            self.ui.say(f"Item {self.done}/{self.total} · {status}")

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
        yield Group(
            self.overall,
            self.file,
            Text(self.current, style=BLUE, no_wrap=True, overflow="ellipsis"),
            Text(rate, style=BLUE, no_wrap=True, overflow="ellipsis"),
            Text(
                f"Hasil  Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}",
                no_wrap=True,
                overflow="ellipsis",
            ),
            Text(
                "Status " + status,
                style=RED if "GAGAL" in status else DIM,
                no_wrap=True,
                overflow="ellipsis",
            ),
            Text("Durasi " + duration(time.monotonic() - self.started), style=DIM),
        )

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
        line.append(
            label
            + f" · {self.done}/{self.total} item · {duration(time.monotonic() - self.started)}",
            style=BLUE,
        )
        self.ui.say(line)
        self.ui.say(
            f"{'' if self.ui.plain else '  '}"
            f"Disimpan {self.saved} · Dilewati {self.skipped} · Gagal {self.failed}"
        )
