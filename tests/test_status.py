"""Lifecycle presentation uses fake providers and small independent records."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config, sample_manifest, server
from cloud_experiments import cli
from cloud_experiments.common import Error
from cloud_experiments.providers import Storage


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = sample_config(temporary.name)
        self.manifest = sample_manifest(self.config)
        self.manifest.update(status="running", compute={"status": "running"},
                             last_recovery={"committed_at": "2026-09-27T12:00:00+00:00", "commit_id": "a" * 32},
                             archive={"status": "pending"})
        self.storage = Mock()
        self.storage.manifest.return_value = self.manifest
        self.storage.manifests.return_value = [self.manifest]
        self.cloud = Mock()
        self.cloud.servers.return_value = []
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cli, "Storage", return_value=self.storage))
        self.stack.enter_context(patch.object(cli, "Hetzner", return_value=self.cloud))
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.errors))

    def invoke(self, command, *arguments):
        getattr(cli, "status" if command == "cloud-status" else "results")(
            cli.parser(command).parse_args(arguments), self.config)
        rows = self.output.getvalue().splitlines()
        return dict(zip(rows[0].split("\t"), rows[1].split("\t")))

    def test_absent_server_marks_active_stored_states_interrupted_in_both_commands(self):
        for command, arguments in (("cloud-status", ["test-run"]), ("cloud-results", ["list"])):
            for status in ("running", "provisioning", "finalizing"):
                with self.subTest(command=command, status=status):
                    self.output.seek(0)
                    self.output.truncate()
                    self.cloud.reset_mock()
                    self.manifest["status"] = status
                    row = self.invoke(command, *arguments)
                    self.assertEqual(row["STATUS"], "interrupted")
                    self.assertEqual(row["VM"], "gone")
                    self.assertEqual(row["RECOVERY"], "2026-09-27T12:00:00+00:00")
                    self.cloud.servers.assert_called_once_with()

    def test_provider_failure_means_unknown_not_interrupted(self):
        self.cloud.servers.side_effect = Error("private provider failure output")
        row = self.invoke("cloud-results", "list")
        self.assertEqual(row["STATUS"], "unknown")
        self.assertEqual(row["VM"], "unknown")
        self.assertNotIn("private provider", self.errors.getvalue())

    def test_compute_completion_archive_failure_and_vm_are_distinct(self):
        self.cloud.servers.return_value = [server()]
        self.manifest.update(status="finalizing", compute={"status": "completed"}, archive={"status": "failed"})
        row = self.invoke("cloud-status", "test-run")
        self.assertEqual(row["STATUS"], "finalization_failed")
        self.assertEqual(row["EWS"], "completed")
        self.assertEqual(row["ARCHIVE"], "failed")
        self.assertEqual(row["VM"], "123")

    def test_status_without_id_includes_interrupted_stored_studies(self):
        row = self.invoke("cloud-status")
        self.assertEqual(row["STATUS"], "interrupted")

    def test_periodic_failure_keeps_last_success_visible_while_compute_runs(self):
        self.cloud.servers.return_value = [server()]
        self.manifest["sync"] = {"status": "failed"}
        row = self.invoke("cloud-results", "list")
        self.assertEqual(row["STATUS"], "running")
        self.assertEqual(row["RECOVERY"], "2026-09-27T12:00:00+00:00 (sync failed)")

    def test_force_delete_publishes_small_status_after_deletion_even_without_upload(self):
        self.cloud.find.return_value = server()
        order = []
        self.cloud.delete.side_effect = lambda *args: order.append("delete")
        self.storage.record_deletion.side_effect = lambda *args: order.append("lifecycle")
        cli.cancel(cli.parser("cloud-cancel").parse_args(["test-run", "--force-delete", "--yes"]), self.config)
        self.assertEqual(order, ["delete", "lifecycle"])
        self.storage.upload.assert_not_called()


class LifecycleStorageTests(unittest.TestCase):
    def test_independent_lifecycle_overrides_stale_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = sample_config(tmp)
            stored = sample_manifest(config)
            stored["status"] = "running"
            lifecycle = {"schema_version": 1, "run_id": "test-run", "status": "finalization_failed",
                         "compute": {"status": "completed"}, "archive": {"status": "failed"}}
            storage = Storage(config)
            with patch.object(storage, "call", side_effect=[Mock(stdout=json.dumps(stored)),
                              Mock(stdout='[{"Name":"lifecycle.json"}]'), Mock(stdout=json.dumps(lifecycle))]):
                result = storage.manifest("test-run")
            self.assertEqual(result["status"], "finalization_failed")
            self.assertEqual(result["compute"]["status"], "completed")
            self.assertEqual(result["source"], stored["source"])

    def test_lifecycle_read_errors_and_wrong_identity_are_not_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(sample_config(tmp))
            for result in (Error("offline"), Mock(stdout='{"schema_version":1,"run_id":"other"}')):
                with patch.object(storage, "call", side_effect=[Mock(stdout=json.dumps(sample_manifest(sample_config(tmp)))),
                                  Mock(stdout='[{"Name":"lifecycle.json"}]'), result]), self.assertRaises(Error):
                    storage.manifest("test-run")

    def test_forced_deletion_upload_contains_no_source_or_secret_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(sample_config(tmp))
            manifest = {"run_id": "test-run", "status": "running", "secret": "never-copy", "compute": {"status": "running"}}
            def upload(*args, **kwargs):
                record = json.loads(Path(args[1]).read_text())
                self.assertNotIn("secret", record)
                self.assertEqual(record["status"], "interrupted")
                self.assertEqual(record["deletion"]["status"], "confirmed")
                self.assertEqual(args[0], "copyto")
            with patch.object(storage, "manifest", return_value=manifest), patch.object(storage, "call", side_effect=upload):
                storage.record_deletion("test-run")


if __name__ == "__main__":
    unittest.main()
