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
import signal
import threading
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .common import Error, command, read_json, redact, remote_path, require_managed, utcnow, write_json
from .workspace import extract_snapshot, inventory
from . import environment, persistence, ews_contract, synchronization

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
            message = (f"{manifest['run_id']}: {manifest['status']} | "
                       f"EWS={manifest.get('compute', {}).get('status', 'pending')} | "
                       f"recovery={manifest.get('last_recovery', {}).get('committed_at', 'none')} | "
                       f"archive={manifest.get('archive', {}).get('status', 'pending')} | "
                       f"VM deletion={manifest.get('deletion', {}).get('status', 'pending')}")
            api(url, method="POST", body={"content": message, "allowed_mentions": {"parse": []}})
        except Exception:
            print("Discord notification failed; cleanup continues.", flush=True)


class Worker:
    def __init__(self, base=BASE, work=WORK, run=command):
        self.base, self.work, self.run = Path(base), Path(work), run
        self.out = self.base / "out"
        self.manifest_path = self.base / "manifest.json"
        self.state_lock = threading.RLock()

    def manifest(self):
        return read_json(self.manifest_path)

    def secrets(self):
        return read_json(self.base / "credentials.json")

    def systemctl(self, *args, **kwargs):
        return self.run(["systemctl", *args], **kwargs)

    def save(self, manifest):
        with self.state_lock:
            reason = self.base / "reason.json"
            if manifest.get("status") == "running" and reason.exists():
                requested = read_json(reason)
                manifest.update(status="finalizing", compute={"status": requested["status"],
                                "exit_code": requested["exit_code"]}, archive={"status": "pending"})
            write_json(self.manifest_path, manifest)
            write_json(self.out / "manifest.json", manifest)

    def publish_lifecycle(self, manifest, *, persist=True):
        """A small independent status write must never wait for artifact collection."""
        with self.state_lock, (self.base / "lifecycle.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._publish_lifecycle(manifest, persist=persist)

    def _publish_lifecycle(self, manifest, *, persist=True):
        if persist:
            self.save(manifest)
        lifecycle = {key: manifest[key] for key in
                     ("run_id", "status", "compute", "last_recovery", "sync", "archive", "deletion", "ews", "tooling")
                     if key in manifest}
        lifecycle.update(schema_version=1, updated_at=utcnow())
        path = self.base / "lifecycle.json"
        write_json(path, lifecycle)
        try:
            self.rclone("copyto", str(path), remote_path(manifest["storage"], manifest["run_id"]) + "/lifecycle.json", timeout=15)
        except Exception:
            print("Lifecycle status publication failed; cleanup protection remains active.", flush=True)

    def request(self, status, exit_code=None):
        # Only root can request cancellation or choose the final status.
        with (self.base / "reason.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.base / "reason.json"
            previous = read_json(path) if path.exists() else None
            current = self.manifest()
            if current["status"] in ("completed", "failed", "cancelled", "timeout", "finalization_failed"):
                return  # Deletion may be retrying; do not rewrite a finished run as timeout.
            if previous is None or status == "timeout" or current["status"] == "setup_failed":
                write_json(path, {"status": status, "exit_code": exit_code})
            current.update(status="finalizing", compute={"status": status, "exit_code": exit_code,
                                                       "finished_at": utcnow()}, archive={"status": "pending"})
            # Cancellation may arrive in another process while a sync accepts a
            # new parent. The request must never overwrite recovery bookkeeping.
            # reason.json is authoritative until the finalizer stops the writer.
            self.publish_lifecycle(current, persist=False)
            notify(current, self.secrets())
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

    def user_step(self, argv, *, cwd=None, timeout=1200):
        env = ["env", "-i", "HOME=" + str(self.work / "home"),
               "PATH=" + str(self.work / ".venv/bin") + ":/usr/local/bin:/usr/bin:/bin",
               "LANG=C.UTF-8", "GIT_TERMINAL_PROMPT=0", "PIP_DISABLE_PIP_VERSION_CHECK=1"]
        argv = list(argv)
        lock = self.work / "runtime/requirements-lock.txt"
        if argv[:4] == ["python", "-m", "pip", "install"] and lock.exists():
            argv.extend(["--constraint", str(lock)])
        result = self.run(["runuser", "-u", "experiment", "--", *env, *map(str, argv)], cwd=cwd,
                          timeout=timeout, check=False)
        with (self.out / "logs/setup.log").open("ab") as log:
            # Experiment/pip has no worker secrets in its environment, still redact defensively.
            clean = redact((result.stdout + result.stderr).decode(errors="replace"), self.secrets().values())
            log.write(clean.encode())
        if result.returncode:
            raise Error("Worker environment installation failed; see logs/setup.log.")
        return result

    def verify_lease(self, manifest):
        """Fence restoration/publication against the live provider identity, not stale objects."""
        if not manifest.get("study_id"):
            return
        identity = own_server_id()
        if manifest.get("server_id") != identity:
            raise Error("Continuation VM identity changed; refusing study writes.")
        response = http_json(f"{API}/servers/{identity}", token=self.secrets()["HCLOUD_WORKER_TOKEN"])
        if response is None:
            raise Error("Study VM lease has ended; refusing state publication.")
        require_managed(response["server"], manifest["run_id"])
        if response["server"]["id"] != identity:
            raise Error("Study lease provider identity mismatch.")

    def capture_environment(self):
        return environment.decode(self.user_step(["python", "-c", environment.CAPTURE]).stdout)

    def seal_environment(self, manifest, expected):
        actual = self.capture_environment()
        if expected:
            environment.verify(expected, actual)
        write_json(self.out / "machine/environment.json", actual)
        record = {"attempt_id": manifest["run_id"], "created_at": manifest["created_at"], "environment": actual}
        self.verify_lease(manifest)
        path = self.base / "environment-record.json"
        write_json(path, record)
        remote = remote_path(manifest["storage"], manifest["study_id"]) + "/environments/" + manifest["run_id"] + ".json"
        self.rclone("copyto", str(path), remote, timeout=45)
        if json.loads(self.rclone("cat", remote, timeout=30).stdout) != record:
            raise Error("Environment lock publication failed.")
        write_json(self.base / "environment-ready.json", actual)

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
            expected_environment = None
            if m.get("study_id") and m["ews"].get("cloud_contract"):
                self.verify_lease(m)
                _, expected_environment = persistence.read_head(self, m)
            if not expected_environment and m.get("reproduction_environment_sha256"):
                from .common import sha256
                path = self.base / "incoming/environment.json"
                if sha256(path) != m["reproduction_environment_sha256"]:
                    raise Error("Archived reproduction environment checksum mismatch.")
                expected_environment = environment.decode(path.read_bytes())
            if expected_environment:
                write_json(self.base / "expected-environment.json", expected_environment)
                (self.work / "runtime/requirements-lock.txt").write_text(environment.constraints(expected_environment))
            self.run(["chown", "-R", "experiment:experiment", str(self.work)])
            ews = self.work / ".ews"
            self.user_step(["git", "init", str(ews)])
            self.user_step(["git", "-C", str(ews), "fetch", "--depth=1", m["ews"]["repository"], m["ews"]["commit"]])
            self.user_step(["git", "-C", str(ews), "checkout", "--detach", "FETCH_HEAD"])
            resolved = self.user_step(["git", "-C", str(ews), "rev-parse", "HEAD"]).stdout.decode().strip()
            if resolved != m["ews"]["commit"]:
                raise Error("EWS checkout did not match the pinned commit.")
            self.user_step(["python3", "-m", "venv", str(self.work / ".venv")])
            if expected_environment:
                runtime = self.capture_environment()["runtime"]
                if runtime != expected_environment["runtime"]:
                    raise Error("Worker Python/ABI/architecture/libc differs from the continuation lock; no checkpoint loaded.")
                self.user_step(["python", "-m", "pip", "install", "--only-binary=:all:", "-r", str(self.work / "runtime/requirements-lock.txt")])
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
            runtime_options = None
            if m["ews"].get("cloud_contract"):
                # Reject changed capabilities/resources before sealing an environment.
                ews_contract.query(self, m["ews"]["cloud_contract"])
                runtime_options = ews_contract.runtime_options(self, m)
            if m.get("study_id"):
                if runtime_options is None and m.get("portable_continuation", True):
                    # Legacy reproductions retain their original resource checks.
                    self.user_step(["python", "-c", "from experiments_wo_stress import load_config; from experiments_wo_stress.execution.portability import portable_environment; import sys; portable_environment(load_config(sys.argv[1]))", str(source / m["config"]["path"])])
                self.seal_environment(m, expected_environment)
                if runtime_options is not None:
                    restored_environment = persistence.restore(self, m)
                    if restored_environment:
                        environment.verify(restored_environment, self.capture_environment())
                    self.run(["chown", "-R", "experiment:experiment", str(self.work / "output")])
            baseline, _ = inventory(self.work, self.patterns(m))
            # Source baseline always means the uploaded snapshot, before pip/build hooks.
            baseline = {k: v for k, v in baseline.items() if not k.startswith("source/")}
            baseline.update({"source/" + k: v for k, v in read_json(self.base / "incoming/index.json").items()})
            write_json(self.base / "baseline.json", baseline)
            argv = [part.replace("{config}", m["config"]["path"]).replace("{output}", str(self.work / "output")) for part in m["settings"]["command"]]
            if m.get("study_id"):
                if m.get("portable_continuation", True):
                    argv.append("--portable")
                (self.work / "runtime/portable").touch()
            if runtime_options:
                argv = ews_contract.command_options(argv, runtime_options, m["settings"].get("timezone"))
                m["runtime"] = runtime_options
            write_json(self.work / "runtime/command.json", argv, mode=0o644)
            m.update(status="running", started_at=utcnow(), executed_command=argv,
                     compute={"status": "running"}, archive={"status": "pending"}, deletion={"status": "pending"})
            m["machine_info"] = self.machine_info()
            self.save(m)
            self.upload(m)
            self.start_experiment()
            (self.base / "started").touch()
            notify(m, self.secrets())
            synchronization.monitor(self, m)
        except Exception as exc:
            m["setup_error"] = str(exc) if isinstance(exc, Error) else "Worker setup/execution failed (details withheld)."
            self.save(m)
            print("Worker setup/execution failed; finalizing (details withheld).", flush=True)
            with (self.out / "logs/worker.log").open("a") as log:
                log.write(utcnow() + " " + m["setup_error"] + "\n")
            self.request("failed" if (self.base / "started").exists() else "setup_failed")

    def patterns(self, manifest):
        owned = ["output", "output/*"] if manifest.get("study_id") and manifest["ews"].get("cloud_contract") else []
        return ["runtime", "runtime/*", ".ews", ".ews/*", *owned, *manifest["settings"]["artifact_exclude"]]

    def start_experiment(self):
        for name in ("exit.json", "child-pid.json", "stop-requested"):
            (self.work / "runtime" / name).unlink(missing_ok=True)
        self.systemctl("start", "cloud-experiment.service")

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

    def interrupt_experiment(self, grace=90):
        """Ask the unprivileged PTY wrapper to signal EWS alone, then bound the wait."""
        if not (self.base / "started").exists():
            return
        (self.work / "runtime/stop-requested").touch(mode=0o644)
        until = time.monotonic() + grace
        while time.monotonic() < until:
            if (self.work / "runtime/exit.json").exists():
                return
            time.sleep(0.2)

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
                # Stop setup first, then request a safe EWS boundary before cgroup teardown.
                supervisor = self.systemctl("stop", "cloud-supervisor.service", timeout=40, check=False)
                if supervisor.returncode:
                    raise Error("Setup cgroup did not stop; refusing a live filesystem snapshot.")
                # The supervisor may have finished a sync while finalization was
                # requested. Read its last durable commit only after it stops.
                m = self.manifest()
                m.update(status="finalizing", compute={"status": reason["status"],
                         "exit_code": reason["exit_code"], "finished_at": utcnow()},
                         archive={"status": "syncing"})
                self.publish_lifecycle(m)
                self.interrupt_experiment()
                stopped = self.systemctl("stop", "cloud-experiment.service", timeout=40, check=False)
                if stopped.returncode:
                    raise Error("Experiment cgroup did not stop; refusing a live filesystem snapshot.")
                m["exit_code"] = reason["exit_code"]
                m["finished_at"] = utcnow()
                start = dt.datetime.fromisoformat(m.get("started_at") or m["created_at"])
                m["elapsed_seconds"] = round((dt.datetime.now(dt.timezone.utc) - start).total_seconds(), 3)
                m["retained_until_deadline"] = keep
                if m["ews"].get("cloud_contract") and (self.base / "environment-ready.json").exists() and (self.base / "started").exists():
                    m["sync"] = {"status": "syncing", "attempted_at": utcnow(), "contract": m["ews"]["cloud_contract"]}
                    self.publish_lifecycle(m)
                    try:
                        result = self.user_step(["ews", "inspect", str(self.work / "output")], timeout=30)
                        m["ews_counts"] = json.loads(result.stdout)["counts"]
                    except Exception:
                        m["ews_counts"] = {}
                    persistence.sync(self, m, final=True)
                    m["sync"] = {"status": "committed", "attempted_at": utcnow(), "contract": m["ews"]["cloud_contract"]}
                    self.publish_lifecycle(m)
                self.collect(m)
                # Output bytes already live in the recovery object pool. Only
                # logs, inputs, provenance and other workspace deltas remain.
                self.upload(m, final=True)
                m.update(reason)
                m["archive"] = {"status": "published", "published_at": utcnow()}
                self.save(m)
                remote = remote_path(m["storage"], m["run_id"]) + "/manifest.json"
                self.rclone("copyto", str(self.out / "manifest.json"), remote, timeout=30)
                self.publish_lifecycle(m)
                notify(m, self.secrets())
                write_json(done, reason)
            except Exception:
                m["status"] = "finalization_failed"
                m["compute"] = {"status": reason["status"], "exit_code": reason["exit_code"]}
                m["archive"] = {"status": "failed"}
                if m.get("sync", {}).get("status") == "syncing":
                    m["sync"]["status"] = "failed"
                m["upload"] = {"status": "failed", "error": "Final synchronization or archive publication failed; preceding recovery is retained."}
                self.publish_lifecycle(m)
                notify(m, self.secrets())
                print("Finalization failed; VM deletion will still be attempted.", flush=True)
                write_json(done, reason)
            finally:
                if not keep:
                    self.systemctl("start", "--no-block", "cloud-delete.service")

    def delete(self):
        """Best-effort status is independent of finalizer success or service timeout."""
        m = self.manifest()
        if m.get("status") in ("running", "provisioning", "finalizing"):
            m["status"] = "interrupted" if m["status"] != "finalizing" else "finalization_failed"
        m["deletion"] = {"status": "requested", "requested_at": utcnow()}
        self.publish_lifecycle(m, persist=False)
        notify(m, self.secrets())
        delete_self(m, self.secrets())


def execute(work=WORK):
    """Runs in tmux; only an explicitly supplied Discord credential reaches EWS."""
    work = Path(work)
    argv = read_json(work / "runtime/command.json")
    env = {"HOME": str(work / "home"), "PATH": str(work / ".venv/bin") + ":/usr/local/bin:/usr/bin:/bin",
           "TERM": "screen-256color", "LANG": "C.UTF-8", "PYTHONPATH": str(work / "source"),
           "CLOUD_EXPERIMENTS_OUTPUT": str(work / "output"), "TMPDIR": str(work / "home")}
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials:
        # systemd creates a protected, service-scoped copy with LoadCredential.
        # Do not inherit the caller's environment or pass secrets in argv.
        try:
            webhook = (Path(credentials) / "ews-discord-webhook").read_text()
            if not webhook or any(c in webhook for c in "\n\r\x00"):
                raise ValueError("invalid credential")
        except (OSError, ValueError):
            raise Error("EWS Discord runtime credential is missing or invalid; value withheld.") from None
        env["EWS_DISCORD_WEBHOOK_URL"] = webhook
    # The child shim records the EWS process ID before exec. Signals originate in
    # this unprivileged wrapper, never from root using an untrusted PID file.
    done = threading.Event()
    def interrupt():
        while not done.wait(0.2):
            if (work / "runtime/stop-requested").exists():
                try:
                    pid = read_json(work / "runtime/child-pid.json")["pid"]
                    if type(pid) is int and pid > 1:
                        os.kill(pid, signal.SIGINT)
                        return
                except (OSError, ValueError, KeyError):
                    pass
    thread = None
    if (work / "runtime/portable").exists():
        argv = ["/usr/bin/python3", "/opt/cloud-experiments/entry.py", "child"]
        thread = threading.Thread(target=interrupt, daemon=True)
        thread.start()
    # script supplies the experiment with a real PTY for EWS's dashboard and logs it.
    try:
        result = subprocess.run(["script", "--quiet", "--return", "--flush", "--append", "--command", shlex.join(argv),
                                 str(work / "runtime/console.log")], cwd=work / "source", env=env)
    finally:
        done.set()
        if thread:
            thread.join(timeout=1)
    write_json(work / "runtime/exit.json", {"exit_code": result.returncode})


def main():
    os.umask(0o077)
    action = sys.argv[1]
    worker = Worker()
    if action == "child":
        argv = read_json(WORK / "runtime/command.json")
        write_json(WORK / "runtime/child-pid.json", {"pid": os.getpid()})
        os.execvpe(argv[0], argv, os.environ)
    elif action == "execute":
        execute()
    elif action == "supervise":
        worker.supervise()
    elif action == "finalize":
        worker.finalize()
    elif action in ("cancelled", "timeout", "setup_failed"):
        worker.request(action)
    elif action == "delete":
        # systemd restarts this service on failure, even if the finalizer has died.
        worker.delete()
    else:
        raise Error("Unknown worker operation.")


if __name__ == "__main__":
    main()
