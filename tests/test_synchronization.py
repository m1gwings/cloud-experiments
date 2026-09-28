"""Cloud pause/resume and cleanup boundaries, with no provider or paid effects."""

import contextlib
import datetime as dt
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config, sample_manifest
from cloud_experiments import synchronization, worker
from cloud_experiments.common import Error, read_json, write_json
from cloud_experiments.ews_contract import CONTRACT


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base, self.work = self.root / "base", self.root / "work"
        self.base.mkdir()
        (self.work / "runtime").mkdir(parents=True)
        self.m = sample_manifest(sample_config(self.root))
        self.m.update(status="running", started_at=self.m["created_at"])
        self.m["ews"]["cloud_contract"] = dict(CONTRACT)
        self.m["settings"]["sync_seconds"] = 5
        self.w = worker.Worker(self.base, self.work, Mock(return_value=Mock(returncode=0)))
        self.w.save(self.m)
        write_json(self.base / "credentials.json", {})

    def test_compute_resumes_before_network_and_temporary_failure_retries(self):
        clock, cycles, events = [0], [0], []
        def sleep(_):
            threading.Event().wait(0.001)
            clock[0] += 5
            if (self.work / "runtime/stop-requested").exists():
                write_json(self.work / "runtime/exit.json", {"exit_code": 130})
        def start():
            events.append("resume")
            for name in ("exit.json", "stop-requested"):
                (self.work / "runtime" / name).unlink(missing_ok=True)
        def sync(*args, **kwargs):
            events.append("upload")
            cycles[0] += 1
            if cycles[0] == 1:
                raise Error("network failed")
            self.m["last_recovery"] = {"commit_id": "committed", "committed_at": "now"}
            write_json(self.work / "runtime/exit.json", {"exit_code": 0})
        with patch.object(self.w, "start_experiment", side_effect=start), patch.object(self.w, "verify_lease"), \
             patch.object(self.w, "request") as request, patch.object(self.w, "publish_lifecycle"), \
             patch.object(synchronization.persistence, "prepare", side_effect=lambda *a: events.append("seal") or self.root), \
             patch.object(synchronization.persistence, "sync", side_effect=sync), \
             patch.object(synchronization.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(synchronization.time, "sleep", side_effect=sleep), contextlib.redirect_stdout(io.StringIO()):
            synchronization.monitor(self.w, self.m)
        self.assertEqual(events, ["seal", "resume", "upload"] * 2)
        request.assert_called_once_with("completed", 0)
        self.assertEqual(self.m["last_recovery"]["commit_id"], "committed")

    def test_compute_completion_is_observed_during_blocked_upload(self):
        transferring, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            transferring.set()
            release.wait(3)
        thread = threading.Thread(target=synchronization.transfer, args=(self.w, self.m, self.root))
        with patch.object(synchronization.persistence, "sync", side_effect=blocked), \
             patch.object(self.w, "publish_lifecycle"), patch.object(self.w, "request") as request:
            thread.start()
            try:
                self.assertTrue(transferring.wait(2))
                write_json(self.work / "runtime/exit.json", {"exit_code": 0})
                synchronization.monitor(self.w, self.m)
                request.assert_called_once_with("completed", 0)
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(2)

    def test_seal_failure_restarts_single_invocation_without_upload(self):
        with patch.object(self.w, "start_experiment") as start, patch.object(self.w, "verify_lease"), \
             patch.object(self.w, "publish_lifecycle"), patch.object(synchronization.persistence, "prepare", side_effect=Error("lock")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(synchronization.checkpoint_cycle(self.w, self.m))
        start.assert_called_once()
        self.assertEqual(self.m["sync"]["status"], "failed")

    def test_failed_cgroup_stop_never_seals_or_starts_another_writer(self):
        with patch.object(self.w, "systemctl", return_value=Mock(returncode=1)), \
             patch.object(self.w, "start_experiment") as start, patch.object(synchronization.persistence, "prepare") as prepare:
            with self.assertRaisesRegex(Error, "cgroup"):
                synchronization.checkpoint_cycle(self.w, self.m)
        start.assert_not_called()
        prepare.assert_not_called()

    def test_finalization_publishes_compute_before_sync_and_cleanup_after_failure(self):
        write_json(self.base / "reason.json", {"status": "completed", "exit_code": 0})
        write_json(self.base / "environment-ready.json", {})
        (self.base / "started").touch()
        write_json(self.work / "runtime/exit.json", {"exit_code": 0})
        events = []
        def lifecycle(manifest):
            events.append(("status", manifest["status"], manifest.get("archive", {}).get("status")))
            self.w.save(manifest)
        def sync(*args, **kwargs):
            events.append(("sync", kwargs.get("final")))
            raise Error("large upload failed")
        with patch.object(self.w, "publish_lifecycle", side_effect=lifecycle), \
             patch.object(self.w, "user_step", return_value=Mock(stdout=b'{"counts":{}}')), \
             patch.object(synchronization.persistence, "sync", side_effect=sync), \
             patch.object(worker, "notify"), contextlib.redirect_stdout(io.StringIO()):
            self.w.finalize()
        self.assertEqual(events[0], ("status", "finalizing", "syncing"))
        self.assertIn(("sync", True), events)
        self.assertEqual(events[-1], ("status", "finalization_failed", "failed"))
        self.assertEqual(self.w.manifest()["compute"]["status"], "completed")
        self.assertEqual(self.w.manifest()["sync"]["status"], "failed")
        self.assertEqual(self.w.run.call_args.args[0], ["systemctl", "start", "--no-block", "cloud-delete.service"])

    def test_deletion_service_reports_aborted_finalization_without_collecting(self):
        self.m["status"] = "finalizing"
        self.w.save(self.m)
        with patch.object(self.w, "publish_lifecycle") as publish, patch.object(worker, "notify"), \
             patch.object(worker, "delete_self") as delete, patch.object(self.w, "collect") as collect, \
             patch.object(self.w, "systemctl", return_value=Mock(returncode=3)):
            self.w.delete()
        self.assertEqual(publish.call_args.args[0]["status"], "finalization_failed")
        self.assertEqual(publish.call_args.args[0]["deletion"]["status"], "requested")
        delete.assert_called_once()
        collect.assert_not_called()

    def test_final_sync_can_pass_45_minutes_and_delete_only_after_publication(self):
        started = dt.datetime.now(dt.timezone.utc)
        self.m["deadline_at"] = (started + dt.timedelta(hours=2)).isoformat()
        self.w.save(self.m)
        write_json(self.base / "reason.json", {"status": "completed", "exit_code": 0})
        write_json(self.base / "environment-ready.json", {})
        (self.base / "started").touch()
        write_json(self.work / "runtime/exit.json", {"exit_code": 0})
        events = []

        def sync(_worker, manifest, **kwargs):
            self.assertTrue(kwargs["final"])
            self.assertFalse(self.w.deadline_expired(manifest, started + dt.timedelta(minutes=46)))
            with self.assertRaisesRegex(Error, "Finalization is active"):
                self.w.delete()
            events.append("recovery committed")
            manifest["last_recovery"] = {"commit_id": "final-commit", "committed_at": "now"}

        def upload(manifest, **kwargs):
            self.assertEqual(manifest["last_recovery"]["commit_id"], "final-commit")
            events.append("archive uploaded")

        def run(argv, **kwargs):
            if argv[-1] == "cloud-delete.service":
                events.append("delete requested")
            return Mock(returncode=0, stdout=b'{"counts":{}}')

        self.w.run = Mock(side_effect=run)
        with patch.object(self.w, "user_step", return_value=Mock(stdout=b'{"counts":{}}')), \
             patch.object(self.w, "collect"), patch.object(self.w, "upload", side_effect=upload), \
             patch.object(synchronization.persistence, "sync", side_effect=sync), \
             patch.object(self.w, "rclone", return_value=Mock(stdout=b"")), \
             patch.object(worker, "notify"), contextlib.redirect_stdout(io.StringIO()):
            self.w.finalize()
        self.assertEqual(events, ["recovery committed", "archive uploaded", "delete requested"])
        self.assertEqual(self.w.manifest()["status"], "completed")
        self.assertEqual(self.w.manifest()["archive"]["status"], "published")

    def test_absolute_deadline_interrupts_finalizer_and_deletes(self):
        self.m.update(status="finalizing", compute={"status": "completed", "exit_code": 0},
                      archive={"status": "syncing"}, sync={"status": "syncing"},
                      deadline_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat())
        self.w.save(self.m)
        with patch.object(self.w, "publish_lifecycle", side_effect=lambda manifest, **kwargs: self.w.save(manifest)), \
             patch.object(worker, "notify"), patch.object(worker, "delete_self") as delete:
            self.w.expire()
            self.w.delete()
        stops = [call.args[0] for call in self.w.run.call_args_list if call.args[0][1] == "stop"]
        self.assertEqual(stops, [["systemctl", "stop", service] for service in
                                 ("cloud-finalize.service", "cloud-supervisor.service", "cloud-experiment.service")])
        self.assertEqual(self.w.manifest()["status"], "finalization_failed")
        self.assertEqual(self.w.manifest()["sync"]["status"], "failed")
        delete.assert_called_once()

    def test_absolute_deadline_stops_running_compute_and_preserves_completed_state(self):
        self.m["deadline_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        self.w.save(self.m)
        with patch.object(self.w, "publish_lifecycle", side_effect=lambda manifest, **kwargs: self.w.save(manifest)):
            self.w.expire()
        self.assertEqual(self.w.manifest()["status"], "timeout")
        self.assertEqual(self.w.manifest()["compute"]["status"], "timeout")
        self.m.update(status="completed", compute={"status": "completed", "exit_code": 0})
        self.w.save(self.m)
        with patch.object(self.w, "publish_lifecycle", side_effect=lambda manifest, **kwargs: self.w.save(manifest)):
            self.w.expire()
        self.assertEqual(self.w.manifest()["status"], "completed")

    def test_finished_finalizer_does_not_wait_for_deadline_to_delete(self):
        self.m["status"] = "completed"
        self.w.save(self.m)
        reason = {"status": "completed", "exit_code": 0}
        write_json(self.base / "reason.json", reason)
        write_json(self.base / "finalized.json", reason)
        with patch.object(self.w, "publish_lifecycle"), patch.object(worker, "notify"), \
             patch.object(worker, "delete_self") as delete:
            self.w.delete()
        delete.assert_called_once()
        self.w.run.assert_not_called()

    def test_background_sync_cannot_revert_requested_finalization_to_running(self):
        write_json(self.base / "reason.json", {"status": "completed", "exit_code": 0})
        self.m["last_recovery"] = {"commit_id": "latest"}
        self.w.save(self.m)
        saved = self.w.manifest()
        self.assertEqual(saved["status"], "finalizing")
        self.assertEqual(saved["compute"]["status"], "completed")
        self.assertEqual(saved["last_recovery"]["commit_id"], "latest")

    def test_cancellation_request_does_not_overwrite_concurrent_recovery_parent(self):
        stale = dict(self.m)
        newer = {**self.m, "parent_state": "new", "pending_recovery": {"commit_id": "pending"}}
        self.w.save(newer)
        with patch.object(self.w, "manifest", return_value=stale), patch.object(worker, "notify"):
            self.w.request("cancelled")
        saved = read_json(self.base / "manifest.json")
        self.assertEqual(saved["parent_state"], "new")
        self.assertEqual(saved["pending_recovery"]["commit_id"], "pending")

    def test_compute_completion_notification_does_not_claim_archive_or_deletion(self):
        self.m.update(status="finalizing", compute={"status": "completed"},
                      archive={"status": "pending"}, deletion={"status": "pending"},
                      last_recovery={"committed_at": "2026-01-01T01:00:00+00:00"})
        api = Mock()
        worker.notify(self.m, {"DISCORD_WEBHOOK_URL": "https://example.invalid/fake"}, api=api)
        message = api.call_args.kwargs["body"]["content"]
        for value in ("EWS=completed", "archive=pending", "VM deletion=pending", "recovery=2026"):
            self.assertIn(value, message)


if __name__ == "__main__":
    unittest.main()
