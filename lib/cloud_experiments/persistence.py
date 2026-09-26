"""Complete EWS filesystem snapshots with SHA-256 verification and immutable commits."""

import json
from pathlib import Path
import shutil
import stat

from .common import Error, remote_path, sha256, valid_result_path, write_json
from .environment import validate, verify
from .source import secret_path
from .studies import attempt_study, state_head


def inventory(root):
    """No exclusions: an incomplete or linked EWS tree must never be committed."""
    root = Path(root)
    if root.is_symlink():
        raise Error("EWS output root must not be a symlink.")
    result = {}
    for path in sorted(root.rglob("*")):
        name = valid_result_path(path.relative_to(root).as_posix())
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) or secret_path(name):
            raise Error("EWS state contains a linked, special, or secret-like file; refusing incomplete persistence.")
        result[name] = {"sha256": sha256(path), "mode": 0o755 if mode & 0o111 else 0o644}
    return result


def verify_tree(root, expected):
    if not isinstance(expected, dict):
        raise Error("Invalid EWS state inventory.")
    for name in expected:
        if valid_result_path(name) != name:
            raise Error("Noncanonical EWS state path.")
    actual = inventory(root)
    if actual != expected:
        raise Error("Restored EWS state failed complete SHA-256/mode verification; no experiment was started.")


def read_head(worker, manifest):
    root = remote_path(manifest["storage"], manifest["study_id"])
    entries = json.loads(worker.rclone("lsjson", root, "--recursive", "--files-only", timeout=120).stdout)
    commits, environments = [], []
    for entry in entries:
        name = valid_result_path(entry["Path"])
        if name.startswith("commits/") and name.endswith(".json"):
            commits.append(json.loads(worker.rclone("cat", root + "/" + name, timeout=30).stdout))
        if name.startswith("environments/") and name.endswith(".json"):
            record = json.loads(worker.rclone("cat", root + "/" + name, timeout=30).stdout)
            if attempt_study(record["attempt_id"]) != manifest["study_id"]:
                raise Error("Environment record belongs to another study.")
            validate(record["environment"])
            environments.append(record)
    environments.sort(key=lambda value: (value["created_at"], value["attempt_id"]))
    environment = environments[0]["environment"] if environments else None
    for record in environments[1:]:
        verify(environment, record["environment"])
    return state_head(manifest["study_id"], commits), environment


def restore(worker, manifest):
    """Refresh under the provider VM lease, then verify every restored byte."""
    worker.verify_lease(manifest)
    head, environment = read_head(worker, manifest)
    manifest["parent_state"] = head["attempt_id"] if head else None
    worker.save(manifest)
    if head:
        remote = remote_path(manifest["storage"], head["attempt_id"])
        index_path = worker.base / "restore-index.json"
        worker.rclone("copyto", remote + "/ews-state.json", str(index_path), timeout=90)
        if sha256(index_path) != head["inventory_sha256"]:
            raise Error("EWS state inventory checksum mismatch.")
        expected = json.loads(index_path.read_text())
        output = worker.work / "output"
        worker.rclone("copy", remote + "/artifacts/output", str(output), timeout=1200)
        # Object storage cannot retain executable bits. Restore only inventory modes.
        for name, info in expected.items():
            if valid_result_path(name) != name or info.get("mode") not in (0o644, 0o755):
                raise Error("Invalid EWS state inventory path/mode.")
            path = output / name
            if path.is_file() and not path.is_symlink():
                path.chmod(info["mode"])
        verify_tree(output, expected)
        print("Verified and restored persisted EWS output; EWS will select compatible variants.", flush=True)
    return environment


def collect_state(worker, manifest):
    manifest.pop("state_inventory_sha256", None)
    output = worker.work / "output"
    if not (worker.base / "environment-ready.json").exists() or not (output / "metadata.json").is_file():
        return
    expected = inventory(output)
    destination = worker.out / "artifacts/output"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(output, destination)
    verify_tree(destination, expected)
    write_json(worker.out / "ews-state.json", expected)
    manifest["state_inventory_sha256"] = sha256(worker.out / "ews-state.json")


def publish_state(worker, manifest):
    """Publish only after the full attempt archive has passed upload verification."""
    if not manifest.get("state_inventory_sha256"):
        return
    worker.verify_lease(manifest)
    # A remote delayed commit must never get overwritten or silently hidden.
    head, _ = read_head(worker, manifest)
    current = head["attempt_id"] if head else None
    if current == manifest["run_id"]:
        return  # Idempotent retry of a verified finalization.
    if current != manifest.get("parent_state"):
        raise Error("Study state changed while this attempt was running; refusing publication.")
    counts = manifest.get("ews_counts", {})
    complete = (manifest["status"] == "completed" and manifest.get("exit_code") == 0
                and counts.get("completed", 0) > 0
                and all(counts.get(k) == 0 for k in ("pending", "running", "paused", "failed", "corrupt")))
    commit = {"schema_version": 1, "study_id": manifest["study_id"], "attempt_id": manifest["run_id"],
              "parent": current, "inventory_sha256": manifest["state_inventory_sha256"],
              "request_key": manifest["request_key"], "completed": complete,
              "status": manifest["status"], "counts": counts}
    path = worker.base / "state-commit.json"
    write_json(path, commit)
    remote = remote_path(manifest["storage"], manifest["study_id"]) + "/commits/" + manifest["run_id"] + ".json"
    worker.rclone("copyto", str(path), remote, timeout=45)
    if json.loads(worker.rclone("cat", remote, timeout=30).stdout) != commit:
        raise Error("Study state publication verification failed.")
