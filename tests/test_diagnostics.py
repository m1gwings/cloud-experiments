"""Offline failure-capsule behavior with synthetic credentials and storage."""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_core import sample_config, sample_manifest
from cloud_experiments import cli, diagnostics, worker
from cloud_experiments.common import Error, read_json, write_json
from cloud_experiments.providers import Storage


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = sample_config(self.root)
        self.base, self.work = self.root / "worker", self.root / "work"
        self.base.mkdir()
        self.work.mkdir()
        self.manifest = sample_manifest(self.config)
        self.manifest.update(status="finalizing", compute={"status": "completed", "exit_code": 0},
                             archive={"status": "syncing"}, deletion={"status": "pending"},
                             last_recovery={"committed_at": "2026-09-28T14:20:59+00:00"})
        write_json(self.base / "manifest.json", self.manifest)
        write_json(self.base / "credentials.json", {"HCLOUD_WORKER_TOKEN": "fake-hcloud-secret",
                                                   "DISCORD_WEBHOOK_URL": "https://discord.invalid/fake-webhook"})
        (self.base / "rclone.conf").write_text("[test]\nsecret_access_key = fake-rclone-secret\n")
        self.objects = {}
        self.actions = []

        def run(argv, **kwargs):
            self.actions.append((argv, kwargs.get("timeout")))
            if argv[:2] == ["systemctl", "show"]:
                return subprocess.CompletedProcess(argv, 0, b"Result=exit-code\nExecMainStatus=1\n", b"")
            if argv[0] == "journalctl":
                self.assertEqual(kwargs["timeout"], 8)
                return subprocess.CompletedProcess(argv, 0, self.journal.encode(), b"")
            if argv[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        self.journal = "finalizer: verifying blobs\n"
        self.w = worker.Worker(self.base, self.work, run)

        def rclone(*args, **kwargs):
            self.actions.append((args, kwargs["timeout"]))
            operation = args[0]
            if operation == "copyto":
                self.objects[args[2]] = Path(args[1]).read_bytes()
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if operation == "check":
                self.assertEqual(self.objects[args[2] + "/journal.log"], Path(args[1], "journal.log").read_bytes())
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if operation == "cat":
                return subprocess.CompletedProcess(args, 0, self.objects[args[1]], b"")
            raise AssertionError(args)

        self.w.rclone = rclone

    def capsules(self):
        return sorted(self.base.glob("diagnostics/*/failure.json"))

    def test_failure_capsule_is_append_only_structured_and_does_not_touch_recovery(self):
        self.w.stage("finalizer", "recovery.verify_blobs")
        with patch.dict(os.environ, {"ARBITRARY_SECRET": "fake-arbitrary-secret"}), \
             patch("cloud_experiments.worker.notify_failure") as alert:
            self.journal = ("HCLOUD_WORKER_TOKEN=fake-hcloud-secret\n"
                            "RCLONE_SECRET_ACCESS_KEY=fake-rclone-secret\n"
                            "webhook https://discord.invalid/fake-webhook\n"
                            "worker fake-arbitrary-secret\n")
            for _ in range(2):
                self.assertTrue(self.w.report_failure(self.manifest, component="finalizer",
                              exc=Error("Verification failed: fake-arbitrary-secret"),
                              unit="cloud-finalize.service"))
        paths = self.capsules()
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0].parent, paths[1].parent)
        self.assertTrue(all((path.parent / "journal.log").stat().st_mode & 0o777 == 0o600 for path in paths))
        record = read_json(paths[-1])
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["stage"], "recovery.verify_blobs")
        self.assertEqual(record["systemd"]["result"], "caught-exception")
        self.assertEqual(record["compute"]["status"], "completed")
        self.assertEqual(record["last_recovery"]["committed_at"], "2026-09-28T14:20:59+00:00")
        self.assertTrue(all("/diagnostics/" in path or path.endswith("/lifecycle.json") for path in self.objects))
        self.assertEqual(self.w.manifest(), self.manifest)
        for path in paths:
            contents = path.read_text() + (path.parent / "journal.log").read_text()
            for secret in ("fake-hcloud-secret", "fake-rclone-secret", "fake-webhook", "fake-arbitrary-secret"):
                self.assertNotIn(secret, contents)
            self.assertNotIn("ARBITRARY_SECRET", contents)
        self.assertEqual(alert.call_count, 2)

    def test_journal_is_bounded_and_traceback_is_useful(self):
        self.journal = "x" * 10000 + "\n" + ("recent line" + "y" * 2040 + "\n") * 400
        try:
            raise Error("synthetic failure")
        except Error as exc:
            self.assertTrue(self.w.report_failure(self.manifest, component="finalizer", exc=exc))
        journal = (self.capsules()[0].parent / "journal.log").read_bytes()
        self.assertLessEqual(len(journal), diagnostics.MAX_JOURNAL_BYTES)
        self.assertLessEqual(len(journal.splitlines()), diagnostics.MAX_JOURNAL_LINES)
        self.assertIn(b"recent line", journal)
        self.assertNotIn(b"x" * 10000, journal)
        self.assertTrue(read_json(self.capsules()[0])["traceback"])
        self.assertTrue(all(timeout is not None and timeout <= 10 for args, timeout in self.actions
                            if isinstance(args, tuple) and args[0] in {"copyto", "check", "cat"}))

    def test_discord_failure_message_is_concise_and_reports_upload_state(self):
        record = diagnostics.failure(self.manifest, component="finalizer", stage="recovery.verify_blobs",
                                     error_kind="Error", message="Verification failed.")
        sent = []
        def api(url, **kwargs):
            sent.append((url, kwargs["body"]["content"]))
        credentials = read_json(self.base / "credentials.json")
        worker.notify_failure(record, True, credentials, api=api)
        worker.notify_failure(record, False, credentials, api=api)
        self.assertEqual(len(sent), 2)
        self.assertIn("diagnostics=persisted", sent[0][1])
        self.assertIn("diagnostics=unavailable", sent[1][1])
        self.assertIn("cloud-diagnose test-run", sent[0][1])
        self.assertNotIn("fake-webhook", sent[0][1])
        self.assertLess(len(sent[0][1]), 500)

    def test_caught_setup_and_finalization_exceptions_are_reported_before_cleanup(self):
        with patch("cloud_experiments.worker.own_server_id", side_effect=Error("setup broke")), \
             patch.object(self.w, "request") as request, \
             patch("cloud_experiments.worker.notify_failure"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.w.supervise()
        self.assertEqual(read_json(self.capsules()[0])["component"], "supervisor")
        self.assertEqual(request.call_args.args[0], "setup_failed")
        self.assertEqual(self.actions[-1][0][0], "cat")

        write_json(self.base / "reason.json", {"status": "completed", "exit_code": 0})
        with patch.object(self.w, "upload", side_effect=Error("archive failed")), \
             patch("cloud_experiments.worker.notify_failure"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.w.finalize()
        final = next(read_json(path) for path in self.capsules() if read_json(path)["component"] == "finalizer")
        self.assertEqual(final["component"], "finalizer")
        self.assertEqual(final["stage"], "archive.collect")
        self.assertEqual(self.actions[-1][0][-1], "cloud-delete.service")

    def test_systemd_capture_precedes_next_unit_and_has_fallback(self):
        self.w.stage("finalizer", "recovery.verify_blobs")
        with patch("cloud_experiments.worker.notify_failure"):
            self.w.diagnose_service_failure("finalizer")
        self.assertEqual(read_json(self.capsules()[0])["stage"], "recovery.verify_blobs")
        self.assertEqual(self.actions[-1][0], ["systemctl", "start", "--no-block", "cloud-delete.service"])
        self.assertLess(next(i for i, (args, _) in enumerate(self.actions) if isinstance(args, tuple) and args[0] == "cat"),
                        len(self.actions) - 1)
        unit = Path("templates/cloud-failure@.service").read_text()
        self.assertIn("OnFailure=cloud-delete.service", unit)
        self.assertIn("TimeoutStartSec=90s", unit)
        self.assertIn("diagnose-failure %i", unit)
        self.assertIn("OnFailure=cloud-failure@finalizer.service", Path("templates/cloud-finalize.service").read_text())
        self.assertIn("OnFailure=cloud-failure@supervisor.service", Path("templates/cloud-supervisor.service").read_text())
        self.assertIn("TimeoutStartSec=180s", Path("templates/cloud-delete.service").read_text())

    def test_upload_failure_still_starts_cleanup_and_is_unavailable_alert(self):
        def fail(*args, **kwargs):
            raise Error("fake storage outage")
        self.w.rclone = fail
        with patch("cloud_experiments.worker.notify_failure") as alert, contextlib.redirect_stdout(io.StringIO()):
            self.w.diagnose_service_failure("finalizer")
        self.assertFalse(alert.call_args.args[1])
        self.assertEqual(self.actions[-1][0][-1], "cloud-delete.service")
        self.assertEqual(len(self.capsules()), 1)  # local copy remains for inspection

    def test_deadline_service_failure_explains_interrupted_finalization(self):
        self.w.stage("finalizer", "recovery.verify_blobs")
        with patch("cloud_experiments.worker.notify_failure"):
            self.w.diagnose_service_failure("deadline")
        record = read_json(self.capsules()[0])
        self.assertEqual(record["component"], "finalizer")
        self.assertEqual(record["error_kind"], "AbsoluteDeadline")
        self.assertEqual(record["systemd"]["unit"], "cloud-deadline.service")
        self.assertEqual(self.actions[-1][0][-1], "cloud-delete.service")

    def test_absolute_deadline_interrupts_finalizer_with_diagnostic(self):
        self.w.stage("finalizer", "recovery.verify_blobs")
        with patch("cloud_experiments.worker.notify_failure"), contextlib.redirect_stdout(io.StringIO()):
            self.w.expire()
        record = read_json(self.capsules()[0])
        self.assertEqual(record["stage"], "recovery.verify_blobs")
        self.assertEqual(record["error_kind"], "AbsoluteDeadline")
        self.assertIn("Absolute deadline", record["message"])
        self.assertEqual(self.w.manifest()["status"], "finalization_failed")
        self.assertEqual(self.actions[-1][0][-1], "cloud-delete.service")

    def test_success_creates_no_capsule_and_deletes_immediately(self):
        write_json(self.base / "reason.json", {"status": "completed", "exit_code": 0})
        with patch.object(self.w, "upload"), patch("cloud_experiments.worker.notify"):
            self.w.finalize()
        self.assertEqual(self.capsules(), [])
        self.assertEqual(self.actions[-1][0][-1], "cloud-delete.service")

    def test_deletion_failure_diagnostic_is_best_effort_and_retryable(self):
        self.manifest["deadline_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        write_json(self.base / "manifest.json", self.manifest)
        with patch("cloud_experiments.worker.delete_self", side_effect=Error("provider down")), \
             patch("cloud_experiments.worker.notify"), patch("cloud_experiments.worker.notify_failure"):
            with self.assertRaises(Error):
                self.w.delete()
            self.assertEqual(len(self.capsules()), 1)
            with self.assertRaises(Error):
                self.w.delete()
        self.assertEqual(len(self.capsules()), 1)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = sample_config(Path(self.temp.name))
        self.manifest = sample_manifest(self.config)
        self.record = diagnostics.failure(self.manifest, component="finalizer", stage="archive.upload",
                                          error_kind="Error", message="Archive upload failed.")
        self.storage = Storage(self.config)
        self.events = ["20260928T120000000001Z-000000000001", "20260928T130000000002Z-000000000002"]

        def call(*args, **kwargs):
            if args[0] == "lsjson" and args[1].endswith("/diagnostics"):
                return subprocess.CompletedProcess(args, 0, json.dumps([{"Path": e + "/failure.json"} for e in self.events]).encode(), b"")
            if args[0] == "lsjson":
                return subprocess.CompletedProcess(args, 0, b'[{"Name":"diagnostics"}]', b"")
            if args[1].endswith("failure.json"):
                return subprocess.CompletedProcess(args, 0, json.dumps(self.record).encode(), b"")
            return subprocess.CompletedProcess(args, 0, b"last useful traceback\n", b"")

        self.storage.call = call

    def test_latest_select_and_cli_journal(self):
        capsule = self.storage.diagnostic(self.manifest["run_id"])
        self.assertEqual(capsule["event"], self.events[-1])
        self.assertEqual(capsule["count"], 2)
        self.assertEqual(self.storage.diagnostic(self.manifest["run_id"], self.events[0])["event"], self.events[0])
        with patch("cloud_experiments.cli.Storage", return_value=self.storage):
            for show_journal in (False, True):
                args = cli.parser("cloud-diagnose").parse_args([self.manifest["run_id"]] + (["--journal"] if show_journal else []))
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    cli.diagnose(args, self.config)
                self.assertIn("archive.upload", output.getvalue())
                self.assertEqual("last useful traceback" in output.getvalue(), show_journal)
            args = cli.parser("cloud-diagnose").parse_args([self.manifest["run_id"], "--list"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.diagnose(args, self.config)
            self.assertEqual(output.getvalue().splitlines(), self.events)

    def test_missing_legacy_capsule_is_clear_and_schema_is_strict(self):
        self.storage.call = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"[]", b"")
        self.assertIsNone(self.storage.diagnostic(self.manifest["run_id"]))
        with patch("cloud_experiments.cli.Storage", return_value=self.storage):
            args = cli.parser("cloud-diagnose").parse_args([self.manifest["run_id"]])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.diagnose(args, self.config)
            self.assertIn("No failure diagnostics", output.getvalue())
        bad = dict(self.record, compute={"status": "completed", "environment": "secret"})
        with self.assertRaises(Error):
            diagnostics.validate(bad)

    def test_partial_upload_is_ignored_and_record_identity_is_checked(self):
        normal = self.storage.call
        def partial(*args, **kwargs):
            if args[0] == "lsjson" and args[1].endswith("/diagnostics"):
                return subprocess.CompletedProcess(args, 0,
                    json.dumps([{"Path": self.events[-1] + "/journal.log"}]).encode(), b"")
            return normal(*args, **kwargs)
        self.storage.call = partial
        self.assertIsNone(self.storage.diagnostic(self.manifest["run_id"]))
        self.storage.call = normal
        self.record["run_id"] = "different-run"
        with self.assertRaisesRegex(Error, "identity mismatch"):
            self.storage.diagnostic(self.manifest["run_id"])


if __name__ == "__main__":
    unittest.main()
