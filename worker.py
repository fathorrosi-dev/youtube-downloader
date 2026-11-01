"""yt-dlp public hooks and FFmpeg stream-copy finalization in an isolated process."""

import copy
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stdout, suppress
from compat import deno_path, worker_start
from pathlib import Path
import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.utils import ISO639Utils
from model import (
    container,
    basename,
    variant,
    classify,
    pace_of,
    pace_profile,
    subtitle_path,
    subtitle_prefix,
)
from store import BASE, probe


class Timings:
    """How long each phase of one download took, by a monotonic clock, and how much of that time
    was spent waiting on purpose (the pause between downloads, the pause before a retry).

    The phases are metadata, video, audio, subtitle and finish (merge, check, move into place);
    a phase that did not run is absent. The waits are counted inside the phase they happened
    in, so the phases add up to the total apart from the start and the gaps between them."""

    PHASES = ("metadata", "video", "audio", "subtitle", "finish")

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.began = clock()
        self.seconds = {}
        self.waits = {}
        self.current = None
        self.bytes = 0

    @contextmanager
    def phase(self, name):
        before, self.current = self.current, name
        started = self.clock()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0.0) + self.clock() - started
            self.current = before

    def waited(self, seconds):
        self.waits[self.current] = self.waits.get(self.current, 0.0) + seconds

    def report(self):
        out = {name: round(self.seconds[name], 3) for name in self.PHASES if name in self.seconds}
        out["total"] = round(self.clock() - self.began, 3)
        out["waited"] = round(sum(self.waits.values()), 3)
        out["bytes"] = self.bytes
        # The average speed counts only the time the streams were not waiting.
        busy = sum(
            self.seconds.get(name, 0.0) - self.waits.get(name, 0.0) for name in ("video", "audio")
        )
        out["speed"] = round(self.bytes / busy, 1) if self.bytes and busy > 0.05 else None
        return out


class Events:
    def __init__(self, stream):
        self.stream = stream
        self.tick = 0
        self.stream_name = "media"
        self.started = False
        self.timings = None

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
            # A file that was already there finishes without ever having been downloaded.
            if self.started and self.timings:
                self.timings.bytes += data.get("downloaded_bytes") or data.get("total_bytes") or 0
            self.started = False

    def pp_hook(self, data):
        if data.get("status") == "started":
            self.send("stage", message="Memproses " + str(data.get("postprocessor", "media")))


def wait(events, seconds, message):
    """Tell the panel that the worker is waiting for `seconds`, and count the time as waiting in
    the phase that is running."""
    events.send("wait", until=time.time() + seconds, message=message)
    timings = getattr(events, "timings", None)
    if timings:
        timings.waited(seconds)


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
        wait(events, seconds, "Menunggu sebelum mencoba kembali")
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
        # The pause before each file is made by the worker (pause_before_download), where the
        # panel can show it; yt-dlp keeps the pause between the requests of one lookup.
        "sleep_interval_requests": pace_of(request)["requests"],
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


def ffmpeg(args, events, stage, phase="finish"):
    events.send("stage", message=stage, phase=phase)
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
            check=True,
            stdout=sys.stderr,
        )
    except subprocess.CalledProcessError as exc:
        # The command line (with every temporary path) and FFmpeg's own message are in the log;
        # the person sees one sentence.
        raise ValueError(
            f"FFmpeg gagal (kode {exc.returncode}) saat {stage[:1].lower() + stage[1:]}; "
            "detail di .state/download.log"
        ) from exc


WINDOWS_PATH_LIMIT = 259


def path_too_long(path, windows=None, limit=WINDOWS_PATH_LIMIT):
    """The length of `path` when Windows would refuse it (260 characters with the terminator
    unless long paths are on, and FFmpeg does not rely on them), otherwise None."""
    windows = os.name == "nt" if windows is None else windows
    return len(str(path)) if windows and len(str(path)) > limit else None


# The longest the worker sleeps because a server asked it to. A Retry-After of an hour would
# otherwise hold one subtitle of a playlist for an hour, once per attempt.
MAX_RETRY_AFTER = 60
# A server that has answered "too many requests" for this many subtitles in a row has said
# enough: the rest of the subtitles of this video are left, with that as the reason, instead of
# asking it a hundred more times. A later run (resume) asks again.
LIMITED_STOP = 2


def rate_limited(exc):
    """True when a request was refused with HTTP 429 (too many requests)."""
    status = getattr(getattr(exc, "response", None), "status", None) or getattr(exc, "status", None)
    return status == 429 or classify(exc) == "rate_limit"


def impersonation_target(ydl, wanted):
    """The browser that a request has to look like, or None when it need not or cannot.

    yt-dlp marks the subtitles of YouTube `impersonate: True`: YouTube answers 429 to a request
    that does not come with the TLS fingerprint of a browser, however long one waits. Its own
    downloader looks the target up like this (and warns when there is none); curl_cffi is what
    provides one. None means: ask the plain way."""
    if not wanted:
        return None
    try:
        available, _requested = ydl._parse_impersonate_targets(wanted)
    except (AttributeError, TypeError, ValueError):
        return None
    return available


def failure_reason(exc):
    """Why a subtitle was not saved, as a short code (model.MISSING_REASONS says it in words).
    Only the kind of failure is kept, never the message, which can carry a signed link."""
    if rate_limited(exc):
        return "limited"
    text = str(exc)
    if text.startswith("FFmpeg gagal"):
        return "ffmpeg"
    if text.startswith("Subtitle kosong"):
        return "empty"
    return "failed"


def longest_subtitle_path(root, item):
    """The longest path that saving the subtitles of `item` writes to: a subtitle itself, the
    download beside it (.vtt, .srt or .ttml followed by .part) or the conversion (.new.srt).
    None when the item has no subtitles. The format of the download is not known before the
    video is looked up, so the longest of the three is taken."""
    prefix = subtitle_prefix(item)
    paths = []
    for track in item.get("subtitles", []):
        target = subtitle_path(root, prefix, track)
        paths += [target, target.with_suffix(".ttml.part"), target.with_suffix(".new.srt")]
    return max(paths, key=lambda path: len(str(path)), default=None)


def subtitle_files(info, item, root, events, opts, pace=None, sleep=time.sleep):
    """Save the subtitles of `item` as .srt files. Returns (saved, missing): the tracks saved,
    with their path, and the tracks that were not, each with the `reason` it was not.

    The tracks are fetched one after the other with the pause of the pace profile between them,
    as media files are: a video can have a hundred of them, and a server that is asked that
    often without a pause answers 429."""
    pace = pace or pace_profile()
    saved = []
    missing = []
    prefix = subtitle_prefix(item)
    fetched = 0
    limited = 0
    unequipped = False
    with yt_dlp.YoutubeDL(opts) as ydl:
        for track in item.get("subtitles", []):
            target = subtitle_path(root, prefix, track)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size:
                saved.append({**track, "path": str(target)})
                continue
            if limited >= LIMITED_STOP:
                missing.append({**track, "reason": "limited"})
                continue
            if unequipped:
                # Refused once for want of curl_cffi: the next request would be refused the same.
                missing.append({**track, "reason": "impersonation"})
                continue
            if fetched:
                pause_before_download(events, pace, sleep, message="Menunggu · jeda antar-subtitle")
            fetched += 1
            events.send("stage", message="Mengambil subtitle · " + track["name"], phase="subtitle")
            fs = info.get(track["origin"], {}).get(track["key"], [])
            formats = sorted(
                [f for f in fs if f.get("ext") in ("vtt", "srt", "ttml")],
                key=lambda f: ("vtt", "srt", "ttml").index(f["ext"]),
            )
            if not formats:
                missing.append({**track, "reason": "unavailable"})
                events.send("notice", message="Subtitle tidak tersedia · " + track["name"])
                continue
            f = formats[0]
            browser = impersonation_target(ydl, f.get("impersonate"))
            # Wanted and not possible: the plain request is still made (some sessions are let
            # through), but a refusal of it is blamed on the missing curl_cffi, not on the rate.
            plain = bool(f.get("impersonate")) and browser is None
            extensions = {"impersonate": browser} if browser else {}
            raw = target.with_suffix("." + f["ext"] + ".part")
            temp = target.with_suffix(".new.srt")
            try:
                error = None
                for attempt in range(5):
                    try:
                        with (
                            ydl.urlopen(
                                Request(
                                    f["url"],
                                    headers=info.get("http_headers", {}),
                                    extensions=extensions,
                                )
                            ) as response,
                            raw.open("wb") as stream,
                        ):
                            shutil.copyfileobj(response, stream)
                        error = None
                        break
                    except Exception as exc:
                        error = exc
                        reply = getattr(exc, "response", None)
                        headers = getattr(reply, "headers", {}) or {}
                        if reply is not None:
                            # An error answer holds its connection until it is closed; a video
                            # with a hundred refused subtitles would hold a hundred.
                            with suppress(Exception):
                                reply.close()
                        # A server that says "too many requests" gets one more try after the
                        # pause it asked for, not four: asking on only makes the limit last.
                        # Without the browser look the answer will not change, so no more tries.
                        last_try = 4
                        if rate_limited(exc):
                            last_try = 0 if plain else 1
                        if attempt == last_try:
                            break
                        seconds = min(30, 2**attempt)
                        value = headers.get("Retry-After")
                        if value and str(value).isdigit():
                            seconds = min(float(value), MAX_RETRY_AFTER)
                        wait(events, seconds, "Menunggu retry subtitle")
                        sleep(seconds)
                if error:
                    raise error
                ffmpeg(
                    ["-i", str(raw), "-c:s", "srt", str(temp)],
                    events,
                    "Menyiapkan subtitle SRT",
                    "subtitle",
                )
                if not temp.stat().st_size:
                    raise ValueError("Subtitle kosong")
                temp.replace(target)
                saved.append({**track, "path": str(target)})
                limited = 0
            except Exception as exc:
                print("Subtitle gagal:", exc, file=sys.stderr, flush=True)
                reason = failure_reason(exc)
                if reason == "limited" and plain:
                    reason = "impersonation"
                    unequipped = True
                    print(
                        "Subtitle YouTube diminta tanpa tiruan browser (curl_cffi tidak ada); "
                        "jalankan setup untuk memasangnya",
                        file=sys.stderr,
                        flush=True,
                    )
                missing.append({**track, "reason": reason})
                limited = limited + 1 if reason == "limited" else 0
                events.send(
                    "notice", message="Subtitle gagal · " + track["name"] + "; media tetap disimpan"
                )
                if unequipped:
                    events.send(
                        "notice",
                        message="Subtitle YouTube butuh curl_cffi (belum terpasang) · "
                        "jalankan setup, lalu resume",
                    )
                if limited >= LIMITED_STOP:
                    events.send(
                        "notice",
                        message="YouTube membatasi permintaan (429) · sisa subtitle video ini "
                        "dilewati; lanjutkan nanti dengan resume",
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


def adopt_result(item, root, target, mode, events):
    """A finished file that has no archive record, found where this download's file belongs.

    That happens when the tool stopped between moving the file into place and recording it. The
    file name carries the hash of the variant, so it is the same download: if it passes the same
    check as a fresh result it is adopted as it is. No network and no FFmpeg, so nothing is
    rewritten. A file that does not pass is never touched; the caller refuses to overwrite it."""
    try:
        probe(target, mode)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    events.send("stage", message="Hasil sebelumnya ditemukan; dicatat ke arsip", phase="finish")
    prefix = subtitle_prefix(item)
    saved, missing = [], []
    for track in item.get("subtitles", []):
        path = subtitle_path(root, prefix, track)
        if path.is_file() and path.stat().st_size:
            saved.append({**track, "path": str(path)})
        else:
            missing.append({**track, "reason": "gone"})
    return {"path": str(target), "mode": mode, "subtitles": saved, "missing": missing}


# What a first extraction leaves in the info once it has chosen formats. The next pass chooses
# for itself, and these leftovers would be joined into its download URL.
SELECTION_KEYS = ("requested_formats", "requested_downloads", "requested_subtitles")
# HTTP statuses that mean a signed link is no longer good.
STALE_LINK = re.compile(r"\b(403|404|410)\b")


def stream_present(folder, stream):
    """True when the temporary folder already holds the finished file of this stream. yt-dlp
    then does not download it again, and does not pause before it either."""
    return any(
        path.is_file() and path.suffix not in (".part", ".ytdl")
        for path in Path(folder).glob(f"*-{stream}.*")
    )


def pause_before_download(
    events, pace, sleep=time.sleep, uniform=random.uniform, message="Menunggu · jeda antar-unduhan"
):
    """The pause before a file that yt-dlp used to make without saying anything (sleep_interval):
    a random time within the profile's range, shown in the panel as "Menunggu" with the time
    left. Returns the seconds waited."""
    low, high = pace["file"]
    seconds = uniform(low, high)
    if seconds < 0.05:
        return 0.0
    wait(events, seconds, message)
    sleep(seconds)
    return seconds


def download_stream(opts, info, url):
    """One stream into the temporary folder, and the path of the file.

    The extraction made a moment ago for the metadata check is reused: its signed links are
    seconds old, and extracting again costs a full round of requests (and the JavaScript
    challenge) for every stream. Only if the links turn out to be refused, or yt-dlp cannot use
    the reused information, is the video extracted once more, as every stream used to be."""
    reuse = copy.deepcopy(info)
    for key in SELECTION_KEYS:
        reuse.pop(key, None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            downloaded = ydl.process_ie_result(reuse, download=True)
        except (yt_dlp.utils.DownloadError, KeyError, TypeError, AttributeError) as exc:
            if isinstance(exc, yt_dlp.utils.DownloadError) and not STALE_LINK.search(str(exc)):
                raise
            print("Info ekstraksi tidak dapat dipakai ulang:", exc, file=sys.stderr, flush=True)
            downloaded = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(downloaded)


def remove_temporary(folder):
    """Remove the temporary folder of a download whose file is already in place. Something that
    still holds it open (a virus scanner or an indexer on Windows) is not a failed download:
    the folder is left, said once, and `clean` removes it later."""
    try:
        shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass
    if Path(folder).exists():
        print(f"Folder sementara tidak terhapus: {folder} (jalankan clean)", file=sys.stderr)
        return False
    return True


def download_subtitles(request, events, timings, opts):
    """The subtitle-only mode: the subtitles of one video as .srt files in <output>/subtitle, and
    nothing else. No video and no audio is downloaded, nothing is merged, and the result is not
    one for the archive of downloads. A subtitle that is already on disk is kept and costs
    nothing, so running the same choice again fetches only what is missing."""
    item = request["item"]
    root = Path(request["output"])
    longest = longest_subtitle_path(root, item)
    longest = longest and path_too_long(longest)
    if longest:
        raise ValueError(
            f"Path file subtitle {longest} karakter, melewati batas Windows (260). "
            "Pilih folder tujuan yang lebih pendek dengan --output."
        )
    events.send("stage", message="Memeriksa subtitle yang tersedia", phase="metadata")
    with timings.phase("metadata"), yt_dlp.YoutubeDL({**opts, "skip_download": True}) as ydl:
        info = ydl.extract_info(item["url"], download=False)
    if not info:
        raise ValueError("Informasi video kosong")
    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("Livestream aktif tidak didukung")
    with timings.phase("subtitle"):
        subtitles, missing = subtitle_files(info, item, root, events, opts, pace_of(request))
    events.send("stage", message="Subtitle tersimpan")
    return {
        "path": str(root / "subtitle"),
        "mode": "subtitle",
        "subtitles": subtitles,
        "missing": missing,
        "timings": timings.report(),
    }


def download(request, events):
    timings = events.timings = Timings()
    item = request["item"]
    choice = item["choice"]
    root = Path(request["output"])
    opts = options(request, events)
    if choice["mode"] == "subtitle":
        return download_subtitles(request, events, timings, opts)
    prefix = basename(item)
    mode = choice["mode"]
    extension = container(choice, item.get("subtitles"))
    target = root / ("audio" if mode == "audio" else "video") / (prefix + "." + extension)
    longest = path_too_long(target.with_name("." + target.stem + ".assembling." + extension))
    if longest:
        raise ValueError(
            f"Path file sementara {longest} karakter, melewati batas Windows (260). "
            "Pilih folder tujuan yang lebih pendek dengan --output."
        )
    # The subtitles sit in another folder and have a longer name than the media (the language
    # and where it comes from are added to it), so their paths are checked as well, before
    # anything is downloaded: a subtitle that cannot be written is a media file that cannot be
    # finished.
    longest_subtitle = longest_subtitle_path(root, item)
    longest = longest_subtitle and path_too_long(longest_subtitle)
    if longest:
        raise ValueError(
            f"Path file subtitle {longest} karakter, melewati batas Windows (260). "
            "Pilih folder tujuan yang lebih pendek dengan --output."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = BASE / ".state/temporary" / variant(item)
    existing = request.get("existing_file")
    if target.exists() and not existing:
        adopted = adopt_result(item, root, target, mode, events)
        if adopted is None:
            raise ValueError(
                "Benturan hasil: file sudah ada tanpa catatan arsip dan tidak lolos "
                "pemeriksaan; tidak ditimpa"
            )
        remove_temporary(temp_root)
        return adopted
    temp_root.mkdir(parents=True, exist_ok=True)
    events.send("stage", message="Memeriksa format dan akses terbaru", phase="metadata")
    metadata_opts = {**opts, "skip_download": True}
    with timings.phase("metadata"), yt_dlp.YoutubeDL(metadata_opts) as ydl:
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
            events.send("stage", message="Mengunduh " + stream, phase=stream)
            events.stream_name = stream
            events.started = False
            local = {
                **opts,
                "format": format_id,
                "outtmpl": str(temp_root / ("%(id)s-" + stream + ".%(ext)s")),
                "writeautomaticsub": False,
                "writesubtitles": False,
            }
            with timings.phase(stream):
                if not stream_present(temp_root, stream):
                    pause_before_download(events, pace_of(request))
                filepath = download_stream(local, info, item["url"])
            if not Path(filepath).is_file():
                raise ValueError("File sementara tidak ditemukan setelah download")
            paths[stream] = filepath
            paths[format_id] = filepath
    with timings.phase("subtitle"):
        subtitles, missing = subtitle_files(info, item, root, events, opts, pace_of(request))
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
    with timings.phase("finish"):
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
        # The file is already in place. A temporary folder that something still holds open (a
        # virus scanner or an indexer on Windows) is not a failed download: `clean` removes it
        # later.
        remove_temporary(temp_root)
    events.send("stage", message="Hasil akhir tersimpan")
    return {
        "path": str(target),
        "mode": mode,
        "subtitles": subtitles,
        "missing": missing,
        "timings": timings.report(),
    }


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
