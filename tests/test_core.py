import base64
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from cloud_experiments import bootstrap, cli, config, source
from cloud_experiments.common import Error, command, managed_server, read_json, redact, remote_path, run_id, sha256, valid_run, write_json
from cloud_experiments.providers import Hetzner, SSH, Storage

CONFIG = '''
[hetzner]
context = "experiments"
location = "nbg1"
ssh_key = "test-key"
default_server_type = "cpx32"
[storage]
rclone_remote = "test"
bucket = "test-bucket"
[ews]
repository = "https://github.com/example/ews.git"
default_ref = "main"
'''


def decode_cloud_file(entry):
    """Decode the cloud-init write_files encoding actually supplied by bootstrap."""
    if "encoding" not in entry:
        return entry["content"].encode()
    content = base64.b64decode(entry['content'])
    return gzip.decompress(content) if entry['encoding'] == 'gz+b64' else content


def sample_config(root):
    path = Path(root) / "config.toml"
    path.write_text(CONFIG)
    c = config.load(path)
    c["local"] = {"state_dir": str(Path(root) / "state"), "results_dir": str(Path(root) / "results"),
                  "worker_env": str(Path(root) / "worker.env"), "rclone_config": str(Path(root) / "rclone.conf")}
    return c


def sample_manifest(c, rid="test-run"):
    return cli.new_manifest(c, rid, {"repository": "git@example.com:research/experiment.git", "commit": "a" * 40, "dirty": True},
                            "config.yml", "b" * 64, "c" * 64,
                            {"repository": c["ews"]["repository"], "requested_ref": "main", "commit": "d" * 40})


def server(rid="test-run", **kwargs):
    value = {"id": 123, "name": rid, "labels": {"managed-by": "cloud-experiments", "run-id": rid},
             "public_net": {"ipv4": {"ip": "192.0.2.10"}}, "server_type": {"name": "cpx32"}}
    value.update(kwargs)
    return value


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = sample_config(self.root)

    def test_defaults_and_validation(self):
        self.assertEqual(self.c["run"]["command"], ["ews", "run", "{config}", "--output", "{output}"])
        self.assertEqual(self.c["hetzner"]["max_runtime_hours"], 24)
        for value in (0, -1, float("nan"), float("inf"), 169, True):
            with self.assertRaises(Error):
                config.runtime_hours(value)
        for value in ("file:///tmp/repo", "https://user:password@example.com/repo", "https://example.com/repo?token=x"):
            with self.assertRaises(Error):
                config.repository_url(value)

    def test_credentials_are_narrowly_selected_not_shell_executed(self):
        path = self.root / "worker.env"
        path.write_text('# Fake test-only values\nexport HCLOUD_WORKER_TOKEN="fake-token"\nUNRELATED=never-copy\n')
        path.chmod(0o600)
        self.assertEqual(config.worker_secrets(path), {"HCLOUD_WORKER_TOKEN": "fake-token"})
        path.chmod(0o644)
        with self.assertRaises(Error):
            config.worker_secrets(path)
        rclone = self.root / "rclone.conf"
        rclone.write_text('[test]\ntype=s3\naccess_key_id=fake-access\nsecret_access_key=fake-secret\nendpoint=example.invalid\n[unrelated]\ntoken=never-copy\n')
        rclone.chmod(0o600)
        selected = config.storage_credentials(rclone, "test")
        self.assertIn("fake-secret", selected)
        self.assertNotIn("never-copy", selected)

    def test_ids_and_paths(self):
        ids = {run_id("Test / shell; $(stuff) " * 10) for _ in range(100)}
        self.assertEqual(len(ids), 100)
        for rid in ids:
            valid_run(rid)
        self.assertEqual(remote_path(self.c["storage"], "test-run"), "test:test-bucket/runs/test-run")
        for value in ("../escape", "x/y", "x;rm", "-x", "x" * 64):
            with self.assertRaises(Error):
                valid_run(value)

    def test_manifest_reproduction_does_not_resolve_new_refs(self):
        original = sample_manifest(self.c)
        self.c["run"]["command"] = ["different"]
        self.c["ews"]["default_ref"] = "new-main"
        m = cli.reproduce_manifest(self.c, original, "new-run")
        for field in ("experiment", "source", "config", "ews", "settings", "machine", "location", "max_runtime_hours"):
            self.assertEqual(m[field], original[field])
        self.assertTrue(m["dirty"])
        self.assertEqual(m["reproduces_run_id"], "test-run")
        self.assertIsNone(m["server_id"])
        self.assertEqual(m["status"], "provisioning")

    def test_bootstrap_arms_absolute_deadline_before_setup(self):
        payload = bootstrap.render(sample_manifest(self.c), "FAKE-BOOT-TOKEN")
        self.assertLessEqual(len(payload.encode()), 24 * 1024)
        data = json.loads(payload.split("\n", 1)[1])
        files = {x["path"]: x for x in data["write_files"]}
        self.assertEqual(files["/opt/cloud-experiments/failsafe-token"]["permissions"], "0600")
        self.assertNotIn("/opt/cloud-experiments/rclone.conf", files)
        self.assertNotIn("/opt/cloud-experiments/credentials.json", files)
        self.assertNotIn("/opt/cloud-experiments/code.zip", files)
        self.assertNotIn("FAKE-BOOT-TOKEN", payload)
        self.assertNotIn("apt-get", json.dumps(data["runcmd"]))
        self.assertIn("cloud-deadline.timer", json.dumps(data["runcmd"]))
        self.assertNotIn("cloud-reap.timer", json.dumps(data))

    def test_doctor_is_offline_and_never_opens_credentials(self):
        with patch("cloud_experiments.cli.shutil.which", return_value="/fake/tool"), patch("builtins.open", side_effect=AssertionError("unexpected read")), contextlib.redirect_stdout(io.StringIO()):
            cli.doctor(self.c)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Offline Tests")
        self.git("remote", "add", "origin", "git@example.com:research/experiments.git")
        (self.root / "config.yml").write_text("seed: 1\n")
        (self.root / "main.py").write_text("print('original')\n")
        (self.root / ".gitignore").write_text("ignored/\n.env\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True).stdout.decode().strip()

    def test_clean_and_exact_dirty_snapshots(self):
        dest = Path(self.temp.name) / "out/source.tar.gz"
        provenance, _, _ = source.snapshot(self.root, "config.yml", dest)
        self.assertFalse(provenance["dirty"])
        (self.root / "main.py").write_text("print('changed')\n")
        (self.root / "new.txt").write_text("untracked input")
        with self.assertRaisesRegex(Error, "dirty"):
            source.snapshot(self.root, "config.yml", dest)
        provenance, _, index = source.snapshot(self.root, "config.yml", dest, True)
        self.assertTrue(provenance["dirty"])
        out = Path(self.temp.name) / "extract"
        source.extract_snapshot(dest, out, sha256(dest))
        self.assertEqual((out / "main.py").read_text(), "print('changed')\n")
        self.assertIn("new.txt", index)
        self.assertFalse((out / ".git").exists())

    def test_deleted_and_ignored_files(self):
        (self.root / "main.py").unlink()
        (self.root / "ignored").mkdir()
        (self.root / "ignored/old-results").write_text("not input")
        dest = Path(self.temp.name) / "source.tar.gz"
        _, _, index = source.snapshot(self.root, "config.yml", dest, True)
        self.assertNotIn("main.py", index)
        self.assertNotIn("ignored/old-results", index)

    def test_secret_file_rejected_before_read(self):
        (self.root / "private.secret").write_text("FAKE-DO-NOT-READ")
        with patch("cloud_experiments.source.shutil.copyfile", side_effect=AssertionError("should reject before copying this fixture")):
            # Force this file to be first so no other snapshot data is copied.
            with patch("cloud_experiments.source.command", return_value=subprocess.CompletedProcess([], 0, b"private.secret\0")):
                with patch("cloud_experiments.source.git_info", return_value=(self.root, {}, "dirty")):
                    with self.assertRaisesRegex(Error, "secret-like"):
                        source.snapshot(self.root, "config.yml", Path(self.temp.name) / "x.tar.gz", True)

    def test_symlinks_and_malicious_archives_rejected(self):
        (self.root / "link").symlink_to("main.py")
        with self.assertRaisesRegex(Error, "symlinks"):
            source.snapshot(self.root, "config.yml", Path(self.temp.name) / "x.tar.gz", True)
        for name in ("../escape", "/absolute", ".env"):
            archive = Path(self.temp.name) / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as t:
                member = tarfile.TarInfo(name)
                member.size = 1
                t.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(Error):
                source.extract_snapshot(archive, Path(self.temp.name) / "bad-extract")

    def test_ref_resolution_peels_tags_and_verifies_exact_sha(self):
        commit = "a" * 40
        with patch("cloud_experiments.source.git", side_effect=["", commit]) as git, patch("cloud_experiments.source.command") as cmd:
            self.assertEqual(source.resolve_ref("https://example.com/repo.git", "v1"), commit)
            self.assertIn("FETCH_HEAD^{commit}", git.call_args.args)
            self.assertEqual(cmd.call_args.args[0][-1], "v1")
        with patch("cloud_experiments.source.git", side_effect=["", "b" * 40]), patch("cloud_experiments.source.command"):
            with self.assertRaises(Error):
                source.resolve_ref("https://example.com/repo.git", commit)

    def test_resolve_real_local_git_branch_annotated_tag_and_commit(self):
        old = self.git("rev-parse", "HEAD")
        self.git("tag", "-a", "v1", "-m", "annotated tag fixture")
        (self.root / "main.py").write_text("print('new commit')")
        self.git("add", ".")
        self.git("commit", "-qm", "second fixture")
        head = self.git("rev-parse", "HEAD")
        branch = self.git("symbolic-ref", "--short", "HEAD")
        # Permit only this test's local repository as the remote; no network involved.
        with patch("cloud_experiments.source.repository_url", return_value=str(self.root)):
            self.assertEqual(source.resolve_ref(str(self.root), "v1"), old)
            self.assertEqual(source.resolve_ref(str(self.root), branch), head)
            self.assertEqual(source.resolve_ref(str(self.root), old), old)


class ProviderTests(unittest.TestCase):
    def test_list_does_not_trust_the_provider_selector_alone(self):
        c = Hetzner({"context": "test"})
        candidates = [server(), server("other-run"), server(labels={"run-id": "../unsafe"})]
        with patch.object(c, "call", return_value=subprocess.CompletedProcess([], 0, json.dumps(candidates).encode())):
            self.assertEqual(c.find("test-run"), server())

    def test_only_exact_managed_server_can_be_deleted(self):
        c = Hetzner({"context": "test"})
        for value in (server(labels={}), server(name="wrong"), server(id="123"), server(labels={"managed-by": "cloud-experiments", "run-id": "different"})):
            with patch.object(c, "call") as call:
                with self.assertRaises(Error):
                    c.delete(value, "test-run")
                call.assert_not_called()
        with patch.object(c, "call", return_value=subprocess.CompletedProcess([], 0, json.dumps(server(labels={})).encode())) as call:
            with self.assertRaises(Error):
                c.delete(server(), "test-run")
            self.assertEqual(call.call_count, 1)

    def test_create_uses_stdin_and_structured_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = sample_config(tmp)
            cloud = Hetzner(c["hetzner"])
            with patch.object(cloud, "call", return_value=subprocess.CompletedProcess([], 0, json.dumps({"server": server()}).encode())) as call:
                cloud.create(sample_manifest(c), "FAKE-SECRET-PAYLOAD")
                self.assertNotIn("FAKE-SECRET-PAYLOAD", str(call.call_args.args))
                self.assertEqual(call.call_args.kwargs["input"], b"FAKE-SECRET-PAYLOAD")
                self.assertIn("--user-data-from-file", call.call_args.args)

    def test_ssh_quotes_shell_values_and_isolates_known_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = SSH(server(), tmp)
            with patch("cloud_experiments.providers.command") as run:
                ssh.call(["echo", "a; $(touch /bad) ' space"])
                import shlex
                self.assertEqual(shlex.split(run.call_args.args[0][-1]), ["echo", "a; $(touch /bad) ' space"])
            self.assertIn("StrictHostKeyChecking=accept-new", ssh.options)
            self.assertTrue(any("known_hosts-123" in x for x in ssh.options))

    def test_errors_and_redaction_never_expose_subprocess_output(self):
        with patch("cloud_experiments.common.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"secret", b"secret")):
            with self.assertRaises(Error) as caught:
                command(["fake", "secret"])
            self.assertNotIn("secret", str(caught.exception).replace("secrets", ""))
        self.assertEqual(redact("fake-token fake-url", ["fake-token", "fake-url"]), "[REDACTED] [REDACTED]")


if __name__ == "__main__":
    unittest.main()
