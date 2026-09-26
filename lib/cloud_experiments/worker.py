"""Worker entry points. The experiment is unprivileged; root owns cleanup/secrets.

All API calls, subprocesses and paths are injectable for offline lifecycle tests.
"""

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .common import Error, command, read_json, redact, remote_path, require_managed, utcnow, write_json
from .source import extract_snapshot, inventory

BASE = Path("/opt/cloud-experiments")
WORK = Path("/work")
API = "https://api.hetzner.cloud/v1"


def http_json(url, *, method="GET", token=None, body=None):
    headers = {"User-Agent": "cloud-experiments/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method in ("GET", "DELETE"):
            return None
        raise Error(f"HTTP operation failed ({exc.code}); details withheld.") from None
    except (OSError, ValueError):
        raise Error("HTTP operation failed; details withheld.") from None


def own_server_id():
    # Link-local metadata cannot be redirected through a user-configured proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://169.254.169.254/hetzner/v1/metadata/instance-id", timeout=10) as response:
        value = response.read(128).decode().strip()
    if not value.isdigit() or int(value) <= 0:
        raise Error("Invalid server metadata identity.")
    return int(value)


def delete_self(manifest, secrets, identity=own_server_id, api=http_json):
    server_id = identity()
    if manifest.get("server_id") not in (None, server_id):
        raise Error("Metadata identity differs from manifest; refusing deletion.")
    url = f"{API}/servers/{server_id}"
    token = secrets["HCLOUD_WORKER_TOKEN"]
    response = api(url, token=token)
    if response is None:  # Already deleted: idempotent success.
        return
    server = response["server"]
    require_managed(server, manifest["run_id"])
    if server["id"] != server_id:
        raise Error("API identity mismatch; refusing deletion.")
    api(url, method="DELETE", token=token)


def notify(manifest, secrets, api=http_json):
    url = secrets.get("DISCORD_WEBHOOK_URL")
    if url:
        try:
            message = (f"{manifest['run_id']}: {manifest['status']} | {manifest['machine']} | "
                       f"exit={manifest.get('exit_code')} | elapsed={manifest.get('elapsed_seconds', 0)}s")
            if manifest.get("upload", {}).get("status") == "failed":
                message += " | artifact upload FAILED"
            api(url, method="POST", body={"content": message, "allowed_mentions": {"parse": []}})
        except Exception:
            print("Discord notification failed; cleanup continues.", flush=True)


class Worker:
    def __init__(self, base=BASE, work=WORK, run=command):
        self.base, self.work, self.run = Path(base), Path(work), run
        self.out = self.base / "out"
        self.manifest_path = self.base / "manifest.json"

    def manifest(self):
        return read_json(self.manifest_path)

    def secrets(self):
        return read_json(self.base / "credentials.json")

    def systemctl(self, *args, **kwargs):
        return self.run(["systemctl", *args], **kwargs)

    def save(self, manifest):
        write_json(self.manifest_path, manifest)
        write_json(self.out / "manifest.json", manifest)

    def request(self, status, exit_code=None):
        # Only root can request cancellation or choose the final status.
        with (self.base / "reason.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.base / "reason.json"
            previous = read_json(path) if path.exists() else None
            current = self.manifest()
            if current["status"] in ("completed", "failed", "cancelled", "timeout"):
                return  # Deletion may be retrying; do not rewrite a finished run as timeout.
            if previous is None or status == "timeout" or current["status"] == "setup_failed":
                write_json(path, {"status": status, "exit_code": exit_code})
        self.systemctl("start", "--no-block", "cloud-finalize.service")

    def rclone(self, *args, timeout=180):
        return self.run(["rclone", "--config", str(self.base / "rclone.conf"),
                         "--contimeout", "10s", "--timeout", "30s", "--retries", "2", *args], timeout=timeout)

    def upload(self, manifest, final=False):
        """Upload payload first, verify it, publish the authoritative manifest last."""
        remote = remote_path(manifest["storage"], manifest["run_id"])
        self.rclone("copy", str(self.out), remote, "--exclude", "/manifest.json", timeout=360)
        self.rclone("check", str(self.out), remote, "--exclude", "/manifest.json", "--one-way", timeout=120)
        manifest["upload"] = {"status": "verified", "verified_at": utcnow()}
        self.save(manifest)
        self.rclone("copyto", str(self.out / "manifest.json"), remote + "/manifest.json", timeout=45)
        content = json.loads(self.rclone("cat", remote + "/manifest.json", timeout=30).stdout)
        if content != manifest:
            raise Error("Uploaded manifest verification failed.")

    def user_step(self, argv, *, cwd=None):
        env = ["env", "-i", "HOME=" + str(self.work / "home"),
               "PATH=" + str(self.work / ".venv/bin") + ":/usr/local/bin:/usr/bin:/bin",
               "LANG=C.UTF-8", "GIT_TERMINAL_PROMPT=0", "PIP_DISABLE_PIP_VERSION_CHECK=1"]
        result = self.run(["runuser", "-u", "experiment", "--", *env, *map(str, argv)], cwd=cwd,
                          timeout=1200, check=False)
        with (self.out / "logs/setup.log").open("ab") as log:
            # Experiment/pip has no worker secrets in its environment, still redact defensively.
            clean = redact((result.stdout + result.stderr).decode(errors="replace"), self.secrets().values())
            log.write(clean.encode())
        if result.returncode:
            raise Error("Worker environment installation failed; see logs/setup.log.")
        return result

    def supervise(self):
        m = self.manifest()
        self.out.mkdir(exist_ok=True)
        (self.out / "logs").mkdir(exist_ok=True)
        try:
            m["server_id"] = own_server_id()
            self.save(m)
            apt_env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            for argv in (["apt-get", "update"], ["apt-get", "install", "-y", "git", "python3", "python3-venv", "python3-pip", "tmux", "rclone", "curl", "util-linux"]):
                result = self.run(argv, env=apt_env, timeout=1200, check=False)
                with (self.out / "logs/setup.log").open("ab") as f:
                    f.write(redact((result.stdout + result.stderr).decode(errors="replace"), self.secrets().values()).encode())
                if result.returncode:
                    raise Error("apt setup failed.")
            self.run(["useradd", "--system", "--user-group", "--home-dir", str(self.work / "home"), "--shell", "/bin/bash", "experiment"])
            self.work.mkdir(exist_ok=True)
            for name in ("source", "output", "home", "runtime", ".ews"):
                (self.work / name).mkdir(exist_ok=True)
            archive = self.base / "incoming/source.tar.gz"
            extract_snapshot(archive, self.work / "source", m["source"]["sha256"])
            (self.out / "source").mkdir(exist_ok=True)
            shutil.copyfile(archive, self.out / "source/source.tar.gz")
            shutil.copyfile(self.base / "incoming/index.json", self.out / "source/index.json")
            (self.out / "config").mkdir(exist_ok=True)
            shutil.copyfile(self.work / "source" / m["config"]["path"], self.out / "config/experiment-config")
            self.run(["chown", "-R", "experiment:experiment", str(self.work)])
            ews = self.work / ".ews"
            self.user_step(["git", "init", str(ews)])
            self.user_step(["git", "-C", str(ews), "fetch", "--depth=1", m["ews"]["repository"], m["ews"]["commit"]])
            self.user_step(["git", "-C", str(ews), "checkout", "--detach", "FETCH_HEAD"])
            resolved = self.user_step(["git", "-C", str(ews), "rev-parse", "HEAD"]).stdout.decode().strip()
            if resolved != m["ews"]["commit"]:
                raise Error("EWS checkout did not match the pinned commit.")
            self.user_step(["python3", "-m", "venv", str(self.work / ".venv")])
            self.user_step(["python", "-m", "pip", "install", "-e", str(ews)])
            source = self.work / "source"
            if m["settings"]["install_experiment"] == "auto":
                if (source / "requirements.txt").is_file():
                    self.user_step(["python", "-m", "pip", "install", "-r", "requirements.txt"], cwd=source)
                if (source / "pyproject.toml").is_file() or (source / "setup.py").is_file():
                    self.user_step(["python", "-m", "pip", "install", "-e", "."], cwd=source)
            # A project dependency must never silently replace the selected EWS revision.
            self.user_step(["python", "-m", "pip", "install", "--no-deps", "-e", str(ews)])
            frozen = self.user_step(["python", "-m", "pip", "freeze"]).stdout.decode(errors="replace")
            (self.out / "machine").mkdir(exist_ok=True)
            (self.out / "machine/pip-freeze.txt").write_text(redact(frozen, self.secrets().values()))
            baseline, _ = inventory(self.work, self.patterns(m))
            # Source baseline always means the uploaded snapshot, before pip/build hooks.
            baseline = {k: v for k, v in baseline.items() if not k.startswith("source/")}
            baseline.update({"source/" + k: v for k, v in read_json(self.base / "incoming/index.json").items()})
            write_json(self.base / "baseline.json", baseline)
            argv = [part.replace("{config}", m["config"]["path"]).replace("{output}", str(self.work / "output")) for part in m["settings"]["command"]]
            write_json(self.work / "runtime/command.json", argv, mode=0o644)
            m.update(status="running", started_at=utcnow(), executed_command=argv)
            m["machine_info"] = self.machine_info()
            self.save(m)
            self.upload(m)
            self.systemctl("start", "cloud-experiment.service")
            (self.base / "started").touch()
            notify(m, self.secrets())
            while True:
                result_path = self.work / "runtime/exit.json"
                if result_path.exists():
                    code = read_json(result_path)["exit_code"]
                    self.request("completed" if code == 0 else "failed", code)
                    return
                active = self.systemctl("is-active", "cloud-experiment.service", check=False)
                if active.returncode:
                    raise Error("Experiment tmux service exited without a completion record.")
                time.sleep(2)
        except Exception:
            print("Worker setup/execution failed; finalizing (details withheld).", flush=True)
            with (self.out / "logs/worker.log").open("a") as log:
                log.write(utcnow() + " Worker setup/execution failed; see setup.log and console.log.\n")
            self.request("failed" if (self.base / "started").exists() else "setup_failed")

    def patterns(self, manifest):
        return ["runtime", "runtime/*", ".ews", ".ews/*", *manifest["settings"]["artifact_exclude"]]

    def machine_info(self):
        return {"python": sys.version, "platform": platform.platform(), "uname": list(platform.uname()),
                "cpu_count": os.cpu_count(), "os_release": Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else None}

    def collect(self, manifest):
        baseline = read_json(self.base / "baseline.json") if (self.base / "baseline.json").exists() else {}
        current, omitted = inventory(self.work, self.patterns(manifest))
        changed = []
        for name, info in current.items():
            if baseline.get(name) != info:
                dest = self.out / "artifacts" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.work / name, dest)
                dest.chmod(info["mode"])
                changed.append(name)
        write_json(self.out / "artifacts/index.json", {"changed": {p: current[p] for p in changed},
                    "deleted": sorted(set(baseline) - set(current)), "excluded": sorted(omitted), "root": str(self.work)})
        console = self.work / "runtime/console.log"
        if console.is_file() and not console.is_symlink():
            (self.out / "logs").mkdir(exist_ok=True)
            shutil.copyfile(console, self.out / "logs/console.log")
        info = self.machine_info()
        write_json(self.out / "machine/runtime.json", info)
        manifest["machine_info"] = info

    def finalize(self):
        with (self.base / "finalize.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            m = self.manifest()
            reason = read_json(self.base / "reason.json") if (self.base / "reason.json").exists() else {"status": "failed" if (self.base / "started").exists() else "setup_failed", "exit_code": None}
            keep = reason["status"] == "setup_failed" and m.get("keep_on_setup_failure", False)
            done = self.base / "finalized.json"
            if done.exists() and read_json(done) == reason:
                if not keep:
                    self.systemctl("start", "--no-block", "cloud-delete.service")
                return
            try:
                # Stop both cgroups. systemd escalates to SIGKILL after 20 seconds.
                self.systemctl("stop", "cloud-supervisor.service", timeout=40, check=False)
                self.systemctl("stop", "cloud-experiment.service", timeout=40, check=False)
                m.update(reason)
                m["finished_at"] = utcnow()
                start = dt.datetime.fromisoformat(m.get("started_at") or m["created_at"])
                m["elapsed_seconds"] = round((dt.datetime.now(dt.timezone.utc) - start).total_seconds(), 3)
                m["retained_until_deadline"] = keep
                try:
                    self.collect(m)
                except Exception:
                    m["collection_error"] = "Artifact collection was incomplete; compute cleanup takes priority."
                self.save(m)
                try:
                    self.upload(m, final=True)
                except Exception:
                    m["upload"] = {"status": "failed", "error": "Upload or verification failed; some artifacts may be missing."}
                    self.save(m)
                    print("Result upload failed; VM deletion will still be attempted.", flush=True)
                    try:
                        self.rclone("copyto", str(self.out / "manifest.json"), remote_path(m["storage"], m["run_id"]) + "/manifest.json", timeout=30)
                    except Exception:
                        pass
                notify(m, self.secrets())
                write_json(done, reason)
            finally:
                if not keep:
                    self.systemctl("start", "--no-block", "cloud-delete.service")


def execute(work=WORK):
    """Runs inside tmux as experiment, with no access to root credentials."""
    work = Path(work)
    argv = read_json(work / "runtime/command.json")
    env = {"HOME": str(work / "home"), "PATH": str(work / ".venv/bin") + ":/usr/local/bin:/usr/bin:/bin",
           "TERM": "screen-256color", "LANG": "C.UTF-8", "PYTHONPATH": str(work / "source"),
           "CLOUD_EXPERIMENTS_OUTPUT": str(work / "output"), "TMPDIR": str(work / "home")}
    # script supplies the experiment with a real PTY for EWS's dashboard and logs it.
    result = subprocess.run(["script", "--quiet", "--return", "--flush", "--command", shlex.join(argv),
                             str(work / "runtime/console.log")], cwd=work / "source", env=env)
    write_json(work / "runtime/exit.json", {"exit_code": result.returncode})


def main():
    os.umask(0o077)
    action = sys.argv[1]
    worker = Worker()
    if action == "execute":
        execute()
    elif action == "supervise":
        worker.supervise()
    elif action == "finalize":
        worker.finalize()
    elif action in ("cancelled", "timeout", "setup_failed"):
        worker.request(action)
    elif action == "delete":
        # systemd restarts this service on failure, even if the finalizer has died.
        delete_self(worker.manifest(), worker.secrets())
    else:
        raise Error("Unknown worker operation.")


if __name__ == "__main__":
    main()
