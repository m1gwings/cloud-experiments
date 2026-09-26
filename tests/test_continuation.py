"""Logical studies, environment locks, provider leases and filesystem restore: offline only."""

import base64
import contextlib
import io
import json
import lzma
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from test_core import sample_config, sample_manifest, server
from cloud_experiments import bootstrap, cli, environment, persistence, studies, worker
from cloud_experiments.common import Error, managed_server, read_json, remote_path, sha256, write_json
from cloud_experiments.providers import Hetzner, Storage


def env_lock():
    return {"schema_version": 1, "runtime": {"python": "3.12.3", "implementation": "CPython",
            "abi": "cpython-312-x86_64-linux-gnu", "system": "Linux", "machine": "x86_64",
            "byteorder": "little", "pointer_bits": 64, "libc": ["glibc", "2.39"]},
            "packages": {"numpy": "2.2.0", "pip": "25.0"},
            "local_projects": {"experiments-wo-stress": {"source": "ews", "version": "0.1.0"}}}


def study_manifest(config, study=None):
    m = sample_manifest(config)
    sid = study or studies.study_id(m["experiment"]["repository"], m["config"]["path"])
    m.update(study_id=sid, run_id=studies.attempt_id(sid), request_key="e" * 64)
    return m


class IdentityTests(unittest.TestCase):
    def test_study_identity_is_stable_across_git_transport_and_attempt_settings(self):
        sid = studies.study_id("git@github.com:owner/repo.git", "configs/grid.yml")
        self.assertEqual(sid, studies.study_id("https://github.com/owner/repo", "configs/grid.yml"))
        self.assertNotEqual(sid, studies.study_id("https://github.com/owner/repo", "other.yml"))
        self.assertNotEqual(sid, studies.study_id("https://github.com/owner/repo", "configs/grid.yml", fresh=True))
        first, second = studies.attempt_id(sid), studies.attempt_id(sid)
        self.assertNotEqual(first, second)
        self.assertEqual(studies.attempt_study(first), sid)
        m = study_manifest(sample_config('/tmp'))
        original = studies.request_key(m, {"source": "bytes"})
        m.update(machine="cpx52", max_runtime_hours=6)
        self.assertEqual(studies.request_key(m, {"source": "bytes"}), original)
        self.assertNotEqual(studies.request_key(m, {"source": "changed"}), original)
        m["ews"]["commit"] = "f" * 40
        self.assertNotEqual(studies.request_key(m, {"source": "bytes"}), original)

    def test_remote_paths_separate_studies_attempts_and_legacy(self):
        m = study_manifest(sample_config('/tmp'))
        root = remote_path(m['storage'], m['study_id'])
        self.assertIn('/studies/', root)
        self.assertEqual(remote_path(m['storage'], m['run_id']), root + '/attempts/' + m['run_id'])
        self.assertTrue(remote_path(m['storage'], 'legacy-run').endswith('/runs/legacy-run'))

    def test_state_history_rejects_forks_missing_parents_and_delayed_competing_writes(self):
        sid = studies.study_id('https://github.com/a/b', 'c.yml')
        def commit(parent=None):
            return dict(schema_version=1, study_id=sid, attempt_id=studies.attempt_id(sid), parent=parent, inventory_sha256='a'*64)
        a = commit()
        b = commit(a['attempt_id'])
        self.assertEqual(studies.state_head(sid, [b, a]), b)
        for records in ([a, b, commit(a['attempt_id'])], [b], [a, commit()], [a, a]):
            with self.subTest(records=records), self.assertRaises(Error):
                studies.state_head(sid, records)


class EnvironmentTests(unittest.TestCase):
    def test_recreatable_pins_exclude_editable_source_and_validate_exact_runtime(self):
        lock = env_lock()
        self.assertEqual(environment.constraints(lock), 'numpy==2.2.0\npip==25.0\n')
        environment.verify(lock, json.loads(json.dumps(lock)))
        for section, key, changed in [('packages', 'numpy', '2.3.0'), ('runtime', 'python', '3.13.0'),
                                      ('runtime', 'machine', 'aarch64'), ('runtime', 'libc', ['glibc', '9'])]:
            actual = json.loads(json.dumps(lock))
            actual[section][key] = changed
            with self.subTest(key=key), self.assertRaisesRegex(Error, 'differs'):
                environment.verify(lock, actual)
        for version in ['1\n--index-url=https://secret.invalid', 'git+https://example.invalid/repo', '/tmp/local']:
            lock['packages']['numpy'] = version
            with self.assertRaises(Error):
                environment.constraints(lock)

    def test_new_archived_source_version_is_left_to_ews_compatibility(self):
        actual = env_lock()
        actual['local_projects']['experiments-wo-stress']['version'] = '0.2.0'
        environment.verify(env_lock(), actual)
        actual['local_projects']['unknown'] = {'source': 'experiment', 'version': '1'}
        with self.assertRaises(Error):
            environment.verify(env_lock(), actual)

    def test_capture_has_no_raw_freeze_or_environment_dump(self):
        self.assertNotIn('os.environ', environment.CAPTURE)
        self.assertNotIn('pip freeze', environment.CAPTURE)
        self.assertIn('Unsupported direct/local dependency', environment.CAPTURE)

    def test_capture_extracts_index_versions_and_rejects_unrecreatable_origins(self):
        class Distribution:
            metadata = {'Name': 'Numpy'}
            version = '2.2.0'
            origin = None
            def read_text(self, name):
                return json.dumps(self.origin) if self.origin else None
        dist = Distribution()
        out = io.StringIO()
        with patch('importlib.metadata.distributions', return_value=[dist]), contextlib.redirect_stdout(out):
            exec(environment.CAPTURE, {})
        self.assertEqual(environment.decode(out.getvalue())['packages'], {'numpy': '2.2.0'})
        dist.origin = {'url': 'file:///work/source', 'dir_info': {'editable': True}}
        with patch('importlib.metadata.distributions', return_value=[dist]), contextlib.redirect_stdout(io.StringIO()) as out:
            exec(environment.CAPTURE, {})
        self.assertEqual(environment.decode(out.getvalue())['local_projects']['numpy']['source'], 'experiment')
        dist.origin = {'url': 'https://secret.invalid/private.whl'}
        with patch('importlib.metadata.distributions', return_value=[dist]), self.assertRaises(SystemExit) as error:
            exec(environment.CAPTURE, {})
        self.assertNotIn('secret.invalid', str(error.exception))


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.config = sample_config(self.root)
        self.index = {'config.yml': {'sha256': 'a'*64, 'mode': 0o644}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('cloud_experiments.source.git', return_value=str(self.repo)))
        self.stack.enter_context(patch('cloud_experiments.cli.resolve_ref', return_value='c'*40))
        def snapshot(cwd, arg, dest, dirty):
            dest.write_bytes(b'archive')
            (dest.parent/'experiment-config').write_text('seed: 1')
            return {'repository': 'https://github.com/owner/science.git', 'commit': 'b'*40, 'dirty': False}, 'config.yml', self.index
        self.stack.enter_context(patch('cloud_experiments.cli.snapshot', side_effect=snapshot))
        self.cloud = self.stack.enter_context(patch('cloud_experiments.cli.Hetzner')).return_value
        self.cloud.find.return_value = None
        self.storage = self.stack.enter_context(patch('cloud_experiments.cli.Storage')).return_value
        self.storage.study_state.return_value = None
        self.storage.studies.return_value = []
        self.launch = self.stack.enter_context(patch('cloud_experiments.cli.launch'))
        self.out = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def run_command(self, *args):
        cli.run_command(cli.parser('cloud-run').parse_args(['config.yml', *args]), self.config)
        return self.launch.call_args.args[1] if self.launch.called else None

    def test_repeated_source_with_changed_machine_runtime_restores_same_study(self):
        first = self.run_command('--machine', 'cpx32', '--max-runtime', '2')
        self.storage.study_state.return_value = {'completed': False, 'attempt_id': first['run_id'], 'counts': {'completed': 83}}
        second = self.run_command('--machine', 'cpx52', '--max-runtime', '6')
        self.assertEqual(first['study_id'], second['study_id'])
        self.assertEqual(first['request_key'], second['request_key'])
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertIn('83 completed runs', self.out.getvalue())

    def test_exact_completed_request_avoids_compute_but_changed_source_launches(self):
        first = self.run_command()
        self.storage.study_state.return_value = {'completed': True, 'request_key': first['request_key'], 'counts': {'completed': 9}}
        self.launch.reset_mock()
        self.run_command('--machine', 'cpx52')
        self.launch.assert_not_called()
        self.assertIn('No compute created', self.out.getvalue())
        self.index['new-scientific-module.py'] = {'sha256': 'd'*64, 'mode': 0o644}
        changed = self.run_command()
        self.assertEqual(changed['study_id'], first['study_id'])
        self.assertNotEqual(changed['request_key'], first['request_key'])

    def test_fresh_lineage_is_independent_and_can_be_explicitly_continued(self):
        normal = self.run_command()
        fresh = self.run_command('--fresh')
        self.assertNotEqual(normal['study_id'], fresh['study_id'])
        again = self.run_command('--study', fresh['study_id'])
        self.assertEqual(fresh['study_id'], again['study_id'])

    def test_active_study_reports_existing_attempt_without_mutation(self):
        self.cloud.find.return_value = {'labels': {'run-id': 'active-attempt'}}
        self.run_command()
        self.launch.assert_not_called()
        self.storage.study_state.assert_not_called()
        self.assertIn('active-attempt', self.out.getvalue())


class LeaseTests(unittest.TestCase):
    def test_worker_rejects_deleted_or_reassigned_lease_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = study_manifest(sample_config(tmp))
            m['server_id'] = 123
            w = worker.Worker(tmp, Path(tmp)/'work')
            write_json(Path(tmp)/'credentials.json', {'HCLOUD_WORKER_TOKEN': 'fake'})
            bad = server(name=m['study_id'], labels={'managed-by': 'cloud-experiments',
                         'study-id': m['study_id'], 'run-id': studies.attempt_id(m['study_id'])})
            for response in (None, {'server': bad}):
                with patch('cloud_experiments.worker.own_server_id', return_value=123), patch('cloud_experiments.worker.http_json', return_value=response), self.assertRaises(Error):
                    w.verify_lease(m)

    def test_unique_provider_name_is_study_and_ownership_is_attempt_specific(self):
        m = study_manifest(sample_config('/tmp'))
        owned = server(name=m['study_id'], labels={'managed-by': 'cloud-experiments', 'study-id': m['study_id'], 'run-id': m['run_id']})
        self.assertTrue(managed_server(owned, m['run_id']))
        rival = studies.attempt_id(m['study_id'])
        self.assertFalse(managed_server(owned, rival))
        cloud = Hetzner(sample_config('/tmp')['hetzner'])
        with patch.object(cloud, 'call', return_value=Mock(stdout=json.dumps({'server': owned}))) as call:
            cloud.create(m, 'bootstrap')
            args = call.call_args.args
            self.assertEqual(args[args.index('--name')+1], m['study_id'])
            self.assertIn('run-id='+m['run_id'], args)
        with patch.object(cloud, 'call', return_value=Mock(stdout=json.dumps([owned]))):
            self.assertEqual(cloud.find(m['study_id']), owned)
        with patch.object(cloud, 'call', return_value=Mock(stdout='[]')):
            self.assertIsNone(cloud.find(m['study_id']))  # Deleted VM releases the provider lease.
        with self.assertRaises(Error):
            cloud.delete(owned, rival)

    def test_collision_cleanup_never_targets_winning_attempt(self):
        from test_lifecycle import LaunchTests
        case = LaunchTests('test_launch_transfers_snapshot_and_starts_supervisor')
        case.setUp()
        try:
            case.m = study_manifest(case.c)
            case.cloud.create.side_effect = Error('name already exists')
            case.cloud.find.return_value = None  # This losing attempt owns no VM.
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Error):
                cli.launch(case.c, case.m, case.directory)
            case.cloud.find.assert_called_once_with(case.m['run_id'])
            case.cloud.delete.assert_not_called()
            case.ssh.call.assert_not_called()
        finally:
            case.doCleanups()


@unittest.skipUnless(shutil.which('rclone'), 'local rclone unavailable')
class StateRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = sample_config(self.root)
        self.empty = self.root / 'empty.conf'
        self.empty.write_text('')
        self.remote = self.root / 'objects'
        self.m = study_manifest(self.config)
        self.m.update(status='timeout', exit_code=None, parent_state=None)
        self.w = self.make_worker('first', self.m)

    def call(self, *args, **kwargs):
        prefix = 'test:test-bucket'
        mapped = [str(self.remote)+a[len(prefix):] if isinstance(a, str) and a.startswith(prefix) else a for a in args]
        result = subprocess.run(['rclone', '--config', str(self.empty), *mapped], capture_output=True, timeout=20)
        if result.returncode:
            raise Error('local fake storage failed: '+result.stderr.decode())
        return result

    def make_worker(self, name, manifest):
        base, work = self.root / name / 'base', self.root / name / 'work'
        base.mkdir(parents=True)
        (work / 'output').mkdir(parents=True)
        w = worker.Worker(base, work)
        w.rclone = self.call
        w.verify_lease = Mock()
        w.save(manifest)
        return w

    def persist(self, status='timeout'):
        output = self.w.work / 'output'
        write_json(output / 'metadata.json', {'schema_version': 2})
        write_json(output / 'runs/run/checkpoints/one/state.json', {'rng': 123, 'step': 500})
        (output / 'runs/run/checkpoints/one/arrays.npz').write_bytes(b'exact-binary-checkpoint')
        write_json(output / 'artifacts.json', {'schema': 'experiments-wo-stress/artifacts', 'schema_version': 1,
            'artifacts': {'figures': {'path': 'renamed-charts', 'kind': 'directory', 'optional': True}}})
        (output / 'renamed-charts').mkdir()
        (output / 'renamed-charts/figure.pdf').write_bytes(b'plot')
        write_json(self.w.base / 'environment-ready.json', env_lock())
        self.m.update(status=status)
        persistence.collect_state(self.w, self.m)
        self.w.save(self.m)
        self.w.upload(self.m, final=True)
        persistence.publish_state(self.w, self.m)

    def test_timeout_and_cancellation_restore_complete_checkpoints_on_next_machine(self):
        self.persist('timeout')
        next_m = study_manifest(self.config, self.m['study_id'])
        next_m['machine'] = 'cpx52'
        next_m['max_runtime_hours'] = 6
        second = self.make_worker('second', next_m)
        persistence.restore(second, next_m)
        self.assertEqual(persistence.inventory(second.work/'output'), persistence.inventory(self.w.work/'output'))
        self.assertEqual(next_m['parent_state'], self.m['run_id'])
        next_m.update(status='cancelled', exit_code=None)
        write_json(second.base/'environment-ready.json', env_lock())
        persistence.collect_state(second, next_m)
        second.save(next_m)
        second.upload(next_m, final=True)
        persistence.publish_state(second, next_m)
        head, _ = persistence.read_head(second, next_m)
        self.assertEqual(head['attempt_id'], next_m['run_id'])
        self.assertFalse(head['completed'])
        third_m = study_manifest(self.config, self.m['study_id'])
        third = self.make_worker('third', third_m)
        persistence.restore(third, third_m)
        self.assertEqual(persistence.inventory(third.work/'output'), persistence.inventory(second.work/'output'))

    def test_corrupt_restored_bytes_are_rejected_before_execution(self):
        self.persist()
        source = self.remote/'studies'/self.m['study_id']/'attempts'/self.m['run_id']/'artifacts/output'
        (source/'runs/run/checkpoints/one/arrays.npz').write_bytes(b'corrupt')
        m = study_manifest(self.config, self.m['study_id'])
        w = self.make_worker('bad', m)
        with self.assertRaisesRegex(Error, 'SHA-256'):
            persistence.restore(w, m)

    def test_study_semantic_selectors_use_only_current_committed_catalog(self):
        self.persist()
        storage = Storage(self.config)
        storage.call = self.call
        destination = self.root/'selected'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.pull_selection(storage, self.config, self.m['study_id'], destination=destination, role='figures')
        paths = list(destination.rglob('figure.pdf'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), b'plot')
        self.assertFalse((destination/'.cloud-pulled.json').exists())
        archive = self.root/'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            cli.pull_run(storage, self.config, self.m['study_id'], archive)
        self.assertTrue((archive/'.cloud-pulled.json').exists())
        self.assertTrue(list(archive.rglob('arrays.npz')))

    def test_environment_is_persisted_and_reloaded_without_credentials(self):
        with patch.object(self.w, 'capture_environment', return_value=env_lock()):
            self.w.seal_environment(self.m, None)
        # Establish the attempt prefix that laptop preflight creates before a VM exists.
        self.w.upload(self.m)
        _, locked = persistence.read_head(self.w, self.m)
        self.assertEqual(locked, env_lock())
        actual = env_lock()
        actual['packages']['numpy'] = '999'
        with patch.object(self.w, 'capture_environment', return_value=actual), self.assertRaises(Error):
            self.w.seal_environment(self.m, locked)

    def test_failed_environment_publication_never_marks_state_ready(self):
        with patch.object(self.w, 'capture_environment', return_value=env_lock()), patch.object(self.w, 'rclone', side_effect=Error('offline')), self.assertRaises(Error):
            self.w.seal_environment(self.m, None)
        self.assertFalse((self.w.base/'environment-ready.json').exists())
        write_json(self.w.work/'output/metadata.json', {})
        persistence.collect_state(self.w, self.m)
        self.assertNotIn('state_inventory_sha256', self.m)

    def test_only_successful_complete_ews_request_can_publish_completed_shortcut(self):
        self.m.update(exit_code=0, ews_counts=dict(completed=9, pending=0, running=0, paused=0, failed=0, corrupt=0))
        self.persist('completed')
        head, _ = persistence.read_head(self.w, self.m)
        self.assertTrue(head['completed'])
        history = Storage(self.config)
        history.call = self.call
        with patch('cloud_experiments.cli.Storage', return_value=history), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.results(cli.parser('cloud-results').parse_args(['list', '--attempts', self.m['study_id']]), self.config)
        self.assertIn(self.m['run_id'], out.getvalue())

    def test_secret_like_and_linked_state_cannot_be_silently_excluded(self):
        output = self.w.work/'output'
        linked = output/'linked'
        linked.symlink_to(self.empty)
        with self.assertRaises(Error):
            persistence.inventory(output)
        linked.unlink()
        (output/'.env').write_text('FAKE_SECRET=never-upload')
        with self.assertRaises(Error):
            persistence.inventory(output)

    def test_reproduce_uses_archived_lock_and_independent_output_lineage(self):
        import tarfile
        config_path = self.root/'config.yml'
        config_path.write_text('seed: 1')
        archive = self.w.out/'source/source.tar.gz'
        archive.parent.mkdir(parents=True)
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(config_path, arcname='config.yml')
        self.m['source']['sha256'] = sha256(archive)
        self.m['config']['sha256'] = sha256(config_path)
        write_json(self.w.out/'machine/environment.json', env_lock())
        self.persist()
        storage = Storage(self.config)
        storage.call = self.call
        with patch('cloud_experiments.cli.Storage', return_value=storage), patch('cloud_experiments.cli.launch') as launch, contextlib.redirect_stdout(io.StringIO()):
            cli.reproduce(cli.parser('cloud-reproduce').parse_args([self.m['study_id']]), self.config)
        new = launch.call_args.args[1]
        self.assertNotEqual(new['study_id'], self.m['study_id'])
        self.assertEqual(new['ews'], self.m['ews'])
        out = launch.call_args.args[2]/'out'
        self.assertEqual(read_json(out/'machine/environment.json'), env_lock())
        self.assertEqual(sha256(out/'machine/environment.json'), new['reproduction_environment_sha256'])
        self.assertFalse((out/'artifacts/output').exists())


class WorkerSetupTests(unittest.TestCase):
    """Exercise install ordering and final package verification without apt/pip or a VM."""

    def setUp(self):
        import tarfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base, self.work = self.root/'base', self.root/'work'
        self.base.mkdir()
        self.work.mkdir()
        self.m = study_manifest(sample_config(self.root))
        incoming = self.base/'incoming'
        incoming.mkdir()
        with tarfile.open(incoming/'source.tar.gz', 'w:gz') as tar:
            data = b'seed: 1\n'
            info = tarfile.TarInfo('config.yml')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        self.m['source']['sha256'] = sha256(incoming/'source.tar.gz')
        write_json(incoming/'index.json', {})
        write_json(self.base/'credentials.json', {'HCLOUD_WORKER_TOKEN': 'fake'})
        self.commands = []
        def run(argv, **kwargs):
            self.commands.append(argv)
            raw = b''
            if argv[-2:] == ['rev-parse', 'HEAD']:
                raw = self.m['ews']['commit'].encode()
            if argv == ['systemctl', 'start', 'cloud-experiment.service']:
                write_json(self.work/'runtime/exit.json', {'exit_code': 0})
            return subprocess.CompletedProcess(argv, 0, raw, b'')
        self.w = worker.Worker(self.base, self.work, run)
        self.w.save(self.m)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('cloud_experiments.worker.own_server_id', return_value=123))
        stack.enter_context(patch('cloud_experiments.worker.notify'))
        self.restore = stack.enter_context(patch('cloud_experiments.persistence.restore', return_value=env_lock()))
        self.capture = stack.enter_context(patch.object(self.w, 'capture_environment', return_value=env_lock()))
        stack.enter_context(patch.object(self.w, 'verify_lease'))
        stack.enter_context(patch.object(self.w, 'upload'))
        self.remote = None
        def rclone(*args, **kwargs):
            if args[0] == 'copyto':
                self.remote = Path(args[1]).read_bytes()
            return subprocess.CompletedProcess([], 0, self.remote, b'')
        stack.enter_context(patch.object(self.w, 'rclone', side_effect=rclone))

    def test_locked_setup_pins_packages_then_verifies_environment_before_ews(self):
        self.w.supervise()
        self.restore.assert_called_once()
        self.assertEqual(self.capture.call_count, 2)  # Runtime before install, full lock afterward.
        installs = [a for a in self.commands if 'pip' in a and 'install' in a]
        self.assertTrue(all('--constraint' in a for a in installs))
        self.assertIn('--only-binary=:all:', installs[0])
        self.assertEqual(read_json(self.work/'runtime/command.json')[-1], '--portable')
        self.assertEqual(read_json(self.base/'reason.json')['status'], 'completed')
        self.assertTrue((self.base/'environment-ready.json').exists())

    def test_incompatible_runtime_stops_before_package_install_and_checkpoint_loading(self):
        changed = env_lock()
        changed['runtime']['python'] = '3.13.0'
        self.capture.return_value = changed
        self.w.supervise()
        self.assertEqual(read_json(self.base/'reason.json')['status'], 'setup_failed')
        self.assertFalse(any('pip' in a and 'install' in a for a in self.commands))
        self.assertFalse((self.base/'environment-ready.json').exists())
        self.assertFalse((self.work/'runtime/command.json').exists())

    def test_package_drift_after_install_is_rejected_before_start(self):
        changed = env_lock()
        changed['packages']['numpy'] = '3.0.0'
        self.capture.side_effect = [env_lock(), changed]
        self.w.supervise()
        self.assertEqual(read_json(self.base/'reason.json')['status'], 'setup_failed')
        self.assertFalse((self.base/'started').exists())


class TimeoutTests(unittest.TestCase):
    def test_pty_wrapper_signals_only_recorded_child_and_retains_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work/'runtime').mkdir()
            write_json(work/'runtime/command.json', ['ews', 'run', 'config.yml', '--portable'])
            write_json(work/'runtime/child-pid.json', {'pid': 12345})
            (work/'runtime/portable').touch()
            (work/'runtime/stop-requested').touch()
            signalled = threading.Event()
            def wait_child(*args, **kwargs):
                self.assertTrue(signalled.wait(3), 'coordinator did not receive graceful signal')
                return subprocess.CompletedProcess([], 130)
            with patch('cloud_experiments.worker.os.kill', side_effect=lambda *args: signalled.set()) as kill, patch('cloud_experiments.worker.subprocess.run', side_effect=wait_child):
                worker.execute(work)
            kill.assert_called_once_with(12345, signal.SIGINT)
            self.assertEqual(read_json(work/'runtime/exit.json')['exit_code'], 130)

    def test_graceful_stop_waits_for_exit_and_forced_fallback_is_bounded(self):
        from test_lifecycle import WorkerTests
        case = WorkerTests('test_success_failure_cancel_timeout_all_delete_after_upload')
        case.setUp()
        try:
            (case.base/'started').touch()
            (case.work/'runtime').mkdir()
            with patch('cloud_experiments.worker.time.monotonic', side_effect=[0, 0, 91]), patch('cloud_experiments.worker.time.sleep'):
                case.w.interrupt_experiment()
            self.assertTrue((case.work/'runtime/stop-requested').exists())
            write_json(case.work/'runtime/exit.json', {'exit_code': 130})
            with patch('cloud_experiments.worker.time.sleep') as sleep:
                case.w.interrupt_experiment()
                sleep.assert_not_called()
            case.finalize('timeout', upload_error=Error('fake failure'))
            self.assertEqual(case.commands[-1][-1], 'cloud-delete.service')
        finally:
            case.doCleanups()

    def test_bootstrap_contains_complete_importable_bundle_and_independent_reaper(self):
        manifest = study_manifest(sample_config('/tmp'))
        data = bootstrap.render(manifest, {'HCLOUD_WORKER_TOKEN': 'fake-token'}, 'fake-rclone')
        self.assertLess(len(data.encode()), 32768)
        files = {f['path']: base64.b64decode(f['content']) for f in json.loads(data.split('\n',1)[1])['write_files']}
        with zipfile.ZipFile(io.BytesIO(lzma.decompress(files['/opt/cloud-experiments/code.zip.xz']))) as bundle:
            for name in bundle.namelist():
                compile(bundle.read(name), name, 'exec')
            self.assertIn('cloud_experiments/persistence.py', bundle.namelist())
        self.assertIn(b'cloud-delete.service', files['/etc/systemd/system/cloud-reap.timer'])
        self.assertIn(b'12min', files['/etc/systemd/system/cloud-finalize.service'])


if __name__ == '__main__':
    unittest.main()
