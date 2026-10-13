"""yt-dlp public hooks and FFmpeg stream-copy finalization in an isolated process."""

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from compat import deno_path, worker_start
from pathlib import Path
import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.utils import ISO639Utils
from model import container, basename, variant, classify
from store import BASE, probe


class Events:
    def __init__(self, stream):
        self.stream = stream
        self.tick = 0
        self.stream_name = "media"
        self.started = False

    def send(self, kind, **kwargs):
        self.stream.write(json.dumps({"type": kind, **kwargs}, ensure_ascii=True) + "\n")
        self.stream.flush()

    def hook(self, data):
        status = data.get("status")
        if status == "downloading":
            if not self.started:
                self.send(
                    "start", name=Path(data.get("filename") or "").name, stream=self.stream_name
                )
                self.started = True
            now = time.monotonic()
            if now - self.tick >= 0.25:
                self.tick = now
                self.send(
                    "progress",
                    downloaded=data.get("downloaded_bytes"),
                    total=data.get("total_bytes") or data.get("total_bytes_estimate"),
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                )
        elif status == "finished":
            self.send(
                "file_done", downloaded=data.get("downloaded_bytes"), total=data.get("total_bytes")
            )
            self.started = False

    def pp_hook(self, data):
        if data.get("status") == "started":
            self.send("stage", message="Memproses " + str(data.get("postprocessor", "media")))


class Logger:
    def __init__(self, events):
        self.events = events

    def debug(self, message):
        print(message, file=sys.stderr, flush=True)

    def info(self, message):
        print(message, file=sys.stderr, flush=True)

    def warning(self, message):
        print(message, file=sys.stderr, flush=True)
        if "retry" in message.lower():
            self.events.send("retry", message="Mencoba kembali · detail di log")

    def error(self, message):
        print(message, file=sys.stderr, flush=True)


def options(request, events):
    def retry_delay(n):
        seconds = min(30, 2**n)
        events.send("wait", until=time.time() + seconds, message="Menunggu sebelum mencoba kembali")
        return seconds

    opts = {
        "quiet": True,
        "no_warnings": False,
        "logger": Logger(events),
        "noprogress": True,
        "progress_hooks": [events.hook],
        "postprocessor_hooks": [events.pp_hook],
        "retries": 4,
        "fragment_retries": 4,
        "extractor_retries": 4,
        "socket_timeout": 30,
        "sleep_interval": 1,
        "max_sleep_interval": 2,
        "sleep_interval_requests": 1,
        "writeautomaticsub": True,
        "cachedir": str(BASE / ".state/cache"),
        "js_runtimes": {"deno": {"path": str(deno_path(BASE))}},
        "noplaylist": True,
        "overwrites": False,
        "continuedl": True,
        "retry_sleep_functions": {
            kind: retry_delay for kind in ("http", "fragment", "extractor", "file_access")
        },
    }
    browser = request.get("browser")
    if browser:
        opts["cookiesfrombrowser"] = (
            "firefox",
            browser.split(":", 1)[1] if ":" in browser else None,
            None,
            None,
        )
    return opts


def compact(info):
    keys = (
        "id",
        "title",
        "duration",
        "channel",
        "uploader",
        "webpage_url",
        "availability",
        "live_status",
        "language",
    )
    output = {k: info.get(k) for k in keys}
    fields = (
        "format_id",
        "width",
        "height",
        "fps",
        "vcodec",
        "acodec",
        "language",
        "language_preference",
        "filesize",
        "filesize_approx",
        "abr",
        "tbr",
        "ext",
        "format_note",
        "has_drm",
        "dynamic_range",
    )
    output["formats"] = [{k: f.get(k) for k in fields} for f in info.get("formats", [])]
    output["subtitles"] = info.get("subtitles", {})
    output["automatic_captions"] = info.get("automatic_captions", {})
    return output


def inspect(request, events):
    opts = options(request, events)
    opts["skip_download"] = True
    if request.get("playlist"):
        opts.update(noplaylist=False, extract_flat="in_playlist", lazy_playlist=False)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(request["url"], download=False)
    if not info:
        raise ValueError("YouTube tidak mengembalikan informasi")
    if info.get("_type") in ("playlist", "multi_video"):
        entries = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            identifier = entry.get("id")
            url = entry.get("webpage_url") or entry.get("url")
            if identifier and (not url or not url.startswith("http")):
                url = "https://www.youtube.com/watch?v=" + identifier
            entries.append(
                {
                    "id": identifier,
                    "url": url,
                    "title": entry.get("title") or identifier or "Tidak tersedia",
                }
            )
        return {"playlist": True, "title": info.get("title"), "entries": entries}
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("Livestream aktif/premiere mendatang belum didukung")
    return compact(info)


def ffmpeg(args, events, stage):
    events.send("stage", message=stage)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        stdout=sys.stderr,
    )


def subtitle_files(info, item, root, events, opts):
    saved = []
    missing = []
    prefix = basename(item)
    with yt_dlp.YoutubeDL(opts) as ydl:
        for track in item.get("subtitles", []):
            suffix = track["origin"] + "-" + track["key"]
            suffix = "".join(c if c.isalnum() or c in "-_" else "_" for c in suffix)
            target = root / "subtitle" / f"{prefix}.{suffix}.srt"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size:
                saved.append({**track, "path": str(target)})
                continue
            events.send("stage", message="Mengambil subtitle · " + track["name"])
            fs = info.get(track["origin"], {}).get(track["key"], [])
            formats = sorted(
                [f for f in fs if f.get("ext") in ("vtt", "srt", "ttml")],
                key=lambda f: ("vtt", "srt", "ttml").index(f["ext"]),
            )
            if not formats:
                missing.append(track)
                events.send("notice", message="Subtitle tidak tersedia · " + track["name"])
                continue
            f = formats[0]
            raw = target.with_suffix("." + f["ext"] + ".part")
            temp = target.with_suffix(".new.srt")
            try:
                error = None
                for attempt in range(5):
                    try:
                        with (
                            ydl.urlopen(
                                Request(f["url"], headers=info.get("http_headers", {}))
                            ) as response,
                            raw.open("wb") as stream,
                        ):
                            shutil.copyfileobj(response, stream)
                        error = None
                        break
                    except Exception as exc:
                        error = exc
                        if attempt == 4:
                            break
                        seconds = min(30, 2**attempt)
                        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                        value = headers.get("Retry-After")
                        if value and str(value).isdigit():
                            seconds = float(value)
                        events.send(
                            "wait", until=time.time() + seconds, message="Menunggu retry subtitle"
                        )
                        time.sleep(seconds)
                if error:
                    raise error
                ffmpeg(
                    ["-i", str(raw), "-c:s", "srt", str(temp)], events, "Menyiapkan subtitle SRT"
                )
                if not temp.stat().st_size:
                    raise ValueError("Subtitle kosong")
                temp.replace(target)
                saved.append({**track, "path": str(target)})
            except Exception as exc:
                print("Subtitle gagal:", exc, file=sys.stderr, flush=True)
                missing.append(track)
                events.send(
                    "notice", message="Subtitle gagal · " + track["name"] + "; media tetap disimpan"
                )
            finally:
                raw.unlink(missing_ok=True)
                temp.unlink(missing_ok=True)
    return saved, missing


def audio_language(code, extension):
    """The language tag to write on the audio track, or None to write none.

    Matroska accepts YouTube's two-letter codes (en, id, en-US). The MP4 muxer silently drops
    anything that is not a three-letter ISO 639-2 code, so those are converted for MP4."""
    if extension != "mp4":
        return code
    short = code.split("-")[0].lower()
    if len(short) == 3:
        return short
    return ISO639Utils.short2long(short)


def download(request, events):
    item = request["item"]
    choice = item["choice"]
    root = Path(request["output"])
    opts = options(request, events)
    prefix = basename(item)
    mode = choice["mode"]
    extension = container(choice, item.get("subtitles"))
    target = root / ("audio" if mode == "audio" else "video") / (prefix + "." + extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = BASE / ".state/temporary" / variant(item)
    temp_root.mkdir(parents=True, exist_ok=True)
    existing = request.get("existing_file")
    if target.exists() and not existing:
        raise ValueError("Benturan hasil: file sudah ada tanpa catatan arsip; tidak ditimpa")
    events.send("stage", message="Memeriksa format dan akses terbaru")
    metadata_opts = {**opts, "skip_download": True}
    with yt_dlp.YoutubeDL(metadata_opts) as ydl:
        info = ydl.extract_info(item["url"], download=False)
    if not info:
        raise ValueError("Informasi video kosong")
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("Livestream aktif tidak didukung")
    available = {f["format_id"] for f in info.get("formats", [])}
    needed = {x for x in (choice.get("video_format"), choice.get("audio_format_id")) if x}
    if not needed <= available:
        raise ValueError(
            "Format pilihan berubah; buat job baru untuk meninjau kualitas yang tersedia"
        )
    paths = {}
    if existing:
        probe(existing, mode)
        paths = {"video": existing, "audio": existing}
    else:
        for stream, format_id in (
            ("video", choice.get("video_format")),
            ("audio", choice.get("audio_format_id")),
        ):
            if not format_id:
                continue
            if format_id in paths:
                paths[stream] = paths[format_id]
                continue
            events.stream_name = stream
            events.started = False
            local = {
                **opts,
                "format": format_id,
                "outtmpl": str(temp_root / ("%(id)s-" + stream + ".%(ext)s")),
                "writeautomaticsub": False,
                "writesubtitles": False,
            }
            with yt_dlp.YoutubeDL(local) as ydl:
                downloaded = ydl.extract_info(item["url"], download=True)
                filepath = ydl.prepare_filename(downloaded)
            if not Path(filepath).is_file():
                raise ValueError("File sementara tidak ditemukan setelah download")
            paths[stream] = filepath
            paths[format_id] = filepath
    subtitles, missing = subtitle_files(info, item, root, events, opts)
    temp = target.with_name("." + target.stem + ".assembling." + extension)
    args = []
    if mode == "video-audio":
        args = [
            "-i",
            paths["video"],
            "-i",
            paths["audio"],
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
        ]
    elif mode == "video":
        args = ["-i", paths["video"], "-map", "0:v:0", "-an", "-c:v", "copy"]
    else:
        args = ["-i", paths["audio"], "-map", "0:a:0", "-vn"]
        args += (
            ["-c:a", "libmp3lame", "-q:a", "0"]
            if choice["audio_format"] == "mp3"
            else ["-c:a", "copy"]
        )
    if subtitles and mode != "audio" and extension == "mkv":
        # Only Matroska takes the SRT tracks; an MP4 keeps them as the separate .srt files.
        # ffmpeg input options must precede mapping/output options.
        base_inputs = ["-i", paths["video"]]
        if mode == "video-audio":
            base_inputs += ["-i", paths["audio"]]
        input_count = 2 if mode == "video-audio" else 1
        args = (
            base_inputs
            + sum((["-i", track["path"]] for track in subtitles), [])
            + args[len(base_inputs) :]
        )
        for index, track in enumerate(subtitles):
            args += [
                "-map",
                f"{input_count + index}:0",
                f"-metadata:s:s:{index}",
                "title=" + track["name"],
                f"-metadata:s:s:{index}",
                "language=" + track["language"],
            ]
        args += ["-c:s", "srt"]
    if mode != "video" and choice.get("language") != "und":
        language = audio_language(choice["language"], extension)
        if language:
            args += ["-metadata:s:a:0", "language=" + language]
    if extension == "mp4":
        # Moves the index to the front so QuickTime, iOS and browsers can start playing at once.
        args += ["-movflags", "+faststart"]
    try:
        ffmpeg(
            [*args, str(temp)],
            events,
            "Menggabungkan video + audio" if mode == "video-audio" else "Menyiapkan " + mode,
        )
        probe(temp, mode)
        if target.exists() and not existing:
            raise ValueError("Benturan nama setelah download; hasil lama tidak ditimpa")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    shutil.rmtree(temp_root)
    events.send("stage", message="Hasil akhir tersimpan")
    return {"path": str(target), "mode": mode, "subtitles": subtitles, "missing": missing}


def main():
    os.umask(0o077)
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    events = Events(sys.stdout)
    code = 0
    try:
        worker_start()
        with redirect_stdout(sys.stderr):
            result = (
                inspect(request, events)
                if request["action"] == "inspect"
                else download(request, events)
            )
        events.send("done", result=result)
    except KeyboardInterrupt:
        code = 130
        events.send("done", error="Dihentikan", category="interrupted")
    except Exception as exc:
        import traceback

        traceback.print_exc(file=sys.stderr)
        code = 1
        events.send("done", error=str(exc), category=classify(exc))
    finally:
        events.stream.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
