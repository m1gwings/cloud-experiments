"""Offline tests for failsafe protection and verified post-SSH installation."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import failsafe
from test_core import sample_config, sample_manifest, server
from cloud_experiments import bootstrap, cli, runtime
from cloud_experiments.common import Error


class MinimalBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = sample_manifest(sample_config(self.root), "a" * 63)
        self.manifest.update(study_id="s-" + "a" * 32,
                             deadline_at="2026-12-31T23:59:59+00:00")
        self.manifest["settings"].update(ews_discord=True, timezone="Europe/Rome")

    def test_maximum_representative_data_has_headroom_and_only_delete_secret(self):
        payload = bootstrap.render(self.manifest, "synthetic-token-" + "z" * 112)
        self.assertLessEqual(len(payload.encode()), 24 * 1024)
        self.assertGreaterEqual(32 * 1024 - len(payload.encode()), 8 * 1024)
        paths = {item["path"] for item in json.loads(payload.split("\n", 1)[1])["write_files"]}
        self.assertEqual(paths, {"/opt/cloud-experiments/failsafe.py",
                                 "/opt/cloud-experiments/failsafe-identity.json",
                                 "/opt/cloud-experiments/failsafe-token",
                                 "/etc/systemd/system/cloud-deadline.service",
                                 "/etc/systemd/system/cloud-deadline.timer"})
        for value in ("rclone", "Discord", "cloud-supervisor", "code.zip"):
            self.assertNotIn(value, payload)

    def test_ordinary_worker_growth_does_not_change_bootstrap(self):
        before = bootstrap.render(self.manifest, "synthetic-token")
        with tempfile.TemporaryDirectory() as root:
            copy = Path(root)
            (copy / "templates").mkdir()
            (copy / "failsafe.py").write_bytes((bootstrap.ROOT / "failsafe.py").read_bytes())
            for name in ("cloud-deadline.service", "cloud-deadline.timer"):
                shutil.copyfile(bootstrap.ROOT / "templates" / name, copy / "templates" / name)
            (copy / "lib/cloud_experiments").mkdir(parents=True)
            (copy / "lib/cloud_experiments/worker.py").write_text("# new worker features\n" * 100000)
            with patch.object(bootstrap, "ROOT", copy):
                after = bootstrap.render(self.manifest, "synthetic-token")
        self.assertEqual(before, after)

    def test_failsafe_checks_metadata_api_identity_and_labels(self):
        identity = {"run_id": "a-" + "a" * 32 + "-" + "b" * 20,
                    "study_id": "s-" + "a" * 32, "name": "s-" + "a" * 32}
        owned = server(identity["run_id"], name=identity["name"],
                       labels={"managed-by": "cloud-experiments", "run-id": identity["run_id"],
                               "study-id": identity["study_id"]})
        api = Mock(side_effect=[{"server": owned}, {}])
        failsafe.checked_delete(identity, "fake-token", metadata=lambda: 123, api=api)
        self.assertEqual(api.call_args.kwargs["method"], "DELETE")
        for bad in (dict(owned, id=999), dict(owned, name="other"),
                    dict(owned, labels={"managed-by": "cloud-experiments", "run-id": identity["run_id"]})):
            api = Mock(return_value={"server": bad})
            with self.assertRaises(ValueError):
                failsafe.checked_delete(identity, "fake-token", metadata=lambda: 123, api=api)
            api.assert_called_once()
        api = Mock(return_value=None)
        failsafe.checked_delete(identity, "fake-token", metadata=lambda: 123, api=api)
        api.assert_called_once()

    def test_preflight_bootstrap_error_causes_no_provider_or_storage_mutation(self):
        c = sample_config(self.root)
        m = sample_manifest(c)
        directory = cli.state(c, m["run_id"])
        (directory / "out").mkdir()
        with patch("cloud_experiments.cli.Hetzner") as cloud, patch("cloud_experiments.cli.Storage") as storage, \
             patch("cloud_experiments.cli.worker_secrets", return_value={"HCLOUD_WORKER_TOKEN": "fake"}), \
             patch("cloud_experiments.cli.storage_credentials", return_value="fake"), \
             patch("cloud_experiments.cli.render", side_effect=Error("synthetic bootstrap error")):
            with self.assertRaisesRegex(Error, "synthetic bootstrap error"):
                cli.launch(c, m, directory)
            cloud.return_value.create.assert_not_called()
            storage.return_value.upload.assert_not_called()

    def test_local_only_preflight_failure_removes_provisional_state(self):
        c = sample_config(self.root)
        directory = self.root / "state/provisional"
        def fail(args, config, provisional):
            directory.mkdir(parents=True)
            provisional.append(directory)
            raise Error("synthetic local preflight failure")
        with patch.object(cli, "_run_command", side_effect=fail):
            with self.assertRaises(Error):
                cli.run_command(None, c)
        self.assertFalse(directory.exists())
        def remote_started(args, config, provisional):
            directory.mkdir(parents=True)
            (directory / "remote-started").touch()
            provisional.append(directory)
            raise Error("synthetic remote failure")
        with patch.object(cli, "_run_command", side_effect=remote_started):
            with self.assertRaises(Error):
                cli.run_command(None, c)
        self.assertTrue(directory.exists())


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "vm"
        self.units = self.root / "units"
        self.bin = self.root / "bin"
        for path in (self.base / "incoming", self.units, self.bin):
            path.mkdir(parents=True)
        script = self.bin / "systemctl"
        script.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n")
        script.chmod(0o755)
        self.log = self.root / "systemctl.log"
        m = sample_manifest(sample_config(self.root))
        self.manifest = m
        self.archive = self.base / "incoming/runtime.tar.gz"
        self.digest = runtime.build(self.archive, m, {"HCLOUD_WORKER_TOKEN": "fake-token",
                                                       "DISCORD_WEBHOOK_URL": "fake-webhook"}, "fake-rclone")

    def run_installer(self, digest=None):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], SYSTEMCTL_LOG=str(self.log))
        return subprocess.run([sys.executable, "-c", runtime.INSTALLER, digest or self.digest,
                               str(self.base), str(self.units)], env=env, capture_output=True)

    def test_verified_archive_activates_without_disarming_deadline(self):
        repeat = self.root / "repeat.tar.gz"
        self.assertEqual(runtime.build(repeat, self.manifest,
                                       {"HCLOUD_WORKER_TOKEN": "fake-token",
                                        "DISCORD_WEBHOOK_URL": "fake-webhook"}, "fake-rclone"), self.digest)
        self.assertEqual(repeat.read_bytes(), self.archive.read_bytes())
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.base / "runtime-ready").read_text().strip(), self.digest)
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.base / "credentials.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.base / "rclone.conf").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.log.read_text().splitlines(),
                         ["is-enabled --quiet cloud-deadline.timer",
                          "is-active --quiet cloud-deadline.timer", "daemon-reload",
                          "is-enabled --quiet cloud-deadline.timer",
                          "is-active --quiet cloud-deadline.timer"])
        self.assertTrue((self.units / "cloud-supervisor.service").exists())

    def test_corrupt_and_truncated_archive_never_activates(self):
        original = self.archive.read_bytes()
        for bad in (original[:len(original) // 2], original[:-1] + b"x"):
            with self.subTest(length=len(bad)):
                self.archive.write_bytes(bad)
                self.assertNotEqual(self.run_installer().returncode, 0)
                self.assertFalse((self.base / "runtime-ready").exists())
                self.assertFalse((self.units / "cloud-supervisor.service").exists())

    def test_full_deadline_path_and_broken_runtime_fallback(self):
        (self.base / "failsafe-identity.json").write_text(json.dumps({"run_id": "test-run", "name": "test-run"}))
        (self.base / "failsafe-token").write_text("fake-token")
        with patch.object(failsafe, "BASE", self.base), patch.object(failsafe, "checked_delete") as delete:
            failsafe.main()  # laptop disappeared before runtime upload
            delete.assert_called_once()
            delete.reset_mock()
            (self.base / "runtime-ready").touch()
            with patch.object(failsafe.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as full:
                failsafe.main()
            full.assert_called_once()
            delete.assert_not_called()
            with patch.object(failsafe.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                failsafe.main()
            delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
