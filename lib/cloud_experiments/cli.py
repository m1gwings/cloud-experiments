"""User commands. No provider operations occur on import, --help, or doctor."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .artifacts import artifact_selection
from .bootstrap import render
from .common import Error, FINAL, SHA_RE, read_json, run_id, sha256, utcnow, valid_result_path, valid_run, write_json
from .config import DEFAULT_COMMAND, DEPENDENCIES, load, repository_url, run_settings, runtime_hours, storage_credentials, worker_secrets
from .providers import Hetzner, SSH, Storage
from .progress import Activity, human_bytes, interactive
from .source import extract_snapshot, resolve_ews, snapshot
from .provenance import current as tooling_provenance
from .ews_contract import validate_contract
from .studies import STUDY_RE, attempt_id, repository_identity, request_key, study_id


def state(config, rid):
    directory = Path(config["local"]["state_dir"]) / valid_run(rid)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def ssh_for(config, server):
    return SSH(server, config["local"]["state_dir"], config["local"].get("ssh_identity"))


def new_manifest(config, rid, experiment, config_path, source_sha, config_sha, ews, *, machine=None, hours=None, keep=False):
    machine = machine or config["hetzner"]["default_server_type"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", machine):
        raise Error("Invalid server type.")
    hours = runtime_hours(hours if hours is not None else config["hetzner"]["max_runtime_hours"])
    now = dt.datetime.now(dt.timezone.utc)
    return {"schema_version": 1, "tool_version": __version__, "tooling": tooling_provenance(), "run_id": valid_run(rid), "status": "provisioning",
            "experiment": experiment, "dirty": experiment["dirty"],
            "source": {"path": "source/source.tar.gz", "sha256": source_sha, "index": "source/index.json"},
            "config": {"path": config_path, "sha256": config_sha, "artifact": "config/experiment-config"},
            "ews": ews, "machine": machine, "location": config["hetzner"]["location"], "image": "ubuntu-24.04",
            "created_at": now.isoformat(timespec="seconds"), "started_at": None, "finished_at": None,
            "deadline_at": (now + dt.timedelta(hours=hours)).isoformat(timespec="seconds"),
            "max_runtime_hours": hours, "exit_code": None, "elapsed_seconds": None,
            "settings": dict(config["run"]), "storage": {k: config["storage"][k] for k in ("rclone_remote", "bucket")},
            "keep_on_setup_failure": keep, "server_id": None}


def launch(config, manifest, directory):
    """Single ownership boundary: recover ambiguous creation and finalize on failure."""
    rid = manifest["run_id"]
    cloud, storage = Hetzner(config["hetzner"]), Storage(config)
    secrets = worker_secrets(config["local"]["worker_env"])
    rclone = storage_credentials(config["local"]["rclone_config"], config["storage"]["rclone_remote"])
    out = directory / "out"
    write_json(directory / "manifest.json", manifest)
    write_json(out / "manifest.json", manifest)
    # Freeze the deadline at creation, after the potentially long preflight upload.
    payload = render(manifest, secrets, rclone)  # validate size before any provider mutation
    print(f"Creating run...\nRun:        {rid}\nMachine:    {manifest['machine']}\nEWS:        {manifest['ews']['commit']}\nExperiment: {manifest['experiment']['commit']}")
    if manifest["dirty"]:
        print("DIRTY SOURCE: executing the stored working-tree snapshot.")
    # Store immutable input before provisioning, so even early setup failures are discoverable.
    storage.upload(out, rid)
    created = dt.datetime.now(dt.timezone.utc)
    manifest["created_at"] = created.isoformat(timespec="seconds")
    manifest["deadline_at"] = (created + dt.timedelta(hours=manifest["max_runtime_hours"])).isoformat(timespec="seconds")
    write_json(directory / "manifest.json", manifest)
    payload = render(manifest, secrets, rclone)
    server, ssh, attempted = None, None, False
    try:
        attempted = True
        server = cloud.create(manifest, payload)
        manifest["server_id"] = server["id"]
        write_json(directory / "manifest.json", manifest)
        write_json(out / "manifest.json", manifest)
        storage.call("copyto", str(out / "manifest.json"), storage.path(rid) + "/manifest.json", timeout=45)
        ssh = ssh_for(config, server)
        ssh.wait()
        print("✓ VM ready (absolute deadline armed)")
        inputs = [out / "source/source.tar.gz", out / "source/index.json"]
        if manifest.get("reproduction_environment_sha256"):
            inputs.append(out / "machine/environment.json")
        ssh.upload(inputs)
        print("✓ source uploaded")
        with Activity("Starting environment setup"):
            ssh.call(["systemctl", "start", "cloud-supervisor.service"])
        with Activity("Installing environment and restoring saved progress"):
            until = time.monotonic() + min(10800, manifest["max_runtime_hours"] * 3600)
            while time.monotonic() < until:
                try:
                    result = ssh.call(["test", "-f", "/opt/cloud-experiments/started"], check=False, timeout=20)
                    if result.returncode == 0:
                        launch_message = "✓ environment ready\n✓ experiment started"
                        break
                    status = json.loads(ssh.call(["cat", "/opt/cloud-experiments/manifest.json"], timeout=20).stdout)
                    if status["status"] in FINAL:
                        if status["status"] == "setup_failed":
                            raise Error("Worker setup failed; inspect cloud-results pull " + rid)
                        launch_message = f"Run already finished: {status['status']}"
                        break
                except Error:
                    # Very short runs can delete the server before the next SSH poll.
                    remote = storage.manifest(rid)
                    if remote.get("started_at") and remote["status"] in FINAL:
                        launch_message = f"Run already finished: {remote['status']}"
                        break
                    if remote["status"] == "setup_failed":
                        raise Error("Worker setup failed; inspect cloud-results pull " + rid)
                    if cloud.find(rid) is None:
                        raise Error("VM disappeared before launch confirmation; inspect cloud-results pull " + rid)
                time.sleep(3)
            else:
                raise Error("Environment setup did not finish within the launch wait limit.")
        print(launch_message)
    except (Exception, KeyboardInterrupt) as exc:
        # A create call can succeed server-side but fail locally before JSON arrives.
        try:
            server = server or (cloud.find(rid) if attempted else None)
            if server:
                ssh = ssh or ssh_for(config, server)
                reason = "cancelled" if isinstance(exc, KeyboardInterrupt) else "setup_failed"
                try:
                    with Activity("Requesting worker finalization"):
                        ssh.call(["/usr/bin/python3", "/opt/cloud-experiments/entry.py", reason], timeout=30)
                    print("Worker finalization requested; deadline protection remains active.")
                except Exception:
                    cloud.delete(server, rid)
                    print("Unreachable setup worker deleted after identity verification.")
                    manifest.update(status=reason, finished_at=utcnow(),
                                    finalization_note="Laptop deleted unreachable worker; only preflight artifacts are guaranteed.")
                    write_json(out / "manifest.json", manifest)
                    storage.call("copyto", str(out / "manifest.json"), storage.path(rid) + "/manifest.json", timeout=45)
        except Exception:
            print(f"CLEANUP COULD NOT BE CONFIRMED. Run cloud-status {rid}; if needed: cloud-cancel {rid} --force-delete --yes", file=sys.stderr)
        raise
    print(f"\nAttach:\n  cloud-attach {rid}\n\nStatus:\n  cloud-status {rid}")
    return rid


def run_command(args, config):
    # Local state must never dirty or disclose credentials into the experiment tree.
    from .source import git
    root = Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    if Path(config["local"]["state_dir"]).resolve().is_relative_to(root.resolve()):
        raise Error("local.state_dir must be outside the experiment repository.")
    rid = run_id(args.name or Path(args.experiment_config).stem)
    directory = state(config, rid)
    out = directory / "out"
    (out / "source").mkdir(parents=True)
    with Activity("Preparing source snapshot"):
        experiment, config_path, index = snapshot(Path.cwd(), args.experiment_config, out / "source/source.tar.gz", args.allow_dirty)
    write_json(out / "source/index.json", index)
    (out / "config").mkdir()
    (out / "source/experiment-config").replace(out / "config/experiment-config")
    ref = args.ews_ref or config["ews"]["default_ref"]
    with Activity("Resolving pinned EWS revision"):
        ews = resolve_ews(config["ews"]["repository"], ref)
    with Activity("Checksumming source snapshot"):
        source_sha = sha256(out / "source/source.tar.gz")
    manifest = new_manifest(config, rid, experiment, config_path, source_sha,
                            index[config_path]["sha256"], ews,
                            machine=args.machine, hours=args.max_runtime, keep=args.keep_on_setup_failure)
    sid = args.study or study_id(experiment["repository"], config_path, args.fresh)
    if not STUDY_RE.fullmatch(sid):
        raise Error("--study must identify a logical study returned by cloud-run.")
    if config["run"]["command"] != DEFAULT_COMMAND:
        raise Error("Automatic continuation requires the standard EWS command; custom commands are not portable.")
    manifest.update(study_id=sid, portable_continuation=True, name=args.name)
    manifest["request_key"] = request_key(manifest, index)
    active = Hetzner(config["hetzner"]).find(sid)
    if active:
        print(f"Study {sid} already has active attempt {active['labels']['run-id']}. Use cloud-attach {sid}.")
        return
    storage = Storage(config)
    head = storage.study_state(sid)
    if head:
        validate_contract(head.get("contract"))
    if args.study and sid in storage.studies():
        original = storage.study_manifest(sid)
        if (repository_identity(original["experiment"]["repository"]) != repository_identity(experiment["repository"])
                or original["config"]["path"] != config_path):
            raise Error("--study belongs to a different repository/configuration path.")
    if head and head.get("completed") is True and head.get("request_key") == manifest["request_key"]:
        print(f"✓ Study already completed: {sid}\nNo compute created.")
        return
    if head:
        print(f"✓ Found persisted study state: {sid}\n{head.get('counts', {}).get('completed', '?')} completed runs recorded; EWS will validate compatibility.")
    rid = attempt_id(sid)
    destination = Path(config["local"]["state_dir"]) / rid
    directory.rename(destination)
    directory = destination
    manifest["run_id"] = rid
    print(f"Study: {sid}\nAttempt: {rid}")
    launch(config, manifest, directory)


def reproduce_manifest(config, original, rid):
    if original.get("schema_version") != 1:
        raise Error("Unsupported reproduction manifest schema.")
    ews = original["ews"]
    repository_url(ews["repository"], public=True)
    if not SHA_RE.fullmatch(ews["commit"]):
        raise Error("Original EWS revision is not an exact commit.")
    repository_url(original["experiment"]["repository"])
    if not SHA_RE.fullmatch(original["experiment"]["commit"]):
        raise Error("Invalid original experiment commit.")
    manifest = new_manifest(config, rid, dict(original["experiment"]), original["config"]["path"],
                            original["source"]["sha256"], original["config"]["sha256"], dict(ews),
                            machine=original["machine"], hours=original["max_runtime_hours"])
    manifest["settings"] = run_settings(original["settings"])
    manifest["location"] = original["location"]
    if not re.fullmatch(r"[a-z0-9-]+", manifest["location"]):
        raise Error("Invalid original location.")
    manifest["reproduces_run_id"] = valid_run(original["run_id"])
    return manifest


def reproduce(args, config):
    storage = Storage(config)
    original = storage.manifest(valid_run(args.run_id))
    original_id = original.get("attempt_id", original["run_id"])
    if STUDY_RE.fullmatch(args.run_id):
        original = storage.manifest(original_id)
    sid = study_id(original["experiment"]["repository"], original["config"]["path"], fresh=True)
    rid = attempt_id(sid)
    manifest = reproduce_manifest(config, original, rid)
    if manifest["settings"]["command"] != DEFAULT_COMMAND:
        raise Error("Cloud reproduction requires the standard EWS command and supported recovery contract; custom legacy archives remain available for download.")
    # Reproduction retains the exact commit. Re-query the public contract rather
    # than trusting missing or archived capability metadata for a new lineage.
    manifest["ews"] = resolve_ews(original["ews"]["repository"], original["ews"]["commit"])
    if "requested_ref" in original["ews"]:
        manifest["ews"]["requested_ref"] = original["ews"]["requested_ref"]
    directory = state(config, rid)
    out = directory / "out"
    (out / "source").mkdir(parents=True)
    storage.file(original_id, "source/source.tar.gz", out / "source/source.tar.gz")
    # Derive index and config from the checksummed immutable archive, never live Git.
    with Activity("Verifying and preparing reproduction"), tempfile.TemporaryDirectory(prefix="cloud-reproduce-") as temp:
        extract_snapshot(out / "source/source.tar.gz", temp, manifest["source"]["sha256"])
        from .source import inventory
        index, _ = inventory(temp)
        name = manifest["config"]["path"]
        if name not in index or index[name]["sha256"] != manifest["config"]["sha256"]:
            raise Error("Reproduction config does not match the stored source snapshot.")
        (out / "config").mkdir()
        shutil.copyfile(Path(temp) / name, out / "config/experiment-config")
        write_json(out / "source/index.json", index)
    manifest.update(study_id=sid, portable_continuation=original.get("portable_continuation", bool(original.get("study_id"))), reproduces_run_id=original_id)
    if any(entry["path"] == "machine/environment.json" for entry in storage.files(original_id)):
        from .environment import decode
        path = out / "machine/environment.json"
        path.parent.mkdir()
        storage.file(original_id, "machine/environment.json", path)
        decode(path.read_bytes())
        manifest["reproduction_environment_sha256"] = sha256(path)
    manifest["request_key"] = request_key(manifest, index)
    print(f"Independent reproduction study: {sid}")
    launch(config, manifest, directory)


def attach(args, config):
    server = Hetzner(config["hetzner"]).find(valid_run(args.run_id))
    if not server:
        raise Error(f"No active VM for {args.run_id}; it likely completed or failed. Use cloud-results pull {args.run_id}.")
    print("Connecting to the experiment terminal...", file=sys.stderr, flush=True)
    return subprocess.call(ssh_for(config, server).attach_argv())


STATUS_HEADER = "STUDY/RUN\tSTATUS\tEWS\tRECOVERY\tARCHIVE\tVM\tMACHINE\tCONFIG\tAGE\tATTEMPT"
ACTIVE_STATUSES = {"provisioning", "running", "finalizing"}


def managed_servers(config):
    """A failed provider lookup is unknown, never evidence that a VM is gone."""
    try:
        return Hetzner(config["hetzner"]).servers()
    except (Error, ValueError, KeyError, TypeError):
        print("Warning: cloud worker lookup failed; VM presence is unknown.", file=sys.stderr)
        return None


def matching_server(manifest, servers):
    if servers is None:
        return None
    rid = manifest.get("attempt_id", manifest["run_id"])
    matches = [server for server in servers if server["labels"]["run-id"] == rid]
    if len(matches) > 1:
        raise Error("Multiple servers match this run; refusing an ambiguous status.")
    return matches[0] if matches else None


def print_row(m, server=None, *, provider_known=True):
    stamp = m.get("created_at") or (server or {}).get("created")
    try:
        age = str(round((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600, 1)) + "h"
    except (TypeError, ValueError, AttributeError):
        age = "?"
    identity = m.get('study_id', m['run_id'])
    attempt = m.get('attempt_id', m['run_id']) if m.get('study_id') else '-'
    status = m.get("status", "unknown")
    if status in ACTIVE_STATUSES and server is None:
        status = "interrupted" if provider_known else "unknown"
    compute = m.get("compute", {}).get("status", "unknown")
    recovery = m.get("last_recovery", {}).get("committed_at", "never")
    if m.get("sync", {}).get("status") == "failed":
        recovery += " (sync failed)"
    archive = m.get("archive", {}).get("status", m.get("upload", {}).get("status", "unknown"))
    if archive == "failed":
        status = "finalization_failed"
    vm = str(server["id"]) if server else "gone" if provider_known else "unknown"
    print(f"{identity}\t{status}\t{compute}\t{recovery}\t{archive}\t{vm}\t{m.get('machine', '?')}\t{m.get('config', {}).get('path', '?')}\t{age}\t{attempt}")


def status(args, config):
    if args.run_id:
        valid_run(args.run_id)
    storage = Storage(config)
    servers = managed_servers(config)
    if args.run_id:
        rid = valid_run(args.run_id)
        manifests = [storage.manifest(rid)]
    else:
        manifests = list(storage.manifests())
        known = {m.get("attempt_id", m["run_id"]) for m in manifests}
        for server in servers or []:
            rid = server["labels"]["run-id"]
            if rid not in known:
                path = Path(config["local"]["state_dir"]) / rid / "manifest.json"
                manifests.append(read_json(path) if path.exists() else {
                    "run_id": rid, "machine": server["server_type"]["name"], "status": "unknown"})
    print(STATUS_HEADER)
    for manifest in manifests:
        print_row(manifest, matching_server(manifest, servers), provider_known=servers is not None)


def cancel(args, config):
    rid = valid_run(args.run_id)
    cloud = Hetzner(config["hetzner"])
    server = cloud.find(rid)
    if not server:
        raise Error(f"No managed VM for {rid}. Use cloud-results pull {rid}.")
    if args.force_delete:
        print("WARNING: forced VM deletion may permanently lose unsaved results.")
    if not args.yes:
        if not sys.stdin.isatty() or input(f"{'DELETE' if args.force_delete else 'Cancel'} {rid}? Type the run ID: ").strip() != rid:
            raise Error("Cancellation not confirmed. Use --yes for explicit noninteractive confirmation.")
    if args.force_delete:
        attempt = server["labels"]["run-id"]
        cloud.delete(server, attempt)
        try:
            Storage(config).record_deletion(attempt)
        except Error:
            print("Warning: VM deleted, but its small lifecycle update could not be stored.", file=sys.stderr)
        print("VM deleted. Recovery is available only through the last successful sync.")
    else:
        with Activity("Requesting cancellation and finalization"):
            ssh_for(config, server).call(["/usr/bin/python3", "/opt/cloud-experiments/entry.py", "cancelled"])
        print("Graceful cancellation requested; the worker will upload partial results and delete itself.")


def pull_run(storage, config, rid, destination=None):
    rid = valid_run(rid)
    destination = Path(destination).expanduser().absolute() if destination else Path(config["local"]["results_dir"]) / rid
    destination.mkdir(parents=True, exist_ok=True)
    storage.pull(rid, destination)
    # Marker only after a successful complete download; running manifests get refreshed.
    manifest = read_json(destination / "manifest.json")
    write_json(destination / ".cloud-pulled.json", {"manifest": manifest})
    print(f"Download complete: {rid}", file=sys.stderr, flush=True)
    print(destination)


def pull_selection(storage, config, rid, selection=None, destination=None, *, role=None):
    rid = valid_run(rid)
    if role is None:
        selection = valid_result_path(selection)
    files = storage.files(rid)
    if not files:
        raise Error("No stored files found for this run; check the run ID and storage configuration.")
    kind, optional = None, False
    if role is not None:
        entry = artifact_selection(storage, rid, files, role)
        if entry is None:
            print(f"This run publishes no {role} role; nothing downloaded. "
                  "Use cloud-results ls RUN_ID and --path to inspect other files.", file=sys.stderr)
            return
        selection, kind, optional = entry["path"], entry["kind"], entry["optional"]
    paths = [entry["path"] for entry in files
             if (kind != "directory" and entry["path"] == selection)
             or (kind != "file" and entry["path"].startswith(selection + "/"))]
    if not paths:
        if optional:
            print(f"This run has no stored {role} files; nothing downloaded. "
                  "The optional artifact may not have been generated or uploaded.", file=sys.stderr)
            return
        raise Error("The selected path has no stored files; use cloud-results ls RUN_ID to inspect the run.")
    destination = Path(destination).expanduser().absolute() if destination else Path(config["local"]["results_dir"]) / rid
    storage.pull_paths(rid, destination, paths)
    # A partial copy must never create or refresh the full-run sync marker.
    print(f"Selective download complete: {rid} ({len(paths)} selected files)", file=sys.stderr, flush=True)
    print(destination / selection)


def results(args, config):
    storage = Storage(config)
    if args.operation == "list":
        if args.attempts and not STUDY_RE.fullmatch(args.attempts):
            raise Error("--attempts requires a logical study ID.")
        servers = managed_servers(config)
        manifests = list(storage.manifests())
        if args.attempts:
            history = storage.study_manifest(args.attempts)["attempts"]
            manifests = [storage.manifest(attempt["run_id"]) for attempt in history]
        print(STATUS_HEADER)
        for manifest in manifests:
            print_row(manifest, matching_server(manifest, servers), provider_known=servers is not None)
    elif args.operation == "ls":
        files = storage.files(valid_run(args.run_id))
        if args.json:
            print(json.dumps(files, ensure_ascii=True))
        elif interactive():
            print(f"{'SIZE':>12}  PATH")
            for entry in files:
                print(f"{human_bytes(entry['size']):>12}  {entry['path']}")
        else:
            for entry in files:
                print(f"{entry['size']}\t{entry['path']}")
        if not files:
            print("No stored files found for this run; check the run ID and storage configuration.", file=sys.stderr)
    elif args.operation == "pull":
        role = "figures" if args.plots else "analysis" if args.analysis else "compute_report" if args.report else None
        if args.path is not None or role is not None:
            pull_selection(storage, config, args.run_id, args.path, args.dest, role=role)
        else:
            pull_run(storage, config, args.run_id, args.dest)
    else:
        downloaded = skipped = 0
        for manifest in storage.manifests():
            rid = manifest["run_id"]
            marker = Path(config["local"]["results_dir"]) / rid / ".cloud-pulled.json"
            if manifest["status"] in FINAL and marker.exists() and read_json(marker).get("manifest") == manifest:
                skipped += 1
                continue
            pull_run(storage, config, rid)
            downloaded += 1
        print(f"Sync complete: {downloaded} downloaded, {skipped} unchanged.", file=sys.stderr, flush=True)


def doctor(config):
    missing = [name for name in DEPENDENCIES if not shutil.which(name)]
    if missing:
        raise Error("Missing local CLI dependencies: " + ", ".join(missing))
    print("✓ Python 3.11+ and local CLI executables available")
    print("✓ Non-secret configuration parses correctly")
    # Deliberately do not read, stat, copy or test credentials and do not contact providers.
    print("Offline checks passed. Credentials and cloud connectivity were not tested.")


def parser(name):
    p = argparse.ArgumentParser(prog=name)
    p.add_argument("--config", help="Local non-secret TOML configuration path")
    if name == "cloud-run":
        p.description = "Continue a persistent EWS study with a disposable VM; unchanged completed requests create no compute."
        lineage = p.add_mutually_exclusive_group()
        lineage.add_argument("--fresh", action="store_true", help="Start an independent study lineage; use its returned --study ID to continue it")
        lineage.add_argument("--study", help="Continue an explicit logical study (e.g. one created with --fresh)")
        p.add_argument("experiment_config")
        p.add_argument("--machine")
        p.add_argument("--ews-ref")
        p.add_argument("--name")
        p.add_argument("--max-runtime", type=float)
        p.add_argument("--allow-dirty", action="store_true")
        p.add_argument("--keep-on-setup-failure", action="store_true")
    elif name in ("cloud-attach", "cloud-cancel", "cloud-reproduce"):
        p.add_argument("run_id", help="Logical study ID, attempt ID, or legacy run ID")
        if name == "cloud-reproduce":
            p.description = "Create an independent study from archived source, EWS revision and available environment lock. Ordinary continuation uses cloud-run."
        if name == "cloud-cancel":
            p.add_argument("--yes", action="store_true")
            p.add_argument("--force-delete", action="store_true")
        if name == "cloud-reproduce":
            p.add_argument("--name")
    elif name == "cloud-status":
        p.add_argument("run_id", nargs="?")
    elif name == "cloud-results":
        p.description = "Browse persistent studies, individual attempts, and legacy runs; download full archives or semantic EWS artifacts."
        p.epilog = ("Examples: cloud-results ls RUN_ID; cloud-results pull RUN_ID --plots; "
                    "cloud-results pull RUN_ID --analysis; cloud-results pull RUN_ID --report; cloud-results pull RUN_ID --path RELATIVE_PATH. "
                    "Use pull --help for download options.")
        sub = p.add_subparsers(dest="operation", required=True)
        history = sub.add_parser("list", help="List logical studies and legacy runs, or one study's attempt history")
        history.add_argument("--attempts", metavar="STUDY_ID", help="List every execution attempt for a logical study")
        listing = sub.add_parser("ls", help="Recursively list stored file paths and sizes without downloading",
                                 description="List all files recursively. Redirected stdout is headerless BYTES<TAB>PATH; activity uses stderr.")
        listing.add_argument("run_id")
        listing.add_argument("--json", action="store_true", help="Output a JSON array of {path, size} records; size is bytes")
        pull = sub.add_parser("pull", help="Download a full run or select --plots, --analysis, --report or --path",
                              description="Download a full run by default. Semantic selectors read EWS artifacts.json; legacy runs require --path. Selectors preserve run-relative paths and never mark a run fully downloaded.")
        pull.add_argument("run_id")
        pull.add_argument("--dest", metavar="PATH", help="Local run root (default: configured results_dir/RUN_ID); also applies to selectors")
        selectors = pull.add_mutually_exclusive_group()
        selectors.add_argument("--plots", action="store_true", help="Fetch the figures role from EWS artifacts.json; optional missing figures succeed")
        selectors.add_argument("--analysis", action="store_true", help="Fetch the analysis role from EWS artifacts.json (does not execute analysis)")
        selectors.add_argument("--report", action="store_true", help="Fetch the compute_report role from EWS artifacts.json")
        selectors.add_argument("--path", metavar="RELATIVE_PATH", help="Fetch one literal file or subtree inside the run, preserving its relative path")
        sub.add_parser("sync", help="Download new/changed full runs; skip unchanged final archives")
    return p


def main(name=None):
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 1
    os.umask(0o077)
    name = name or Path(sys.argv[0]).name
    args = parser(name).parse_args()
    try:
        config = load(args.config)
        actions = {"cloud-run": run_command, "cloud-attach": attach, "cloud-status": status,
                   "cloud-results": results, "cloud-cancel": cancel, "cloud-reproduce": reproduce}
        if name == "cloud-doctor":
            doctor(config)
            return 0
        return actions[name](args, config) or 0
    except Error as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError):
        print("Error: invalid data or a local I/O failure (details withheld to protect secrets).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
