"""Cloud compatibility, operational EWS overrides, and implementation provenance."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config, sample_manifest
from cloud_experiments import config, ews_contract as contract, provenance, source
from cloud_experiments.common import Error


def sealed_manifest(**changes):
    value = {**contract.CONTRACT, "files": {"metadata.json": {"sha256": "a" * 64, "size": 12}},
             "directories": [], "pruned_runs": [], "omitted_checkpoints": [], **changes}
    value["snapshot_id"] = contract.fingerprint(value)
    return value


class RecoveryContractTests(unittest.TestCase):
    def test_known_contract_accepts_additive_fields_but_future_version_fails_closed(self):
        manifest = sealed_manifest(optional_future_field={"detail": 2})
        self.assertEqual(contract.validate_manifest(manifest), manifest)
        for version in (None, True, "1", 2):
            with self.subTest(version=version), self.assertRaisesRegex(Error, "Unsupported EWS cloud"):
                contract.validate_manifest(sealed_manifest(schema_version=version))

    def test_envelope_rejects_unsafe_paths_invalid_sizes_and_changed_inventory(self):
        for name in ("../escape", "/absolute", "a//b", "a/", "a\\b", "a:b", "a/./b"):
            manifest = sealed_manifest(directories=[name])
            with self.subTest(name=name), self.assertRaises(Error):
                contract.validate_manifest(manifest)
        for descriptor in ({"sha256": "a" * 64, "size": True}, {"sha256": "a" * 64, "size": -1},
                           {"sha256": "not-a-hash", "size": 1}):
            with self.assertRaises(Error):
                contract.validate_manifest(sealed_manifest(files={"metadata.json": descriptor}))
        manifest = sealed_manifest()
        manifest["files"]["metadata.json"]["size"] += 1
        with self.assertRaisesRegex(Error, "identity mismatch"):
            contract.validate_manifest(manifest)

    def test_static_preflight_requires_public_api_and_explicit_supported_version(self):
        exports = "from .recovery import create_snapshot, validate_snapshot, restore_snapshot"
        declaration = 'RECOVERY_SCHEMA = "experiments-wo-stress/recovery"\nRECOVERY_VERSION = 1'
        self.assertEqual(contract.inspect_source(declaration, exports), contract.CONTRACT)
        for text, public in ((declaration.replace("= 1", "= 2"), exports), (declaration, ""),
                             ("raise Exception('never executed')", exports)):
            with self.assertRaises(Error):
                contract.inspect_source(text, public)

    def test_installed_api_is_queried_and_unknown_version_rejected(self):
        worker = Mock()
        worker.user_step.return_value.stdout = json.dumps(contract.CONTRACT).encode()
        self.assertEqual(contract.query(worker, contract.CONTRACT), contract.CONTRACT)
        self.assertIn("from experiments_wo_stress.storage import", worker.user_step.call_args.args[0][2])
        worker.user_step.return_value.stdout = json.dumps({**contract.CONTRACT, "schema_version": 2}).encode()
        with self.assertRaisesRegex(Error, "Unsupported EWS cloud"):
            contract.query(worker)

    def test_resolved_source_records_exact_commit_and_contract_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery = root / contract.SOURCE
            recovery.parent.mkdir(parents=True)
            recovery.write_text('RECOVERY_SCHEMA = "experiments-wo-stress/recovery"\nRECOVERY_VERSION = 1\nraise Exception("must never execute")')
            (root / contract.EXPORTS).write_text("from .recovery import create_snapshot, validate_snapshot, restore_snapshot")
            for arguments in (("init", "-q"), ("config", "user.email", "tests@example.invalid"),
                              ("config", "user.name", "Tests"), ("add", "."), ("commit", "-qm", "fixture")):
                subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True).stdout.decode().strip()
            with patch("cloud_experiments.source.repository_url", return_value=str(root)):
                resolved = source.resolve_ews(str(root), commit)
            self.assertEqual(resolved, {"repository": str(root), "requested_ref": commit,
                                        "commit": commit, "cloud_contract": contract.CONTRACT})


class RuntimeOptionsTests(unittest.TestCase):
    """Public EWS semantics own validation; cloud merely chooses CPU allocation."""

    def run_script(self, execution, timezone=None, portable=True):
        study = types.SimpleNamespace(execution=execution, display={"timezone": "UTC"})
        ews = types.ModuleType("experiments_wo_stress")
        ews.load_config = Mock(return_value=study)
        settings = types.ModuleType("experiments_wo_stress.study.config")
        settings.resolve_display_timezone = Mock(return_value=types.SimpleNamespace(key=timezone or "UTC"))
        settings.validate_gpu_workers = Mock()
        portability = types.ModuleType("experiments_wo_stress.execution.portability")
        portability.portable_environment = Mock()
        modules = {ews.__name__: ews, settings.__name__: settings, portability.__name__: portability}
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", ["script", "study.yml", json.dumps(timezone), "portable" if portable else "strict"]), patch("os.sched_getaffinity", return_value=set(range(12))), contextlib.redirect_stdout(output):
            exec(contract.RUNTIME_SCRIPT, {})
        return json.loads(output.getvalue()), settings, portability

    def test_cpu_uses_runtime_logical_processors_and_forwards_ews_timezone(self):
        options, settings, portability = self.run_script({"workers": 2}, "Europe/Rome")
        self.assertEqual(options, {"workers": 12, "timezone": "Europe/Rome", "mode": "cpu"})
        settings.resolve_display_timezone.assert_called_once_with({"timezone": "UTC"}, "Europe/Rome")
        portability.portable_environment.assert_called_once()
        command = contract.command_options(["ews", "run", "study.yml", "--workers=2", "--timezone", "UTC"], options, "Europe/Rome")
        self.assertEqual(command, ["ews", "run", "study.yml", "--workers", "12", "--timezone", "Europe/Rome"])

    def test_gpu_preserves_the_explicit_ews_worker_relationship(self):
        options, settings, portability = self.run_script({"workers": 2, "gpu_ids": [0, 1]}, portable=False)
        self.assertEqual(options["workers"], 2)
        self.assertEqual(options["mode"], "gpu")
        settings.validate_gpu_workers.assert_called_once_with({"workers": 2, "gpu_ids": [0, 1]}, 2)
        portability.portable_environment.assert_not_called()
        self.assertEqual(contract.command_options(["ews", "run", "study.yml"], options), ["ews", "run", "study.yml"])

    def test_runtime_response_requires_a_positive_integer_worker_count(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Mock(work=Path(directory))
            manifest = sample_manifest(sample_config(directory))
            for count in (None, 0, -1, True, "12"):
                worker.user_step.return_value.stdout = json.dumps({"workers": count, "timezone": "UTC", "mode": "cpu"}).encode()
                with self.assertRaises(Error):
                    contract.runtime_options(worker, manifest)

    def test_sync_and_display_are_validated_operational_settings(self):
        self.assertEqual(config.run_settings({})["sync_seconds"], 300)
        for seconds in (0, -1, True, float("inf"), float("nan"), "300"):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(Error, "sync_seconds"):
                config.run_settings({"sync_seconds": seconds})
        self.assertEqual(config.run_settings({"timezone": "UTC"})["timezone"], "UTC")
        for zone in ("", False, "UTC\n", []):
            with self.assertRaisesRegex(Error, "timezone"):
                config.run_settings({"timezone": zone})


class ToolProvenanceTests(unittest.TestCase):
    def test_version_revision_and_implementation_bytes_are_identified(self):
        current = provenance.current()
        self.assertEqual(current["version"], "0.6.0")
        self.assertRegex(current["git_commit"], r"^[a-f0-9]{40}$")
        self.assertRegex(current["implementation_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(current, provenance.current())


if __name__ == "__main__":
    unittest.main()
