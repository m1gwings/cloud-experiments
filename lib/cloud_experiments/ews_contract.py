"""The supported EWS recovery boundary, independent of its scientific layout.

Only this adapter knows the versioned snapshot envelope and public EWS API.
Scientific reuse, pruning receipts and checkpoint selection remain EWS-owned.
"""

import ast
import hashlib
import json
from pathlib import Path
import re

from .common import Error, read_json, valid_result_path

SCHEMA = "experiments-wo-stress/recovery"
VERSION = 1
MANIFEST = "recovery.json"
CONTRACT = {"schema": SCHEMA, "schema_version": VERSION}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
SOURCE = "src/experiments_wo_stress/storage/recovery.py"
EXPORTS = "src/experiments_wo_stress/storage/__init__.py"
_API = {"create_snapshot", "validate_snapshot", "restore_snapshot"}

QUERY_SCRIPT = '''
import json
from experiments_wo_stress.storage import create_snapshot, validate_snapshot, restore_snapshot
from experiments_wo_stress.storage.recovery import RECOVERY_SCHEMA, RECOVERY_VERSION
assert all(callable(value) for value in (create_snapshot, validate_snapshot, restore_snapshot))
print(json.dumps({"schema": RECOVERY_SCHEMA, "schema_version": RECOVERY_VERSION}))
'''
CREATE_SCRIPT = '''
import sys
from experiments_wo_stress.storage import create_snapshot
create_snapshot(sys.argv[1], sys.argv[2])
'''
VALIDATE_SCRIPT = '''
import sys
from experiments_wo_stress.storage import validate_snapshot
validate_snapshot(sys.argv[1])
'''
RESTORE_SCRIPT = '''
import sys
from experiments_wo_stress.storage import restore_snapshot
restore_snapshot(sys.argv[1], sys.argv[2])
'''
RUNTIME_SCRIPT = '''
import json, os, sys
from experiments_wo_stress import load_config
from experiments_wo_stress.study.config import resolve_display_timezone, validate_gpu_workers
from experiments_wo_stress.execution.portability import portable_environment
config = load_config(sys.argv[1])
override = json.loads(sys.argv[2])
zone = resolve_display_timezone(config.display, override)
if sys.argv[3] == "portable":
    portable_environment(config)
if config.execution.get("gpu_ids") is not None:
    workers = config.execution["workers"]
    validate_gpu_workers(config.execution, workers)
    mode = "gpu"
else:
    workers = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    mode = "cpu"
if type(workers) is not int or workers <= 0:
    raise ValueError("Cannot determine a positive available logical CPU count")
print(json.dumps({"workers": workers, "timezone": getattr(zone, "key", str(zone)), "mode": mode}))
'''


def validate_contract(value):
    """Reject unknown protocols rather than treating a source commit as a version."""
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or type(value.get("schema_version")) is not int or value["schema_version"] != VERSION):
        raise Error("Unsupported EWS cloud recovery contract; this cloud implementation supports recovery v1. Use a compatible EWS pin or a new lineage/migration.")
    return dict(CONTRACT)


def inspect_source(recovery_source, exports_source):
    """Feature-detect a fetched pin without executing untrusted EWS on the laptop.

    Public declarations are checked again through the installed API on the VM.
    A future producer using a different declaration needs explicit adapter review.
    """
    try:
        tree = ast.parse(recovery_source)
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"RECOVERY_SCHEMA", "RECOVERY_VERSION"}:
                        constants[target.id] = ast.literal_eval(node.value)
        exports = ast.parse(exports_source)
        names = {alias.name for node in exports.body if isinstance(node, ast.ImportFrom)
                 and node.module == "recovery" for alias in node.names}
        if not _API <= names:
            raise ValueError("missing public recovery API")
    except (SyntaxError, TypeError, ValueError):
        raise Error("EWS pin does not expose the supported public cloud recovery API.") from None
    return validate_contract({"schema": constants.get("RECOVERY_SCHEMA"),
                              "schema_version": constants.get("RECOVERY_VERSION")})


def fingerprint(value):
    """Canonical manifest identity specified by EWS recovery v1."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise Error("Invalid EWS recovery manifest.") from None
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_manifest(manifest):
    """Check an inventory before using any of its paths for cloud transfer.

    This checks the envelope; EWS's validate_snapshot verifies restored bytes.
    """
    validate_contract(manifest)
    if manifest.get("snapshot_id") != fingerprint({k: v for k, v in manifest.items() if k != "snapshot_id"}):
        raise Error("EWS recovery manifest identity mismatch.")
    entries, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(entries, dict) or "metadata.json" not in entries or not isinstance(directories, list):
        raise Error("EWS recovery manifest is missing its output inventory.")
    if any(not isinstance(name, str) for name in directories) or len(set(directories)) != len(directories):
        raise Error("Invalid EWS recovery directory inventory.")
    for name in [*entries, *directories]:
        if valid_result_path(name) != name:
            raise Error("Invalid EWS recovery path.")
    for descriptor in entries.values():
        if (not isinstance(descriptor, dict) or type(descriptor.get("size")) is not int
                or descriptor["size"] < 0 or not isinstance(descriptor.get("sha256"), str)
                or not _DIGEST.fullmatch(descriptor["sha256"])):
            raise Error("Invalid EWS recovery file descriptor.")
    if not isinstance(manifest.get("pruned_runs"), list) or not isinstance(manifest.get("omitted_checkpoints"), list):
        raise Error("Invalid EWS recovery pruning/checkpoint information.")
    return manifest


def query(worker, expected=None):
    """Query the installed EWS public API as the unprivileged experiment user."""
    try:
        actual = json.loads(worker.user_step(["python", "-c", QUERY_SCRIPT]).stdout)
    except (ValueError, TypeError):
        raise Error("Cannot query the installed EWS cloud recovery contract.") from None
    actual = validate_contract(actual)
    if expected is not None and actual != validate_contract(expected):
        raise Error("Installed EWS cloud recovery contract differs from the launch pin.")
    return actual


def create_snapshot(worker, output, destination, *, timeout=1200):
    """Seal a stopped writer through EWS; no private filesystem interpretation."""
    worker.user_step(["python", "-c", CREATE_SCRIPT, str(output), str(destination)], timeout=timeout)
    return validate_manifest(read_json(Path(destination) / MANIFEST))


def validate_snapshot(worker, directory):
    """Have EWS verify exact payload inventory, paths, sizes and checksums."""
    worker.user_step(["python", "-c", VALIDATE_SCRIPT, str(directory)])
    return validate_manifest(read_json(Path(directory) / MANIFEST))


def restore_snapshot(worker, directory, output):
    """Restore to a nonexistent output using EWS's verified atomic restore."""
    manifest = validate_manifest(read_json(Path(directory) / MANIFEST))
    worker.user_step(["python", "-c", RESTORE_SCRIPT, str(directory), str(output)])
    return manifest


def runtime_options(worker, manifest):
    """Ask EWS to validate resources/display, then allocate all available CPU workers."""
    settings = manifest["settings"]
    mode = "portable" if manifest.get("portable_continuation", True) else "strict"
    result = worker.user_step(["python", "-c", RUNTIME_SCRIPT,
                              str(worker.work / "source" / manifest["config"]["path"]),
                              json.dumps(settings.get("timezone")), mode])
    try:
        options = json.loads(result.stdout)
        if (type(options["workers"]) is not int or options["workers"] <= 0
                or options["mode"] not in ("cpu", "gpu") or not isinstance(options["timezone"], str)):
            raise ValueError("invalid options")
        return options
    except (ValueError, KeyError, TypeError):
        raise Error("Invalid EWS cloud runtime allocation/display response.") from None


def command_options(argv, options, timezone_override=None):
    """Apply operational CLI overrides without changing study YAML or identities."""
    result = []
    skip = False
    overridden = {"--workers"} if options["mode"] == "cpu" else set()
    if timezone_override is not None:
        overridden.add("--timezone")
    for part in argv:
        if skip:
            skip = False
        elif part in overridden:
            skip = True
        elif part.split("=", 1)[0] not in overridden:
            result.append(part)
    if options["mode"] == "cpu":
        result.extend(["--workers", str(options["workers"])])
    if timezone_override is not None:
        result.extend(["--timezone", timezone_override])
    return result
