"""Incremental recovery, VM loss and failure ordering with local fake storage."""

import hashlib
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_core import sample_config
from test_continuation import env_lock, study_manifest
from cloud_experiments import ews_contract, persistence, physical, studies, worker
from cloud_experiments.common import Error, read_json, remote_path, sha256, write_json


def sealed_snapshot(output, destination, *, pruned=()):
    """Fake EWS public snapshot producer; cloud tests see only its sealed inventory."""
    output, destination = Path(output), Path(destination)
    destination.mkdir()
    shutil.copytree(output, destination / 'output')
    files = {path.relative_to(output).as_posix(): {'sha256': sha256(path), 'size': path.stat().st_size}
             for path in output.rglob('*') if path.is_file()}
    directories = sorted(path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_dir())
    document = {**ews_contract.CONTRACT, 'files': files, 'directories': directories,
                'omitted_checkpoints': [], 'pruned_runs': list(pruned)}
    document['snapshot_id'] = ews_contract.fingerprint(document)
    write_json(destination / 'recovery.json', document)
    return document


class LocalStorage:
    """Exact filesystem rclone effects, including failures at publication boundaries."""

    def __init__(self, root):
        self.root = Path(root)
        self.events = []
        self.uploads = []
        self.fail = None

    def path(self, value):
        value = str(value)
        return self.root / value[len('test:test-bucket/'): ] if value.startswith('test:test-bucket/') else Path(value)

    def __call__(self, *args, **kwargs):
        self.events.append(args)
        if self.fail and self.fail(args):
            raise Error('injected storage interruption')
        operation = args[0]
        source = self.path(args[1])
        data = b''
        if operation == 'lsjson':
            if '--stat' in args:
                entry = {'Path': source.name, 'IsDir': False} if source.is_file() else {'Path': '', 'IsDir': True}
                data = json.dumps(entry).encode()
            else:
                data = json.dumps([{'Path': p.relative_to(source).as_posix(), 'Name': p.name,
                                    'IsDir': False, 'Size': p.stat().st_size}
                                   for p in sorted(source.rglob('*')) if p.is_file()]).encode()
        elif operation == 'cat':
            data = source.read_bytes()
        elif operation == 'copyto':
            destination = self.path(args[2])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        elif operation == 'copy':
            destination = self.path(args[2])
            paths = ([source / name for name in Path(args[args.index('--files-from') + 1]).read_text().splitlines()]
                     if '--files-from' in args else source.rglob('*'))
            for path in paths:
                if path.is_file():
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                    if str(args[2]).endswith(('/blobs', '/packs')):
                        self.uploads.append((path.name, path.stat().st_size))
        elif operation == 'check':
            destination = self.path(args[2])
            for path in source.rglob('*'):
                if path.is_file() and path.read_bytes() != (destination / path.relative_to(source)).read_bytes():
                    raise Error('download verification mismatch')
        elif operation == 'deletefile':
            source.unlink(missing_ok=True)
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, data, b'')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = sample_config(self.root)
        self.storage = LocalStorage(self.root / 'objects')
        self.manifest = study_manifest(self.config)
        self.manifest.update(status='running', parent_state=None, tooling={'version': '0.6.0',
                             'git_commit': 'f' * 40, 'implementation_sha256': '9' * 64})
        self.manifest['ews']['cloud_contract'] = dict(ews_contract.CONTRACT)
        self.worker = self.new_worker('vm-one', self.manifest)
        self.output = self.worker.work / 'output'
        write_json(self.output / 'metadata.json', {'revision': 1})
        self.large = b'unchanging retained payload' * 5000
        (self.output / 'durable.bin').write_bytes(self.large)
        self.create = patch.object(ews_contract, 'create_snapshot', side_effect=lambda w, src, dst, **kwargs: sealed_snapshot(src, dst))
        self.snapshot_api = self.create.start()
        self.addCleanup(self.create.stop)
        self.restore_api = patch.object(ews_contract, 'restore_snapshot', side_effect=self.restore_bytes)
        self.restore_api.start()
        self.addCleanup(self.restore_api.stop)

    def new_worker(self, name, manifest):
        base, work = self.root / name / 'base', self.root / name / 'work'
        base.mkdir(parents=True)
        work.mkdir()
        result = worker.Worker(base, work)
        result.rclone = self.storage
        result.verify_lease = Mock()
        write_json(base / 'environment-ready.json', env_lock())
        result.save(manifest)
        return result

    def restore_bytes(self, w, snapshot, output):
        recovery = ews_contract.validate_manifest(read_json(Path(snapshot) / 'recovery.json'))
        for name, descriptor in recovery['files'].items():
            self.assertEqual(sha256(Path(snapshot) / 'output' / name), descriptor['sha256'])
        shutil.copytree(Path(snapshot) / 'output', output)
        return recovery

    def sync(self, final=False):
        return persistence.sync(self.worker, self.manifest, final=final)

    def restored(self, name='replacement'):
        manifest = study_manifest(self.config, self.manifest['study_id'])
        replacement = self.new_worker(name, manifest)
        persistence.restore(replacement, manifest)
        return replacement, manifest

    def remote_root(self):
        return self.storage.path(remote_path(self.manifest['storage'], self.manifest['study_id']))

    def layout(self, commit):
        root = remote_path(self.manifest['storage'], self.manifest['study_id'])
        recovery = persistence.read_snapshot(self.storage, root, commit)
        return persistence.read_layout(self.storage, root, commit, recovery)

    def loose_legacy_head(self):
        """Construct a genuine descriptor-free schema 2 head for mixed-lineage tests."""
        commit = self.sync()
        root = self.remote_root()
        recovery = persistence.read_snapshot(self.storage,
                    remote_path(self.manifest['storage'], self.manifest['study_id']), commit)
        loose = root / 'blobs'
        loose.mkdir(exist_ok=True)
        for name, entry in recovery['files'].items():
            (loose / entry['sha256']).write_bytes((self.output / name).read_bytes())
        commit.pop('layout_sha256')
        write_json(root / 'commits' / (commit['commit_id'] + '.json'), commit)
        return commit

    def test_periodic_sync_transfers_only_new_changed_payloads(self):
        first = self.sync()
        self.assertEqual(len(self.storage.uploads), 2)
        self.storage.uploads.clear()
        write_json(self.output / 'metadata.json', {'revision': 2})
        second = self.sync()
        self.assertEqual(len(self.storage.uploads), 1)
        self.assertTrue(self.storage.uploads[0][0].endswith('.tar'))
        self.assertEqual(second['parent'], first['commit_id'])
        self.assertNotEqual(second['commit_id'], first['commit_id'])
        root = remote_path(self.manifest['storage'], self.manifest['study_id'])
        self.assertFalse(any(event[0] == 'lsjson' and event[1] in (root, root + '/blobs')
                             for event in self.storage.events))
        self.assertEqual(second['attempt_id'], first['attempt_id'])
        self.assertTrue(any(event[0] == 'check' and '--download' in event for event in self.storage.events))
        self.assertEqual(self.manifest['last_recovery']['commit_id'], second['commit_id'])

    def test_new_layout_packs_small_and_reuses_parent_locations(self):
        first = self.sync()
        first_layout = self.layout(first)
        self.assertEqual(len(first_layout['loose']), 1)
        self.assertEqual(len(first_layout['packs']), 1)
        self.assertEqual(first['transfer']['physical_objects_written'], 2)
        self.storage.uploads.clear()
        second = self.sync()
        self.assertEqual(self.storage.uploads, [])
        self.assertEqual(self.layout(second), first_layout)
        self.assertEqual(second['transfer']['physical_objects_written'], 0)
        self.assertEqual(second['request_key'], first['request_key'])

    def test_descriptor_free_legacy_head_restores_and_new_head_mixes_locations(self):
        first = self.loose_legacy_head()
        self.assertEqual(self.layout(first)['loose'], physical.inventory(
            persistence.read_snapshot(self.storage, remote_path(self.manifest['storage'], self.manifest['study_id']), first)).keys())
        replacement, _ = self.restored()
        self.assertEqual((replacement.work / 'output/durable.bin').read_bytes(), self.large)
        (self.output / 'new-small.txt').write_bytes(b'new tiny file')
        self.storage.uploads.clear()
        second = self.sync()
        self.assertEqual(second['parent'], first['commit_id'])
        layout = self.layout(second)
        self.assertIn(sha256(self.output / 'durable.bin'), layout['loose'])
        self.assertIn(sha256(self.output / 'metadata.json'), layout['loose'])
        self.assertEqual(len(layout['packs']), 1)
        self.assertEqual(len(self.storage.uploads), 1)
        self.assertTrue(self.storage.uploads[0][0].endswith('.tar'))
        restored, _ = self.restored('mixed-replacement')
        self.assertEqual((restored.work / 'output/new-small.txt').read_bytes(), b'new tiny file')

    def test_interrupted_pack_boundaries_never_advance_head(self):
        for operation, fragment, stage in [('copy', '/packs', 'recovery.upload'),
                                           ('check', '/packs', 'recovery.verify'),
                                           ('copyto', 'layout-', 'recovery.publish_layout')]:
            with self.subTest(stage=stage):
                first = self.sync()
                (self.output / 'new-small.txt').write_bytes(stage.encode())
                self.storage.fail = lambda args: args[0] == operation and fragment in str(args)
                with self.assertRaisesRegex(Error, 'interruption'):
                    self.sync()
                self.assertEqual(self.worker.current_stage(), stage)
                self.storage.fail = None
                self.assertEqual(persistence.read_head(self.worker, self.manifest)[0], first)
                (self.output / 'new-small.txt').unlink()

    def test_layout_checksum_and_pack_corruption_fail_before_restore(self):
        commit = self.sync()
        root = self.remote_root()
        layout_path = root / 'snapshots' / commit['snapshot_id'] / ('layout-' + commit['commit_id'] + '.json.gz')
        raw = layout_path.read_bytes()
        layout_path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(Error, 'layout checksum'):
            self.restored()
        layout_path.write_bytes(raw)
        pack = next(iter(self.layout(commit)['packs']))
        pack_path = root / 'packs' / (pack + '.tar')
        pack_path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(Error, 'pack failed SHA-256'):
            self.restored('bad-pack')
        self.assertFalse((self.root / 'bad-pack/work/output').exists())

    def test_unsupported_layout_and_invalid_commit_reference_fail_closed(self):
        commit = self.sync()
        root = remote_path(self.manifest['storage'], self.manifest['study_id'])
        recovery = persistence.read_snapshot(self.storage, root, commit)
        layout = {'schema': physical.SCHEMA, 'schema_version': 2,
                  'snapshot_id': recovery['snapshot_id'], 'loose': [], 'packs': {}}
        raw = gzip.compress(json.dumps(layout).encode(), mtime=0)
        path = self.remote_root() / 'snapshots' / commit['snapshot_id'] / ('layout-' + commit['commit_id'] + '.json.gz')
        path.write_bytes(raw)
        changed = {**commit, 'layout_sha256': hashlib.sha256(raw).hexdigest()}
        with self.assertRaisesRegex(Error, 'Unsupported or invalid cloud physical layout'):
            persistence.read_layout(self.storage, root, changed, recovery)
        with self.assertRaisesRegex(Error, 'physical layout reference'):
            studies.state_head(self.manifest['study_id'], [{**commit, 'layout_sha256': None}])

    def test_pack_parser_rejects_bad_member_bytes_and_unsafe_inventory(self):
        expected = hashlib.sha256(b'right').hexdigest()
        for names, data, error, link in [([expected], b'wrong', 'SHA-256', False),
                                         ([expected, expected], b'right', 'duplicate', False),
                                         (['../escape'], b'right', 'unsafe', False),
                                         (['other'], b'right', 'unsafe', False),
                                         ([expected], b'', 'unsafe', True)]:
            with self.subTest(names=names, link=link):
                pack = self.root / 'crafted.tar'
                with tarfile.open(pack, 'w') as archive:
                    for name in names:
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        if link:
                            info.type = tarfile.SYMTYPE
                            info.linkname = '../escape'
                        archive.addfile(info, io.BytesIO(data) if not link else None)
                destination = self.root / 'extracted'
                destination.mkdir(exist_ok=True)
                with self.assertRaisesRegex(Error, error):
                    physical.extract(pack, sha256(pack), {expected}, {expected}, {expected: 5}, destination)
                if (destination / expected).exists():
                    (destination / expected).unlink()

    def test_pack_pruning_keeps_live_members_and_unknown_orphans(self):
        (self.output / 'small-a').write_bytes(b'A')
        (self.output / 'small-b').write_bytes(b'B')
        first = self.sync()
        old_pack = next(iter(self.layout(first)['packs']))
        root = self.remote_root()
        orphan_pack = root / 'packs' / ('f' * 64 + '.tar')
        orphan_pack.write_bytes(b'orphan')
        orphan_blob = root / 'blobs' / ('e' * 64)
        orphan_blob.write_bytes(b'orphan')
        (self.output / 'small-a').unlink()
        write_json(self.output / 'metadata.json', {'revision': 2})
        self.sync()
        write_json(self.output / 'metadata.json', {'revision': 3})
        self.sync()
        self.assertTrue((root / 'packs' / (old_pack + '.tar')).exists())
        (self.output / 'small-b').unlink()
        write_json(self.output / 'metadata.json', {'revision': 4})
        self.sync()
        self.sync()
        self.assertFalse((root / 'packs' / (old_pack + '.tar')).exists())
        self.assertTrue(orphan_pack.exists())
        self.assertTrue(orphan_blob.exists())

    def test_thousands_of_tiny_files_need_few_remote_payload_objects(self):
        directory = self.output / 'many'
        directory.mkdir()
        for index in range(3000):
            (directory / f'{index:04}.bin').write_bytes(f'{index:04} payload'.encode() * 20)
        commit = self.sync()
        stats = commit['transfer']
        self.assertGreaterEqual(stats['logical_files'], 3002)
        self.assertEqual(stats['physical_objects_written'], 2)
        self.assertEqual(stats['packs'], 1)
        self.assertGreater(stats['unique_digests'] / stats['physical_objects_written'], 100)
        self.assertEqual(len(list((self.remote_root() / 'packs').iterdir())), stats['packs'])

    def test_recovery_timestamp_describes_publication_after_transfer(self):
        with patch('cloud_experiments.persistence.utcnow', side_effect=['preparing', 'durable']):
            commit = self.sync()
        self.assertEqual(commit['committed_at'], 'durable')
        self.assertEqual(self.manifest['last_recovery']['committed_at'], 'durable')

    def test_interrupted_transfer_preserves_prior_recovery_and_compute_can_retry(self):
        first = self.sync()
        write_json(self.output / 'metadata.json', {'revision': 2})
        self.storage.fail = lambda args: args[0] == 'check'
        with self.assertRaisesRegex(Error, 'interruption'):
            self.sync()
        self.assertEqual(self.worker.current_stage(), 'recovery.verify')
        self.storage.fail = None
        head, _ = persistence.read_head(self.worker, self.manifest)
        self.assertEqual(head, first)
        replacement, _ = self.restored()
        self.assertEqual(read_json(replacement.work / 'output/metadata.json'), {'revision': 1})
        second = self.sync()
        self.assertEqual(second['parent'], first['commit_id'])

    def test_prune_is_delayed_until_replacements_are_verified_and_prior_head_is_safe(self):
        first = self.sync()
        old_digest = sha256(self.output / 'durable.bin')
        (self.output / 'durable.bin').unlink()
        (self.output / 'derived.bin').write_bytes(b'retained metric replacement')
        with patch.object(ews_contract, 'create_snapshot', side_effect=lambda w, s, d: sealed_snapshot(s, d, pruned=['fake-run'])):
            second = self.sync()
            self.assertTrue((self.remote_root() / 'blobs' / old_digest).exists())
            write_json(self.output / 'metadata.json', {'revision': 3})
            start = len(self.storage.events)
            third = self.sync()
        events = self.storage.events[start:]
        self.assertFalse((self.remote_root() / 'blobs' / old_digest).exists())
        check_at = next(i for i, event in enumerate(events) if event[0] == 'check')
        delete_at = next(i for i, event in enumerate(events) if event[0] == 'deletefile' and event[1].endswith(old_digest))
        commit_at = next(i for i, event in enumerate(events) if event[0] == 'copyto' and '/commits/' in event[2])
        self.assertLess(check_at, delete_at)
        self.assertLess(delete_at, commit_at)
        self.assertEqual(third['parent'], second['commit_id'])
        # Second remains restorable even when the third marker is interrupted.
        snapshot = self.root / 'second-snapshot'
        persistence.download_snapshot(self.storage, remote_path(self.manifest['storage'], self.manifest['study_id']), second, snapshot)
        self.assertEqual((snapshot / 'output/derived.bin').read_bytes(), b'retained metric replacement')
        self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])

    def test_reintroduced_payload_survives_retirement_across_multiple_commits(self):
        first = self.sync()
        digest = sha256(self.output / 'durable.bin')
        (self.output / 'durable.bin').unlink()
        self.sync()
        self.sync()
        self.assertFalse((self.remote_root() / 'blobs' / digest).exists())
        (self.output / 'durable.bin').write_bytes(self.large)
        reintroduced = self.sync()
        self.assertTrue((self.remote_root() / 'blobs' / digest).exists())
        self.assertEqual(sum(1 for name, _ in self.storage.uploads if name == digest), 2)
        replacement, _ = self.restored()
        self.assertEqual((replacement.work / 'output/durable.bin').read_bytes(), self.large)
        self.assertNotEqual(first['commit_id'], reintroduced['commit_id'])

    def test_failure_after_prune_keeps_the_last_commit_restorable(self):
        self.sync()
        (self.output / 'durable.bin').unlink()
        (self.output / 'derived.bin').write_bytes(b'durable replacement')
        second = self.sync()
        write_json(self.output / 'metadata.json', {'revision': 3})
        self.storage.fail = lambda args: args[0] == 'copyto' and '/commits/' in str(args[2])
        with self.assertRaises(Error):
            self.sync()
        self.storage.fail = None
        self.assertEqual(persistence.read_head(self.worker, self.manifest)[0], second)
        replacement, _ = self.restored()
        self.assertEqual((replacement.work / 'output/derived.bin').read_bytes(), b'durable replacement')
        self.assertFalse((replacement.work / 'output/durable.bin').exists())

    def test_vm_loss_restores_latest_commit_not_the_last_finalized_attempt(self):
        self.sync()
        write_json(self.output / 'metadata.json', {'revision': 2, 'step': 600})
        latest = self.sync()
        write_json(self.output / 'metadata.json', {'revision': 3, 'step': 700})
        replacement, manifest = self.restored()
        self.assertFalse(latest['final'])
        self.assertEqual(read_json(replacement.work / 'output/metadata.json')['step'], 600)
        self.assertEqual(manifest['parent_state'], latest['commit_id'])
        manifest.update(status='running')
        continued = persistence.sync(replacement, manifest)
        self.assertEqual(continued['parent'], latest['commit_id'])
        self.assertEqual(continued['attempt_id'], manifest['run_id'])

    def test_final_sync_reuses_periodic_payload_and_records_versions(self):
        self.sync()
        self.storage.uploads.clear()
        self.manifest.update(status='finalizing', compute={'status': 'completed'}, exit_code=0,
                             ews_counts=dict(completed=4, pending=0, running=0, paused=0, failed=0, corrupt=0))
        final = self.sync(final=True)
        self.assertEqual(self.storage.uploads, [])
        self.assertTrue(final['completed'])
        self.assertTrue(final['final'])
        self.assertEqual(final['provenance']['ews']['commit'], 'd' * 40)
        self.assertEqual(final['contract'], ews_contract.CONTRACT)
        self.assertEqual(final['provenance']['cloud'], self.manifest['tooling'])
        self.assertEqual(self.manifest['status'], 'finalizing')
        self.assertFalse((self.worker.out / 'artifacts/output').exists())

    def test_final_snapshot_and_publication_have_no_subprocess_wall_clock(self):
        self.worker.rclone = Mock(side_effect=self.storage)
        self.sync(final=True)
        self.assertIsNone(self.snapshot_api.call_args.kwargs['timeout'])
        self.assertTrue(self.worker.rclone.call_args_list)
        self.assertTrue(all(call.kwargs['timeout'] is None for call in self.worker.rclone.call_args_list))

    def test_failed_commit_readback_is_idempotently_recovered(self):
        committed = []
        def failure(args):
            if args[0] == 'copyto' and '/commits/' in str(args[2]):
                committed.append(args[2])
            return args[0] == 'cat' and args[1] in committed
        self.storage.fail = failure
        with self.assertRaises(Error):
            self.sync()
        self.assertIn('pending_recovery', self.manifest)
        pending = self.manifest['pending_recovery']
        self.storage.fail = None
        recovered = self.sync()
        self.assertEqual(recovered, pending)
        self.assertEqual(len(list((self.remote_root() / 'commits').iterdir())), 1)
        self.assertNotIn('pending_recovery', self.manifest)

    def test_lease_and_parent_fences_prevent_competing_publication(self):
        first = self.sync()
        self.manifest['parent_state'] = None
        with self.assertRaisesRegex(Error, 'state changed'):
            self.sync()
        self.manifest['parent_state'] = first['commit_id']
        self.worker.verify_lease.side_effect = Error('lease ended')
        with self.assertRaisesRegex(Error, 'lease ended'):
            self.sync()
        self.assertEqual(persistence.read_head(self.worker, self.manifest)[0], first)

    def test_corrupt_download_never_restores_an_output(self):
        self.sync()
        digest = sha256(self.output / 'durable.bin')
        (self.remote_root() / 'blobs' / digest).write_bytes(b'corrupt')
        with self.assertRaisesRegex(Error, 'SHA-256'):
            self.restored()
        self.assertFalse((self.root / 'replacement/work/output').exists())

    def test_large_download_lists_once_and_restores_only_selected_blobs(self):
        root = self.remote_root()
        pool = root / 'blobs'
        pool.mkdir(parents=True)
        files = {}
        for index in range(300):
            payload = f'blob {index}'.encode()
            digest = hashlib.sha256(payload).hexdigest()
            (pool / digest).write_bytes(payload)
            files[f'runs/{index:03d}.bin'] = {'sha256': digest, 'size': len(payload)}
        (pool / hashlib.sha256(b'unrelated').hexdigest()).write_bytes(b'unrelated')
        destination = self.root / 'large-download'
        persistence.download_files(self.storage, remote_path(self.manifest['storage'], self.manifest['study_id']),
                                   {'files': files}, destination, files)
        copies = [args for args in self.storage.events if args[0] == 'copy' and str(args[1]).endswith('/blobs')]
        self.assertEqual(len(copies), 1)
        self.assertIn('--fast-list', copies[0])
        self.assertNotIn('--no-traverse', copies[0])
        self.assertEqual((destination / 'runs/299.bin').read_bytes(), b'blob 299')
        self.assertEqual(len(list(destination.rglob('*.bin'))), 300)

    def test_unknown_cloud_and_ews_contract_versions_fail_closed(self):
        commit = self.sync()
        for field, value in [('schema_version', 3), ('contract', {'schema': ews_contract.SCHEMA, 'schema_version': 2})]:
            changed = {**commit, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(Error, 'contract'):
                studies.state_head(self.manifest['study_id'], [changed])
        for malformed in (True, 2.0, [], None):
            with self.subTest(version=malformed), self.assertRaisesRegex(Error, 'contract'):
                studies.state_head(self.manifest['study_id'], [{**commit, 'schema_version': malformed}], allow_legacy=True)
        legacy = dict(schema_version=1, study_id=self.manifest['study_id'], attempt_id=self.manifest['run_id'],
                      parent=None, inventory_sha256='f' * 64)
        with self.assertRaisesRegex(Error, '--fresh'):
            studies.state_head(self.manifest['study_id'], [legacy])
        self.assertEqual(studies.state_head(self.manifest['study_id'], [legacy], allow_legacy=True), legacy)

    def test_secret_and_linked_files_fail_before_upload(self):
        for name in ['.env', 'key.pem']:
            path = self.output / name
            path.write_text('FAKE_SECRET=never-upload')
            with self.subTest(name=name), self.assertRaisesRegex(Error, 'secret-like'):
                self.sync()
            path.unlink()
        (self.output / 'linked').symlink_to(self.root / 'config.toml')
        with self.assertRaisesRegex(Error, 'linked'):
            self.sync()
        self.assertEqual(self.storage.uploads, [])

    def test_ews_rejects_unexpected_missing_state_without_remote_deletion(self):
        first = self.sync()
        with patch.object(ews_contract, 'create_snapshot', side_effect=Error('EWS rejects missing durable input')):
            with self.assertRaisesRegex(Error, 'missing durable'):
                self.sync()
        self.assertEqual(persistence.read_head(self.worker, self.manifest)[0], first)
        self.assertFalse(any(event[0] == 'deletefile' for event in self.storage.events))


if __name__ == '__main__':
    unittest.main()
