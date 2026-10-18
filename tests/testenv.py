"""Imported first by every test module. It fixes the settings that would otherwise follow the
machine that runs the tests (the theme of its terminal, animation, how many playlist items are
looked up at once), so that a test sees the same thing on every system. A test that needs another
value sets it for itself."""

import os

os.environ["YTDL_THEME"] = "dark"
os.environ["YTDL_MOTION"] = "off"
os.environ["YTDL_INSPECT_WORKERS"] = "1"
