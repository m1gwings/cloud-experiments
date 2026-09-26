"""Small, shared primitives; subprocess failures never echo credentials or output."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

MANAGED = {"managed-by": "cloud-experiments"}
FINAL = {"completed", "failed", "cancelled", "timeout", "setup_failed"}
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


def command(argv, *, cwd=None, input=None, timeout=120, check=True, env=None):
    """No shell; captured output is deliberately omitted from exceptions."""
    child_env = dict(os.environ if env is None else env)
    for key in list(child_env):
        if key.startswith(("HCLOUD_", "RCLONE_")):
            child_env.pop(key)
    child_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run([str(a) for a in argv], cwd=cwd, input=input,
                                capture_output=True, timeout=timeout, env=child_env)
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
    return (labels.get("managed-by") == MANAGED["managed-by"]
            and labels.get("run-id") == rid and server.get("name") == rid
            and isinstance(server.get("id"), int) and server["id"] > 0)


def require_managed(server, rid):
    if not managed_server(server, rid):
        raise Error("Refusing operation: server ID, name and management/run labels must all match.")


def remote_path(storage, rid=None):
    root = f"{storage['rclone_remote']}:{storage['bucket']}/runs"
    return root if rid is None else f"{root}/{valid_run(rid)}"


def redact(text, secrets):
    for value in sorted((v for v in secrets if v), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text
