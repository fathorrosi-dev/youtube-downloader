"""Inject a local fixture extractor into the real yt-dlp API."""

import json
import os
import sys
from pathlib import Path

SOURCE = Path(sys.argv.pop(1))
sys.path.insert(0, str(SOURCE))
ROOT = Path(sys.argv[1]).parent.parent
import worker
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError


class FixtureIE(InfoExtractor):
    _VALID_URL = r"fixture:(?P<id>[a-z]+)"

    def _real_extract(self, url):
        identifier = self._match_id(url)
        if identifier == "crash":
            os._exit(0)
        if identifier == "auth":
            raise ExtractorError("Login required", expected=True)
        if identifier == "gone":
            raise ExtractorError("Private video", expected=True)
        if identifier == "error":
            raise ExtractorError("Unexpected API structure", expected=True)
        config = json.loads((ROOT / "fixture.json").read_text())
        return {**config, "id": identifier, "title": "Fixture " + identifier, "webpage_url": url}


def create(options):
    result = YoutubeDL(options, auto_init=False)
    result.add_info_extractor(FixtureIE())
    return result


worker.yt_dlp.YoutubeDL = create
worker.BASE = ROOT
sys.exit(worker.main())
