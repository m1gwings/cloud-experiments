import argparse
import contextlib
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_core import decode_cloud_file, sample_config, sample_manifest
from cloud_experiments import bootstrap, cli, ews_contract, source
from cloud_experiments.common import Error, read_json, sha256, write_json

ROOT = Path(__file__).resolve().parents[1]


class InterfaceTests(unittest.TestCase):
    def test_all_command_help_pages_work_without_config_or_credentials(self):
        for path in (ROOT / "bin").glob("cloud-*"):
            result = subprocess.run([str(path), "--help"], capture_output=True)
            self.assertEqual(result.returncode, 0, path.name)
            self.assertIn(path.name.encode(), result.stdout)

    def test_installer_is_idempotent_and_refuses_unrelated_commands(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), patch("shutil.which", return_value="/fake/tool"), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / "install"), run_name="__main__")
            runpy.run_path(str(ROOT / "install"), run_name="__main__")
            target = Path(tmp) / ".local/bin/cloud-run"
            self.assertEqual(target.resolve(), ROOT / "bin/cloud-run")
            target.unlink()
            target.write_text("unrelated command")
            with self.assertRaises(SystemExit):
                runpy.run_path(str(ROOT / "install"), run_name="__main__")
            self.assertEqual(target.read_text(), "unrelated command")

    def test_reproduce_downloads_exact_source_and_ignores_current_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            import tarfile
            root = Path(tmp)
            c = sample_config(root)
            config_file = root / "original.yml"
            config_file.write_text("seed: 999\n")
            archive = root / "original.tar.gz"
            with tarfile.open(archive, "w:gz") as t:
                t.add(config_file, arcname="config.yml")
            original = sample_manifest(c)
            original["source"]["sha256"] = sha256(archive)
            original["config"]["sha256"] = sha256(config_file)
            verified_ews = {**original['ews'], 'cloud_contract': dict(ews_contract.CONTRACT)}
            args = argparse.Namespace(run_id="test-run", name="again")
            with patch("cloud_experiments.cli.Storage") as storage, patch("cloud_experiments.cli.launch") as launch, patch("cloud_experiments.cli.resolve_ews", return_value=verified_ews) as resolve, patch("cloud_experiments.source.git", side_effect=AssertionError("must not consult current scientific git")):
                storage.return_value.manifest.return_value = original
                storage.return_value.file.side_effect = lambda rid, name, dest: shutil.copyfile(archive, dest)
                cli.reproduce(args, c)
                _, manifest, directory = launch.call_args.args
                self.assertTrue(manifest["dirty"])
                resolve.assert_called_once_with(original['ews']['repository'], original['ews']['commit'])
                self.assertEqual(manifest["ews"], verified_ews)
                self.assertEqual(manifest["reproduces_run_id"], "test-run")
                self.assertEqual((directory / "out/config/experiment-config").read_text(), "seed: 999\n")

    def test_incompatible_legacy_reproduction_fails_before_input_transfer_or_compute(self):
        with tempfile.TemporaryDirectory() as directory:
            config = sample_config(directory)
            original = sample_manifest(config)
            with patch('cloud_experiments.cli.Storage') as storage, patch('cloud_experiments.cli.launch') as launch, patch('cloud_experiments.cli.resolve_ews', side_effect=Error('Unsupported EWS cloud recovery contract')) as resolve:
                storage.return_value.manifest.return_value = original
                with self.assertRaisesRegex(Error, 'Unsupported EWS cloud'):
                    cli.reproduce(argparse.Namespace(run_id='test-run'), config)
                resolve.assert_called_once_with(original['ews']['repository'], original['ews']['commit'])
                storage.return_value.file.assert_not_called()
                launch.assert_not_called()

    def test_custom_legacy_command_is_not_silently_given_recovery_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            config = sample_config(directory)
            original = sample_manifest(config)
            original['settings']['command'] = ['python', 'custom.py']
            with patch('cloud_experiments.cli.Storage') as storage, patch('cloud_experiments.cli.launch') as launch, patch('cloud_experiments.cli.resolve_ews') as resolve:
                storage.return_value.manifest.return_value = original
                with self.assertRaisesRegex(Error, 'standard EWS command'):
                    cli.reproduce(argparse.Namespace(run_id='test-run'), config)
                resolve.assert_not_called()
                launch.assert_not_called()

    def test_sync_skips_unchanged_final_runs_but_refreshes_running_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = sample_config(tmp)
            m = sample_manifest(c)
            m["status"] = "completed"
            write_json(Path(c["local"]["results_dir"]) / "test-run/.cloud-pulled.json", {"manifest": m})
            with patch("cloud_experiments.cli.Storage") as storage, patch("cloud_experiments.cli.pull_run") as pull:
                storage.return_value.manifests.return_value = [m]
                cli.results(argparse.Namespace(operation="sync"), c)
                pull.assert_not_called()
                m["status"] = "running"
                cli.results(argparse.Namespace(operation="sync"), c)
                pull.assert_called_once()

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not installed")
    def test_systemd_units_parse_with_local_executable_standin(self):
        # No daemon is contacted and no unit is installed or started. This laptop
        # need not have tmux: substitute /usr/bin/true only in a temporary unit.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = sample_config(root)
            payload = bootstrap.render(sample_manifest(c), {"HCLOUD_WORKER_TOKEN": "fake"}, "fake")
            paths = []
            for item in json.loads(payload.split("\n", 1)[1])["write_files"]:
                if item["path"].startswith("/etc/systemd/system/"):
                    path = root / Path(item["path"]).name
                    content = decode_cloud_file(item).decode().replace("/usr/bin/tmux", "/usr/bin/true")
                    path.write_text(content)
                    paths.append(str(path))
            result = subprocess.run(["systemd-analyze", "verify", "--man=no", *paths], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
