"""Interactive YouTube downloader entry point."""

import argparse
import os
import sys
import time
import platform
import subprocess
import shutil
from compat import native_output, platform_key, deno_path, foreign_path, lock_requirements
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from importlib.metadata import PackageNotFoundError, version
from model import (
    VERSION,
    SelectionError,
    valid_url,
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
)
from store import BASE, STATE, lock, settings, new_job, load_job, save_job, Archive
from runner import run, WorkerError
from rich.text import Text
from ui import UI, Session, Cancelled, BLUE, DIM, RED, clock, size, tidy


def inspect_url(url, browser=None, playlist=False):
    return run({"action": "inspect", "url": url, "browser": browser, "playlist": playlist})


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


def wizard(ui, url, browser=None, output=None):
    url = valid_url(url or ui.text("Tempel link YouTube:", label="Link"))
    qs = parse_qs(urlparse(url).query)
    is_playlist = "list" in qs or urlparse(url).path == "/playlist"
    if is_playlist and ("v" in qs or urlparse(url).hostname == "youtu.be"):
        is_playlist = ui.choose(
            "Link berisi video dan playlist. Ambil yang mana?",
            [("Video ini saja", False), ("Playlist", True)],
            label="Sumber",
        )
    with ui.activity("Mengambil informasi YouTube"):
        initial = inspect_url(url, browser, is_playlist)
    if initial.get("playlist"):
        entries = initial["entries"]
        if not entries:
            raise ValueError("Playlist kosong atau tidak dapat diakses")
        ui.say(
            Text.assemble(
                (initial["title"] or "Playlist", "bold"), (f" · {len(entries)} item", DIM)
            )
        )
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
        metadata = []
        unavailable = []
        errors = 0
        total = len(selected)
        label = "Memeriksa kualitas dan subtitle"
        with ui.activity(f"{label} · 0/{total}") as progress:
            for number, entry in enumerate(selected, 1):
                if progress:
                    progress(f"{label} · {number}/{total}")
                try:
                    info = inspect_url(entry["url"], browser)
                    metadata.append(info)
                    errors = 0
                except WorkerError as exc:
                    unavailable.append({"title": entry["title"], "reason": str(exc)})
                    if exc.category == "authentication":
                        raise
                    if exc.category not in ("unavailable",):
                        errors += 1
                    if errors >= 3:
                        raise WorkerError(
                            "Tiga kegagalan metadata berurutan; batch dihentikan. " + str(exc),
                            exc.category,
                        ) from exc
        if unavailable:
            ui.table(
                ("Item tidak tersedia", "Alasan"), [(x["title"], x["reason"]) for x in unavailable]
            )
    else:
        metadata = [initial]
    if not metadata:
        raise ValueError("Tidak ada item yang dapat diunduh")
    if len(metadata) == 1:
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
    ui.say("")
    all_tracks = {}
    for m in metadata:
        for t in tracks(m):
            identity = (
                (t["language"], t["kind"])
                if len(metadata) > 1
                else (t["language"], t["kind"], t["key"], t["origin"])
            )
            all_tracks.setdefault(identity, t)
    mode = ui.choose(
        "Apa yang ingin diunduh?",
        [
            ("Video + audio", "video-audio"),
            ("Audio saja", "audio"),
            ("Video saja — tanpa suara", "video"),
        ],
        label="Unduh apa?",
    )
    steps = Steps(len(settings_plan(mode, bool(all_tracks))))
    quality = "best"
    manual = False
    audio_format = "original"
    compatible = False
    if mode != "audio":
        sound = " + AAC" if mode == "video-audio" else ""
        compatible = ui.choose(
            "Format file",
            [
                ("Otomatis · MKV atau MP4 sesuai codec · Direkomendasikan", False),
                (f"MP4 kompatibel · H.264/AV1{sound} · iPhone, Mac, editor", True),
            ],
            step=steps.next(),
            label="Format file",
        )
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
        quality = ui.choose(title, options, default="best", step=steps.next(), label="Kualitas")
        manual = quality == "manual"
        if manual:
            quality = "best"
    lang = "original"
    if mode != "video":
        available = sorted({lang for m in metadata for lang in languages(m)})
        lang = ui.choose(
            "Bahasa audio",
            [("Bahasa asli per video", "original")] + [(x, x) for x in available],
            step=steps.next(),
            label="Bahasa audio",
        )
        if mode == "audio":
            audio_format = ui.choose(
                "Format audio",
                [
                    ("Audio asli terbaik · tanpa konversi", "original"),
                    ("MP3 · konversi lossy", "mp3"),
                ],
                step=steps.next(),
                label="Format audio",
            )
            manual = ui.choose(
                "Pemilihan kualitas audio",
                [("Audio terbaik otomatis", False), ("Pilih format/bitrate per item", True)],
                step=steps.next(),
                label="Kualitas audio",
            )
    chosen = []
    if all_tracks:
        addon = ui.choose(
            "Tambahan subtitle",
            [("Tanpa subtitle", False), ("Pilih bahasa/track subtitle", True)],
            step=steps.next(),
            label="Subtitle",
        )
        if addon:
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
            chosen = ui.checkboxes(
                "Track subtitle",
                [
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
                ],
            )
    else:
        ui.say(Text("YouTube tidak mengembalikan subtitle untuk pilihan ini.", style=DIM))
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
    if not items:
        raise ValueError("Tidak ada item yang memenuhi pilihan")
    root = str(native_output(output or settings()["output"]))
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
    if (
        ui.choose(
            "Mulai download?",
            [("Mulai", True), ("Batal", False)],
            step=steps.next(),
            label="Mulai?",
        )
        is False
    ):
        raise Cancelled()
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
                    }
                    if previous:
                        request["existing_file"] = previous["path"]
                    item["status"] = "running"
                    save_job(job)
                    session.item_start(phases(item))
                    try:
                        result = run(request, session.event)
                        archive.put(key, result)
                        item["result"] = result
                        item["status"] = "partial" if result.get("missing") else "complete"
                        item.pop("error", None)
                        session.item_end(item["status"])
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
                        if exc.category == "authentication" or consecutive >= 3:
                            job["status"] = "blocked"
                            raise ValueError(
                                "Batch dihentikan. "
                                + (
                                    "Login di Firefox lalu ulangi dengan pilihan sesi yang benar."
                                    if exc.category == "authentication"
                                    else "Tiga kegagalan sistem berurutan; periksa log/dependensi sebelum resume."
                                )
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
        raise ValueError(
            "Ada item/subtitle belum selesai; resume " + job["id"] + " untuk mencoba lagi."
        )


def status(ui):
    rows = [
        ("Versi", VERSION),
        ("Platform", platform_key()),
        ("Python", platform.python_version() + " · " + sys.executable),
        ("Tools", str(BASE)),
        ("Media", str(Path(settings()["output"]).expanduser())),
        ("Catatan", str(STATE)),
    ]
    try:
        from packaging.requirements import Requirement
    except ImportError:
        Requirement = None
        rows.append(("packaging", "belum terpasang · jalankan setup"))
    pins = lock_requirements((BASE / "requirements.lock").read_text(encoding="utf-8"))
    for line in pins if Requirement else []:
        dependency = Requirement(line)
        if dependency.marker and not dependency.marker.evaluate():
            continue
        try:
            actual = version(dependency.name)
        except PackageNotFoundError:
            rows.append((dependency.name, "belum terpasang"))
            continue
        mismatch = " · TIDAK SESUAI " + str(dependency.specifier)
        rows.append(
            (dependency.name, actual + ("" if actual in dependency.specifier else mismatch))
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
            rows.append(
                (name, (r.stdout.splitlines() or ["tidak tersedia"])[0] + " · " + str(binary))
            )
        except (OSError, TypeError, subprocess.TimeoutExpired):
            rows.append((name, "tidak tersedia · jalankan setup --check"))
    ui.table(("Informasi", "Nilai"), rows)
    jobs = (
        sorted((STATE / "jobs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        if (STATE / "jobs").exists()
        else []
    )
    if jobs:
        ui.table(
            ("Job", "Status", "Item"),
            [(j["id"], j["status"], len(j["items"])) for j in (load_job(p.stem) for p in jobs)],
        )


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


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Download YouTube: pilih video/audio, kualitas, dan subtitle."
    )
    parser.add_argument(
        "action", nargs="?", help="Link YouTube, status, preview, resume, atau update"
    )
    parser.add_argument("job", nargs="?", help="ID job untuk resume")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--browser", help="Opsional: firefox atau firefox:/jalur/profil")
    parser.add_argument("--output", help="Folder hasil alternatif")
    parser.add_argument("--check", action="store_true", help="Update: periksa rilis tanpa memasang")
    parser.add_argument("--wait", action="store_true", help="Update: tunggu download aktif selesai")
    args = parser.parse_args()
    ui = UI(args.plain)
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
        if args.action == "preview":
            preview(ui)
            return 0
        if args.action == "status":
            status(ui)
            return 0
        ui.banner(VERSION)
        ui.say("")
        if args.browser and not (args.browser == "firefox" or args.browser.startswith("firefox:")):
            raise ValueError("Pilihan browser: firefox atau firefox:/jalur/profil")
        with lock():
            if args.action == "resume":
                if not args.job:
                    raise ValueError("Gunakan resume ID_JOB")
                job = load_job(args.job)
                if args.browser:
                    job["browser"] = args.browser
                    job["platform"] = platform_key()
                if args.output and str(Path(args.output).expanduser().resolve()) != job["output"]:
                    raise ValueError("Output job tidak dapat diubah saat resume")
            else:
                if args.job:
                    raise ValueError("Argumen tambahan tidak valid")
                if not sys.stdin.isatty():
                    raise ValueError(
                        "Wizard membutuhkan terminal. Gunakan resume ID_JOB untuk job yang sudah tersimpan."
                    )
                job = wizard(
                    ui, args.action, args.browser or settings().get("browser"), args.output
                )
            execute(ui, job)
        return 0
    except (KeyboardInterrupt, Cancelled):
        ui.say(
            "Pembaruan dihentikan sebelum pemasangan."
            if args.action == "update"
            else "Dihentikan; job yang sudah tersimpan dapat dilanjutkan dengan resume ID_JOB.",
            BLUE,
        )
        return 130
    except Exception as exc:
        ui.error(str(exc), STATE / ("update.log" if args.action == "update" else "download.log"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
