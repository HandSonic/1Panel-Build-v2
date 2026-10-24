"""Checksum gaps use server digests without downloading an existing attachment."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scheduled_release as scheduled
from test_release_repair import FakeGitHub


class ChecksumClient(FakeGitHub):
    repo = scheduled.REPOSITORY
    tag = 'v2.4.0'

    def __init__(self):
        super().__init__()
        amd64 = f'1panel-{self.tag}-linux-amd64.tar.gz'
        arm64 = f'1panel-{self.tag}-linux-arm64.tar.gz'
        self.assets = {1: (amd64, b'keep amd64'), 2: (arm64, b'keep arm64'),
                       3: (arm64 + '.sha256', b'keep existing checksum'),
                       4: ('release-notes.txt', b'keep unrelated attachment')}
        self.original_names = {name for name, _ in self.assets.values()}
        self.downloads = []

    def release(self):
        return dict(super().release(), id=17, tag_name=self.tag, draft=False, prerelease=False)

    def download(self, name, directory):
        if name in self.original_names:
            raise AssertionError('An existing attachment must not be downloaded')
        self.downloads.append(name)
        super().download(name, directory)

    def download_asset(self, asset, directory):
        raise AssertionError('An existing asset must not be downloaded')


class ScheduledChecksums(unittest.TestCase):
    def test_incomplete_upload_stops_checksum_repair_without_writes(self):
        for suffix in ('', '.sha256'):
            client = ChecksumClient()
            release = client.release
            name = f'1panel-{client.tag}-linux-arm64.tar.gz' + suffix
            def incomplete():
                value = release()
                next(row for row in value['assets'] if row['name'] == name).update(state='starter', size=0, digest=None)
                return value
            client.release = incomplete
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory, \
                    patch.object(scheduled, 'GitHub', return_value=client):
                with self.assertRaisesRegex(ValueError, 'Incomplete public asset'):
                    scheduled.repair_checksums(client.repo, client.tag, Path(directory) / 'work')
            self.assertEqual(client.calls, [])
            self.assertEqual(client.downloads, [])

    def test_repairs_only_missing_sidecar_on_bound_release_and_preserves_all_assets(self):
        client = ChecksumClient()
        before = copy.deepcopy(client.assets)
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduled, 'GitHub', return_value=client):
            work = Path(directory) / 'checksum-work'
            result = scheduled.repair_checksums(client.repo, client.tag, work)
            name = before[1][0] + '.sha256'
            self.assertEqual(client.calls, [('upload', name)])
            self.assertEqual(client.downloads, [])
            self.assertEqual(client.release_id, 17)
            self.assertEqual(next(body for filename, body in client.assets.values() if filename == name),
                             (hashlib.sha256(before[1][1]).hexdigest() + '  ' + before[1][0] + '\n').encode())
            for identity, value in before.items():
                self.assertEqual(client.assets[identity], value)
            self.assertEqual(result, {'state': 'complete', 'release_id': 17, 'checksum_count': 1})
            self.assertEqual(json.loads((work / 'checksum-journal.json').read_text())['phase'], 'complete')

    def test_bound_snapshot_change_stops_before_upload(self):
        client = ChecksumClient()
        original = client.release
        reads = []
        def changed():
            value = original(); reads.append(value)
            if len(reads) > 1:
                value['id'] = 18
            return value
        client.release = changed
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduled, 'GitHub', return_value=client):
            with self.assertRaisesRegex(ValueError, 'changed before checksum repair'):
                scheduled.repair_checksums(client.repo, client.tag, Path(directory) / 'work')
        self.assertEqual(client.calls, [])
        self.assertEqual(client.downloads, [])

    def test_legacy_archive_without_digest_stops_without_readback_or_upload(self):
        client = ChecksumClient()
        original = client.release
        def without_digest():
            value = original()
            value['assets'][0].pop('digest')
            return value
        client.release = without_digest
        with tempfile.TemporaryDirectory() as directory, patch.object(scheduled, 'GitHub', return_value=client):
            with self.assertRaisesRegex(ValueError, 'server SHA-256'):
                scheduled.repair_checksums(client.repo, client.tag, Path(directory) / 'work')
        self.assertEqual(client.calls, [])
        self.assertEqual(client.downloads, [])
