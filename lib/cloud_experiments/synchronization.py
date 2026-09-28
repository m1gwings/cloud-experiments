"""Cooperative EWS pause/seal/resume scheduling; remote failures never stop compute."""

import time
import threading

from .common import Error, read_json, utcnow
from . import persistence


def checkpoint_cycle(worker, manifest):
    """The EWS writer has exited; seal before restarting the sole invocation."""
    stopped = worker.systemctl("stop", "cloud-experiment.service", timeout=40, check=False)
    if stopped.returncode:
        raise Error("Experiment cgroup did not stop; refusing recovery snapshot.")
    snapshot = None
    try:
        snapshot = persistence.prepare(worker, manifest)
    except Exception as exc:
        worker.report_failure(manifest, component="supervisor", exc=exc)
        sync_failed(worker, manifest)
    # Sealed snapshot bytes are independent of the writer. Network I/O happens
    # only after resuming; no provider credentials enter the experiment process.
    worker.verify_lease(manifest)
    worker.start_experiment()
    return snapshot


def sync_failed(worker, manifest):
    manifest["sync"] = {"status": "failed", "attempted_at": utcnow(),
                        "contract": manifest["ews"]["cloud_contract"],
                        "error": "Recovery sync failed; preceding committed recovery remains authoritative."}
    worker.publish_lifecycle(manifest)
    print("Recovery sync failed; compute continues and synchronization will retry.", flush=True)


def transfer(worker, manifest, snapshot):
    try:
        persistence.sync(worker, manifest, snapshot=snapshot)
        manifest["sync"] = {"status": "committed", "attempted_at": utcnow(),
                            "contract": manifest["ews"]["cloud_contract"]}
        worker.publish_lifecycle(manifest)
    except Exception as exc:
        worker.report_failure(manifest, component="supervisor", exc=exc)
        sync_failed(worker, manifest)
    finally:
        if not (worker.base / "reason.json").exists():
            worker.stage("compute", "compute.run")


def monitor(worker, manifest):
    interval = manifest["settings"].get("sync_seconds", 300)
    due = time.monotonic() + interval
    pausing = False
    upload = None
    while True:
        result_path = worker.work / "runtime/exit.json"
        if result_path.exists():
            code = read_json(result_path)["exit_code"]
            if not pausing or code != 130:
                if code != 0 and not (worker.base / "reason.json").exists():
                    worker.report_failure(dict(manifest, status="failed", compute={"status": "failed", "exit_code": code}),
                                          component="compute", message=f"EWS exited with code {code}.", stage="compute.run",
                                          unit="cloud-experiment.service")
                worker.request("completed" if code == 0 else "failed", code)
                return
            snapshot = checkpoint_cycle(worker, manifest)
            pausing = False
            due = time.monotonic() + interval
            if snapshot is not None:
                # Keep observing compute completion while transfer blocks. The
                # finalizer stops this supervisor cgroup before its own sync.
                upload = threading.Thread(target=transfer, args=(worker, manifest, snapshot), daemon=True)
                upload.start()
            continue
        active = worker.systemctl("is-active", "cloud-experiment.service", check=False)
        if active.returncode:
            raise Error("Experiment tmux service exited without a completion record.")
        if (manifest.get("ews", {}).get("cloud_contract") and not pausing
                and not (upload and upload.is_alive()) and time.monotonic() >= due):
            # EWS can take longer than an interval to finish one protocol step.
            # Do not kill useful work merely to satisfy a snapshot clock.
            (worker.work / "runtime/stop-requested").touch(mode=0o644)
            pausing = True
        time.sleep(2)
