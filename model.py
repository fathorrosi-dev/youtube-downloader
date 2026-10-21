"""Selection policy; no network, shell or filesystem mutations."""

import hashlib
import json
import math
import re
from datetime import date
from urllib.parse import urlparse, parse_qs

VERSION = "1.3.1"
# A yt-dlp release older than this many days is worth a hint when downloads fail.
PIN_WARN_DAYS = 45
MODES = ("video-audio", "audio", "video")


class SelectionError(ValueError):
    pass


def resolution(f):
    values = [int(f[k]) for k in ("width", "height") if f.get(k)]
    return min(values) if len(values) == 2 else int(f.get("height") or 0)


def has_video(f):
    return f.get("vcodec", "none") not in ("none", None) and resolution(f) > 0


def has_audio(f):
    return f.get("acodec", "none") not in ("none", None)


def languages(info):
    return sorted({f.get("language") or "und" for f in info.get("formats", []) if has_audio(f)})


def default_language(info):
    fs = [f for f in info.get("formats", []) if has_audio(f)]
    original = info.get("language")
    if original and original in languages(info):
        return original
    if not fs:
        return "und"
    preferred = max(fs, key=lambda f: f.get("language_preference") or 0)
    return preferred.get("language") or "und"


def compatible_video_rank(f):
    """0 for H.264, 1 for AV1, None for anything else.

    These are the video codecs the "MP4 kompatibel" option accepts. H.264 comes first because
    every phone, Mac, Windows PC and video editor decodes it. YouTube serves nothing above 1080p
    in H.264, so AV1 is the only way to reach 1440p and 2160p inside an MP4."""
    codec = (f.get("vcodec") or "").lower()
    if codec.startswith(("avc", "h264")):
        return 0
    if codec.startswith(("av01", "av1")):
        return 1
    return None


def compatible_audio(f):
    return (f.get("acodec") or "").lower().startswith(("mp4a", "aac"))


def codec_name(codec):
    """A short readable name for a yt-dlp codec string, e.g. avc1.640028 -> H.264."""
    codec = (codec or "").lower()
    for prefixes, name in (
        (("avc", "h264"), "H.264"),
        (("av01", "av1"), "AV1"),
        (("vp09", "vp9"), "VP9"),
        (("hev", "hvc", "h265"), "H.265"),
        (("mp4a", "aac"), "AAC"),
        (("opus",), "Opus"),
    ):
        if codec.startswith(prefixes):
            return name
    return codec.split(".")[0].upper() or "?"


def qualities(info, compatible=False):
    return sorted(
        {
            resolution(f)
            for f in info.get("formats", [])
            if has_video(f) and (not compatible or compatible_video_rank(f) is not None)
        },
        reverse=True,
    )


def pick(
    info,
    mode="video-audio",
    quality=1080,
    language=None,
    audio_format="original",
    video_id=None,
    audio_id=None,
    compatible=False,
):
    fs = [f for f in info.get("formats", []) if f.get("format_id") and not f.get("has_drm")]
    language = language or default_language(info)
    v = None
    a = None
    if mode not in MODES:
        raise SelectionError("Mode tidak valid")
    # "MP4 kompatibel" only concerns the video file, and only when the user did not pick the
    # streams by hand: a hand-picked stream is used as chosen.
    compatible = bool(compatible) and mode != "audio"
    if mode != "audio":
        vs = [f for f in fs if has_video(f)]
        if video_id:
            vs = [f for f in vs if f["format_id"] == video_id]
        else:
            if compatible:
                vs = [f for f in vs if compatible_video_rank(f) is not None]
                if not vs:
                    raise SelectionError("Tidak ada video H.264/AV1 untuk MP4 kompatibel")
            if quality != "best":
                # An exact tier wins; playlist fallback uses only lower tiers.
                exact = [f for f in vs if resolution(f) == int(quality)]
                vs = exact or [f for f in vs if resolution(f) < int(quality)]
        if not vs:
            raise SelectionError("Tidak ada video pada atau di bawah target kualitas")
        tier = max(resolution(f) for f in vs)
        at_tier = [f for f in vs if resolution(f) == tier]
        if compatible and not video_id:
            best = min(compatible_video_rank(f) for f in at_tier)
            at_tier = [f for f in at_tier if compatible_video_rank(f) == best]
        # Metadata formats retain yt-dlp's native worst-to-best ordering.
        v = at_tier[-1]
    if mode != "video":
        af = [f for f in fs if has_audio(f) and (f.get("language") or "und") == language]
        if audio_id:
            af = [f for f in af if f["format_id"] == audio_id]
        if not af:
            raise SelectionError("Track audio yang dipilih tidak tersedia")
        only = [f for f in af if not has_video(f)]
        pool = only or af
        if compatible and not audio_id:
            # Without AAC in this language the best stream is kept and the file becomes MKV.
            pool = [f for f in pool if compatible_audio(f)] or pool
        a = pool[-1]
    streams = {f["format_id"]: f for f in (v, a) if f}
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in streams.values()]
    estimate = sum(sizes) if sizes and all(sizes) else 0
    choice = {
        "mode": mode,
        "quality": quality,
        "actual_quality": resolution(v) if v else None,
        "language": language,
        "audio_format": audio_format,
        "video_format": v["format_id"] if v else None,
        "audio_format_id": a["format_id"] if a else None,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "fps": v.get("fps") if v else None,
        "vcodec": v.get("vcodec") if v else None,
        "dynamic_range": v.get("dynamic_range") if v else None,
        "acodec": a.get("acodec") if a else None,
        "estimated_size": estimate,
    }
    if compatible:
        # Stored only when asked for, so the identity of every other download is unchanged.
        choice["compatible"] = True
    return choice


def tracks(info):
    rows = []
    seen = set()
    for origin, kind in (("subtitles", "manual"), ("automatic_captions", "automatic")):
        for key, formats in info.get(origin, {}).items():
            if key == "live_chat" or not formats:
                continue
            qs = parse_qs(urlparse(formats[0].get("url", "")).query)
            translated = bool(qs.get("tlang") and qs.get("tlang") != qs.get("lang"))
            source = qs.get("lang", [""])[0]
            target = qs.get("tlang", [re.sub(r"-orig$", "", key)])[0]
            # Suppress duplicate aliases of the same automatic original track.
            identity = (kind, target, source, translated)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(
                {
                    "key": key,
                    "origin": origin,
                    "kind": "translated" if translated else kind,
                    "language": target,
                    "source": source,
                    "name": formats[0].get("name") or key,
                }
            )
    return rows


def container(choice, subtitles):
    if choice["mode"] == "audio":
        if choice["audio_format"] == "mp3":
            return "mp3"
        codec = choice.get("acodec") or ""
        return (
            "m4a"
            if codec.startswith(("mp4a", "aac", "alac"))
            else "opus"
            if codec.startswith("opus")
            else "ogg"
            if codec.startswith("vorbis")
            else "mp3"
            if codec.startswith("mp3")
            else "mka"
        )
    plays_in_mp4 = (choice.get("vcodec") or "").startswith(
        ("avc", "h264", "hevc", "h265", "hev", "hvc", "av01", "av1", "mpeg4")
    ) and (
        choice["mode"] == "video"
        or (choice.get("acodec") or "").startswith(("mp4a", "aac", "alac"))
    )
    if choice.get("compatible") and plays_in_mp4:
        # MP4 cannot carry subtitles that a player can switch off, so with this option they stay
        # next to the video as .srt files.
        return "mp4"
    if subtitles:
        return "mkv"
    return "mp4" if plays_in_mp4 else "mkv"


def phases(item):
    """The stages the worker goes through for one item, in order.

    The worker announces each one as a "stage" event whose "phase" is one of these keys. A stage
    can be skipped (a stream that is already on disk); the order never changes."""
    mode = item["choice"]["mode"]
    plan = ["metadata"]
    if mode != "audio":
        plan.append("video")
    if mode != "video":
        plan.append("audio")
    if item.get("subtitles"):
        plan.append("subtitle")
    plan.append("finish")
    return plan


def variant(item):
    identity = {"id": item["id"], "choice": item["choice"], "subtitles": item.get("subtitles", [])}
    # Estimated sizes do not identify a media variant.
    identity["choice"] = {
        k: v for k, v in identity["choice"].items() if k not in ("estimated_size", "quality")
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def basename(item):
    title = (
        re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", item.get("title") or item["id"]).strip(" .")[:100]
        or "video"
    )
    title = title.encode("utf-8")[:140].decode("utf-8", errors="ignore")
    choice = item["choice"]
    quality = (
        str(choice["actual_quality"]) + "p" if choice["actual_quality"] else choice["audio_format"]
    )
    return f"{title} [{item['id']}] - {quality} - {choice['mode']} - {variant(item)[:10]}"


def item_indices(text, total):
    text = (text or "").strip()
    if text.lower() in ("all", "semua"):
        return list(range(total))
    if not text:
        # An empty answer is an accident (a stray Enter), never "every item" of a long playlist.
        raise SelectionError("Ketik nomor/rentang, misalnya 1,3,5-10, atau all untuk semua")
    result = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not m:
            raise SelectionError("Gunakan nomor/rentang, misalnya 1,3,5-10")
        first = int(m[1])
        last = int(m[2] or m[1])
        if first < 1 or last < first or last > total:
            raise SelectionError("Nomor item di luar daftar")
        result.update(range(first - 1, last))
    return sorted(result)


def valid_browser(value):
    """The cookie source: nothing, Firefox's default profile, or Firefox with a profile path."""
    if not value or value == "firefox" or str(value).startswith("firefox:"):
        return value
    raise SelectionError("Pilihan browser hanya firefox atau firefox:/jalur/profil")


def valid_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    ):
        raise SelectionError("Masukkan link YouTube yang valid")
    if (
        host != "youtu.be"
        and parsed.path not in ("/watch", "/playlist")
        and not parsed.path.startswith(("/shorts/", "/embed/", "/live/"))
    ):
        raise SelectionError(
            "Gunakan link video, Shorts, atau playlist; seluruh kanal belum didukung"
        )
    return url


_AUTHENTICATION = (
    "sign in",
    "log in",
    "authentication",
    "confirm your age",
    "not a bot",
    "login required",
)
# Whole phrases only: a bare "not available" or "copyright" also matches errors that are not
# about the video at all (for example "Requested format is not available").
_UNAVAILABLE = (
    "private video",
    "video is private",
    "video unavailable",
    "video is unavailable",
    "this video is not available",
    "this video is no longer available",
    "has been removed",
    "deleted video",
    "members-only",
    "copyright grounds",
    "copyright claim",
    "available in your country",
)
_FORMAT = (
    "requested format is not available",
    "requested format not available",
    "no video formats found",
)
_NETWORK = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "temporary failure",
    "name or service not known",
    "getaddrinfo failed",
    "remote end closed",
    "bad gateway",
    "service unavailable",
)


def _http(code, text):
    """An HTTP status code as a status, not as digits inside an id or a version."""
    return re.search(r"(?:http(?: error)?|status(?: code)?|error(?: code)?)\W{0,3}" + code, text)


def classify(message):
    """The kind of failure a message describes: it decides what the tool does and advises next.

    authentication, rate_limit and unavailable stop or skip on their own; format, network and
    system count toward the three-failures stop. An unrecognised message is "system"."""
    low = str(message).lower()
    if any(x in low for x in _AUTHENTICATION):
        return "authentication"
    if any(x in low for x in _UNAVAILABLE):
        return "unavailable"
    if any(x in low for x in _FORMAT):
        return "format"
    if _http("429", low) or "too many requests" in low:
        return "rate_limit"
    if any(x in low for x in _NETWORK) or _http("50[234]", low):
        return "network"
    return "system"


def release_age_days(version, today=None):
    """Days since a date-versioned release (yt-dlp uses YYYY.M.D); None if it is not one."""
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", str(version))
    if not m:
        return None
    try:
        released = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return max(0, ((today or date.today()) - released).days)


# What a download records about where its time went (worker.Timings). The phases that ran, the
# total of the worker, the seconds of that spent waiting on purpose, the media bytes it fetched,
# their average speed when it is known, and (added by the app) the wall time around the worker.
TIMING_KEYS = (
    "metadata",
    "video",
    "audio",
    "subtitle",
    "finish",
    "total",
    "waited",
    "bytes",
    "speed",
    "wall",
)
TIMING_PHASES = TIMING_KEYS[:5]


def clean_timings(value):
    """The timings a worker reported, cut down to the known fields and to plain numbers that are
    finite and not negative, or None when there is nothing usable (no total)."""
    if not isinstance(value, dict):
        return None
    clean = {}
    for key in TIMING_KEYS:
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            continue
        if math.isfinite(number) and number >= 0:
            clean[key] = number
    return clean if "total" in clean else None


# Pacing: how long the worker waits before each file it downloads (a random time between the two
# numbers, in seconds) and between the requests of one lookup. "normal" is what the tool has
# always done; the others are opt-in through YTDL_PACE.
PACES = {
    "normal": ((1, 2), 1),
    "careful": ((3, 6), 2),
    "fast": ((0.5, 1), 0),
}
DEFAULT_PACE = "normal"


def pace_profile(name=None):
    """The pacing fields a request carries, for the profile `name` (the default for None or an
    empty value). SelectionError, naming the accepted values, for any other."""
    key = (name or "").strip().lower() or DEFAULT_PACE
    if key not in PACES:
        raise SelectionError(
            f"YTDL_PACE={name} tidak dikenal. Pilih salah satu: {', '.join(PACES)}."
        )
    (low, high), requests = PACES[key]
    return {"name": key, "file": [low, high], "requests": requests}


def pace_of(request):
    """The pacing of a worker request. A request without it (an older job, a test) or with
    nonsense in it gets the default, so the worker never fails over this."""
    value = request.get("pace")
    try:
        low, high = (float(x) for x in value["file"])
        requests = float(value["requests"])
        if all(math.isfinite(x) and x >= 0 for x in (low, high, requests)) and low <= high:
            return {"name": str(value.get("name") or ""), "file": [low, high], "requests": requests}
    except (TypeError, KeyError, ValueError):
        pass
    return pace_profile()
