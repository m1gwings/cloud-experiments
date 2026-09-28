"""Small, failure-only records shared by the worker and diagnostic reader."""

import datetime as dt
import re
import traceback
import uuid

from .common import Error, redact, valid_run

SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 256 * 1024
MAX_JOURNAL_LINES = 200
MAX_MESSAGE = 600
UNITS = ("cloud-supervisor.service", "cloud-experiment.service", "cloud-finalize.service",
         "cloud-deadline.service", "cloud-delete.service", "cloud-failure@supervisor.service",
         "cloud-failure@finalizer.service", "cloud-failure@deadline.service")
COMPONENTS = {"supervisor", "compute", "finalizer", "deadline", "deletion"}
STATUSES = {"provisioning", "running", "finalizing", "completed", "failed", "setup_failed",
            "finalization_failed", "timeout", "cancelled", "interrupted", "unknown"}
EVENT = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}\Z")
STAGE = re.compile(r"[a-z]+(?:\.[a-z_]+)+\Z")
_ASSIGNMENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=\S+")
_URL = re.compile(r"https?://\S+")


def event_id(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:12]


def safe_text(value, secrets):
    """Redact known values and drop URLs and assignment values from untrusted text."""
    value = redact(str(value), [secret for secret in secrets if isinstance(secret, str) and secret])
    value = _URL.sub("[REDACTED URL]", value)
    value = _ASSIGNMENT.sub("[REDACTED FIELD]", value)
    return "".join(char if char.isprintable() or char == "\n" else "?" for char in value)


def journal_tail(raw, secrets):
    lines = safe_text(raw.decode(errors="replace") if isinstance(raw, bytes) else raw, secrets).splitlines()
    kept, size = [], 0
    for line in reversed(lines[-MAX_JOURNAL_LINES:]):
        encoded = (line[:2048] + "\n").encode()
        if size + len(encoded) > MAX_JOURNAL_BYTES:
            break
        kept.append(encoded)
        size += len(encoded)
    return b"".join(reversed(kept)).decode()


def frames(exc):
    if exc is None:
        return []
    return [f"{frame.name} ({frame.filename.rsplit('/', 1)[-1]}:{frame.lineno})"[:200]
            for frame in traceback.extract_tb(exc.__traceback__)[-12:]]


def failure(manifest, *, component, stage, error_kind, message, systemd=None, exc=None, secrets=()):
    """Select only cloud-owned lifecycle fields; never copy arbitrary manifest data."""
    def select(name, keys):
        source = manifest.get(name, {})
        if not isinstance(source, dict):
            return {}
        selected = {}
        for key in keys:
            value = source.get(key)
            if key in source and (isinstance(value, str) or type(value) is int or value is None):
                selected[key] = safe_text(value, secrets)[:120] if isinstance(value, str) else value
        return selected

    record = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"],
              "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "status": manifest.get("status") if manifest.get("status") in STATUSES else "unknown",
              "component": component if component in COMPONENTS else "supervisor",
              "stage": stage if isinstance(stage, str) and STAGE.fullmatch(stage) else "lifecycle.unknown",
              "error_kind": safe_text(error_kind, secrets)[:80],
              "message": safe_text(message, secrets)[:MAX_MESSAGE],
              "traceback": [safe_text(frame, secrets)[:200] for frame in frames(exc)],
              "compute": select("compute", ("status", "exit_code", "finished_at")),
              "sync": select("sync", ("status", "attempted_at")),
              "last_recovery": select("last_recovery", ("commit_id", "committed_at")),
              "archive": select("archive", ("status", "published_at")),
              "deletion": select("deletion", ("status", "requested_at")),
              "systemd": {key: safe_text(value, secrets)[:80] for key, value in (systemd or {}).items()
                          if key in {"unit", "result", "exec_status"} and isinstance(value, str)}}
    return validate(record)


def validate(record):
    """Reject untrusted or future diagnostic records before display."""
    required = {"schema_version", "run_id", "occurred_at", "status", "component", "stage",
                "error_kind", "message", "traceback", "compute", "sync", "last_recovery",
                "archive", "deletion", "systemd"}
    if (not isinstance(record, dict) or set(record) != required
            or type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION):
        raise Error("Unsupported or malformed failure diagnostic.")
    valid_run(record["run_id"])
    try:
        when = dt.datetime.fromisoformat(record["occurred_at"])
    except (TypeError, ValueError):
        raise Error("Invalid failure diagnostic timestamp.") from None
    if (when.tzinfo is None or not isinstance(record["component"], str)
            or record["component"] not in COMPONENTS
            or not isinstance(record["status"], str) or record["status"] not in STATUSES
            or not isinstance(record["stage"], str) or not STAGE.fullmatch(record["stage"])
            or not isinstance(record["message"], str) or len(record["message"]) > MAX_MESSAGE
            or not isinstance(record["error_kind"], str) or len(record["error_kind"]) > 80
            or not isinstance(record["traceback"], list) or len(record["traceback"]) > 12
            or any(not isinstance(line, str) or len(line) > 200 for line in record["traceback"])):
        raise Error("Malformed failure diagnostic fields.")
    allowed = {"compute": {"status", "exit_code", "finished_at"},
               "sync": {"status", "attempted_at"},
               "last_recovery": {"commit_id", "committed_at"},
               "archive": {"status", "published_at"},
               "deletion": {"status", "requested_at"},
               "systemd": {"unit", "result", "exec_status"}}
    for name, keys in allowed.items():
        if (not isinstance(record[name], dict) or not set(record[name]) <= keys
                or any(not (isinstance(value, str) and len(value) <= 120 or
                            type(value) is int or value is None) for value in record[name].values())):
            raise Error("Malformed failure diagnostic state.")
    if record["systemd"].get("unit") not in (*UNITS, None):
        raise Error("Malformed diagnostic service identity.")
    return record
