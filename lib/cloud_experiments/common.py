"""Small, shared primitives; subprocess failures never echo credentials or output."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid

MANAGED = {"managed-by": "cloud-experiments"}
FINAL = {"completed", "failed", "cancelled", "timeout", "setup_failed", "finalization_failed", "interrupted"}
RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class Error(Exception):
    """An actionable, safe-to-display error."""


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_id(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:25] or "run"
    return f"{slug}-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:10]}"


def valid_run(value):
    if not isinstance(value, str) or not RUN_RE.fullmatch(value):
        raise Error("Invalid run ID (use lowercase letters, digits and hyphens; at most 63 characters).")
    return value


def valid_result_path(value):
    """A literal run-relative path; permit one trailing slash for directories."""
    message = ("Invalid result path: use a nonempty relative path with / separators; "
               "absolute paths, ., .., empty components, backslashes, colons and control characters are not allowed.")
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(not char.isprintable() for char in value)):
        raise Error(message)
    value = value.removesuffix("/")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise Error(message)
    return value


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def _stream_command(argv, *, cwd, input, timeout, env, stderr_line):
    """Drain both pipes while inspecting bounded stderr lines; preserve captured bytes."""
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                          start_new_session=True) as process:
        output, errors = bytearray(), bytearray()
        pending = bytearray()
        oversized = False
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, errors)
                if input:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, memoryview(input))
                elif process.stdin:
                    process.stdin.close()
                while selector.get_map():
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(0.2 if remaining is None else min(remaining, 0.2)):
                        if key.fileobj is process.stdin:
                            try:
                                sent = os.write(key.fd, key.data[:4096])
                                rest = key.data[sent:]
                            except BrokenPipeError:
                                rest = b""
                            if rest:
                                selector.modify(key.fileobj, selectors.EVENT_WRITE, rest)
                            else:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(chunk)
                        if key.fileobj is process.stderr:
                            for part in chunk.splitlines(keepends=True):
                                pending.extend(part)
                                if len(pending) > 65536:
                                    oversized = True
                                if part.endswith(b"\n"):
                                    if not oversized:
                                        stderr_line(bytes(pending))
                                    pending.clear()
                                    oversized = False
                                elif oversized:
                                    pending.clear()
                if pending and not oversized:
                    stderr_line(bytes(pending))
                process.wait(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
        except BaseException:
            # Stop the transfer (and any helpers) on timeout or Ctrl-C, then reap it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        return subprocess.CompletedProcess(argv, process.returncode, bytes(output), bytes(errors))


def command(argv, *, cwd=None, input=None, timeout=120, check=True, env=None, stderr_line=None):
    """No shell; captured output is deliberately omitted from exceptions."""
    child_env = dict(os.environ if env is None else env)
    for key in list(child_env):
        if key.startswith(("HCLOUD_", "RCLONE_")):
            child_env.pop(key)
    child_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        args = [str(a) for a in argv]
        if stderr_line is None:
            result = subprocess.run(args, cwd=cwd, input=input,
                                    capture_output=True, timeout=timeout, env=child_env)
        else:
            result = _stream_command(args, cwd=cwd, input=input, timeout=timeout,
                                     env=child_env, stderr_line=stderr_line)
    except subprocess.TimeoutExpired:
        raise Error(f"{Path(argv[0]).name} exceeded its {timeout}s time limit.") from None
    except OSError:
        raise Error(f"Could not execute {Path(argv[0]).name}; check installation and permissions.") from None
    if check and result.returncode:
        raise Error(f"{Path(argv[0]).name} failed (exit {result.returncode}); output withheld to protect secrets.")
    return result


def managed_server(server, rid):
    if not isinstance(rid, str) or not RUN_RE.fullmatch(rid):
        return False
    labels = server.get("labels", {})
    from .studies import attempt_study
    study = attempt_study(rid)
    expected_name = study or rid
    return (labels.get("managed-by") == MANAGED["managed-by"]
            and labels.get("run-id") == rid and server.get("name") == expected_name
            and (study is None or labels.get("study-id") == study)
            and isinstance(server.get("id"), int) and server["id"] > 0)


def require_managed(server, rid):
    if not managed_server(server, rid):
        raise Error("Refusing operation: server ID, name and management/run labels must all match.")


def remote_path(storage, rid=None):
    from .studies import STUDY_RE, attempt_study
    root = f"{storage['rclone_remote']}:{storage['bucket']}"
    if rid is None:
        return root + "/runs"
    valid_run(rid)
    if STUDY_RE.fullmatch(rid):
        return root + "/studies/" + rid
    study = attempt_study(rid)
    if study:
        return root + "/studies/" + study + "/attempts/" + rid
    return root + "/runs/" + rid


def redact(text, secrets):
    for value in sorted((v for v in secrets if v), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text
