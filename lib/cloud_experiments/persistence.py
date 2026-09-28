"""Incremental recovery storage driven exclusively by EWS's sealed inventory.

Content-addressed payloads are shared by a study. Immutable parent-linked cloud
commits select verified EWS snapshots; interrupted transfers never replace the
current recovery point. Garbage collection keeps the current and candidate
inventories, delaying removal by one successful synchronization.
"""

import hashlib
import json
from itertools import chain
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from .common import Error, read_json, remote_path, sha256, utcnow, valid_result_path, write_json
from .environment import validate, verify
from .workspace import secret_path
from .studies import attempt_study, state_head
from . import ews_contract, physical


def _read_history(call, root, study):
    # Commit and environment records are flat prefixes. Listing the study root
    # also walks every content-addressed result blob and can time out at scale.
    entries = []
    for prefix in ("commits", "environments"):
        listed = json.loads(call("lsjson", root + "/" + prefix, "--files-only", timeout=120).stdout)
        entries.extend({"Path": prefix + "/" + entry["Path"]} for entry in listed)
    commits, environments = [], []
    for entry in entries:
        name = valid_result_path(entry["Path"])
        if name.startswith("commits/") and name.endswith(".json"):
            commit = json.loads(call("cat", root + "/" + name, timeout=30).stdout)
            identifier = commit.get("commit_id", commit.get("attempt_id"))
            if name != "commits/" + str(identifier) + ".json":
                raise Error("Study recovery commit filename does not match its identity.")
            commits.append(commit)
        if name.startswith("environments/") and name.endswith(".json"):
            record = json.loads(call("cat", root + "/" + name, timeout=30).stdout)
            if attempt_study(record["attempt_id"]) != study:
                raise Error("Environment record belongs to another study.")
            validate(record["environment"])
            environments.append(record)
    environments.sort(key=lambda value: (value["created_at"], value["attempt_id"]))
    environment = environments[0]["environment"] if environments else None
    for record in environments[1:]:
        verify(environment, record["environment"])
    return commits, environment


def read_head(worker, manifest, call=None):
    """Discover the compatible lineage and environment before EWS installation."""
    root = remote_path(manifest["storage"], manifest["study_id"])
    commits, environment = _read_history(call or worker.rclone, root, manifest["study_id"])
    return state_head(manifest["study_id"], commits), environment


def read_snapshot(call, root, commit):
    """Fetch and validate a small EWS inventory selected by a cloud commit."""
    ews_contract.validate_contract(commit.get("contract"))
    if any(not isinstance(commit.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", commit[key])
           for key in ("snapshot_id", "recovery_sha256")):
        raise Error("Invalid cloud recovery snapshot reference.")
    remote = root + "/snapshots/" + commit["snapshot_id"] + "/recovery.json"
    raw = call("cat", remote, timeout=60).stdout
    raw = raw.encode() if isinstance(raw, str) else raw
    if hashlib.sha256(raw).hexdigest() != commit["recovery_sha256"]:
        raise Error("EWS recovery inventory checksum mismatch.")
    try:
        recovery = ews_contract.validate_manifest(json.loads(raw))
    except (ValueError, UnicodeError):
        raise Error("Invalid committed EWS recovery inventory.") from None
    if recovery["snapshot_id"] != commit["snapshot_id"]:
        raise Error("EWS recovery snapshot identity mismatch.")
    return recovery


def read_layout(call, root, commit, recovery):
    """A missing descriptor denotes the original loose-blob schema 2 layout."""
    if "layout_sha256" not in commit:
        return physical.legacy(recovery)
    checksum = commit["layout_sha256"]
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise Error("Invalid cloud physical layout checksum reference.")
    remote = root + "/snapshots/" + commit["snapshot_id"] + "/layout-" + commit["commit_id"] + ".json.gz"
    raw = call("cat", remote, timeout=60).stdout
    raw = raw.encode() if isinstance(raw, str) else raw
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise Error("Cloud physical layout checksum mismatch.")
    return physical.decode(raw, recovery)


def download_files(call, root, recovery, destination, paths, layout=None, *, pack_cache=None, remember_pack=False):
    """Batch-fetch exact blobs, verify all bytes, then materialize selected output."""
    destination = Path(destination)
    paths = list(paths)
    if not paths:
        return
    digests = {}
    for name in paths:
        if valid_result_path(name) != name or name not in recovery["files"]:
            raise Error("File is absent from the committed EWS recovery inventory.")
        descriptor = recovery["files"][name]
        digest, size = descriptor["sha256"], descriptor["size"]
        if digest in digests and digests[digest] != size:
            raise Error("Inconsistent size for a committed recovery blob.")
        digests[digest] = size
        target = destination / name
        for parent in (target, *target.parents):
            if parent.is_symlink():
                raise Error("Recovery destination contains a symlink.")
    destination.mkdir(parents=True, exist_ok=True)
    layout = layout if layout is not None else physical.legacy(recovery)
    locations = physical.locations(layout)
    if not set(digests) <= locations.keys():
        raise Error("Cloud physical layout does not cover selected recovery files.")
    with tempfile.TemporaryDirectory(prefix=".download-", dir=destination) as temporary:
        temporary = Path(temporary)
        pool = temporary / "blobs"
        pool.mkdir()
        loose = {digest for digest in digests if locations[digest] is None}
        selection = temporary / "blobs.txt"
        selection.write_text("".join(digest + "\n" for digest in sorted(loose)))
        # Direct lookups are faster for a few selected results; large recovery
        # inventories are faster when the blob pool is listed and filtered once.
        listing = ["--no-traverse"] if len(loose) <= 256 else ["--fast-list"]
        if loose:
            call("copy", root + "/blobs", str(pool), "--files-from", str(selection),
                 *listing, "--transfers", "16", timeout=7200)
        for digest in loose:
            size = digests[digest]
            blob = pool / digest
            if blob.is_symlink() or not blob.is_file() or blob.stat().st_size != size or sha256(blob) != digest:
                raise Error("Restored EWS state failed SHA-256 verification; no experiment was started.")
        needed_packs = {locations[digest] for digest in digests if locations[digest] is not None}
        if needed_packs:
            packs = temporary / "packs"
            packs.mkdir()
            cached = needed_packs & (pack_cache.keys() if pack_cache is not None else set())
            for pack in cached:
                (packs / (pack + ".tar")).write_bytes(pack_cache[pack])
            missing = needed_packs - cached
            pack_selection = temporary / "packs.txt"
            pack_selection.write_text("".join(digest + ".tar\n" for digest in sorted(missing)))
            if missing:
                call("copy", root + "/packs", str(packs), "--files-from", str(pack_selection),
                     "--no-traverse", "--transfers", "16", timeout=7200)
            for pack in sorted(needed_packs):
                required = {digest for digest in digests if locations[digest] == pack}
                physical.extract(packs / (pack + ".tar"), pack, layout["packs"][pack], required, digests, pool)
            if remember_pack and pack_cache is not None and len(needed_packs) == 1:
                pack = next(iter(needed_packs))
                path = packs / (pack + ".tar")
                if path.stat().st_size <= 16 * 1024 * 1024:
                    pack_cache.clear()
                    pack_cache[pack] = path.read_bytes()
        for name in paths:
            target = destination / name
            for parent in (target, *target.parents):
                if parent.is_symlink():
                    raise Error("Recovery destination contains a symlink.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".write-", dir=target.parent) as writing:
                partial = Path(writing) / "payload"
                shutil.copyfile(pool / recovery["files"][name]["sha256"], partial)
                os.replace(partial, target)


def download_snapshot(call, root, commit, directory):
    """Download exactly one committed snapshot, never merge stale remote objects."""
    directory = Path(directory)
    if directory.exists():
        raise Error("Recovery download destination must not exist.")
    recovery = read_snapshot(call, root, commit)
    layout = read_layout(call, root, commit, recovery)
    directory.mkdir(parents=True)
    output = directory / "output"
    output.mkdir()
    for name in recovery["directories"]:
        (output / name).mkdir(parents=True, exist_ok=True)
    download_files(call, root, recovery, output, recovery["files"], layout)
    write_json(directory / "recovery.json", recovery, mode=0o644)
    return recovery


def restore(worker, manifest):
    """Restore under the VM lease after the pinned EWS reader is installed."""
    worker.verify_lease(manifest)
    head, environment = read_head(worker, manifest)
    manifest["parent_state"] = head["commit_id"] if head else None
    worker.save(manifest)
    if head:
        root = remote_path(manifest["storage"], manifest["study_id"])
        snapshot = worker.work / "runtime" / ("restore-" + uuid.uuid4().hex)
        try:
            download_snapshot(worker.rclone, root, head, snapshot)
            output = worker.work / "output"
            if output.is_dir() and not output.is_symlink() and not any(output.iterdir()):
                output.rmdir()
            # EWS runs unprivileged and must be able to traverse/read the download.
            for path in chain([snapshot], snapshot.rglob("*")):
                path.chmod(0o755 if path.is_dir() else 0o644)
            ews_contract.restore_snapshot(worker, snapshot, output)
            manifest["last_recovery"] = _summary(head)
            worker.save(manifest)
            print("Verified and restored committed EWS recovery; EWS selects compatible variants.", flush=True)
        finally:
            shutil.rmtree(snapshot, ignore_errors=True)
    return environment


def prepare(worker, manifest, final=False):
    """Seal the stopped writer through EWS; its v1 API requires a local copy."""
    worker.stage("finalizer" if final else "supervisor", "recovery.seal")
    if not (worker.base / "environment-ready.json").exists():
        return None
    output = worker.work / "output"
    if not output.is_dir():
        raise Error("EWS output is missing; refusing to certify a recovery snapshot.")
    # Check names/types without rehashing the complete live tree. This scan also
    # catches hidden secrets which EWS correctly omits as unpublished artifacts.
    for path in chain([output], output.rglob("*")):
        mode = path.lstat().st_mode
        if (not stat.S_ISDIR(mode) and not stat.S_ISREG(mode)) or (
                path != output and secret_path(path.relative_to(output).as_posix())):
            raise Error("EWS state contains a linked, special, or secret-like file; refusing persistence.")
    destination = worker.work / "runtime" / ("recovery-" + uuid.uuid4().hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if final:
            ews_contract.create_snapshot(worker, output, destination, timeout=None)
        else:
            ews_contract.create_snapshot(worker, output, destination)
        return destination
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _summary(commit):
    return {key: commit[key] for key in ("committed_at", "commit_id", "snapshot_id", "contract")}


def _accept(worker, manifest, commit):
    manifest["parent_state"] = commit["commit_id"]
    manifest["last_recovery"] = _summary(commit)
    manifest.pop("pending_recovery", None)
    worker.save(manifest)


def _fence(worker, manifest, head):
    """Recover a remotely accepted marker whose read-back was interrupted."""
    pending = manifest.get("pending_recovery")
    if head and pending and head == pending:
        _accept(worker, manifest, head)
    current = head["commit_id"] if head else None
    if current != manifest.get("parent_state"):
        raise Error("Study state changed while this attempt was running; refusing publication.")
    return current


def _commit(manifest, recovery, recovery_sha256, parent, final):
    counts = manifest.get("ews_counts", {})
    compute_status = manifest.get("compute", {}).get("status", manifest["status"])
    complete = (final and compute_status == "completed" and manifest.get("exit_code") == 0
                and counts.get("completed", 0) > 0
                and all(counts.get(k) == 0 for k in ("pending", "running", "paused", "failed", "corrupt")))
    return {"schema_version": 2, "study_id": manifest["study_id"], "attempt_id": manifest["run_id"],
            "commit_id": uuid.uuid4().hex, "parent": parent, "snapshot_id": recovery["snapshot_id"],
            "recovery_sha256": recovery_sha256, "contract": dict(ews_contract.CONTRACT),
            "committed_at": utcnow(), "final": final, "completed": complete,
            "request_key": manifest["request_key"], "status": compute_status if final else manifest["status"], "counts": counts,
            "provenance": {"ews": manifest["ews"],
                           "cloud": manifest.get("tooling", {"version": manifest["tool_version"]}),
                           "experiment": manifest["experiment"], "source": manifest["source"],
                           "config": manifest["config"]}}


def _prune(call, root, commits, head, retained):
    """Delete only superseded committed content, retaining the current recovery.

    EWS's sealed inventory validates intentional pruning and incomplete tails.
    Missing live files are never deletion evidence. Unknown/orphan uploads are
    retained conservatively because they have no committed inventory to retire.
    """
    # A published commit certifies that its own pruning completed. Only the
    # head's predecessor can introduce newly collectible bytes; failed pruning
    # cannot advance the head, so retrying revisits the same inventory.
    predecessor = next((commit for commit in commits
                        if head and commit["commit_id"] == head.get("parent")), None)
    if predecessor is None:
        return
    old_recovery = read_snapshot(call, root, predecessor)
    old_layout = read_layout(call, root, predecessor, old_recovery)
    obsolete = old_layout["loose"] - retained["loose"]
    for digest in sorted(obsolete):
        blob = root + "/blobs/" + digest
        entry = json.loads(call("lsjson", blob, "--stat", timeout=30).stdout)
        if not entry.get("IsDir") and entry.get("Path") == digest:
            call("deletefile", blob, timeout=60)
    for pack in sorted(old_layout["packs"].keys() - retained["packs"].keys()):
        remote = root + "/packs/" + pack + ".tar"
        entry = json.loads(call("lsjson", remote, "--stat", timeout=30).stdout)
        if not entry.get("IsDir") and entry.get("Path") == pack + ".tar":
            call("deletefile", remote, timeout=60)


def sync(worker, manifest, snapshot=None, final=False):
    """Upload new bytes, verify replacements, prune safely, and commit last.

    A snapshot may be prepared while EWS is stopped and transferred after it
    resumes. Transfer failures leave its previous remote recovery usable.
    """
    owned = snapshot is None
    snapshot = prepare(worker, manifest, final=final) if owned else Path(snapshot)
    if snapshot is None:
        return None
    component = "finalizer" if final else "supervisor"
    try:
        def call(*args, timeout=180):
            return worker.rclone(*args, timeout=None if final else timeout)

        recovery = ews_contract.validate_manifest(read_json(snapshot / "recovery.json"))
        for name in recovery["files"]:
            if secret_path(name):
                raise Error("Recovery snapshot contains a secret-like file; refusing upload.")
        worker.verify_lease(manifest)
        root = remote_path(manifest["storage"], manifest["study_id"])
        commits, _ = _read_history(call, root, manifest["study_id"])
        head = state_head(manifest["study_id"], commits)
        recovered = head is not None and head == manifest.get("pending_recovery")
        current = _fence(worker, manifest, head)
        if recovered and head["snapshot_id"] == recovery["snapshot_id"] and head["final"] == final:
            return head
        previous = read_snapshot(call, root, head) if head else None
        previous_layout = read_layout(call, root, head, previous) if head else {"loose": set(), "packs": {}}
        previous_locations = physical.locations(previous_layout)
        sizes = physical.inventory(recovery)
        candidate_digests = set(sizes)
        commit = _commit(manifest, recovery, sha256(snapshot / "recovery.json"), current, final)
        # The parent descriptor gives reusable locations without remote probes.
        layout = {"loose": candidate_digests & previous_layout["loose"],
                  "packs": {pack: set(members) for pack, members in previous_layout["packs"].items()
                            if set(members) & candidate_digests}}
        sources = {}
        for name, descriptor in recovery["files"].items():
            digest = descriptor["sha256"]
            if digest not in previous_locations and digest not in sources:
                sources[digest] = (snapshot / "output" / name, descriptor["size"])
        new_loose = {digest: source for digest, source in sources.items() if source[1] >= physical.SMALL_LIMIT}
        small = {digest: source for digest, source in sources.items() if source[1] < physical.SMALL_LIMIT}
        layout["loose"].update(new_loose)
        # Hardlinks reference the immutable sealed snapshot, not the live output.
        # rclone checks remote bytes by downloading; S3 ETags are not checksums.
        with tempfile.TemporaryDirectory(prefix="sync-", dir=snapshot.parent) as temporary:
            transfer = Path(temporary)
            loose_dir = transfer / "blobs"
            pack_dir = transfer / "packs"
            loose_dir.mkdir()
            pack_dir.mkdir()
            worker.stage(component, "recovery.build_packs")
            for digest, (source, size) in new_loose.items():
                if not stat.S_ISREG(source.lstat().st_mode) or source.stat().st_size != size or sha256(source) != digest:
                    raise Error("Sealed recovery payload changed before synchronization.")
                os.link(source, loose_dir / digest)
            batch, batch_size = {}, 0
            def flush_pack():
                nonlocal batch, batch_size
                if not batch:
                    return
                temporary_pack = transfer / "building.tar"
                digest = physical.pack_sources(batch, temporary_pack)
                target = pack_dir / (digest + ".tar")
                if target.exists():
                    temporary_pack.unlink()
                else:
                    temporary_pack.rename(target)
                layout["packs"].setdefault(digest, set()).update(batch)
                batch, batch_size = {}, 0
            for digest, source in sorted(small.items()):
                estimate = 512 + ((source[1] + 511) // 512) * 512
                if batch and batch_size + estimate > physical.PACK_TARGET:
                    flush_pack()
                batch[digest] = source
                batch_size += estimate
            flush_pack()
            layout_bytes = physical.encode(recovery["snapshot_id"], layout)
            physical.decode(layout_bytes, recovery)
            commit["layout_sha256"] = hashlib.sha256(layout_bytes).hexdigest()
            commit["transfer"] = {"logical_files": len(recovery["files"]), "unique_digests": len(sizes),
                                  "logical_bytes": sum(entry["size"] for entry in recovery["files"].values()),
                                  "standalone_objects": len(layout["loose"]),
                                  "packed_members": sum(len(members & candidate_digests) for members in layout["packs"].values()),
                                  "packs": len(layout["packs"]),
                                  "physical_objects_written": len(new_loose) + len(list(pack_dir.iterdir()))}
            manifest["pending_recovery"] = commit
            worker.save(manifest)
            worker.stage(component, "recovery.upload")
            for local, remote in ((loose_dir, root + "/blobs"), (pack_dir, root + "/packs")):
                if any(local.iterdir()):
                    call("copy", str(local), remote, "--ignore-times", timeout=1800)
                    worker.stage(component, "recovery.verify")
                    call("check", str(local), remote, "--one-way", "--download", timeout=1800)
        # The EWS marker is published only after all of its payload is verified.
        worker.stage(component, "recovery.publish_snapshot")
        marker = root + "/snapshots/" + recovery["snapshot_id"] + "/recovery.json"
        call("copyto", str(snapshot / "recovery.json"), marker, timeout=45)
        read_snapshot(call, root, commit)
        worker.stage(component, "recovery.publish_layout")
        local_layout = worker.base / "recovery-layout.json.gz"
        local_layout.write_bytes(layout_bytes)
        call("copyto", str(local_layout), root + "/snapshots/" + recovery["snapshot_id"] + "/layout-" + commit["commit_id"] + ".json.gz", timeout=45)
        read_layout(call, root, commit, recovery)
        # Recheck ownership and parent immediately before destructive/publication effects.
        worker.verify_lease(manifest)
        latest, _ = read_head(worker, manifest, call=call)
        if (latest["commit_id"] if latest else None) != current:
            raise Error("Study recovery parent changed during synchronization; refusing publication.")
        retained = {"loose": previous_layout["loose"] | layout["loose"],
                    "packs": {**previous_layout["packs"], **layout["packs"]}}
        _prune(call, root, commits, head, retained)
        commit["committed_at"] = utcnow()
        worker.save(manifest)  # Persist the exact pending marker before its remote write.
        path = worker.base / "recovery-commit.json"
        write_json(path, commit)
        worker.stage(component, "recovery.publish_commit")
        remote = root + "/commits/" + commit["commit_id"] + ".json"
        call("copyto", str(path), remote, timeout=45)
        if json.loads(call("cat", remote, timeout=30).stdout) != commit:
            raise Error("Study recovery publication verification failed.")
        _accept(worker, manifest, commit)
        return commit
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
