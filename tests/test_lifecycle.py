"""Entirely offline lifecycle tests. All provider/worker effects are fakes."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config, sample_manifest, server
from cloud_experiments import cli, source, worker
from cloud_experiments.common import Error, read_json, sha256, write_json


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.work = self.root / "worker", self.root / "work"
        self.base.mkdir()
        self.work.mkdir()
        self.c = sample_config(self.root)
        self.m = sample_manifest(self.c)
        self.m["status"] = "running"
        self.m["started_at"] = self.m["created_at"]
        write_json(self.base / "manifest.json", self.m)
        write_json(self.base / "credentials.json", {"HCLOUD_WORKER_TOKEN": "fake-token"})
        self.commands = []

        def run(argv, **kwargs):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        self.w = worker.Worker(self.base, self.work, run)

    def finalize(self, status, **kwargs):
        write_json(self.base / "reason.json", {"status": status, "exit_code": kwargs.get("exit_code")})
        with patch.object(self.w, "upload", side_effect=kwargs.get("upload_error")) as upload, patch("cloud_experiments.worker.notify") as notify, contextlib.redirect_stdout(io.StringIO()):
            self.w.finalize()
            return upload, notify

    def test_success_failure_cancel_timeout_all_delete_after_upload(self):
        for status in ("completed", "failed", "cancelled", "timeout", "setup_failed"):
            with self.subTest(status=status):
                self.commands.clear()
                self.finalize(status, exit_code=0 if status == "completed" else None)
                result = read_json(self.base / "manifest.json")
                self.assertEqual(result["status"], status)
                self.assertGreaterEqual(result["elapsed_seconds"], 0)
                self.assertIn(["systemctl", "stop", "cloud-experiment.service"], self.commands)
                self.assertEqual(self.commands[-1], ["systemctl", "start", "--no-block", "cloud-delete.service"])

    def test_upload_and_notification_failure_do_not_prevent_deletion(self):
        self.finalize("failed", upload_error=Error("fake upload failure"))
        self.assertEqual(read_json(self.base / "manifest.json")["upload"]["status"], "failed")
        self.assertEqual(self.commands[-1][-1], "cloud-delete.service")
        m = read_json(self.base / "manifest.json")
        with contextlib.redirect_stdout(io.StringIO()):
            worker.notify(m, {"DISCORD_WEBHOOK_URL": "https://example.invalid/fake"}, api=Mock(side_effect=Error("unavailable")))

    def test_idempotent_finalization_and_retention_still_times_out(self):
        self.m["keep_on_setup_failure"] = True
        write_json(self.base / "manifest.json", self.m)
        self.finalize("setup_failed")
        self.assertFalse(any("cloud-delete.service" in x for x in self.commands))
        upload, _ = self.finalize("setup_failed")
        upload.assert_not_called()
        self.w.request("timeout")
        self.assertEqual(read_json(self.base / "reason.json")["status"], "timeout")
        with patch.object(self.w, "upload"), patch("cloud_experiments.worker.notify"):
            self.w.finalize()
        self.assertEqual(self.commands[-1][-1], "cloud-delete.service")
        self.assertEqual(self.w.manifest()["status"], "timeout")

    def test_timeout_does_not_relabel_completed_run_during_delete_retries(self):
        self.finalize("completed", exit_code=0)
        self.w.request("timeout")
        self.assertEqual(read_json(self.base / "reason.json")["status"], "completed")

    def test_new_modified_deleted_files_and_exclusions_are_recorded(self):
        (self.work / "source").mkdir()
        (self.work / "source/input").write_text("unchanged")
        (self.work / "source/changed").write_text("before")
        (self.work / "source/deleted").write_text("remove me")
        baseline, _ = source.inventory(self.work)
        write_json(self.base / "baseline.json", baseline)
        (self.work / "source/changed").write_text("after")
        (self.work / "source/deleted").unlink()
        (self.work / "unexpected-output").write_text("result")
        (self.work / ".venv").mkdir()
        (self.work / ".venv/dependency").write_text("skip")
        (self.work / "secret-link").symlink_to(self.base / "credentials.json")
        self.w.collect(self.m)
        idx = read_json(self.base / "out/artifacts/index.json")
        self.assertEqual(set(idx["changed"]), {"source/changed", "unexpected-output"})
        self.assertEqual(idx["deleted"], ["source/deleted"])
        self.assertIn(".venv", idx["excluded"])
        self.assertIn("secret-link", idx["excluded"])
        self.assertFalse((self.base / "out/artifacts/secret-link").exists())

    def test_self_delete_requires_metadata_and_matching_labels(self):
        api = Mock(return_value={"server": server()})
        worker.delete_self(self.m, {"HCLOUD_WORKER_TOKEN": "fake-token"}, identity=lambda: 123, api=api)
        self.assertEqual(api.call_args.kwargs["method"], "DELETE")
        for bad in (server(labels={}), server(id=456), server(name="other")):
            api = Mock(return_value={"server": bad})
            with self.assertRaises(Error):
                worker.delete_self(self.m, {"HCLOUD_WORKER_TOKEN": "fake-token"}, identity=lambda: 123, api=api)
            self.assertEqual(api.call_count, 1)
        self.m["server_id"] = 456
        api.reset_mock()
        with self.assertRaises(Error):
            worker.delete_self(self.m, {"HCLOUD_WORKER_TOKEN": "fake-token"}, identity=lambda: 123, api=api)
        api.assert_not_called()

    def test_already_deleted_server_is_success(self):
        api = Mock(return_value=None)
        worker.delete_self(self.m, {"HCLOUD_WORKER_TOKEN": "fake-token"}, identity=lambda: 123, api=api)
        self.assertEqual(api.call_count, 1)

    def test_upload_verifies_artifacts_before_publishing_manifest(self):
        self.w.save(self.m)
        calls = []
        timeouts = []

        def rclone(*args, **kwargs):
            calls.append(args)
            timeouts.append(kwargs["timeout"])
            return subprocess.CompletedProcess([], 0, json.dumps(self.w.manifest()).encode(), b"")

        with patch.object(self.w, "rclone", side_effect=rclone):
            self.w.upload(self.m, final=True)
        self.assertEqual([x[0] for x in calls], ["copy", "check", "copyto", "cat"])
        self.assertEqual(timeouts, [None] * 4)
        self.assertEqual(self.m["upload"]["status"], "verified")

    def test_execute_preserves_tty_and_exit_status_without_secret_environment(self):
        (self.work / "runtime").mkdir()
        argv = ["python", "script.py", "config with spaces;$(bad).yml"]
        write_json(self.work / "runtime/command.json", argv)
        with patch("cloud_experiments.worker.subprocess.run", return_value=subprocess.CompletedProcess([], 7)) as run:
            worker.execute(self.work)
        import shlex
        command = run.call_args.args[0]
        self.assertEqual(shlex.split(command[command.index("--command") + 1]), argv)
        self.assertNotIn("HCLOUD_WORKER_TOKEN", run.call_args.kwargs["env"])
        self.assertEqual(read_json(self.work / "runtime/exit.json")["exit_code"], 7)

    def test_worker_setup_pins_ews_then_launches_and_supervises_completion(self):
        import tarfile
        incoming = self.base / "incoming"
        incoming.mkdir()
        data = b"seed: 123\n"
        with tarfile.open(incoming / "source.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("config.yml")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        self.m["source"]["sha256"] = sha256(incoming / "source.tar.gz")
        write_json(self.base / "manifest.json", self.m)
        write_json(incoming / "index.json", {})

        def run(argv, **kwargs):
            self.commands.append(argv)
            output = b""
            if argv[-2:] == ["rev-parse", "HEAD"]:
                output = (self.m["ews"]["commit"] + "\n").encode()
            if argv == ["systemctl", "start", "cloud-experiment.service"]:
                write_json(self.work / "runtime/exit.json", {"exit_code": 0})
            return subprocess.CompletedProcess(argv, 0, output, b"")

        self.w.run = run
        with patch("cloud_experiments.worker.own_server_id", return_value=123), patch.object(self.w, "upload") as upload, patch("cloud_experiments.worker.notify"):
            self.w.supervise()
        self.assertTrue((self.base / "started").exists())
        self.assertEqual(read_json(self.base / "reason.json")["status"], "completed")
        self.assertEqual(self.w.manifest()["server_id"], 123)
        self.assertTrue(any("fetch" in argv and self.m["ews"]["commit"] in argv for argv in self.commands))
        self.assertEqual(read_json(self.work / "runtime/command.json"), ["ews", "run", "config.yml", "--output", str(self.work / "output")])
        upload.assert_called_once()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = sample_config(self.root)
        self.m = sample_manifest(self.c)
        self.directory = cli.state(self.c, self.m["run_id"])
        (self.directory / "out").mkdir()
        self.patches = [patch("cloud_experiments.cli.Hetzner"), patch("cloud_experiments.cli.Storage"),
                        patch("cloud_experiments.cli.ssh_for"), patch("cloud_experiments.cli.worker_secrets", return_value={"HCLOUD_WORKER_TOKEN": "fake"}),
                        patch("cloud_experiments.cli.storage_credentials", return_value="fake")]
        mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.cloud, self.storage, self.ssh = mocks[0].return_value, mocks[1].return_value, mocks[2].return_value
        self.cloud.create.return_value = server()
        self.cloud.find.return_value = server()
        self.ssh.call.return_value = subprocess.CompletedProcess([], 0, b"", b"")

    def test_launch_transfers_snapshot_and_starts_supervisor(self):
        events = []
        self.cloud.create.side_effect = lambda *args: (events.append("create"), server())[1]
        self.ssh.wait.side_effect = lambda: events.append("armed")
        self.ssh.upload.side_effect = lambda paths: events.append(
            "runtime" if paths[0].name == "runtime.tar.gz" else "source")
        self.ssh.call.side_effect = lambda argv, **kwargs: (
            events.append("install" if argv[:2] == ["/usr/bin/python3", "-c"]
                          else "supervisor" if argv == ["systemctl", "start", "cloud-supervisor.service"]
                          else "poll"), subprocess.CompletedProcess(argv, 0, b"", b""))[1]
        with contextlib.redirect_stdout(io.StringIO()):
            cli.launch(self.c, self.m, self.directory)
        self.storage.upload.assert_called_once()
        self.assertEqual(self.ssh.upload.call_count, 2)
        self.assertEqual(self.ssh.upload.call_args_list[0].args[0][0].name, "runtime.tar.gz")
        self.ssh.wait.assert_called_once()
        self.assertTrue(any(call.args[0][:3] == ["/usr/bin/python3", "-c", cli.runtime.INSTALLER]
                            for call in self.ssh.call.call_args_list))
        self.ssh.call.assert_any_call(["systemctl", "start", "cloud-supervisor.service"])
        self.assertLess(events.index("create"), events.index("armed"))
        self.assertLess(events.index("armed"), events.index("runtime"))
        self.assertLess(events.index("runtime"), events.index("install"))
        self.assertLess(events.index("install"), events.index("source"))
        self.assertLess(events.index("source"), events.index("supervisor"))
        self.cloud.delete.assert_not_called()

    def test_ambiguous_create_recovers_by_labels_and_deletes_if_ssh_fails(self):
        self.cloud.create.side_effect = Error("ambiguous create response")
        self.ssh.call.side_effect = Error("SSH unavailable")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
            cli.launch(self.c, self.m, self.directory)
        self.cloud.find.assert_called_once_with("test-run")
        self.cloud.delete.assert_called_once_with(server(), "test-run")

    def test_preflight_upload_failure_never_creates_compute(self):
        self.storage.upload.side_effect = Error("storage unavailable")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
            cli.launch(self.c, self.m, self.directory)
        self.cloud.create.assert_not_called()

    def test_failed_transfer_requests_worker_finalization(self):
        self.ssh.upload.side_effect = [None, Error("fake transfer failure")]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
            cli.launch(self.c, self.m, self.directory)
        self.ssh.call.assert_called_with(["/usr/bin/python3", "/opt/cloud-experiments/entry.py", "setup_failed"], timeout=30)
        self.cloud.delete.assert_not_called()

    def test_runtime_upload_failure_deletes_without_invoking_missing_worker(self):
        self.ssh.upload.side_effect = Error("fake transfer failure")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
            cli.launch(self.c, self.m, self.directory)
        self.cloud.delete.assert_called_once_with(server(), "test-run")
        self.assertFalse(any("entry.py" in str(call) for call in self.ssh.call.call_args_list))

    def test_forced_cancellation_requires_confirmation_and_checks_identity(self):
        args = type("Args", (), {"run_id": "test-run", "force_delete": True, "yes": False})()
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=False), self.assertRaises(Error):
            cli.cancel(args, self.c)
        self.cloud.delete.assert_not_called()
        args.yes = True
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cancel(args, self.c)
        self.cloud.delete.assert_called_once_with(server(), "test-run")


if __name__ == "__main__":
    unittest.main()
