"""Interactive YouTube downloader entry point."""

import argparse
import json
import os
import shlex
import sqlite3
import sys
import time
import platform
import subprocess
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from compat import (
    native_output,
    platform_key,
    deno_path,
    foreign_path,
    folder_size,
    lock_requirements,
)
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from importlib.metadata import PackageNotFoundError, version
from model import (
    VERSION,
    PIN_WARN_DAYS,
    SelectionError,
    valid_url,
    valid_browser,
    qualities,
    languages,
    default_language,
    pick,
    tracks,
    container,
    variant,
    phases,
    item_indices,
    codec_name,
    compatible_audio,
    compatible_video_rank,
    has_video,
    has_audio,
    resolution,
    release_age_days,
    pace_profile,
    clean_timings,
    TIMING_PHASES,
)
from store import (
    BASE,
    STATE,
    lock,
    settings,
    new_job,
    load_job,
    save_job,
    list_jobs,
    resumable_jobs,
    Archive,
)
from runner import run, WorkerError, cancelled_by
from rich.text import Text
from ui import UI, Session, Cancelled, BLUE, DIM, RED, clock, size, tidy

# Words that are commands, not links.
COMMANDS = ("status", "preview", "resume", "update", "clean", "rebase-media")
# Run through setup.sh / migrate.sh / rollback.sh: they change the Python environment this very
# process is running in, which a Windows process cannot do to itself.
SCRIPT_COMMANDS = ("setup", "migrate", "rollback")

UNSET = object()
# What --yes answers for every prompt that no flag answered.
YES_DEFAULTS = {
    "mode": "video-audio",
    "compatible": False,
    "quality": "best",
    "lang": "original",
    "audio_format": "original",
    "audio_manual": False,
}
# The flag behind each preset key, for messages.
FLAGS = {
    "mode": "--mode",
    "compatible": "--format",
    "quality": "--quality",
    "lang": "--lang",
    "audio_format": "--audio-format",
    "subs": "--subs",
}
SUBTITLE_ORDER = ("manual", "automatic", "translated")


def launcher(windows=None):
    """The command that starts this tool, written so it works from wherever it is pasted.
    `windows` is for the tests; by default it is whether this is Windows."""
    windows = os.name == "nt" if windows is None else windows
    name = "youtube-downloader.cmd" if windows else "youtube-downloader"
    if os.path.samefile(os.getcwd(), BASE):
        return (".\\" if windows else "./") + name
    path = str(BASE / name)
    return subprocess.list2cmdline([path]) if windows else shlex.quote(path)


def resume_command(job_id, login=False):
    return f"{launcher()} resume {job_id}" + (" --browser firefox" if login else "")


class Stopped(ValueError):
    """A batch that ended without finishing. It carries what the closing message needs."""

    def __init__(self, message, job_id, login=False, category=None):
        super().__init__(message)
        self.job_id = job_id
        self.login = login
        self.category = category


def ytdlp_age():
    """Days since the installed yt-dlp was released, or None."""
    try:
        return release_age_days(version("yt-dlp"))
    except PackageNotFoundError:
        return None


def stop_message(category):
    """Why a batch stopped, and what to do before carrying on; the command follows it."""
    if category == "authentication":
        return "YouTube meminta login. Login di Firefox, lalu lanjutkan:"
    if category == "rate_limit":
        return "YouTube membatasi permintaan. Tunggu beberapa menit, lalu lanjutkan:"
    if category == "network":
        return "Tiga kegagalan jaringan berurutan. Periksa koneksi internet, lalu lanjutkan:"
    text = "Tiga kegagalan berurutan; periksa log."
    age = ytdlp_age()
    if age is not None and age > PIN_WARN_DAYS:
        text += f" yt-dlp rilis {age} hari lalu; periksa rilis alat baru dengan update --check."
    return text + " Lalu lanjutkan:"


def current_pace():
    """The pacing profile named by YTDL_PACE (normal when it is not set); an unknown name is an
    error that lists the accepted ones."""
    return pace_profile(os.environ.get("YTDL_PACE"))


def inspect_url(url, browser=None, playlist=False):
    return run(
        {
            "action": "inspect",
            "url": url,
            "browser": browser,
            "playlist": playlist,
            "pace": current_pace(),
        }
    )


def format_label(f):
    quality = (
        str(resolution(f)) + "p · " + str(f.get("width")) + "×" + str(f.get("height"))
        if has_video(f)
        else "Audio"
    )
    return f"{f['format_id']} · {quality} · {f.get('fps') or '—'} fps · {f.get('dynamic_range') or 'SDR'} · {f.get('vcodec') or '—'} / {f.get('acodec') or '—'} · {f.get('language') or 'und'} · {f.get('abr') or f.get('tbr') or '—'} kbps"


def settings_plan(mode, has_subtitles):
    """The numbered prompts that follow the choice of mode, in the order they are asked."""
    plan = []
    if mode != "audio":
        plan += ["container", "quality"]
    if mode != "video":
        plan.append("language")
    if mode == "audio":
        plan += ["audio_format", "audio_quality"]
    if has_subtitles:
        plan.append("subtitles")
    plan.append("confirm")
    return plan


class Steps:
    """Numbers the settings prompts. The mode is asked first and is not numbered because it
    decides how many prompts follow, so every counter shown is exact."""

    def __init__(self, total):
        self.total = total
        self.number = 0

    def next(self):
        self.number += 1
        return (self.number, self.total)


def find_text(options, value):
    for text, candidate in options:
        if candidate == value and type(candidate) is type(value):
            return text
    return None


def ask(ui, preset, key, title, options, label, step=None, previous=None, default=UNSET):
    """One choice, answered in one of three ways: by a command-line flag, by the default under
    --yes, or by the menu (with the earlier answer selected when the settings are being changed)."""
    preset["_used"].add(key)
    given = preset.get(key, UNSET)
    if given is UNSET and preset.get("yes"):
        given = YES_DEFAULTS[key]
    if given is not UNSET:
        text = find_text(options, given)
        if text is None:
            offered = ", ".join(str(v) for _, v in options if v != "manual")
            raise SelectionError(
                f"{FLAGS.get(key, key)} {given}: tidak tersedia. Pilihan: {offered}"
            )
        ui.answered(label, text, step)
        return given
    kwargs = {"label": label}
    if step is not None:
        kwargs["step"] = step
    if previous and key in previous:
        kwargs["default"] = previous[key]
    elif default is not UNSET:
        kwargs["default"] = default
    return ui.choose(title, options, **kwargs)


def choose_source(ui, url, preset):
    qs = parse_qs(urlparse(url).query)
    is_playlist = "list" in qs or urlparse(url).path == "/playlist"
    if preset.get("playlist") and not is_playlist:
        raise SelectionError("--playlist memerlukan link yang berisi playlist")
    if is_playlist and ("v" in qs or urlparse(url).hostname == "youtu.be"):
        if preset.get("playlist") or preset.get("items") is not None:
            ui.answered("Sumber", "Playlist")
            return True
        if preset.get("yes"):
            ui.answered("Sumber", "Video ini saja")
            return False
        return ui.choose(
            "Link berisi video dan playlist. Ambil yang mana?",
            [("Video ini saja", False), ("Playlist", True)],
            label="Sumber",
        )
    return is_playlist


def pick_entries(ui, initial, preset):
    entries = initial["entries"]
    if not entries:
        raise ValueError("Playlist kosong atau tidak dapat diakses")
    ui.say(
        Text.assemble((initial["title"] or "Playlist", "bold"), (f" · {len(entries)} item", DIM))
    )
    wanted = preset.get("items")
    if wanted is not None:
        selected = [entries[i] for i in item_indices(wanted, len(entries))]
        ui.answered("Item", f"{len(selected)} dari {len(entries)}")
    elif preset.get("yes"):
        selected = entries
        ui.answered("Item", "semua item")
    else:
        selection = ui.choose(
            "Pilih item playlist",
            [
                ("Semua item", "all"),
                ("Pilih dengan checkbox", "checkbox"),
                ("Nomor/rentang", "range"),
            ],
            label="Item",
        )
        if selection == "all":
            selected = entries
        elif selection == "checkbox":
            selected = ui.checkboxes(
                "Item playlist", [(f"{i + 1}. {e['title']}", e) for i, e in enumerate(entries)]
            )
        else:
            while True:
                try:
                    selected = [
                        entries[i]
                        for i in item_indices(
                            ui.text("Nomor/rentang, misalnya 1,3,5-10:", label="Item"),
                            len(entries),
                        )
                    ]
                    break
                except SelectionError as exc:
                    ui.say(exc, RED)
    if not selected:
        raise Cancelled()
    return selected


INSPECT_WORKERS = 3


def inspect_workers(count):
    """How many items are looked up at once. YTDL_INSPECT_WORKERS=1 turns it back into one at a
    time; a few at once keeps a long playlist from waiting minutes for its first menu."""
    try:
        wanted = int(os.environ.get("YTDL_INSPECT_WORKERS", INSPECT_WORKERS))
    except ValueError:
        wanted = INSPECT_WORKERS
    return max(1, min(wanted, count))


def inspect_selection(ui, selected, browser):
    """The metadata of every selected playlist item. One that is unavailable is listed and
    skipped; a login request, a rate limit or three failures in a row stop the whole batch.

    A few items are looked up at the same time, but their results are taken in playlist order,
    so what is listed, skipped or counted as a failure is the same as one at a time. When the
    batch stops (a rate limit, three failures, Ctrl+C) the lookups that have not started are
    dropped and the ones that are running are stopped, so nothing keeps the terminal waiting."""
    metadata = []
    unavailable = []
    errors = 0
    total = len(selected)
    label = "Memeriksa kualitas dan subtitle"
    workers = inspect_workers(total)
    pool = ThreadPoolExecutor(workers) if workers > 1 else nullcontext()
    stop = threading.Event()

    def look_up(url):
        with cancelled_by(stop):  # the worker this thread starts ends when `stop` is set
            return inspect_url(url, browser)

    with ui.activity(f"{label} · 0/{total}") as progress, pool:
        if workers > 1:
            futures = [pool.submit(look_up, entry["url"]) for entry in selected]
            fetch = lambda index: futures[index].result()  # noqa: E731
        else:
            futures = []
            fetch = lambda index: inspect_url(selected[index]["url"], browser)  # noqa: E731
        try:
            for index, entry in enumerate(selected):
                number = index + 1
                try:
                    info = fetch(index)
                    metadata.append(info)
                    errors = 0
                except WorkerError as exc:
                    unavailable.append({"title": entry["title"], "reason": str(exc)})
                    if exc.category in ("authentication", "rate_limit"):
                        raise
                    if exc.category not in ("unavailable",):
                        errors += 1
                    if errors >= 3:
                        raise WorkerError(
                            "Tiga kegagalan metadata berurutan; batch dihentikan. " + str(exc),
                            exc.category,
                        ) from exc
                if progress:
                    progress(f"{label} · {number}/{total}", (number, total))
        except BaseException:
            stop.set()
            running = sum(1 for future in futures if future.running())
            for future in futures:
                future.cancel()
            if progress and running:
                # Leaving the pool waits for the workers to close; say so instead of spinning on.
                progress(f"Menghentikan · menutup {running} pencarian yang berjalan")
            raise
    if unavailable:
        ui.table(
            ("Item tidak tersedia", "Alasan"), [(x["title"], x["reason"]) for x in unavailable]
        )
    return metadata


def read_links(path):
    """The links of a --links-file: one per line, blank lines and lines starting with # skipped."""
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"--links-file tidak dapat dibaca: {exc}") from exc
    links = []
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            links.append(valid_url(text))
        except ValueError as exc:
            raise ValueError(f"--links-file baris {number}: {exc}") from exc
    if not links:
        raise ValueError("--links-file tidak berisi link")
    return links


def collect_many(ui, urls, browser, preset):
    """The metadata of several links as one batch; a video that appears twice is taken once."""
    metadata, seen = [], set()
    for url in urls:
        for info in collect_metadata(ui, url, browser, choose_source(ui, url, preset), preset):
            if info["id"] not in seen:
                seen.add(info["id"])
                metadata.append(info)
    if not metadata:
        raise ValueError("Tidak ada item yang dapat diunduh")
    return metadata


def collect_metadata(ui, url, browser, is_playlist, preset):
    with ui.activity("Mengambil informasi YouTube"):
        initial = inspect_url(url, browser, is_playlist)
    if preset.get("items") is not None and not initial.get("playlist"):
        raise SelectionError("--items hanya berlaku untuk playlist")
    if initial.get("playlist"):
        metadata = inspect_selection(ui, pick_entries(ui, initial, preset), browser)
    else:
        metadata = [initial]
    if not metadata:
        raise ValueError("Tidak ada item yang dapat diunduh")
    return metadata


def describe_single(ui, metadata):
    if len(metadata) != 1:
        return
    info = metadata[0]
    details = [
        clock(info.get("duration")),
        info.get("channel") or info.get("uploader") or "YouTube",
    ]
    largest = next((f for f in reversed(info["formats"]) if has_video(f)), None)
    if largest:
        shape = "Portrait" if largest["height"] > largest["width"] else "Landscape"
        details.append(f"{shape} {largest['width']} × {largest['height']}")
    ui.say(Text(info["title"], style="bold"))
    ui.say(Text(" · ".join(details), style=DIM))


def merge_tracks(metadata):
    all_tracks = {}
    for m in metadata:
        for t in tracks(m):
            identity = (
                (t["language"], t["kind"])
                if len(metadata) > 1
                else (t["language"], t["kind"], t["key"], t["origin"])
            )
            all_tracks.setdefault(identity, t)
    return all_tracks


def subtitle_identities(all_tracks, wanted):
    """The track to take for each requested language (original captions before automatic ones
    before translations), and the languages that have none."""
    chosen, missing = [], []
    for language in wanted:
        candidates = [(i, t) for i, t in all_tracks.items() if t["language"] == language]
        if not candidates:
            missing.append(language)
            continue
        candidates.sort(
            key=lambda c: (
                SUBTITLE_ORDER.index(c[1]["kind"]) if c[1]["kind"] in SUBTITLE_ORDER else 9
            )
        )
        chosen.append(candidates[0][0])
    return chosen, missing


def ask_subtitles(ui, preset, mode, compatible, metadata, all_tracks, step, previous):
    """The subtitle tracks to keep, as identities into all_tracks."""
    preset["_used"].add("subs")
    wanted = preset.get("subs", UNSET)
    if wanted is UNSET and preset.get("yes"):
        wanted = []
    if wanted is not UNSET:
        chosen, missing = subtitle_identities(all_tracks, wanted)
        ui.answered(
            "Subtitle", ", ".join(all_tracks[i]["name"] for i in chosen) or "tanpa subtitle", step
        )
        if missing:
            ui.say(Text("Subtitle tidak tersedia: " + ", ".join(missing), style=DIM))
        return chosen, None
    kwargs = {"default": previous["addon"]} if previous and "addon" in previous else {}
    addon = ui.choose(
        "Tambahan subtitle",
        [("Tanpa subtitle", False), ("Pilih bahasa/track subtitle", True)],
        step=step,
        label="Subtitle",
        **kwargs,
    )
    if not addon:
        return [], False
    names = {
        "manual": "CC asli",
        "automatic": "Caption otomatis",
        "translated": "Terjemahan otomatis",
    }
    if mode == "audio":
        ui.say(Text("Audio saja: subtitle disimpan sebagai file .srt.", style=DIM))
    elif compatible:
        ui.say(
            Text(
                "MP4 kompatibel: subtitle disimpan sebagai file .srt di folder subtitle.",
                style=DIM,
            )
        )
    options = [
        (
            f"{t['name']} · {names[t['kind']]}"
            + (
                " · sumber dipilih per video"
                if len(metadata) > 1 and t["kind"] == "translated"
                else ""
            ),
            identity,
        )
        for identity, t in all_tracks.items()
    ]
    extra = {"checked": previous["chosen"]} if previous and previous.get("chosen") else {}
    return ui.checkboxes("Track subtitle", options, **extra), True


def ask_settings(ui, metadata, all_tracks, preset, previous=None):
    """Every numbered prompt after the link. `previous` holds the earlier answers when the person
    chose to change them."""
    preset["_used"] = set()
    mode = ask(
        ui,
        preset,
        "mode",
        "Apa yang ingin diunduh?",
        [
            ("Video + audio", "video-audio"),
            ("Audio saja", "audio"),
            ("Video saja — tanpa suara", "video"),
        ],
        "Unduh apa?",
        previous=previous,
    )
    steps = Steps(len(settings_plan(mode, bool(all_tracks))))
    answers = {"mode": mode}
    quality = "best"
    manual = False
    audio_format = "original"
    compatible = False
    if mode != "audio":
        sound = " + AAC" if mode == "video-audio" else ""
        compatible = ask(
            ui,
            preset,
            "compatible",
            "Format file",
            [
                ("Otomatis · MKV atau MP4 sesuai codec · Direkomendasikan", False),
                (f"MP4 kompatibel · H.264/AV1{sound} · iPhone, Mac, editor", True),
            ],
            "Format file",
            steps.next(),
            previous,
        )
        answers["compatible"] = compatible
        tiers = sorted(
            {q for m in metadata for q in qualities(m, compatible)},
            reverse=True,
        )
        if not tiers:
            raise ValueError(
                "Tidak ada video H.264/AV1 untuk MP4 kompatibel; pilih format file Otomatis"
                if compatible
                else "Tidak ada format video yang tersedia"
            )
        recommended = (
            1080 if 1080 in tiers else max((q for q in tiers if q <= 1080), default=min(tiers))
        )
        with_audio = " + audio terbaik" if mode == "video-audio" else ""
        options = [("Kualitas maksimal" + with_audio + " · Direkomendasikan", "best")]
        options += [
            (f"{q}p" + (" · pilihan harian" if q == recommended else "") + with_audio, q)
            for q in tiers
        ]
        options += [("Pilih format secara detail per item", "manual")]
        title = "Pilih kualitas"
        if len(metadata) > 1:
            title += " · playlist boleh turun kualitas, tidak naik otomatis"
        quality = ask(
            ui, preset, "quality", title, options, "Kualitas", steps.next(), previous, "best"
        )
        answers["quality"] = quality
        manual = quality == "manual"
        if manual:
            quality = "best"
    lang = "original"
    if mode != "video":
        available = sorted({lang for m in metadata for lang in languages(m)})
        lang = ask(
            ui,
            preset,
            "lang",
            "Bahasa audio",
            [("Bahasa asli per video", "original")] + [(x, x) for x in available],
            "Bahasa audio",
            steps.next(),
            previous,
        )
        answers["lang"] = lang
        if mode == "audio":
            audio_format = ask(
                ui,
                preset,
                "audio_format",
                "Format audio",
                [
                    ("Audio asli terbaik · tanpa konversi", "original"),
                    ("MP3 · konversi lossy", "mp3"),
                ],
                "Format audio",
                steps.next(),
                previous,
            )
            answers["audio_format"] = audio_format
            manual = ask(
                ui,
                preset,
                "audio_manual",
                "Pemilihan kualitas audio",
                [("Audio terbaik otomatis", False), ("Pilih format/bitrate per item", True)],
                "Kualitas audio",
                steps.next(),
                previous,
            )
            answers["audio_manual"] = manual
    chosen = []
    if all_tracks:
        chosen, addon = ask_subtitles(
            ui, preset, mode, compatible, metadata, all_tracks, steps.next(), previous
        )
        answers["chosen"] = chosen
        if addon is not None:
            answers["addon"] = addon
    else:
        preset["_used"].add("subs")
        ui.say(Text("YouTube tidak mengembalikan subtitle untuk pilihan ini.", style=DIM))
    unused = [flag for key, flag in FLAGS.items() if key in preset and key not in preset["_used"]]
    if unused:
        raise SelectionError("Flag tidak berlaku untuk pilihan ini: " + ", ".join(unused))
    return {
        "mode": mode,
        "compatible": compatible,
        "quality": quality,
        "manual": manual,
        "lang": lang,
        "audio_format": audio_format,
        "chosen": chosen,
        "steps": steps,
        "answers": answers,
    }


def build_items(ui, metadata, all_tracks, settings_):
    """The job items for the answers given, and the summary rows that describe them."""
    mode = settings_["mode"]
    quality = settings_["quality"]
    manual = settings_["manual"]
    lang = settings_["lang"]
    audio_format = settings_["audio_format"]
    compatible = settings_["compatible"]
    chosen = settings_["chosen"]
    items = []
    rows = []
    not_mp4 = 0
    for m in metadata:
        language = default_language(m) if lang == "original" else lang
        video_id = None
        audio_id = None
        if manual:
            if mode != "audio":
                video_formats = [
                    f
                    for f in reversed(m["formats"])
                    if has_video(f)
                    and not f.get("has_drm")
                    and (not compatible or compatible_video_rank(f) is not None)
                ]
                if not video_formats:
                    rows.append((m["title"], "DILEWATI", "Tidak ada video H.264/AV1"))
                    continue
                video_id = ui.choose(
                    "Format video · " + m["title"],
                    [(format_label(f), f["format_id"]) for f in video_formats],
                    label="Format video",
                )
            if mode != "video":
                af = [
                    f
                    for f in reversed(m["formats"])
                    if has_audio(f) and (f.get("language") or "und") == language
                ]
                if compatible:
                    # Offer AAC first-class: it is what lets the file be an MP4.
                    af = [f for f in af if compatible_audio(f)] or af
                if af:
                    audio_id = ui.choose(
                        "Format audio · " + m["title"],
                        [(format_label(f), f["format_id"]) for f in af],
                        label="Format audio",
                    )
        try:
            choice = pick(m, mode, quality, language, audio_format, video_id, audio_id, compatible)
        except SelectionError as exc:
            rows.append((m["title"], "DILEWATI", str(exc)))
            continue
        selected_tracks = []
        current_tracks = tracks(m)
        for identity in chosen:
            candidates = [t for t in current_tracks if (t["language"], t["kind"]) == identity[:2]]
            if len(identity) > 2:
                candidates = [t for t in candidates if (t["key"], t["origin"]) == identity[2:]]
            if candidates:
                selected_tracks.append(
                    next(
                        (t for t in candidates if t["source"] == default_language(m)), candidates[0]
                    )
                )
            else:
                expected = all_tracks[identity]
                # Keep requested tracks so absence is recorded instead of silently disappearing.
                selected_tracks.append(expected)
        item = {
            "id": m["id"],
            "title": m["title"],
            "url": m.get("webpage_url") or "https://www.youtube.com/watch?v=" + m["id"],
            "choice": choice,
            "subtitles": selected_tracks,
            "status": "pending",
        }
        items.append(item)
        actual = (
            f"{choice['actual_quality']}p · {choice['width']}×{choice['height']} · {choice['fps'] or '—'}fps · {choice.get('dynamic_range') or 'SDR'}"
            if mode != "audio"
            else choice["audio_format"]
        )
        if quality != "best" and choice["actual_quality"] and choice["actual_quality"] != quality:
            actual += f" · turun dari {quality}p"
        extension = container(choice, selected_tracks)
        if compatible and extension != "mp4":
            not_mp4 += 1
        codecs = ""
        if mode != "audio":
            codecs = codec_name(choice["vcodec"])
            if mode == "video-audio":
                codecs += " + " + codec_name(choice["acodec"])
            codecs += " · "
        details = (
            f"{extension.upper()} · {codecs}audio {language if mode != 'video' else 'tanpa suara'} · "
            + (
                "≈ " + size(choice["estimated_size"])
                if choice["estimated_size"]
                else "ukuran belum diketahui"
            )
        )
        if selected_tracks:
            details += " · subtitle " + ", ".join(t["name"] for t in selected_tracks)
            if extension == "mp4":
                details += " (.srt terpisah)"
        rows.append((m["title"], actual, details))
    return items, rows, not_mp4


def show_summary(ui, rows, root, not_mp4):
    ui.say("")
    if len(rows) == 1:
        ui.table(("Video", "Kualitas", "Berkas", "Simpan di"), [(*rows[0], tidy(root))])
    else:
        ui.table(("Video", "Kualitas", "Berkas"), rows)
        ui.say(Text.assemble(("Simpan di ", DIM), tidy(root)))
    if not_mp4:
        ui.say(
            Text(
                f"{not_mp4} item tidak punya pasangan video/audio yang cocok untuk MP4 · "
                "disimpan sebagai MKV",
                style=DIM,
            )
        )
    ui.say("")


def confirm(ui, preset, steps):
    """True to start, "edit" to change the answers, False to cancel."""
    step = steps.next()
    if preset.get("yes"):
        ui.answered("Mulai?", "Ya", step)
        return True
    return ui.choose(
        "Mulai download?",
        [("Mulai", True), ("Ubah pilihan", "edit"), ("Batal", False)],
        step=step,
        label="Mulai?",
    )


def wizard(ui, url, browser=None, output=None, preset=None):
    """Turn a link and a series of answers into a saved job.

    The answers come from the menus, from command-line flags (`preset`), or from --yes."""
    preset = {} if preset is None else preset
    preset["_used"] = set()
    root = str(native_output(output or settings()["output"]))
    if isinstance(url, list):  # the links of a --links-file
        metadata = collect_many(ui, url, browser, preset)
    else:
        url = valid_url(url or ui.text("Tempel link YouTube:", label="Link"))
        is_playlist = choose_source(ui, url, preset)
        metadata = collect_metadata(ui, url, browser, is_playlist, preset)
    describe_single(ui, metadata)
    ui.say("")
    all_tracks = merge_tracks(metadata)
    answers = None
    while True:
        answers = ask_settings(
            ui, metadata, all_tracks, preset, answers["answers"] if answers else None
        )
        items, rows, not_mp4 = build_items(ui, metadata, all_tracks, answers)
        if not items:
            raise ValueError("Tidak ada item yang memenuhi pilihan")
        show_summary(ui, rows, root, not_mp4)
        decision = confirm(ui, preset, answers["steps"])
        if decision == "edit":
            ui.say(Text("Mengubah pilihan · info video tidak diambil ulang", style=DIM))
            # An answer that came from a flag is asked again too, with it selected as the default.
            for key in FLAGS:
                preset.pop(key, None)
            continue
        if decision is False:
            raise Cancelled()
        break
    job = new_job(items, root, browser)
    ui.say(Text.assemble(("Job tersimpan · ", DIM), (job["id"], BLUE)))
    return job


def execute(ui, job):
    native_output(job["output"])
    if job.get("browser") and (
        (job.get("platform") and job["platform"].split("-")[0] != platform_key().split("-")[0])
        or (":" in job["browser"] and foreign_path(job["browser"].split(":", 1)[1]))
    ):
        raise ValueError(
            "Sesi Firefox berasal dari OS lain. Jalankan resume dengan --browser firefox pada perangkat ini."
        )
    archive = Archive()
    consecutive = 0
    job["status"] = "running"
    save_job(job)
    try:
        with Session(ui, len(job["items"]), job["items"]) as session:
            try:
                for item in job["items"]:
                    key = variant(item)
                    previous = archive.get(key, item["choice"]["mode"], job["output"])
                    if previous and not previous.get("missing"):
                        item["status"] = "complete"
                        item["result"] = previous
                        session.item_end("skipped")
                        save_job(job)
                        consecutive = 0
                        continue
                    request = {
                        "action": "download",
                        "item": item,
                        "output": job["output"],
                        "browser": job.get("browser"),
                        "pace": current_pace(),
                    }
                    if previous:
                        request["existing_file"] = previous["path"]
                    item["status"] = "running"
                    save_job(job)
                    session.item_start(phases(item))
                    began = time.monotonic()
                    try:
                        result = run(request, session.event)
                        wall = time.monotonic() - began
                        # Where the time went is kept with the item, not in the archive record.
                        timings = clean_timings(
                            result.pop("timings", None) if isinstance(result, dict) else None
                        )
                        try:
                            archive.put(key, result)
                        except (
                            ValueError,
                            OSError,
                            sqlite3.Error,
                            subprocess.SubprocessError,
                        ) as exc:
                            # The file is in place; only the record is missing. This item fails,
                            # the batch goes on, and resume records the file without downloading.
                            raise WorkerError(
                                "Hasil tersimpan, tetapi belum tercatat di arsip ("
                                + str(exc)
                                + "); resume akan mencatatnya",
                                "system",
                            ) from exc
                        item["result"] = result
                        item["status"] = "partial" if result.get("missing") else "complete"
                        item.pop("error", None)
                        if timings:
                            timings["wall"] = round(wall, 3)
                            item["timings"] = timings
                        else:
                            item.pop("timings", None)
                        session.item_end(item["status"], timings)
                        consecutive = 0
                    except WorkerError as exc:
                        item["status"] = "failed"
                        item["error"] = {"message": str(exc), "category": exc.category}
                        session.event({"type": "failure", "message": "! GAGAL · " + str(exc)})
                        session.item_end("failed")
                        if exc.category != "unavailable":
                            consecutive += 1
                        else:
                            consecutive = 0
                        save_job(job)
                        if exc.category in ("authentication", "rate_limit") or consecutive >= 3:
                            job["status"] = "blocked"
                            raise Stopped(
                                "Batch dihentikan. " + stop_message(exc.category),
                                job["id"],
                                login=exc.category == "authentication",
                                category=exc.category,
                            ) from exc
                    save_job(job)
            except (KeyboardInterrupt, Cancelled):
                job["status"] = "interrupted"
                raise
            except Exception:
                if job["status"] == "running":
                    job["status"] = "blocked"
                raise
        job["status"] = (
            "complete" if all(x["status"] == "complete" for x in job["items"]) else "partial"
        )
    finally:
        save_job(job)
        archive.close()
        ui.report(job, STATE / "download.log")
    if job["status"] != "complete":
        raise Stopped("Ada item atau subtitle yang belum selesai. Lanjutkan:", job["id"])


def emit_paths(job, scripted):
    """The saved files, one path per line on stdout, for a caller that captures it. The display
    is on stderr, so stdout carries nothing else."""
    if not (scripted or not sys.stdout.isatty()):
        return
    for item in job["items"]:
        result = item.get("result")
        if item.get("status") in ("complete", "partial") and result:
            print(result["path"], flush=True)


def pin_note(name, actual):
    """For yt-dlp, how long ago this release came out: downloads fail when YouTube changes and
    the pinned version is old, and the date is part of the version."""
    age = release_age_days(actual) if name.lower() == "yt-dlp" else None
    if age is None:
        return ""
    note = f" · rilis {age} hari lalu"
    if age > PIN_WARN_DAYS:
        note += f" · sudah lama (> {PIN_WARN_DAYS} hari); cek rilis alat baru: update --check"
    return note


def state_figures():
    """(bytes in .state, bytes that `clean` could free), or None when .state cannot be read."""
    try:
        import maintenance

        reclaim = sum(
            w for entries in maintenance.clean_plan(STATE.parent).values() for _, w in entries
        )
        return folder_size(STATE), reclaim
    except OSError:
        return None


def state_note(figures=UNSET):
    note = str(STATE)
    figures = state_figures() if figures is UNSET else figures
    if figures:
        note += " · " + size(figures[0])
        if figures[1]:
            note += f" · {size(figures[1])} dapat dibersihkan (jalankan clean)"
    return note


def pace_note(data):
    """The pacing profile for the status table and JSON; an unknown YTDL_PACE is shown, not
    raised, because `status` is how a person finds out what is wrong."""
    name = os.environ.get("YTDL_PACE")
    try:
        profile = pace_profile(name)
    except SelectionError as exc:
        data["pace"] = {"name": name, "error": str(exc)}
        return f"YTDL_PACE={name} tidak dikenal (pilih: normal, careful, fast)"
    data["pace"] = profile
    low, high = profile["file"]
    return (
        f"{profile['name']} · {low:g}-{high:g} d sebelum tiap unduhan, "
        f"{profile['requests']:g} d antar-permintaan"
    )


def job_timings(job, detail=False):
    """Where the time of a job went, from the timings its items carry: totals over the items that
    have them, and with `detail` one entry per item. None when no item has timings (a job made
    before they were recorded, or whose items were all skipped).

    "overhead" is the wall time around each worker minus the worker's own total: starting the
    process, importing yt-dlp and closing down. "speed" is the bytes of those items over the time
    their streams were not waiting."""
    rows = [
        (item, item["timings"])
        for item in job.get("items") or []
        if isinstance(item, dict) and isinstance(item.get("timings"), dict)
    ]
    if not rows:
        return None
    total = lambda key: round(sum(t.get(key) or 0 for _, t in rows), 3)  # noqa: E731
    busy = sum(t["bytes"] / t["speed"] for _, t in rows if t.get("bytes") and t.get("speed"))
    got = sum(t["bytes"] for _, t in rows if t.get("bytes") and t.get("speed"))
    data = {
        "items": len(rows),
        "wall": total("wall"),
        "worker": total("total"),
        "overhead": round(
            sum(max(0, (t.get("wall") or 0) - (t.get("total") or 0)) for _, t in rows), 3
        ),
        "waited": total("waited"),
        "bytes": total("bytes"),
        "speed": round(got / busy, 1) if busy else None,
        "phases": {name: total(name) for name in TIMING_PHASES},
    }
    if detail:
        data["per_item"] = [
            {"id": item.get("id"), "title": item.get("title"), **timings} for item, timings in rows
        ]
    return data


def timings_table(ui, job):
    """The timings of one job as a table: a row per item and a last row for the sum."""
    data = job_timings(job, detail=True)
    if not data:
        return
    seconds = lambda value: f"{value:.1f}" if value else "-"  # noqa: E731
    rows = []
    for entry in data["per_item"][:20]:
        speed = entry.get("speed")
        rows.append(
            (
                str(entry.get("title") or entry.get("id") or "?")[:28],
                *(seconds(entry.get(name)) for name in (*TIMING_PHASES, "waited", "wall")),
                f"{size(speed)}/s" if speed else "-",
            )
        )
    if len(data["per_item"]) > 20:
        rows.append((f"... {len(data['per_item']) - 20} item lagi", *("" for _ in range(8))))
    rows.append(
        (
            "Jumlah",
            *(seconds(data["phases"][name]) for name in TIMING_PHASES),
            seconds(data["waited"]),
            seconds(data["wall"]),
            f"{size(data['speed'])}/s" if data["speed"] else "-",
        )
    )
    ui.table(
        (
            f"Job {job.get('id')} (detik)",
            "Metadata",
            "Video",
            "Audio",
            "Subtitle",
            "Selesai",
            "Menunggu",
            "Total",
            "Kecepatan",
        ),
        rows,
    )


def status(ui, as_json=False, timings=False):
    """The state of this installation as a table on stderr, or with --json as one JSON object on
    stdout (the table is not drawn) for a script to read. With `timings`, the items' times."""
    figures = state_figures()
    data = {
        "version": VERSION,
        "pace": None,
        "platform": platform_key(),
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "tools": str(BASE),
        "media": str(Path(settings()["output"]).expanduser()),
        "state": {
            "path": str(STATE),
            "bytes": figures[0] if figures else None,
            "reclaimable_bytes": figures[1] if figures else None,
        },
        "dependencies": [],
        "binaries": {},
    }
    rows = [
        ("Versi", VERSION),
        ("Platform", platform_key()),
        ("Python", platform.python_version() + " · " + sys.executable),
        ("Tools", str(BASE)),
        ("Media", data["media"]),
        ("Catatan", state_note(figures)),
        ("Jeda", pace_note(data)),
    ]
    try:
        from packaging.requirements import Requirement
    except ImportError:
        Requirement = None
        rows.append(("packaging", "belum terpasang · jalankan setup"))
    data["packaging_installed"] = Requirement is not None
    pins = lock_requirements((BASE / "requirements.lock").read_text(encoding="utf-8"))
    for line in pins if Requirement else []:
        dependency = Requirement(line)
        if dependency.marker and not dependency.marker.evaluate():
            continue
        try:
            actual = version(dependency.name)
        except PackageNotFoundError:
            rows.append((dependency.name, "belum terpasang"))
            data["dependencies"].append(
                {"name": dependency.name, "pin": str(dependency.specifier), "installed": None}
            )
            continue
        mismatch = " · TIDAK SESUAI " + str(dependency.specifier)
        rows.append(
            (
                dependency.name,
                actual
                + ("" if actual in dependency.specifier else mismatch)
                + pin_note(dependency.name, actual),
            )
        )
        data["dependencies"].append(
            {
                "name": dependency.name,
                "pin": str(dependency.specifier),
                "installed": actual,
                "matches_pin": actual in dependency.specifier,
                "release_age_days": (
                    release_age_days(actual) if dependency.name.lower() == "yt-dlp" else None
                ),
            }
        )
    for name in ("ffmpeg", "ffprobe", "deno"):
        binary = str(deno_path(BASE)) if name == "deno" else shutil.which(name)
        try:
            r = subprocess.run(
                [binary, "--version" if name == "deno" else "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            first = (r.stdout.splitlines() or ["tidak tersedia"])[0]
            rows.append((name, first + " · " + str(binary)))
            data["binaries"][name] = {"available": True, "version": first, "path": str(binary)}
        except (OSError, TypeError, subprocess.TimeoutExpired):
            rows.append((name, "tidak tersedia · jalankan setup --check"))
            data["binaries"][name] = {"available": False}
    jobs, unreadable = list_jobs()
    data["jobs"] = []
    for j in jobs[:10]:
        entry = {"id": j.get("id"), "status": j.get("status"), "items": len(j.get("items") or [])}
        where = job_timings(j, detail=timings)
        if where:
            entry["timings"] = where
        data["jobs"].append(entry)
    data["unreadable_jobs"] = [{"file": name, "reason": reason} for name, reason in unreadable]
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
        return
    ui.table(("Informasi", "Nilai"), rows)
    table = [(j.get("id", "?"), j.get("status", "?"), len(j.get("items") or [])) for j in jobs[:10]]
    table += [(name, "tidak terbaca", "-") for name, _ in unreadable]
    if table:
        ui.table(("Job", "Status", "Item"), table)
    for _name, reason in unreadable:
        ui.say(Text(reason, style=DIM))
    if timings:
        newest = next((j for j in jobs if job_timings(j)), None)
        if newest:
            timings_table(ui, newest)
        else:
            ui.say("Belum ada job dengan catatan waktu.", DIM)


def job_when(job):
    try:
        stamp = job.get("updated_at") or job["created_at"]
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (KeyError, TypeError, ValueError):
        return "-"


def job_label(job):
    items = job.get("items") or []
    done = sum(1 for i in items if i.get("status") == "complete")
    return f"{job.get('id')} · {job.get('status')} · {done}/{len(items)} selesai · {job_when(job)}"


def choose_job(ui, args):
    """The job `resume` carries on with when no ID was given: the newest one that has something
    left to do (--last), or the one picked from a menu."""
    if args.last and args.job:
        raise ValueError("Gunakan resume ID_JOB atau resume --last, bukan keduanya")
    if not args.last and not sys.stdin.isatty():
        raise ValueError("Gunakan resume ID_JOB atau resume --last")
    candidates = resumable_jobs(list_jobs()[0])
    if not candidates:
        raise ValueError("Tidak ada job yang bisa dilanjutkan")
    if args.last:
        return candidates[0]
    picked = ui.choose(
        "Job mana yang dilanjutkan?", [(job_label(j), j["id"]) for j in candidates], label="Job"
    )
    return load_job(picked)


def clean_command(ui, args):
    import maintenance

    abandoned = getattr(args, "abandoned", False)
    if abandoned:
        jobs = maintenance.abandoned_jobs(BASE, args.days)
        if jobs:
            ui.table(
                ("Job ditinggalkan", "Status", "Umur", "Ukuran"),
                [
                    (job_id, status, f"{age} hari", size(sum(w for _, w in entries)))
                    for job_id, status, age, entries in jobs
                ],
            )
            ui.say(
                Text(
                    "Jobnya tetap tersimpan; resume mengunduh ulang dari awal tanpa folder ini.",
                    style=DIM,
                )
            )
    with lock() if args.apply else nullcontext():
        rows = maintenance.clean(BASE, args.apply, args.days, abandoned)
    if not rows:
        ui.say("Tidak ada yang perlu dibersihkan.")
        return 0
    ui.table(
        ("Kategori", "Jumlah", "Ukuran"),
        [(label, count, size(weight)) for label, count, weight in rows],
    )
    total = size(sum(weight for _, _, weight in rows))
    ui.say(
        "Dibebaskan " + total
        if args.apply
        else f"Dapat dibebaskan {total} · jalankan clean --apply untuk menghapusnya"
    )
    return 0


def rebase_command(args):
    if not args.old_root or not args.new_root:
        raise ValueError("Gunakan rebase-media --from LAMA --to BARU [--dry-run]")
    from media_migration import rebase_media

    with lock():
        rebase_media(BASE, args.old_root, args.new_root, args.dry_run)
    return 0


def preview(ui):
    ui.say("YouTube Downloader · preview simulasi", BLUE)
    items = [
        {"title": "Contoh video yang sudah pernah diunduh", "choice": {"mode": "video-audio"}},
        {"title": "Contoh video dan audio", "choice": {"mode": "video-audio"}},
        {"title": "Contoh audio saja", "choice": {"mode": "audio"}},
    ]

    def stage(session, message, phase, pause=0.2):
        session.event({"type": "stage", "message": message, "phase": phase})
        time.sleep(pause)

    def fetch(session, stream, name, unknown=False):
        stage(session, "Mengunduh " + stream, stream, 0)
        session.event({"type": "start", "name": name, "stream": stream})
        if unknown:
            # No size yet: a moving block instead of a percentage, then a pause with a countdown.
            time.sleep(0.25)
            session.event(
                {"type": "wait", "message": "Menunggu · simulasi", "until": time.time() + 0.3}
            )
            time.sleep(0.3)
        for i in range(0, 101, 5):
            session.event(
                {
                    "type": "progress",
                    "downloaded": i * 1048576,
                    "total": 104857600,
                    "speed": 10 * 1048576,
                    "eta": (100 - i) / 10,
                }
            )
            time.sleep(0.035)
        session.event({"type": "file_done"})

    if not ui.plain:
        with ui.activity("Mengambil informasi YouTube · simulasi") as progress:
            for step in range(1, 4):
                time.sleep(0.25 if ui.motion else 0)
                progress(f"Memeriksa kualitas dan subtitle · {step}/3", (step, 3))
    with Session(ui, len(items), items) as session:
        time.sleep(0.3)
        session.item_end("skipped")
        for item in items[1:]:
            audio_only = item["choice"]["mode"] == "audio"
            session.item_start(phases(item))
            stage(session, "Memeriksa format dan akses terbaru", "metadata")
            if not audio_only:
                fetch(session, "video", "Contoh video")
            fetch(
                session,
                "audio",
                "Ukuran belum diketahui" if audio_only else "Contoh audio",
                audio_only,
            )
            stage(
                session,
                "Menyiapkan audio" if audio_only else "Menggabungkan video + audio",
                "finish",
            )
            session.item_end("complete")
    ui.say("Preview selesai; tidak ada jaringan, file, atau download yang dibuat.")


def quality_arg(text):
    if text == "best":
        return text
    if text.isdigit() and 100 <= int(text) <= 9999:
        return int(text)
    raise argparse.ArgumentTypeError("gunakan best atau tinggi video, misalnya 1080")


EPILOG = """contoh:
  youtube-downloader LINK                  wizard interaktif
  youtube-downloader LINK --yes --quality 1080 --subs en
                                           tanpa menu; path hasil dicetak ke stdout
  youtube-downloader resume                pilih job yang dilanjutkan (resume --last: yang terbaru)
  youtube-downloader --links-file link.txt --yes
                                           satu job untuk banyak link (satu per baris)
  youtube-downloader status [--json] [--timings]
                                           versi, dependensi, umur yt-dlp, job, ukuran .state, waktu
  youtube-downloader clean [--apply] [--abandoned]
                                           lihat atau hapus sisa di .state
  youtube-downloader update [--check]      periksa atau pasang rilis stabil
  youtube-downloader rebase-media --from LAMA --to BARU [--dry-run]

setup.sh, migrate.sh, dan rollback.sh (setup.cmd, dst. di Windows) mengubah lingkungan Python
dan dijalankan terpisah dari perintah ini."""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="youtube-downloader",
        description="Download YouTube: pilih video/audio, kualitas, dan subtitle.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"youtube-downloader {VERSION}")
    parser.add_argument(
        "action",
        nargs="?",
        help="Link YouTube, atau: status, preview, resume, update, clean, rebase-media",
    )
    parser.add_argument("job", nargs="?", help="ID job untuk resume")
    parser.add_argument("--plain", action="store_true", help="Keluaran teks biasa tanpa warna")
    parser.add_argument("--browser", help="Opsional: firefox atau firefox:/jalur/profil")
    parser.add_argument("--output", help="Folder hasil alternatif")
    parser.add_argument("--check", action="store_true", help="Update: periksa rilis tanpa memasang")
    parser.add_argument("--wait", action="store_true", help="Update: tunggu download aktif selesai")
    parser.add_argument(
        "--last", action="store_true", help="Resume: job terbaru yang belum selesai"
    )
    parser.add_argument("--apply", action="store_true", help="Clean: hapus, bukan hanya daftar")
    parser.add_argument("--days", type=int, default=30, help="Clean: umur cadangan media (hari)")
    parser.add_argument(
        "--abandoned",
        action="store_true",
        help="Clean: juga folder sementara job yang belum selesai dan lebih tua dari --days",
    )
    parser.add_argument(
        "--json", action="store_true", help="Status: satu objek JSON di stdout, tanpa tabel"
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="Status: waktu tiap fase per item (tabel, atau rinci per item di --json)",
    )
    parser.add_argument("--from", dest="old_root", help="Rebase-media: akar media lama")
    parser.add_argument("--to", dest="new_root", help="Rebase-media: akar media baru")
    parser.add_argument("--dry-run", action="store_true", help="Rebase-media: tanpa mengubah")
    scripting = parser.add_argument_group(
        "tanpa menu (untuk link)", "Jawaban untuk pertanyaan wizard; --yes memakai bawaan sisanya."
    )
    scripting.add_argument("--mode", choices=["video-audio", "audio", "video"])
    scripting.add_argument("--quality", type=quality_arg, help="best atau tinggi video, mis. 1080")
    scripting.add_argument("--format", choices=["auto", "mp4"], help="mp4 = MP4 kompatibel")
    scripting.add_argument("--lang", help="Bahasa audio, atau original")
    scripting.add_argument("--audio-format", choices=["original", "mp3"])
    scripting.add_argument("--subs", help="Bahasa subtitle dipisah koma, atau none")
    scripting.add_argument("--items", help="Playlist: nomor/rentang, mis. 1,3,5-10, atau all")
    scripting.add_argument(
        "--playlist", action="store_true", help="Link video+playlist: ambil playlist"
    )
    scripting.add_argument(
        "--yes", action="store_true", help="Mulai tanpa bertanya; tanpa terminal"
    )
    scripting.add_argument(
        "--links-file",
        help="Berkas berisi satu link per baris (# = komentar); satu job untuk semuanya, "
        "hanya bersama --yes",
    )
    return parser


def scripted_preset(args):
    """The answers the command line gave, by the key of the prompt they answer."""
    preset = {}
    if args.mode:
        preset["mode"] = args.mode
    if args.format:
        preset["compatible"] = args.format == "mp4"
    if args.quality is not None:
        preset["quality"] = args.quality
    if args.lang:
        preset["lang"] = args.lang
    if args.audio_format:
        preset["audio_format"] = args.audio_format
    if args.subs is not None:
        preset["subs"] = (
            []
            if args.subs.strip().lower() == "none"
            else [x.strip() for x in args.subs.split(",") if x.strip()]
        )
    if args.items:
        preset["items"] = args.items
    if args.playlist:
        preset["playlist"] = True
    if args.yes:
        preset["yes"] = True
    return preset


def main(argv=None):
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    ui = UI(args.plain)
    job = None
    try:
        if args.action == "update":
            if args.job or args.browser or args.output:
                raise ValueError("Gunakan update [--check] [--wait] [--plain]")
            from self_update import perform

            return perform(
                BASE, args.check, args.wait, lambda message: ui.say(message, BLUE), ui.activity
            )
        if args.check or args.wait:
            raise ValueError("--check/--wait hanya digunakan dengan update")
        if args.json and args.action != "status":
            raise ValueError("--json hanya digunakan dengan status")
        if args.timings and args.action != "status":
            raise ValueError("--timings hanya digunakan dengan status")
        if args.abandoned and args.action != "clean":
            raise ValueError("--abandoned hanya digunakan dengan clean")
        preset = scripted_preset(args)
        if args.action in COMMANDS and (preset or args.links_file):
            raise ValueError("Flag tanpa menu hanya untuk membuat job dari link")
        if args.action in SCRIPT_COMMANDS:
            ext = "cmd" if os.name == "nt" else "sh"
            raise ValueError(
                f"{args.action} dijalankan lewat {args.action}.{ext}: perintah itu mengubah "
                "lingkungan Python yang sedang dipakai"
            )
        if args.action == "preview":
            preview(ui)
            return 0
        if args.action == "status":
            status(ui, args.json, args.timings)
            return 0
        if args.action == "clean":
            return clean_command(ui, args)
        if args.action == "rebase-media":
            return rebase_command(args)
        source = args.action
        if args.links_file:
            if args.action or args.job:
                raise ValueError("--links-file menggantikan link; jangan beri link lagi")
            if not args.yes:
                raise ValueError("--links-file hanya bersama --yes (dengan flag pilihan)")
            if args.items or args.playlist:
                raise ValueError("--items dan --playlist tidak berlaku bersama --links-file")
            source = read_links(args.links_file)
        ui.banner(VERSION, animate=not preset.get("yes") and sys.stdin.isatty())
        ui.say("")
        if args.browser:
            valid_browser(args.browser)
        current_pace()  # a YTDL_PACE that is not a profile stops here, before anything is asked
        with lock():
            if args.action == "resume":
                job = load_job(args.job) if args.job and not args.last else choose_job(ui, args)
                if args.browser:
                    job["browser"] = args.browser
                    job["platform"] = platform_key()
                if args.output and str(Path(args.output).expanduser().resolve()) != job["output"]:
                    raise ValueError("Output job tidak dapat diubah saat resume")
            else:
                if args.job:
                    raise ValueError("Argumen tambahan tidak valid")
                if not (sys.stdin.isatty() or preset.get("yes")):
                    raise ValueError(
                        "Wizard membutuhkan terminal. Gunakan --yes (dengan flag pilihan) untuk "
                        "berjalan tanpa terminal, atau resume ID_JOB untuk job yang sudah tersimpan."
                    )
                job = wizard(
                    ui, source, args.browser or settings().get("browser"), args.output, preset
                )
            try:
                execute(ui, job)
            finally:
                emit_paths(job, bool(preset.get("yes")))
        return 0
    except (KeyboardInterrupt, Cancelled):
        if args.action == "update":
            ui.say("Pembaruan dihentikan sebelum pemasangan.", BLUE)
        elif job:
            ui.say("Dihentikan. Job tersimpan; lanjutkan dengan:", BLUE)
            ui.say(Text("    " + resume_command(job["id"]), style=BLUE))
        else:
            ui.say("Dihentikan sebelum ada job yang tersimpan.", BLUE)
        return 130
    except Stopped as exc:
        ui.error(str(exc), STATE / "download.log", resume_command(exc.job_id, exc.login))
        return 1
    except Exception as exc:
        log = STATE / ("update.log" if args.action == "update" else "download.log")
        # A usage error never wrote a log: name the file only if there is one to read.
        ui.error(str(exc), log if log.exists() else None)
        return 1


if __name__ == "__main__":
    sys.exit(main())
