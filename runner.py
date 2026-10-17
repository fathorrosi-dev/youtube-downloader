"""Portable isolated worker: NDJSON stdout, private stderr, bounded reader queue."""

import math
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from store import BASE, STATE, write_json
from compat import ProcessTree, clean_environment, rotate_log

# One NDJSON line. A 5,000-item playlist with escaped non-Latin titles is roughly 4 MiB, so 8 MiB
# leaves headroom; MAX_EVENT * MAX_QUEUE (128 MiB) bounds what the reader can buffer.
MAX_EVENT = 8 * 1024**2
MAX_QUEUE = 16


class WorkerError(RuntimeError):
    def __init__(self, message, category="system"):
        super().__init__(message)
        self.category = category


def read_events(stream, messages, cancel):
    def put(value):
        while not cancel.is_set():
            try:
                messages.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    try:
        while not cancel.is_set():
            line = stream.readline(MAX_EVENT + 1)
            if not line:
                break
            if len(line) > MAX_EVENT:
                put(WorkerError("Kanal worker terlalu besar"))
                break
            if not line.endswith(b"\n"):
                put(WorkerError("Worker berhenti dengan event tidak lengkap"))
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                put(WorkerError("Event worker rusak"))
                break
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                put(WorkerError("Event worker tidak valid"))
                break
            if event["type"] not in {
                "start",
                "progress",
                "file_done",
                "stage",
                "retry",
                "wait",
                "failure",
                "notice",
                "done",
            }:
                put(WorkerError("Jenis event worker tidak dikenal"))
                break
            invalid = any(
                k in event
                and event[k] is not None
                and (
                    isinstance(event[k], bool)
                    or not isinstance(event[k], (int, float))
                    or not math.isfinite(event[k])
                    or event[k] < 0
                )
                for k in ("downloaded", "total", "speed", "eta", "until")
            )
            invalid |= any(
                k in event and not isinstance(event[k], str)
                for k in ("message", "name", "stream", "error", "category", "phase")
            )
            if invalid:
                put(WorkerError("Nilai event worker tidak valid"))
                break
            put(event)
    except Exception as exc:
        put(WorkerError("Kanal worker gagal: " + str(exc)))
    finally:
        put(None)


def run(request, on_event=None):
    STATE.mkdir(parents=True, exist_ok=True)
    config = STATE / f"worker-{uuid.uuid4().hex}.json"
    write_json(config, request)
    process = None
    tree = None
    done = None
    reader = None
    messages = queue.Queue(MAX_QUEUE)
    cancel = threading.Event()
    try:
        rotate_log(STATE / "download.log")
        with (STATE / "download.log").open("a", encoding="utf-8") as log:
            flags = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = subprocess.Popen(
                [sys.executable, str(BASE / "worker.py"), str(config)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                env=clean_environment({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}),
                **flags,
            )
            tree = ProcessTree(process)
            reader = threading.Thread(
                target=read_events, args=(process.stdout, messages, cancel), daemon=True
            )
            reader.start()
            process.stdin.write(b"START\n")
            process.stdin.flush()
            process.stdin.close()
            while True:
                try:
                    event = messages.get(timeout=0.25)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if event is None:
                    break
                if isinstance(event, WorkerError):
                    raise event
                if done:
                    raise WorkerError("Worker mengirim event setelah laporan akhir")
                if event["type"] == "done":
                    done = event
                elif on_event:
                    on_event(event)
            try:
                code = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise WorkerError("Worker tidak berhenti setelah menutup kanal") from None
            if not done:
                raise WorkerError(
                    "Worker berhenti tanpa laporan lengkap; lihat .state/download.log"
                )
            if code or done.get("error"):
                raise WorkerError(
                    done.get("error") or f"Worker gagal ({code})", done.get("category", "system")
                )
            return done.get("result")
    except BaseException:
        if tree:
            tree.stop()
        elif process and process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        if tree:
            if os.name != "nt":
                tree.stop()
            tree.close()
        cancel.set()
        if reader:
            reader.join(timeout=3)
        if process and process.stdout:
            process.stdout.close()
        if process and process.stdin and not process.stdin.closed:
            process.stdin.close()
        config.unlink(missing_ok=True)
