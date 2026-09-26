"""Offline rendering, process plumbing and download failure regressions."""

import argparse
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

from test_core import sample_config, sample_manifest, server
from cloud_experiments import cli
from cloud_experiments.common import Error, command, write_json
from cloud_experiments.progress import Activity, transfer_stats
from cloud_experiments.providers import SSH, Storage


class Stream(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


STATS = {"bytes": 1024, "totalBytes": 2048, "speed": 512, "eta": 2,
         "transfers": 1, "totalTransfers": 2, "checks": 2, "totalChecks": 3}


class DisplayTests(unittest.TestCase):
    def display(self, stdout_tty, stderr_tty, environment=None, failure=None):
        out, err = Stream(stdout_tty), Stream(stderr_tty)
        with patch("sys.stdout", out), patch("sys.stderr", err), patch.dict(os.environ, environment or {}, clear=True), patch("cloud_experiments.progress.threading.Thread"), patch("cloud_experiments.progress.time.monotonic", return_value=0) as clock:
            try:
                with Activity("Downloading") as progress:
                    progress.rclone_line(json.dumps({"stats": STATS, "msg": "fake-secret\x1b[31m"}))
                    clock.return_value = 1
                    progress._tick()
                    clock.return_value = 15
                    progress._tick()
                    if failure:
                        raise failure
            except BaseException as exc:
                if exc is not failure:
                    raise
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("fake-secret", err.getvalue())
        return err.getvalue()

    def test_tty_has_bar_spinner_and_clean_final_line(self):
        rendered = self.display(True, True)
        self.assertIn("\r\x1b[2K", rendered)
        self.assertIn("[######------] 50%", rendered)
        self.assertIn("Done: Downloading", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_either_redirected_stream_and_no_color_or_dumb_use_plain_messages(self):
        cases = [(False, False, {}), (True, False, {}), (False, True, {}),
                 (True, True, {"NO_COLOR": "1"}), (True, True, {"TERM": "dumb"})]
        for out, err, environment in cases:
            with self.subTest(out=out, err=err, environment=environment):
                rendered = self.display(out, err, environment)
                self.assertNotIn("\x1b", rendered)
                self.assertNotIn("\r", rendered)
                self.assertEqual(len(rendered.splitlines()), 3)
                self.assertIn("ETA 2s", rendered)
                self.assertNotIn("(1s)", rendered)  # Throttled to 15 seconds.

    def test_failed_or_interrupted_stage_never_claims_success_or_echoes_exception(self):
        for failure, label in ((Error("fake-secret"), "Failed"), (KeyboardInterrupt(), "Interrupted")):
            rendered = self.display(True, True, failure=failure)
            self.assertIn(label + ": Downloading", rendered)
            self.assertNotIn("Done:", rendered)

    def test_nested_activities_share_one_display(self):
        err = Stream()
        with patch("sys.stderr", err), patch("sys.stdout", Stream()), patch("cloud_experiments.progress.threading.Thread"):
            with Activity("Outer") as outer:
                with Activity("Inner") as inner:
                    self.assertIs(inner, outer)
                    inner.rclone_line(json.dumps({"stats": STATS}))
            with Activity("Next") as next_activity:
                self.assertIsNot(next_activity, outer)
        self.assertNotIn("Inner", err.getvalue())
        self.assertEqual(err.getvalue().count("Done:"), 2)

    def test_fast_metadata_is_quiet_but_slow_or_failed_metadata_is_visible(self):
        err = Stream()
        with patch("sys.stderr", err), patch("sys.stdout", Stream()), patch("cloud_experiments.progress.threading.Thread"), patch("cloud_experiments.progress.time.monotonic", return_value=0) as clock:
            with Activity("Fast", delay=0.5):
                pass
            self.assertEqual(err.getvalue(), "")
            with Activity("Slow", delay=0.5) as activity:
                clock.return_value = 1
                activity._tick()
            with self.assertRaises(Error), Activity("Bad", delay=0.5):
                raise Error("secret")
        self.assertIn("Slow", err.getvalue())
        self.assertIn("Failed: Bad", err.getvalue())

    def test_closed_progress_stream_does_not_fail_work(self):
        err = Mock()
        err.isatty.return_value = False
        err.write.side_effect = BrokenPipeError
        with patch("sys.stderr", err), Activity("Work"):
            pass

    def test_real_background_activity_emits_while_work_blocks_and_stops_after_exit(self):
        import threading
        written = threading.Event()

        class ObservedStream(Stream):
            def write(self, text):
                if " ..." in text:
                    written.set()
                return super().write(text)

        with patch("sys.stderr", ObservedStream()), patch("sys.stdout", Stream()):
            with Activity("Blocked work", interval=0.01) as activity:
                written.clear()
                self.assertTrue(written.wait(2))
            self.assertFalse(activity.thread.is_alive())

    def test_only_finite_numeric_stats_are_rendered(self):
        for value in (b"secret", b"[]", b'{"stats":"secret"}', b'{"stats":{"bytes":"secret","speed":NaN,"eta":true}}'):
            self.assertIsNone(transfer_stats(value))
        result = transfer_stats(json.dumps({"msg": "secret", "object": "secret", "stats": {**STATS, "eta": "secret"}}))
        self.assertNotIn("secret", result)
        self.assertIn("files 1/2", result)
        self.assertIsNone(transfer_stats(json.dumps({"stats": {"bytes": -1, "speed": 10**1000}})))
        unknown = transfer_stats('{"stats":{"bytes":0,"totalBytes":0}}')
        self.assertNotIn("100%", unknown)


class StreamingCommandTests(unittest.TestCase):
    def execute(self, code, **kwargs):
        return command([sys.executable, "-c", code], **kwargs)

    def test_streams_before_exit_preserves_both_pipes_and_filters_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / "continue"
            lines = []

            def received(line):
                lines.append(line)
                gate.touch()  # Child cannot complete until the callback is called.

            code = r"""
import os, sys, time
from pathlib import Path
assert not any(k.startswith(('HCLOUD_', 'RCLONE_')) for k in os.environ)
assert os.environ['GIT_TERMINAL_PROMPT'] == '0'
sys.stdout.buffer.write(b'x' * 200000)
sys.stdout.flush()
sys.stderr.buffer.write(b'{"stats":{"bytes":1}}\n')
sys.stderr.flush()
while not Path(sys.argv[1]).exists():
    time.sleep(0.01)
sys.stderr.buffer.write(b'private-error-without-newline')
"""
            with patch.dict(os.environ, {"HCLOUD_TOKEN": "secret", "RCLONE_CONFIG_PASS": "secret"}):
                result = command([sys.executable, "-c", code, str(gate)], stderr_line=received, timeout=3)
            self.assertEqual(result.stdout, b"x" * 200000)
            self.assertEqual(result.stderr, b'{"stats":{"bytes":1}}\nprivate-error-without-newline')
            self.assertEqual(lines, [b'{"stats":{"bytes":1}}\n', b'private-error-without-newline'])

    def test_large_and_fragmented_stderr_lines_are_bounded_without_losing_capture(self):
        lines = []
        code = r'''import os; os.write(2, b'x'*100000); os.write(2, b'\n'); os.write(2, b'{"stats":'); os.write(2, b'{"bytes":1}}\n')'''
        result = self.execute(code, stderr_line=lines.append)
        self.assertEqual(len(result.stderr), 100001 + len(b'{"stats":{"bytes":1}}\n'))
        self.assertEqual(lines, [b'{"stats":{"bytes":1}}\n'])

    def test_stdin_and_nonchecking_exit_status_are_preserved(self):
        result = self.execute("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)",
                              input=b"private input" * 10000, stderr_line=lambda line: None, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b"private input" * 10000)

    def test_failure_timeout_and_missing_program_are_safe(self):
        for code, timeout, expected in (("import sys; print('secret'); sys.stderr.write('secret'); sys.exit(7)", 2, "exit 7"),
                                        ("import time; time.sleep(60)", 0.1, "time limit")):
            with self.assertRaises(Error) as caught:
                self.execute(code, timeout=timeout, stderr_line=lambda line: None)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("secret", str(caught.exception).replace("secrets", ""))
        with self.assertRaises(Error):
            command(["/nonexistent-cloud-test-command"], stderr_line=lambda line: None)

    def test_keyboard_interrupt_kills_and_reaps_transfer(self):
        pid = None

        def interrupt(line):
            nonlocal pid
            pid = int(line)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.execute("import os, sys, time; print(os.getpid(), file=sys.stderr, flush=True); time.sleep(60)", stderr_line=interrupt)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class TransferTests(unittest.TestCase):
    def test_transfer_failure_displays_only_safe_statistics_and_safe_error(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(Stream()) as out, contextlib.redirect_stderr(Stream()) as err:
            storage = Storage(sample_config(tmp))
            code = "import sys; print('fake-secret'); sys.stderr.write('fake-secret\\n'); sys.stderr.write('{\"msg\":\"fake-secret\",\"stats\":{\"bytes\":1,\"totalBytes\":2}}\\n'); sys.exit(8)"

            def fake_rclone(argv, **kwargs):
                return command([sys.executable, "-c", code], **kwargs)

            with patch("cloud_experiments.providers.command", side_effect=fake_rclone), self.assertRaises(Error) as failure:
                storage.pull("test-run", Path(tmp) / "results")
            self.assertIn("exit 8", str(failure.exception))
            self.assertIn("50%", err.getvalue())
            self.assertIn("Failed: Downloading", err.getvalue())
            self.assertNotIn("fake-secret", err.getvalue() + out.getvalue() + str(failure.exception))
            self.assertNotIn("Done:", err.getvalue())

    def test_manifest_discovery_counts_all_entries_and_keeps_warnings_off_stdout(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(Stream()) as out, contextlib.redirect_stderr(Stream()) as err:
            c = sample_config(tmp)
            storage = Storage(c)
            entries = [{"Name": "test-run"}, {"Name": "../bad"}, {"Name": "unreadable"}]
            with patch.object(storage, "call", return_value=subprocess.CompletedProcess([], 0, json.dumps(entries).encode())), patch.object(storage, "manifest", side_effect=[sample_manifest(c), Error("secret")]), patch.object(Activity, "count") as count:
                manifests = list(storage.manifests())
            self.assertEqual(len(manifests), 1)
            self.assertEqual(count.call_count, 3)
            count.assert_called_with(3, 3)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("2 invalid or unreadable", err.getvalue())
            self.assertNotIn("secret", err.getvalue())

    def test_progress_flags_for_transfers_and_plain_capture_for_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(Stream()):
            storage = Storage(sample_config(tmp))
            for operation in ("copy", "copyto", "check", "cat", "lsjson"):
                with self.subTest(operation=operation), patch("cloud_experiments.providers.command") as call:
                    storage.call(operation, "fake-path", timeout=33)
                    args = call.call_args.args[0]
                    self.assertIn("--ask-password=false", args)
                    self.assertNotIn("--progress", args)
                    self.assertNotIn("-P", args)
                    self.assertEqual(call.call_args.kwargs["timeout"], 33)
                    if operation in ("copy", "copyto", "check"):
                        self.assertIn("--use-json-log", args)
                        self.assertEqual(args[args.index("--stats") + 1], "1s")
                        self.assertEqual(args[args.index("--stats-log-level") + 1], "NOTICE")
                        self.assertTrue(callable(call.call_args.kwargs["stderr_line"]))
                    else:
                        self.assertNotIn("--stats", args)
                        self.assertNotIn("stderr_line", call.call_args.kwargs)

    def test_pull_failure_or_interrupt_does_not_write_marker_or_success(self):
        for error in (Error("transfer failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as tmp:
                c = sample_config(tmp)
                destination = Path(c["local"]["results_dir"]) / "test-run"
                write_json(destination / "manifest.json", sample_manifest(c))
                storage = Mock()
                storage.pull.side_effect = error
                out, err = Stream(), Stream()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(type(error)):
                    cli.pull_run(storage, c, "test-run")
                self.assertFalse((destination / ".cloud-pulled.json").exists())
                self.assertNotIn("complete", err.getvalue())
                self.assertEqual(out.getvalue(), "")

    def test_pull_success_preserves_stdout_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = sample_config(tmp)
            destination = Path(c["local"]["results_dir"]) / "test-run"
            write_json(destination / "manifest.json", sample_manifest(c))
            out, err = Stream(), Stream()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.pull_run(Mock(), c, "test-run")
            self.assertTrue((destination / ".cloud-pulled.json").exists())
            self.assertEqual(out.getvalue(), str(destination) + "\n")
            self.assertIn("Download complete", err.getvalue())

    def test_empty_sync_reports_completion(self):
        with tempfile.TemporaryDirectory() as tmp, patch("cloud_experiments.cli.Storage") as storage, contextlib.redirect_stderr(Stream()) as err:
            storage.return_value.manifests.return_value = []
            cli.results(argparse.Namespace(operation="sync"), sample_config(tmp))
            self.assertIn("Sync complete: 0 downloaded, 0 unchanged.", err.getvalue())

    def test_failed_sync_does_not_report_completion(self):
        with tempfile.TemporaryDirectory() as tmp, patch("cloud_experiments.cli.Storage") as storage, patch("cloud_experiments.cli.pull_run", side_effect=Error("failed")), contextlib.redirect_stderr(Stream()) as err:
            c = sample_config(tmp)
            storage.return_value.manifests.return_value = [sample_manifest(c)]
            with self.assertRaises(Error):
                cli.results(argparse.Namespace(operation="sync"), c)
            self.assertNotIn("Sync complete", err.getvalue())

    def test_ssh_retry_failure_is_reported_as_failed_stage(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(Stream()) as err:
            ssh = SSH(server(), tmp)
            with patch.object(ssh, "call", side_effect=[Error("unavailable"), None]) as call, patch("cloud_experiments.providers.time.sleep"):
                ssh.wait()
                self.assertEqual(call.call_count, 2)
            with patch.object(ssh, "call"):
                with self.assertRaises(Error):
                    ssh.wait(seconds=0)
            self.assertIn("Failed: Waiting for SSH", err.getvalue())

    @unittest.skipUnless(shutil.which("rclone"), "rclone unavailable")
    def test_real_rclone_local_only_copy_check_and_captured_manifest(self):
        # Explicit empty config and absolute local paths: no remote or credentials.
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(Stream()) as err:
            root = Path(tmp)
            c = sample_config(root)
            Path(c["local"]["rclone_config"]).write_text("")
            source, destination = root / "input", root / "output"
            write_json(source / "manifest.json", {"test": True})
            storage = Storage(c)
            storage.call("copy", str(source), str(destination))
            storage.call("check", str(source), str(destination), "--one-way")
            result = storage.call("cat", str(destination / "manifest.json"))
            self.assertEqual(json.loads(result.stdout), {"test": True})
            self.assertIn("100%", err.getvalue())
            self.assertIn("checks 1/1", err.getvalue())
            self.assertNotIn("manifest.json", err.getvalue())


if __name__ == "__main__":
    unittest.main()
