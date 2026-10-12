"""Test-only helper: remove a temporary folder even while Windows is still releasing handles."""

import os
import shutil
import time


def remove_tree(path, timeout=15.0):
    """Delete a folder, retrying on Windows for up to `timeout` seconds.

    A process that was just killed (a worker, FFmpeg) can keep its inherited log handle for a
    moment after the kill returns, which makes the delete fail with WinError 32. Retrying hides
    that lag only: a handle that is really leaked is still open after the timeout and fails."""
    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def cleanup_directory(directory):
    """Cleanup for a tempfile.TemporaryDirectory: reliable removal first, then detach it."""
    remove_tree(directory.name)
    directory.cleanup()
