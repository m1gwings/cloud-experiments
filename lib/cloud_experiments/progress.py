"""Small stderr-only progress displays. Never pass subprocess text to a display."""

from contextvars import ContextVar
from functools import wraps
import json
import math
import os
import shutil
import sys
import threading
import time

_active = ContextVar("cloud_activity", default=None)


def interactive():
    # Even a redirected stdout opts out, so pipelines never get animation.
    return (sys.stdout.isatty() and sys.stderr.isatty()
            and os.environ.get("TERM") != "dumb" and not os.environ.get("NO_COLOR"))


def human_bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def transfer_stats(line):
    """Allowlist finite numeric fields only; paths, errors and messages stay private."""
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("stats"), dict):
        return None
    stats = record["stats"]
    values = {}
    for key in ("bytes", "totalBytes", "speed", "eta", "transfers", "totalTransfers", "checks", "totalChecks"):
        value = stats.get(key)
        if type(value) in (int, float) and 0 <= value <= 1e30 and math.isfinite(value):
            values[key] = value
    if not values:
        return None
    done, total = values.get("bytes", 0), values.get("totalBytes", 0)
    parts = []
    if total:
        fraction = min(done / total, 1)
        filled = int(fraction * 12)
        parts.append(f"[{'#' * filled}{'-' * (12 - filled)}] {fraction:.0%} {human_bytes(done)}/{human_bytes(total)}")
    elif "bytes" in values:
        parts.append(f"{human_bytes(done)} transferred")
    if "speed" in values:
        parts.append(human_bytes(values["speed"]) + "/s")
    if "eta" in values:
        parts.append(f"ETA {int(values['eta'])}s")
    for label, key, total_key in (("files", "transfers", "totalTransfers"), ("checks", "checks", "totalChecks")):
        if values.get(total_key):
            parts.append(f"{label} {int(values.get(key, 0))}/{int(values[total_key])}")
    return " | ".join(parts) or None


class Activity:
    """One nested-safe activity; background ticks also cover blocking library work."""

    def __init__(self, label, *, interval=15, delay=0):
        self.label = label  # Static application text or validated run IDs only.
        self.interval = interval
        self.delay = delay
        self.detail = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.frame = 0
        self.heading = False

    def __enter__(self):
        self.parent = _active.get()
        if self.parent is not None:
            return self.parent
        self.stream = sys.stderr
        self.animated = interactive()
        self.started = time.monotonic()
        self.last_plain = self.started
        self.token = _active.set(self)
        self.visible = not self.delay
        if self.visible:
            self._render()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()
        return self

    def rclone_line(self, line):
        detail = transfer_stats(line)
        if detail:
            with self.lock:
                self.detail = detail

    def count(self, done, total):
        fraction = done / total if total else 1
        filled = min(12, int(fraction * 12))
        with self.lock:
            self.detail = f"[{'#' * filled}{'-' * (12 - filled)}] {fraction:.0%} | {done}/{total} manifests"

    def _render(self, outcome=None):
        elapsed = int(time.monotonic() - self.started)
        detail = f" | {self.detail}" if self.detail else ""
        text = f"{self.label}{detail} ({elapsed}s)"
        if outcome:
            text = f"{outcome}: {text}"
        elif not self.animated:
            text += " ..."
        try:
            if self.animated:
                if not self.heading:
                    self.stream.write(self.label + "...\n")
                    self.heading = True
                width = max(1, shutil.get_terminal_size((100, 24)).columns - 1)
                if not outcome:
                    text = "|/-\\"[self.frame % 4] + " " + (self.detail or "Waiting") + f" ({elapsed}s)"
                    self.frame += 1
                # Keep the final record complete; only in-place frames are clipped.
                self.stream.write("\r\033[2K" + (text if outcome else text[:width]) + ("\n" if outcome else ""))
            else:
                self.stream.write(text + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            # A closed progress pipe must never change cloud lifecycle behavior.
            pass

    def _tick(self):
        with self.lock:
            now = time.monotonic()
            if not self.visible:
                if now - self.started < self.delay:
                    return
                self.visible = True
            elif not self.animated and now - self.last_plain < self.interval:
                return
            self._render()
            self.last_plain = now

    def _animate(self):
        while not self.stop.wait(0.2):
            self._tick()

    def __exit__(self, exc_type, exc, traceback):
        if self.parent is not None:
            return False
        self.stop.set()
        self.thread.join()
        _active.reset(self.token)
        outcome = "Done" if exc_type is None else ("Interrupted" if issubclass(exc_type, KeyboardInterrupt) else "Failed")
        if self.visible or exc_type is not None:
            self._render(outcome)
        return False


def activity(label):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with Activity(label):
                return function(*args, **kwargs)
        return wrapped
    return decorate
