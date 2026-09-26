"""Result inspection/retrieval: fakes and temporary local rclone paths only."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config, sample_manifest
from cloud_experiments import cli
from cloud_experiments.artifacts import MAX_INDEX_BYTES
from cloud_experiments.common import Error, read_json, write_json
from cloud_experiments.providers import Storage

OUTPUT = "artifacts/custom/study"
FIGURES = OUTPUT + "/exports/charts"
INDEX_PATH = OUTPUT + "/artifacts.json"
INDEX = {
    "schema": "experiments-wo-stress/artifacts", "schema_version": 1,
    "artifacts": {
        "figures": {"path": "exports/charts", "kind": "directory", "optional": True},
        "analysis": {"path": "exports", "kind": "directory", "optional": True},
        "compute_report": {"path": "resources.md", "kind": "file", "optional": True},
    },
}
FILES = [
    {"path": FIGURES + "/regret.pdf", "size": 2048},
    {"path": FIGURES + "/nested/plot.jpg", "size": 512},
    {"path": FIGURES + "-old/other.pdf", "size": 99},
    {"path": OUTPUT + "/exports/summary.csv", "size": 20},
    {"path": OUTPUT + "/resources.md", "size": 30},
    {"path": "manifest.json", "size": 100},
    {"path": INDEX_PATH, "size": len(json.dumps(INDEX))},
]


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = sample_config(self.root)
        self.destination = Path(self.config["local"]["results_dir"]) / "test-run"
        self.storage = Mock()
        self.storage.files.return_value = FILES
        self.storage.artifact_index.return_value = json.loads(json.dumps(INDEX))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.out))
        self.stack.enter_context(contextlib.redirect_stderr(self.err))
        self.stack.enter_context(patch("cloud_experiments.cli.Storage", return_value=self.storage))

    def run_results(self, *args):
        cli.results(cli.parser("cloud-results").parse_args(args), self.config)

    def test_help_exposes_new_interface_without_credentials(self):
        root = Path(__file__).resolve().parents[1]
        for subcommand in ([], ["ls"], ["pull"]):
            with self.subTest(subcommand=subcommand):
                result = subprocess.run([str(root / "bin/cloud-results"), *subcommand, "--help"], capture_output=True)
                self.assertEqual(result.returncode, 0)
                self.assertIn(b"--json" if subcommand == ["ls"] else b"--plots", result.stdout)

    def test_selectors_are_mutually_exclusive_and_sync_remains_full(self):
        for args in (("pull", "test-run", "--plots", "--analysis"),
                     ("pull", "test-run", "--plots", "--path", "manifest.json"),
                     ("pull", "test-run", "--analysis", "--path", "manifest.json"),
                     ("pull", "test-run", "--report", "--plots"),
                     ("pull", "test-run", "--report", "--path", "logs"),
                     ("sync", "--plots"), ("sync", "--path", "manifest.json")):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_results(*args)

    def test_invalid_paths_fail_before_storage_or_local_writes(self):
        for path in ("", "/", "/tmp", "//host/path", ".", "..", "../other-run", "a/../b",
                     "a/./b", "a//b", "a//", "C:/tmp", "remote:path", "a\\b", "a\nb", "a\tb",
                     "a\x00b", "a\x1bb", "a\x7fb", "a\u202eb"):
            with self.subTest(path=repr(path)), self.assertRaises(Error):
                self.run_results("pull", "test-run", "--path", path)
        self.storage.files.assert_not_called()
        self.storage.pull_paths.assert_not_called()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.out.getvalue(), "")

    def test_invalid_run_ids_fail_before_storage(self):
        for operation in (("ls", "../another"), ("pull", "../another", "--plots")):
            with self.subTest(operation=operation), self.assertRaises(Error):
                self.run_results(*operation)
        self.storage.files.assert_not_called()

    def test_plots_match_only_the_exact_subtree_and_preserve_destination(self):
        self.run_results("pull", "test-run", "--plots")
        self.storage.pull_paths.assert_called_once_with("test-run", self.destination, [x["path"] for x in FILES[:2]])
        self.storage.pull.assert_not_called()
        self.assertEqual(self.out.getvalue(), str(self.destination / FIGURES) + "\n")
        self.assertIn("2 selected files", self.err.getvalue())
        self.assertFalse((self.destination / ".cloud-pulled.json").exists())

    def test_analysis_includes_figures_and_other_analysis_outputs(self):
        self.run_results("pull", "test-run", "--analysis")
        self.assertEqual(self.storage.pull_paths.call_args.args[2], [x["path"] for x in FILES[:4]])

    def test_report_selects_only_the_file_and_not_descendants(self):
        self.storage.files.return_value = FILES + [{"path": OUTPUT + "/resources.md/extra", "size": 1}]
        self.run_results("pull", "test-run", "--report")
        self.storage.artifact_index.assert_called_once_with("test-run", INDEX_PATH)
        self.assertEqual(self.storage.pull_paths.call_args.args[2], [OUTPUT + "/resources.md"])

    def test_optional_absent_analysis_report_and_undeclared_role_succeed(self):
        self.storage.files.return_value = [FILES[-1]]
        for selector in ("--analysis", "--report", "--plots"):
            self.run_results("pull", "test-run", selector)
        self.storage.artifact_index.return_value["artifacts"].pop("figures")
        self.run_results("pull", "test-run", "--plots")
        self.assertIn("publishes no figures role", self.err.getvalue())
        self.storage.pull_paths.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_legacy_runs_require_path_and_generic_paths_do_not_read_ews_metadata(self):
        self.storage.files.return_value = FILES[:-1]
        with self.assertRaisesRegex(Error, "legacy run.*metadata not uploaded"):
            self.run_results("pull", "test-run", "--plots")
        self.run_results("pull", "test-run", "--path", FIGURES)
        self.storage.artifact_index.assert_not_called()
        self.assertEqual(self.storage.pull_paths.call_args.args[2], [x["path"] for x in FILES[:2]])

    def test_multiple_catalogs_and_oversized_metadata_fail_without_downloading(self):
        for files in (FILES + [{"path": "artifacts/second/artifacts.json", "size": 1}],
                      [{"path": INDEX_PATH, "size": MAX_INDEX_BYTES + 1}]):
            self.storage.files.return_value = files
            with self.assertRaises(Error):
                self.run_results("pull", "test-run", "--plots")
        self.storage.artifact_index.assert_not_called()
        self.storage.pull_paths.assert_not_called()

    def test_invalid_schema_and_descriptors_never_fall_back_to_layout_guesses(self):
        indexes = [[], {}, {**INDEX, "schema_version": 2}, {**INDEX, "schema_version": True},
                   {**INDEX, "schema": "another-format"}, {**INDEX, "artifacts": []}]
        for path in ("../outside", "/absolute", "a/../b", "a//b", "a/", "C:/tmp", "a\\b", "a\nb", None):
            indexes.append({**INDEX, "artifacts": {"figures": {"path": path, "kind": "directory", "optional": True}}})
        for entry in ({"path": "ok", "kind": "glob", "optional": True},
                      {"path": "ok", "kind": "file"}, {"path": "ok", "kind": "file", "optional": "true"}):
            indexes.append({**INDEX, "artifacts": {"figures": entry}})
        for index in indexes:
            with self.subTest(index=index), self.assertRaises(Error):
                self.storage.artifact_index.return_value = index
                self.run_results("pull", "test-run", "--plots")
        self.storage.pull_paths.assert_not_called()

    def test_missing_required_artifact_and_catalog_read_failure_are_errors(self):
        self.storage.files.return_value = [FILES[-1]]
        self.storage.artifact_index.return_value["artifacts"]["figures"]["optional"] = False
        with self.assertRaisesRegex(Error, "selected path"):
            self.run_results("pull", "test-run", "--plots")
        self.storage.artifact_index.side_effect = Error("catalog read failed")
        with self.assertRaisesRegex(Error, "catalog read failed"):
            self.run_results("pull", "test-run", "--plots")
        self.storage.pull_paths.assert_not_called()

    def test_file_and_trailing_slash_subtree_with_custom_run_root(self):
        for path, expected in (("manifest.json", ["manifest.json"]), (FIGURES + "/", [x["path"] for x in FILES[:2]])):
            with self.subTest(path=path):
                self.storage.reset_mock()
                self.out.seek(0)
                self.out.truncate()
                self.run_results("pull", "test-run", "--path", path, "--dest", str(self.root / "custom"))
                self.storage.pull_paths.assert_called_once_with("test-run", self.root / "custom", expected)
                self.assertEqual(self.out.getvalue(), str(self.root / "custom" / path.rstrip("/")) + "\n")

    def test_glob_characters_are_literal(self):
        path = "artifacts/[a]*?# report.pdf"
        self.storage.files.return_value = [{"path": path, "size": 1}, {"path": "artifacts/a-report.pdf", "size": 1}]
        self.run_results("pull", "test-run", "--path", path)
        self.assertEqual(self.storage.pull_paths.call_args.args[2], [path])

    def test_missing_figures_succeed_without_creating_a_destination(self):
        self.storage.files.return_value = FILES[2:]
        self.run_results("pull", "test-run", "--plots")
        self.storage.pull_paths.assert_not_called()
        self.assertIn("no stored figures", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")
        self.assertFalse(self.destination.exists())

    def test_missing_path_and_unknown_run_are_errors(self):
        with self.assertRaisesRegex(Error, "selected path"):
            self.run_results("pull", "test-run", "--path", "missing")
        self.storage.files.return_value = []
        with self.assertRaisesRegex(Error, "No stored files"):
            self.run_results("pull", "test-run", "--plots")
        self.storage.pull_paths.assert_not_called()

    def test_lookup_errors_are_not_reported_as_absent_figures(self):
        self.storage.files.side_effect = Error("storage unavailable")
        with self.assertRaisesRegex(Error, "storage unavailable"):
            self.run_results("pull", "test-run", "--plots")
        self.assertNotIn("No figures", self.err.getvalue())

    def test_selective_failure_and_interrupt_leave_marker_untouched(self):
        marker = self.destination / ".cloud-pulled.json"
        write_json(marker, {"previous": "full download"})
        before = marker.read_bytes()
        for error in (Error("transfer failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                self.storage.pull_paths.side_effect = error
                self.run_results("pull", "test-run", "--plots")
            self.assertEqual(marker.read_bytes(), before)
        self.assertNotIn("complete", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_successful_partial_pull_preserves_existing_full_marker(self):
        marker = self.destination / ".cloud-pulled.json"
        write_json(marker, {"previous": "full download"})
        before = marker.read_bytes()
        self.run_results("pull", "test-run", "--plots")
        self.assertEqual(marker.read_bytes(), before)

    def test_sync_still_fetches_the_full_run_after_a_selective_pull(self):
        self.run_results("pull", "test-run", "--plots")
        manifest = sample_manifest(self.config)
        manifest["status"] = "completed"
        self.storage.manifests.return_value = [manifest]
        self.storage.pull.side_effect = lambda rid, dest: write_json(dest / "manifest.json", manifest)
        self.run_results("sync")
        self.storage.pull.assert_called_once_with("test-run", self.destination)
        self.assertEqual(read_json(self.destination / ".cloud-pulled.json"), {"manifest": manifest})

    def test_plain_pull_preserves_full_archive_behavior(self):
        manifest = sample_manifest(self.config)
        self.storage.pull.side_effect = lambda rid, dest: write_json(dest / "manifest.json", manifest)
        self.run_results("pull", "test-run")
        self.storage.files.assert_not_called()
        self.storage.pull.assert_called_once_with("test-run", self.destination)
        self.assertTrue((self.destination / ".cloud-pulled.json").exists())

    def test_non_tty_listing_is_headerless_tsv_with_exact_bytes(self):
        with patch("cloud_experiments.cli.interactive", return_value=False):
            self.run_results("ls", "test-run")
        self.assertEqual(self.out.getvalue(), "".join(f"{x['size']}\t{x['path']}\n" for x in FILES))
        self.storage.pull.assert_not_called()
        self.storage.pull_paths.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_interactive_listing_uses_readable_sizes(self):
        with patch("cloud_experiments.cli.interactive", return_value=True):
            self.run_results("ls", "test-run")
        self.assertIn("SIZE", self.out.getvalue())
        self.assertIn("2.0 KiB", self.out.getvalue())

    def test_json_listing_is_structured_and_empty_listing_is_clear(self):
        self.run_results("ls", "test-run", "--json")
        self.assertEqual(json.loads(self.out.getvalue()), FILES)
        self.out.seek(0)
        self.out.truncate()
        self.storage.files.return_value = []
        self.run_results("ls", "test-run", "--json")
        self.assertEqual(json.loads(self.out.getvalue()), [])
        self.assertIn("No stored files", self.err.getvalue())


class StorageResultTests(unittest.TestCase):
    def test_catalog_reads_are_bounded_and_reject_malformed_duplicate_or_oversized_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(sample_config(tmp))
            with patch.object(storage, "call", return_value=Mock(stdout=json.dumps(INDEX))) as call:
                self.assertEqual(storage.artifact_index("test-run", INDEX_PATH), INDEX)
                call.assert_called_once_with("cat", storage.path("test-run") + "/" + INDEX_PATH,
                                             "--head", str(MAX_INDEX_BYTES + 1), timeout=90)
            for raw in ('{', '{"schema_version": 1, "schema_version": 2}', b'\xff', ' ' * (MAX_INDEX_BYTES + 1)):
                with self.subTest(raw=str(raw)[:40]), patch.object(storage, "call", return_value=Mock(stdout=raw)), self.assertRaises(Error):
                    storage.artifact_index("test-run", INDEX_PATH)

    def test_listing_is_metadata_only_sorted_and_confined_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(sample_config(tmp))
            raw = [{"Path": x["path"], "Size": x["size"], "IsDir": False} for x in FILES]
            with patch.object(storage, "call", return_value=Mock(stdout=json.dumps(raw))) as call:
                self.assertEqual(storage.files("test-run"), sorted(FILES, key=lambda x: x["path"]))
                call.assert_called_once_with("lsjson", "test:test-bucket/runs/test-run", "--recursive", "--files-only",
                                             "--no-modtime", "--no-mimetype")

    def test_untrusted_listing_paths_and_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(sample_config(tmp))
            for path in ("../escape", "/absolute", "a/../../outside", "a\x1b[31m", "a/", "a\\b"):
                with self.subTest(path=path), patch.object(storage, "call", return_value=Mock(stdout=json.dumps([
                        {"Path": path, "Size": 1, "IsDir": False}]))), self.assertRaises(Error):
                    storage.files("test-run")
            for raw in ({}, [None], [{"Path": "ok", "Size": -1, "IsDir": False}],
                        [{"Path": "ok", "Size": True, "IsDir": False}], [{"Path": "ok", "Size": 1, "IsDir": True}],
                        [{"Path": "ok", "Size": 1, "IsDir": False}] * 2):
                with self.subTest(raw=raw), patch.object(storage, "call", return_value=Mock(stdout=json.dumps(raw))), self.assertRaises(Error):
                    storage.files("test-run")

    def test_pull_uses_a_literal_file_list_and_existing_progress_adapter(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as err:
            storage = Storage(sample_config(tmp))
            names = ["dir/# [x]*?.pdf", " spaced name ", "-filename"]

            def fake_command(argv, **kwargs):
                self.assertIn("test:test-bucket/runs/test-run", argv)
                self.assertIn("copy", argv)
                self.assertIn("--no-traverse", argv)
                self.assertIn("--use-json-log", argv)
                self.assertNotIn("--include", argv)
                file_list = Path(argv[argv.index("--files-from-raw") + 1])
                self.assertEqual(file_list.read_text(), "".join(name + "\n" for name in names))
                kwargs["stderr_line"](json.dumps({"stats": {"bytes": 1024, "totalBytes": 1024}, "msg": "fake-secret"}))
                return Mock(returncode=0)

            with patch("cloud_experiments.providers.command", side_effect=fake_command):
                storage.pull_paths("test-run", Path(tmp) / "download", names)
            self.assertIn("100%", err.getvalue())
            self.assertNotIn("fake-secret", err.getvalue())
            self.assertNotIn("\x1b", err.getvalue())

    def test_rejects_symlinks_reserved_markers_and_traversal_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(sample_config(root))
            (root / "download").mkdir()
            (root / "download/link").symlink_to(root / "outside")
            with patch.object(storage, "call") as call:
                for path in ("../outside", "/absolute", "link/file", "link", ".cloud-pulled.json", ".cloud-pulled.json.tmp", ".cloud-pulled.json/file"):
                    with self.subTest(path=path), self.assertRaises(Error):
                        storage.pull_paths("test-run", root / "download", [path])
                call.assert_not_called()

    @unittest.skipUnless(shutil.which("rclone"), "rclone unavailable")
    def test_real_rclone_local_only_selective_copy_preserves_tree_and_literal_names(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            config = sample_config(root)
            Path(config["local"]["rclone_config"]).write_text("")
            source, destination = root / "remote-run", root / "download"
            contents = {x["path"]: "test " + x["path"] for x in FILES}
            contents[INDEX_PATH] = json.dumps(INDEX)
            contents[FIGURES + "/# [x]*?.pdf"] = "literal filename"
            contents[FIGURES + "/ spaced name "] = "spaces retained"
            contents[FIGURES + "/unicode-λ.pdf"] = "unicode name"
            for path, value in contents.items():
                target = source / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value)
            storage = Storage(config)
            with patch.object(storage, "path", return_value=str(source)):
                cli.pull_selection(storage, config, "test-run", destination=destination, role="figures")
                actual = {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()}
                self.assertEqual(actual, {p for p in contents if p.startswith(FIGURES + "/")})
                self.assertFalse((destination / ".cloud-pulled.json").exists())
                cli.pull_selection(storage, config, "test-run", "manifest.json", destination)
                self.assertEqual((destination / "manifest.json").read_text(), contents["manifest.json"])
                # A repeat is incremental; preserve local extras and all source bytes.
                (destination / "keep-local.txt").write_text("keep")
                cli.pull_selection(storage, config, "test-run", destination=destination, role="figures")
                self.assertEqual((destination / "keep-local.txt").read_text(), "keep")
                self.assertEqual({str(p.relative_to(source)): p.read_text() for p in source.rglob("*") if p.is_file()}, contents)


if __name__ == "__main__":
    unittest.main()
