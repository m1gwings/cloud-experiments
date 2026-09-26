"""The laptop's hcloud, SSH and rclone adapters; easily replaced by fakes."""

import ipaddress
import json
from pathlib import Path
import shlex
import time

from .common import Error, command, managed_server, remote_path, require_managed, valid_run


class Hetzner:
    def __init__(self, config):
        self.config = config

    def call(self, *args, **kwargs):
        return command(["hcloud", "--context", self.config["context"], "--http-timeout", "30s", *args], **kwargs)

    def servers(self, rid=None):
        selector = "managed-by=cloud-experiments"
        if rid:
            selector += ",run-id=" + valid_run(rid)
        result = json.loads(self.call("server", "list", "--selector", selector, "-o", "json").stdout)
        return [s for s in result if managed_server(s, rid or s.get("labels", {}).get("run-id", "invalid"))]

    def find(self, rid):
        matches = self.servers(rid)
        if len(matches) > 1:
            raise Error("Multiple servers match this run; refusing an ambiguous operation.")
        return matches[0] if matches else None

    def create(self, manifest, user_data):
        result = self.call("server", "create", "--name", manifest["run_id"],
                           "--type", manifest["machine"], "--location", manifest["location"],
                           "--image", "ubuntu-24.04", "--ssh-key", self.config["ssh_key"],
                           "--label", "managed-by=cloud-experiments", "--label", "run-id=" + manifest["run_id"],
                           "--user-data-from-file", "-", "-o", "json", input=user_data.encode(), timeout=300)
        data = json.loads(result.stdout)
        server = data.get("server", data)
        require_managed(server, manifest["run_id"])
        return server

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

    def wait(self, seconds=600):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.call(["test", "-f", "/opt/cloud-experiments/armed"], timeout=20)
                return
            except Error:
                time.sleep(3)
        raise Error("SSH/failsafe initialization did not become ready within 10 minutes.")

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
        return command(["rclone", "--config", self.config_file, "--contimeout", "15s", "--timeout", "60s", "--retries", "2", *args], timeout=timeout)

    def path(self, rid=None):
        return remote_path(self.storage, rid)

    def manifest(self, rid):
        result = json.loads(self.call("cat", self.path(rid) + "/manifest.json", timeout=90).stdout)
        if result.get("run_id") != rid or result.get("schema_version") != 1:
            raise Error("Remote manifest has an unexpected identity or schema version.")
        return result

    def manifests(self):
        entries = json.loads(self.call("lsjson", self.path(), "--dirs-only", timeout=120).stdout)
        for entry in entries:
            try:
                rid = valid_run(entry["Name"])
                yield self.manifest(rid)
            except (Error, ValueError, KeyError):
                print("Warning: an invalid or unreadable run manifest was skipped.")

    def upload(self, directory, rid):
        self.call("copy", str(directory), self.path(rid))
        self.call("check", str(directory), self.path(rid), "--one-way")

    def pull(self, rid, destination):
        self.call("copy", self.path(rid), str(destination))

    def file(self, rid, name, destination):
        if name not in ("source/source.tar.gz", "source/index.json", "manifest.json"):
            raise Error("Unsupported reproduction artifact.")
        self.call("copyto", self.path(rid) + "/" + name, str(destination))
