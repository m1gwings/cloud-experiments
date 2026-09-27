"""Committed recovery views and semantic downloads on local rclone storage."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_core import sample_config, sample_manifest
from test_results import INDEX
from cloud_experiments import cli, ews_contract, studies
from cloud_experiments.common import Error, sha256, write_json
from cloud_experiments.providers import Storage


@unittest.skipUnless(shutil.which("rclone"), "local rclone unavailable")
class RecoveryResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = sample_config(self.root)
        self.remote = self.root / "remote"
        self.empty = self.root / "rclone.conf"
        self.empty.write_text("")
        self.study = "s-" + "a" * 32
        self.attempt = studies.attempt_id(self.study)
        self.study_root = self.remote / "studies" / self.study
        self.manifest = sample_manifest(self.config, self.attempt)
        self.manifest.update(study_id=self.study, status="running")
        write_json(self.study_root / "attempts" / self.attempt / "manifest.json", self.manifest)
        write_json(self.study_root / "attempts" / self.attempt / "logs/events.json", {"event": "started"})
        self.storage = Storage(self.config)
        self.storage.call = self.call
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        prefix = "test:test-bucket"
        mapped = [str(self.remote) + arg[len(prefix):] if isinstance(arg, str) and arg.startswith(prefix) else arg for arg in args]
        result = subprocess.run(["rclone", "--config", str(self.empty), *mapped], capture_output=True, timeout=20)
        if result.returncode:
            raise Error("Local fake storage failed")
        return result

    def snapshot(self, serial, payloads, *, parent=None, attempt=None):
        payloads = {"metadata.json": b'{}', "artifacts.json": json.dumps(INDEX).encode(), **payloads}
        entries, directories = {}, set()
        for name, data in payloads.items():
            digest = hashlib.sha256(data).hexdigest()
            entries[name] = {"sha256": digest, "size": len(data)}
            path = self.study_root / "blobs" / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            directories.update(p.as_posix() for p in Path(name).parents if p.parts)
        recovery = {**ews_contract.CONTRACT, "files": entries, "directories": sorted(directories),
                    "pruned_runs": [], "omitted_checkpoints": []}
        recovery["snapshot_id"] = ews_contract.fingerprint(recovery)
        path = self.study_root / "snapshots" / recovery["snapshot_id"] / "recovery.json"
        write_json(path, recovery)
        commit = {"schema_version": 2, "study_id": self.study, "attempt_id": attempt or self.attempt,
                  "commit_id": f"{serial:032x}", "parent": parent, "snapshot_id": recovery["snapshot_id"],
                  "recovery_sha256": sha256(path), "contract": ews_contract.CONTRACT,
                  "committed_at": f"2026-09-27T12:{serial:02}:00+00:00", "final": False, "completed": False,
                  "provenance": {"ews": {"commit": "d" * 40}, "cloud": {"version": "test"}}}
        write_json(self.study_root / "commits" / (commit["commit_id"] + ".json"), commit)
        return commit, recovery

    def test_listing_and_selectors_materialize_only_latest_committed_output(self):
        first, _ = self.snapshot(1, {"exports/charts/old.pdf": b"old"})
        latest, _ = self.snapshot(2, {"exports/charts/new.pdf": b"new"}, parent=first["commit_id"])
        files = self.storage.files(self.study)
        paths = {entry["path"] for entry in files}
        self.assertIn("artifacts/output/exports/charts/new.pdf", paths)
        self.assertNotIn("artifacts/output/exports/charts/old.pdf", paths)
        self.assertFalse(any(path.startswith(("blobs/", "snapshots/")) for path in paths))
        destination = self.root / "selected"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.pull_selection(self.storage, self.config, self.study, destination=destination, role="figures")
        self.assertEqual((destination / "artifacts/output/exports/charts/new.pdf").read_bytes(), b"new")
        downloads = [args for args in self.calls if args[0] == 'copy' and str(args[1]).endswith('/blobs')]
        self.assertEqual(len(downloads), 1)
        self.assertIn('--files-from', downloads[0])
        self.assertIn('--no-traverse', downloads[0])
        self.assertEqual(self.storage.study_manifest(self.study)["last_recovery"]["commit_id"], latest["commit_id"])

    def test_full_pull_replaces_output_without_copying_blob_pool_and_keeps_attempt_logs(self):
        _, recovery = self.snapshot(1, {"exports/charts/new.pdf": b"new"})
        destination = self.root / "download"
        write_json(destination / "artifacts/output/pruned.json", {"obsolete": True})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.pull_run(self.storage, self.config, self.study, destination)
        self.assertFalse((destination / "artifacts/output/pruned.json").exists())
        self.assertEqual((destination / "artifacts/output/exports/charts/new.pdf").read_bytes(), b"new")
        self.assertTrue((destination / "attempts" / self.attempt / "logs/events.json").exists())
        self.assertFalse((destination / "blobs").exists())
        self.assertFalse((destination / "snapshots").exists())
        self.assertTrue((destination / ".cloud-pulled.json").exists())
        self.assertEqual(json.loads((destination / "artifacts/recovery.json").read_text()), recovery)

    def test_corrupt_payload_preserves_previous_local_full_output(self):
        _, recovery = self.snapshot(1, {"exports/charts/new.pdf": b"new"})
        blob = self.study_root / "blobs" / recovery["files"]["exports/charts/new.pdf"]["sha256"]
        blob.write_bytes(b"bad")
        destination = self.root / "download"
        write_json(destination / "artifacts/output/keep.json", {"verified": True})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(Error, "SHA-256"):
            self.storage.pull(self.study, destination)
        self.assertTrue((destination / "artifacts/output/keep.json").exists())
        self.assertFalse((destination / ".cloud-pulled.json").exists())

    def test_full_pull_rejects_symlinked_recovery_metadata_before_overwriting_target(self):
        self.snapshot(1, {"exports/charts/new.pdf": b"new"})
        destination = self.root / "download"
        (destination / "artifacts").mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("keep")
        (destination / "artifacts/recovery.json").symlink_to(outside)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(Error, "symlink"):
            self.storage.pull(self.study, destination)
        self.assertEqual(outside.read_text(), "keep")

    def test_superseded_attempt_keeps_metadata_without_claiming_historical_output(self):
        first, recovery = self.snapshot(1, {"exports/charts/old.pdf": b"old"})
        second_attempt = studies.attempt_id(self.study)
        self.snapshot(2, {"exports/charts/new.pdf": b"new"}, parent=first["commit_id"], attempt=second_attempt)
        (self.study_root / "blobs" / recovery["files"]["exports/charts/old.pdf"]["sha256"]).unlink()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            files = self.storage.files(self.attempt)
        self.assertIn("superseded", errors.getvalue())
        self.assertIn("manifest.json", {entry["path"] for entry in files})
        self.assertFalse(any(entry["path"].startswith("artifacts/output/") for entry in files))

    def test_unsupported_contract_is_rejected_by_result_view(self):
        commit, _ = self.snapshot(1, {})
        commit["contract"] = {**ews_contract.CONTRACT, "schema_version": 99}
        write_json(self.study_root / "commits" / (commit["commit_id"] + ".json"), commit)
        with self.assertRaisesRegex(Error, "Unsupported EWS"):
            self.storage.files(self.study)

    def test_uncommitted_first_upload_is_never_exposed_as_recovery_output(self):
        commit, _ = self.snapshot(1, {"exports/charts/new.pdf": b"new"})
        (self.study_root / "commits" / (commit["commit_id"] + ".json")).unlink()
        paths = {entry["path"] for entry in self.storage.files(self.study)}
        self.assertFalse(any(path.startswith(("artifacts/output/", "blobs/", "snapshots/")) for path in paths))
        destination = self.root / "download"
        with contextlib.redirect_stderr(io.StringIO()):
            self.storage.pull(self.study, destination)
        self.assertTrue((destination / "manifest.json").exists())
        self.assertFalse((destination / "blobs").exists())
        self.assertFalse((destination / "artifacts/output").exists())


if __name__ == "__main__":
    unittest.main()
