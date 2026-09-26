"""The laptop's hcloud, SSH and rclone adapters; easily replaced by fakes."""

import ipaddress
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time

from .common import Error, command, managed_server, remote_path, require_managed, valid_result_path, valid_run
from .progress import Activity, activity
from .studies import STUDY_RE, attempt_study, state_head


class Hetzner:
    def __init__(self, config):
        self.config = config

    def call(self, *args, **kwargs):
        label = {"create": "Provisioning VM", "list": "Looking up cloud workers",
                 "delete": "Deleting VM", "describe": "Verifying VM identity"}.get(args[1], "Contacting cloud provider")
        with Activity(label, delay=0.5 if args[1] in ("list", "describe") else 0):
            return command(["hcloud", "--context", self.config["context"], "--http-timeout", "30s", *args], **kwargs)

    def servers(self, rid=None):
        selector = "managed-by=cloud-experiments"
        if rid:
            selector += (",study-id=" if STUDY_RE.fullmatch(rid) else ",run-id=") + valid_run(rid)
        result = json.loads(self.call("server", "list", "--selector", selector, "-o", "json").stdout)
        return [s for s in result if managed_server(s, s.get("labels", {}).get("run-id", "invalid"))
                and (rid is None or (s["labels"].get("study-id") if STUDY_RE.fullmatch(rid) else s["labels"].get("run-id")) == rid)]

    def find(self, rid):
        matches = self.servers(rid)
        if len(matches) > 1:
            raise Error("Multiple servers match this run; refusing an ambiguous operation.")
        return matches[0] if matches else None

    def create(self, manifest, user_data):
        study = manifest.get("study_id")
        labels = ["--label", "study-id=" + study] if study else []
        result = self.call("server", "create", "--name", study or manifest["run_id"],
                           "--type", manifest["machine"], "--location", manifest["location"],
                           "--image", "ubuntu-24.04", "--ssh-key", self.config["ssh_key"],
                           "--label", "managed-by=cloud-experiments", "--label", "run-id=" + manifest["run_id"],
                           *labels, "--user-data-from-file", "-", "-o", "json", input=user_data.encode(), timeout=300)
        data = json.loads(result.stdout)
        server = data.get("server", data)
        require_managed(server, manifest["run_id"])
        return server

    @activity("Verifying and deleting VM")
    def delete(self, server, rid):
        require_managed(server, rid)
        fresh = json.loads(self.call("server", "describe", str(server["id"]), "-o", "json").stdout)
        require_managed(fresh, rid)
        if fresh["id"] != server["id"]:
            raise Error("Server identity changed; refusing deletion.")
        self.call("server", "delete", str(server["id"]))


class SSH:
    def __init__(self, server, state_dir, identity=None):
        self.ip = str(ipaddress.ip_address(server["public_net"]["ipv4"]["ip"]))
        self.host = f"root@{self.ip}"
        directory = Path(state_dir) / valid_run(server["labels"]["run-id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Include immutable server ID so a reused IP/run name never reuses trust.
        self.options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                        "-o", "StrictHostKeyChecking=accept-new", "-o", "GlobalKnownHostsFile=/dev/null",
                        "-o", f"UserKnownHostsFile={directory / ('known_hosts-' + str(server['id']))}"]
        if identity:
            self.options += ["-i", identity, "-o", "IdentitiesOnly=yes"]

    def call(self, args, **kwargs):
        # OpenSSH transmits a remote shell string; quote every argument once.
        return command(["ssh", *self.options, self.host, shlex.join([str(x) for x in args])], **kwargs)

    @activity("Waiting for SSH and deadline protection")
    def wait(self, seconds=600):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.call(["test", "-f", "/opt/cloud-experiments/armed"], timeout=20)
                return
            except Error:
                time.sleep(3)
        raise Error("SSH/failsafe initialization did not become ready within 10 minutes.")

    @activity("Uploading source to VM")
    def upload(self, paths):
        command(["scp", *self.options, *[str(Path(p).absolute()) for p in paths], self.host + ":/opt/cloud-experiments/incoming/"], timeout=1800)

    def attach_argv(self):
        return ["ssh", *self.options, "-t", self.host,
                shlex.join(["runuser", "-u", "experiment", "--", "tmux", "-L", "cloud-experiments", "attach", "-t", "experiment"])]


class Storage:
    def __init__(self, config):
        self.storage = config["storage"]
        self.config_file = config["local"]["rclone_config"]

    def call(self, *args, timeout=1800):
        label = {"cat": "Reading stored manifest", "lsjson": "Listing stored files",
                 "copy": "Transferring files", "copyto": "Transferring stored file",
                 "check": "Verifying stored files"}.get(args[0], "Accessing storage")
        with Activity(label, delay=0.5 if args[0] in ("cat", "lsjson") else 0) as progress:
            flags = []
            kwargs = {}
            if args[0] in ("copy", "copyto", "check"):
                flags = ["--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE"]
                kwargs["stderr_line"] = progress.rclone_line
            return command(["rclone", "--config", self.config_file, "--ask-password=false",
                            "--contimeout", "15s", "--timeout", "60s", "--retries", "2", *args, *flags],
                           timeout=timeout, **kwargs)

    def path(self, rid=None):
        return remote_path(self.storage, rid)

    def manifest(self, rid):
        if STUDY_RE.fullmatch(rid):
            return self.study_manifest(rid)
        result = json.loads(self.call("cat", self.path(rid) + "/manifest.json", timeout=90).stdout)
        if result.get("run_id") != rid or result.get("schema_version") != 1:
            raise Error("Remote manifest has an unexpected identity or schema version.")
        return result

    def artifact_index(self, rid, path):
        """Read bounded discovery JSON through the existing metadata activity UI."""
        from .artifacts import MAX_INDEX_BYTES

        path = valid_result_path(path)
        raw = self.call("cat", self.path(rid) + "/" + path,
                        "--head", str(MAX_INDEX_BYTES + 1), timeout=90).stdout
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if size > MAX_INDEX_BYTES:
            raise Error("EWS artifact catalog exceeds the 64 KiB limit; use --path.")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        try:
            return json.loads(raw, object_pairs_hook=unique_keys)
        except (ValueError, UnicodeError, RecursionError):
            raise Error("Invalid EWS artifact JSON; use --path to inspect stored files.") from None

    def studies(self):
        bucket = self.path().removesuffix("/runs")
        roots = json.loads(self.call("lsjson", bucket, "--dirs-only", timeout=120).stdout)
        if not any(e.get("Name") == "studies" for e in roots):
            return []
        root = bucket + "/studies"
        entries = json.loads(self.call("lsjson", root, "--dirs-only", timeout=120).stdout)
        return [e["Name"] for e in entries if STUDY_RE.fullmatch(e.get("Name", ""))]

    def study_state(self, study):
        if study not in self.studies():
            return None
        files = self.files(study)
        commits = [json.loads(self.call("cat", self.path(study) + "/" + e["path"], timeout=90).stdout)
                   for e in files if e["path"].startswith("commits/") and e["path"].endswith(".json")]
        return state_head(study, commits)

    def study_manifest(self, study):
        files = self.files(study)
        attempts = []
        for entry in files:
            parts = entry["path"].split("/")
            if len(parts) == 3 and parts[0] == "attempts" and parts[2] == "manifest.json" and attempt_study(parts[1]) == study:
                attempts.append(self.manifest(parts[1]))
        if not attempts:
            raise Error("No stored attempts found for this study.")
        latest = max(attempts, key=lambda m: (m["created_at"], m["run_id"]))
        head = self.study_state(study)
        return {**latest, "run_id": study, "study_id": study, "attempt_id": latest["run_id"],
                "state": head, "attempts": [{k: m.get(k) for k in ("run_id", "status", "machine", "created_at", "finished_at", "ews")} for m in sorted(attempts, key=lambda m: (m["created_at"], m["run_id"]))]}

    def manifests(self):
        for study in self.studies():
            yield self.study_manifest(study)
        roots = json.loads(self.call("lsjson", self.path().removesuffix("/runs"), "--dirs-only", timeout=120).stdout)
        if not any(e.get("Name") == "runs" for e in roots):
            return
        entries = json.loads(self.call("lsjson", self.path(), "--dirs-only", timeout=120).stdout)
        manifests, skipped = [], 0
        with Activity("Reading run manifests", delay=0.5) as progress:
            for index, entry in enumerate(entries, 1):
                try:
                    rid = valid_run(entry["Name"])
                    manifests.append(self.manifest(rid))
                except (Error, ValueError, KeyError):
                    skipped += 1
                progress.count(index, len(entries))
        if skipped:
            print(f"Warning: {skipped} invalid or unreadable run manifest(s) skipped.", file=sys.stderr)
        yield from manifests

    def upload(self, directory, rid):
        with Activity("Uploading input snapshot"):
            self.call("copy", str(directory), self.path(rid))
        self.call("check", str(directory), self.path(rid), "--one-way")

    def pull(self, rid, destination):
        with Activity(f"Downloading {valid_run(rid)}"):
            self.call("copy", self.path(rid), str(destination))
        if STUDY_RE.fullmatch(rid):
            from .common import write_json
            write_json(Path(destination) / "manifest.json", self.study_manifest(rid))

    def files(self, rid):
        """Read object names and sizes only, never file contents or hashes."""
        raw = json.loads(self.call("lsjson", self.path(rid), "--recursive", "--files-only",
                                   "--no-modtime", "--no-mimetype").stdout)
        if not isinstance(raw, list):
            raise Error("Invalid stored file listing.")
        files, seen = [], set()
        for entry in raw:
            if (not isinstance(entry, dict) or entry.get("IsDir") is not False
                    or type(entry.get("Size")) is not int or entry["Size"] < 0):
                raise Error("Invalid stored file listing.")
            path = valid_result_path(entry.get("Path"))
            if path != entry["Path"] or path in seen:
                raise Error("Invalid or duplicate stored file path.")
            seen.add(path)
            files.append({"path": path, "size": entry["Size"]})
        return sorted(files, key=lambda entry: entry["path"])

    def pull_paths(self, rid, destination, paths):
        """Copy an exact list of objects, retaining their paths below the run root."""
        remote = self.path(rid)
        destination = Path(destination).expanduser().absolute()
        paths = [valid_result_path(path) for path in paths]
        if not paths:
            return
        for path in paths:
            if path.split("/", 1)[0] in (".cloud-pulled.json", ".cloud-pulled.json.tmp"):
                raise Error("Refusing to overwrite the local full-download marker.")
            local = destination
            for part in path.split("/"):
                local = local / part
                if local.is_symlink():
                    raise Error("Selected destination contains a symlink; choose a different --dest.")
        destination.mkdir(parents=True, exist_ok=True)
        # Raw names are literal, including spaces, # and glob characters. Never
        # build rclone filters from user input or put paths into a shell command.
        with tempfile.TemporaryDirectory(prefix="cloud-results-") as tmp:
            selection = Path(tmp) / "files"
            selection.write_text("".join(path + "\n" for path in paths), encoding="utf-8")
            with Activity(f"Downloading selected files for {valid_run(rid)}"):
                self.call("copy", remote, str(destination), "--files-from-raw", str(selection), "--no-traverse")

    def file(self, rid, name, destination):
        if name not in ("source/source.tar.gz", "source/index.json", "manifest.json", "machine/environment.json"):
            raise Error("Unsupported reproduction artifact.")
        with Activity("Downloading reproduction input"):
            self.call("copyto", self.path(rid) + "/" + name, str(destination))
