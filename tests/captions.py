"""Caption metadata shaped like the one yt-dlp builds for a YouTube video.

The other tests give a video one or two subtitle tracks. A real video with captions has a
hundred and fifty: yt-dlp (extractor/youtube/_video.py, process_language) makes one entry per
language the captions can be translated into, for the creator's captions and again for the
automatic ones, and a menu that was only ever tried on two rows behaves differently on those.

The list of languages is an approximation of what YouTube offers (the real number is YouTube's to
change), long enough to show what a long list does.
"""

LANGUAGES = """
af Afrikaans|am Amharic|ar Arabic|as Assamese|az Azerbaijani|be Belarusian|bg Bulgarian|
bn Bangla|bs Bosnian|ca Catalan|ceb Cebuano|co Corsican|cs Czech|cy Welsh|da Danish|de German|
el Greek|en English|eo Esperanto|es Spanish|et Estonian|eu Basque|fa Persian|fi Finnish|
fil Filipino|fr French|fy Western Frisian|ga Irish|gd Scottish Gaelic|gl Galician|gu Gujarati|
ha Hausa|haw Hawaiian|hi Hindi|hmn Hmong|hr Croatian|ht Haitian Creole|hu Hungarian|
hy Armenian|id Indonesian|ig Igbo|is Icelandic|it Italian|iw Hebrew|ja Japanese|jv Javanese|
ka Georgian|kk Kazakh|km Khmer|kn Kannada|ko Korean|ku Kurdish|ky Kyrgyz|la Latin|
lb Luxembourgish|lo Lao|lt Lithuanian|lv Latvian|mg Malagasy|mi Maori|mk Macedonian|
ml Malayalam|mn Mongolian|mr Marathi|ms Malay|mt Maltese|my Burmese|ne Nepali|nl Dutch|
no Norwegian|ny Nyanja|or Odia|pa Punjabi|pl Polish|ps Pashto|pt Portuguese|ro Romanian|
ru Russian|rw Kinyarwanda|sd Sindhi|si Sinhala|sk Slovak|sl Slovenian|sm Samoan|sn Shona|
so Somali|sq Albanian|sr Serbian|st Southern Sotho|su Sundanese|sv Swedish|sw Swahili|
ta Tamil|te Telugu|tg Tajik|th Thai|tk Turkmen|tr Turkish|tt Tatar|ug Uyghur|uk Ukrainian|
ur Urdu|uz Uzbek|vi Vietnamese|xh Xhosa|yi Yiddish|yo Yoruba|zh-Hans Chinese (Simplified)|
zh-Hant Chinese (Traditional)|zu Zulu|ab Abkhazian|aa Afar|ak Akan|an Aragonese|ay Aymara|
ba Bashkir|bho Bhojpuri|bi Bislama|br Breton|ch Chamorro|cr Cree|dv Divehi|dz Dzongkha|
ee Ewe|fj Fijian|fo Faroese|gn Guarani|gv Manx|ie Interlingue|ik Inupiaq|io Ido|iu Inuktitut|
kg Kongo|ki Kikuyu|kl Kalaallisut|ks Kashmiri|kw Cornish|lg Ganda|li Limburgish|ln Lingala|
nv Navajo|oc Occitan|om Oromo|os Ossetic|qu Quechua|rm Romansh|rn Rundi|sa Sanskrit|
sc Sardinian|se Northern Sami|sg Sango|ss Swati|ti Tigrinya|tn Tswana|to Tongan|ts Tsonga
"""
TRANSLATIONS = [
    tuple(item.strip().split(" ", 1)) for item in LANGUAGES.replace("\n", "").split("|") if item
]

MANUAL = ("en-US", "English (United States)")
ASR = ("en", "English")


def _entries(base, extra, name):
    """The formats yt-dlp lists under one key: one entry per subtitle format."""
    query = "".join(f"&{key}={value}" for key, value in extra.items())
    return [
        {"ext": ext, "url": f"{base}{query}&fmt={ext}", "name": name}
        for ext in ("vtt", "ttml", "srv3", "srv2", "srv1", "json3")
    ]


def captions(video_id, translations=TRANSLATIONS, manual=MANUAL, asr=ASR):
    """(subtitles, automatic_captions) of a video that has captions written by the creator
    (`manual`, or None) and automatic ones (`asr`, or None), both translatable.

    The keys follow yt-dlp: a translation of the creator's captions is `<language>-<source>`, a
    translation of the automatic ones is plain `<language>`, and the automatic captions
    themselves are the source language and `<source>-orig`."""
    subtitles, automatic = {}, {}
    page = f"https://www.youtube.com/api/timedtext?v={video_id}"
    if manual:
        code, name = manual
        base = f"{page}&lang={code}"
        subtitles[code] = _entries(base, {}, name)
        for target, target_name in translations:
            automatic[f"{target}-{code}"] = _entries(
                base, {"tlang": target}, f"{target_name} from {name}"
            )
    if asr:
        code, name = asr
        base = f"{page}&kind=asr&lang={code}"
        automatic[code + "-orig"] = _entries(base, {}, f"{name} (Original)")
        automatic[code] = _entries(base, {}, name)
        for target, target_name in translations:
            if target != code:
                automatic[target] = _entries(base, {"tlang": target}, target_name)
    return subtitles, automatic


def video(video_id, title=None, language="en", **kwargs):
    """What the worker returns for one video (worker.compact) when it has captions."""
    subtitles, automatic = captions(video_id, **kwargs)
    return {
        "id": video_id,
        "title": title or "Video " + video_id,
        "language": language,
        "formats": [],
        "subtitles": subtitles,
        "automatic_captions": automatic,
    }


def playlist(count=3, **kwargs):
    return [video(f"vid{n:08d}", f"Video {n + 1}", **kwargs) for n in range(count)]
