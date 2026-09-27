"""Opt-in webhook delivery, with only synthetic credentials and fake processes."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_core import decode_cloud_file, sample_config, sample_manifest
from cloud_experiments import bootstrap, cli, config, source, worker
from cloud_experiments.common import Error, read_json, write_json

WEBHOOK = "https://discord.com/api/webhooks/123456789/fake-test-token"


class DiscordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = sample_config(self.root)

    def rendered(self, enabled):
        self.config["run"]["ews_discord"] = enabled
        manifest = sample_manifest(self.config)
        payload = bootstrap.render(manifest, {"HCLOUD_WORKER_TOKEN": "fake-cloud-token",
                                             "DISCORD_WEBHOOK_URL": WEBHOOK}, "fake-s3-secret")
        data = json.loads(payload.split("\n", 1)[1])
        files = {entry["path"]: entry for entry in data["write_files"]}
        return manifest, payload, files

    def test_setting_is_opt_in_and_strictly_boolean(self):
        self.assertFalse(self.config["run"]["ews_discord"])
        for value in ("true", 1, None, []):
            with self.subTest(value=value), self.assertRaisesRegex(Error, "boolean"):
                config.run_settings({"ews_discord": value})
        path = self.root / "config.toml"
        with path.open("a") as f:
            f.write("\n[run]\news_discord = true\n")
        self.assertTrue(config.load(path)["run"]["ews_discord"])

    def test_only_selected_webhook_is_loaded_by_systemd_outside_artifacts(self):
        manifest, payload, files = self.rendered(True)
        path = "/opt/cloud-experiments/ews-discord-webhook"
        credential = files[path]
        self.assertEqual(credential["permissions"], "0600")
        self.assertEqual(credential["owner"], "root:root")
        self.assertEqual(decode_cloud_file(credential).decode(), WEBHOOK)
        unit = decode_cloud_file(files["/etc/systemd/system/cloud-experiment.service"]).decode()
        self.assertIn("LoadCredential=ews-discord-webhook:" + path, unit)
        self.assertNotIn(WEBHOOK, unit)
        self.assertNotIn("fake-cloud-token", unit)
        self.assertNotIn("fake-s3-secret", unit)
        self.assertNotIn(WEBHOOK, json.dumps(manifest))
        self.assertNotIn(WEBHOOK, json.dumps(json.loads(payload.split("\n", 1)[1])["runcmd"]))
        self.assertLessEqual(len(payload.encode()), 32768)
        self.assertTrue(source.excluded("home/ews-discord-webhook"))

    def test_disabled_forwarding_retains_root_lifecycle_webhook_only(self):
        _, _, files = self.rendered(False)
        self.assertNotIn("/opt/cloud-experiments/ews-discord-webhook", files)
        unit = decode_cloud_file(files["/etc/systemd/system/cloud-experiment.service"]).decode()
        self.assertNotIn("LoadCredential=", unit)
        secrets = json.loads(decode_cloud_file(files["/opt/cloud-experiments/credentials.json"]))
        self.assertEqual(secrets["DISCORD_WEBHOOK_URL"], WEBHOOK)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_enabled_credential_unit_parses_without_starting_a_service(self):
        _, _, files = self.rendered(True)
        paths = []
        for name, entry in files.items():
            if name.startswith("/etc/systemd/system/"):
                path = self.root / Path(name).name
                content = decode_cloud_file(entry).decode().replace("/usr/bin/tmux", "/usr/bin/true")
                path.write_text(content)
                paths.append(str(path))
        result = subprocess.run(["systemd-analyze", "verify", "--man=no", *paths], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_missing_enabled_webhook_stops_launch_before_provider_mutations(self):
        self.config["run"]["ews_discord"] = True
        manifest = sample_manifest(self.config)
        directory = cli.state(self.config, manifest["run_id"])
        with patch("cloud_experiments.cli.Hetzner") as cloud, patch("cloud_experiments.cli.Storage") as storage, patch("cloud_experiments.cli.worker_secrets", return_value={"HCLOUD_WORKER_TOKEN": "fake"}), patch("cloud_experiments.cli.storage_credentials", return_value="fake"):
            with self.assertRaisesRegex(Error, "requires DISCORD_WEBHOOK_URL"):
                cli.launch(self.config, manifest, directory)
            cloud.return_value.create.assert_not_called()
            storage.return_value.upload.assert_not_called()

    def test_reproduction_keeps_opt_in_but_uses_current_webhook(self):
        original, _, _ = self.rendered(True)
        self.config["run"]["ews_discord"] = False
        reproduced = cli.reproduce_manifest(self.config, original, "reproduction")
        self.assertTrue(reproduced["settings"]["ews_discord"])
        replacement = WEBHOOK + "-rotated"
        payload = bootstrap.render(reproduced, {"HCLOUD_WORKER_TOKEN": "fake", "DISCORD_WEBHOOK_URL": replacement}, "fake")
        credential = next(entry for entry in json.loads(payload.split("\n", 1)[1])["write_files"] if entry["path"].endswith("/ews-discord-webhook"))
        self.assertEqual(decode_cloud_file(credential).decode(), replacement)
        self.assertNotIn(replacement, json.dumps(reproduced))
        del original["settings"]["ews_discord"]  # old manifests keep the original no-forwarding behavior
        self.assertFalse(cli.reproduce_manifest(self.config, original, "legacy")["settings"]["ews_discord"])

    def prepare_execution(self):
        work = self.root / "work"
        write_json(work / "runtime/command.json", ["ews", "run", "study.yml", "--output", str(work / "output")])
        credentials = self.root / "credentials" / "cloud-experiment.service"
        credentials.mkdir(parents=True)
        (credentials / "ews-discord-webhook").write_text(WEBHOOK)
        return work, credentials

    def test_execute_forwards_only_webhook_in_environment_not_arguments(self):
        work, credentials = self.prepare_execution()
        inherited = {"CREDENTIALS_DIRECTORY": str(credentials), "HCLOUD_WORKER_TOKEN": "fake-cloud-token", "RCLONE_SECRET_ACCESS_KEY": "fake-s3-secret", "UNRELATED": "never-copy"}
        with patch.dict(os.environ, inherited, clear=True), patch("cloud_experiments.worker.subprocess.run", return_value=subprocess.CompletedProcess([], 7)) as run:
            worker.execute(work)
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["EWS_DISCORD_WEBHOOK_URL"], WEBHOOK)
        for key in inherited:
            self.assertNotIn(key, env)
        self.assertNotIn(WEBHOOK, str(argv))
        self.assertEqual(read_json(work / "runtime/exit.json"), {"exit_code": 7})
        for path in work.rglob("*"):
            if path.is_file():
                self.assertNotIn(WEBHOOK.encode(), path.read_bytes())

    def test_disabled_execute_does_not_inherit_laptop_webhook(self):
        work, _ = self.prepare_execution()
        with patch.dict(os.environ, {"EWS_DISCORD_WEBHOOK_URL": WEBHOOK}, clear=True), patch("cloud_experiments.worker.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            worker.execute(work)
        self.assertNotIn("EWS_DISCORD_WEBHOOK_URL", run.call_args.kwargs["env"])

    def test_broken_runtime_credential_never_starts_research_process(self):
        work, credentials = self.prepare_execution()
        credential = credentials / "ews-discord-webhook"
        for value in (None, "", WEBHOOK + "\n"):
            if value is None:
                credential.unlink()
            else:
                credential.write_text(value)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credentials)}, clear=True), patch("cloud_experiments.worker.subprocess.run") as run:
                with self.assertRaisesRegex(Error, "missing or invalid") as error:
                    worker.execute(work)
                self.assertNotIn(WEBHOOK, str(error.exception))
                run.assert_not_called()

    def test_lifecycle_delivery_failure_does_not_raise_or_expose_url(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            def fail(*args, **kwargs):
                raise OSError(WEBHOOK)
            worker.notify(sample_manifest(self.config), {"DISCORD_WEBHOOK_URL": WEBHOOK}, api=fail)
        self.assertNotIn(WEBHOOK, output.getvalue())


if __name__ == "__main__":
    unittest.main()
