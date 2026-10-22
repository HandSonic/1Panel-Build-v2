"""Draft releases remain hidden by tag, but publish and recover by their exact ID."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automatic_publication as ap
from release_asset_repair import GitHub, repair
import test_automatic_publication as fixtures

Client = fixtures.Client


OPTIONAL_RELEASE = ap.optional_release


def draft(target='a' * 40):
    return {'id': 17, 'tag_name': Client.tag, 'name': Client.tag,
            'draft': True, 'prerelease': False, 'body': '', 'assets': [],
            'target_commitish': target,
            'author': {'id': 41898282, 'login': 'github-actions[bot]', 'type': 'Bot'}}


def missing(status=404):
    return subprocess.CalledProcessError(1, ['gh', 'api'], stderr=f'gh: error (HTTP {status})')


class TransportGitHub(GitHub):
    """Keep the production adapter; fake only GitHub's HTTP/gh transport."""
    def __init__(self, release=None):
        super().__init__(Client.repo, Client.tag)
        self.value = copy.deepcopy(release)
        self.calls = []
        self.bytes = {}
        self.ancestor = True

    def run(self, *args):
        self.calls.append(args)
        if args == ('api', f'repos/{self.repo}/releases/tags/{self.tag}'):
            if self.value is None or self.value['draft']:
                raise missing()
            return json.dumps(self.value)
        if args[0] == 'api' and args[1].startswith(f'repos/{self.repo}/releases?'):
            return json.dumps([self.value] if self.value and self.value['draft'] else [])
        if args[0] == 'api' and args[1].startswith(f'repos/{self.repo}/compare/'):
            return json.dumps({'status': 'ahead' if self.ancestor else 'diverged',
                               'base_commit': {'sha': self.value['target_commitish']},
                               'merge_base_commit': {'sha': self.value['target_commitish'] if self.ancestor else 'c' * 40}})
        if args[:3] == ('api', '--method', 'POST'):
            endpoint = args[3]
            if endpoint == f'repos/{self.repo}/releases':
                self.value = draft(os.environ['GITHUB_SHA'])
                return json.dumps(self.value)
            if endpoint.startswith('https://uploads.github.com/'):
                self.assert_bound_endpoint(endpoint)
                path = Path(args[args.index('--input') + 1])
                name = parse_qs(urlparse(endpoint).query)['name'][0]
                raw = path.read_bytes()
                asset = {'id': 100 + len(self.bytes), 'name': name, 'size': len(raw),
                         'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
                self.bytes[asset['id']] = raw
                self.value['assets'].append(asset)
                return json.dumps(asset)
        if args[:3] == ('api', '--method', 'PATCH'):
            if args[3] != f'repos/{self.repo}/releases/17':
                raise AssertionError(args)
            fields = dict(item.split('=', 1) for item in args[4:] if '=' in item)
            for item in args:
                if item == 'draft=false': self.value['draft'] = False
                if item == 'draft=true': self.value['draft'] = True
                if item.startswith('target_commitish='):
                    self.value['target_commitish'] = item.split('=', 1)[1]
                if item.startswith('tag_name='):
                    self.value['tag_name'] = item.split('=', 1)[1]
            # GitHub can replace an uncreated draft tag when retargeting it
            # without also supplying tag_name (observed on the real release).
            if 'target_commitish' in fields and 'tag_name' not in fields:
                self.value['tag_name'] = 'untagged-7c790eb0db956d418c85'
            return json.dumps(self.value)
        if args == ('api', f'repos/{self.repo}/releases/17'):
            return json.dumps(self.value)
        raise AssertionError(args)

    def assert_bound_endpoint(self, endpoint):
        if not endpoint.startswith(f'https://uploads.github.com/repos/{self.repo}/releases/17/assets?'):
            raise AssertionError(endpoint)

    def download_asset(self, asset, directory):
        (Path(directory) / asset['name']).write_bytes(self.bytes[asset['id']])


class DraftDiscovery(unittest.TestCase):
    def test_multiple_matching_drafts_stop_before_create(self):
        client = Mock()
        client.release.side_effect = missing()
        client.drafts.return_value = [draft(), dict(draft(), id=18)]
        with self.assertRaises(ValueError): OPTIONAL_RELEASE(client)

    def test_permissions_or_transport_error_never_means_missing(self):
        for error in (missing(403), missing(500), OSError('network interrupted')):
            client = Mock()
            client.release.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)):
                OPTIONAL_RELEASE(client)
            client.drafts.assert_not_called()

class DraftTransactions(unittest.TestCase):
    setUp = fixtures.AutomaticTransaction.setUp
    producer = fixtures.AutomaticTransaction.producer
    repair = fixtures.AutomaticTransaction.repair

    def test_writer_only_retry_reuses_partial_create_or_resume_from_the_same_artifact(self):
        prepared = self.work
        receipt = self.proofpath.read_bytes()
        for initial in (None, draft('b' * 40)):
            with self.subTest(resuming=initial is not None), tempfile.TemporaryDirectory() as runners:
                self.args.work = Path(runners) / 'first'
                shutil.copytree(prepared, self.args.work)
                client = TransportGitHub(initial)
                upload = client.upload
                attempts = []
                def fail_second_upload(path):
                    attempts.append(path.name)
                    if len(attempts) == 2:
                        raise OSError('second upload interrupted')
                    return upload(path)
                with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair), \
                     patch.object(client, 'upload', side_effect=fail_second_upload), \
                     self.assertRaisesRegex(OSError, 'second upload interrupted'):
                    ap.publish(self.args, client)
                self.assertTrue(client.value['draft'])
                self.assertEqual(len(client.value['assets']), 1)
                existing = copy.deepcopy(client.value['assets'][0])

                # A new runner downloads the original preparation artifact,
                # without either writer journal, and discovers remote progress.
                self.args.work = Path(runners) / 'retry'
                shutil.copytree(prepared, self.args.work)
                retry = TransportGitHub(client.value)
                retry.bytes = copy.deepcopy(client.bytes)
                with patch.dict(os.environ, GITHUB_RUN_ATTEMPT='2'), \
                     patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair):
                    self.assertEqual(ap.publish(self.args, retry), 'published')
                self.assertFalse(retry.value['draft'])
                self.assertEqual(retry.value['tag_name'], retry.tag)
                self.assertEqual(len(retry.value['assets']), 2)
                self.assertEqual(next(asset for asset in retry.value['assets'] if asset['id'] == existing['id']), existing)
                uploads = [parse_qs(urlparse(call[3]).query)['name'][0] for call in retry.calls
                           if call[:3] == ('api', '--method', 'POST') and call[3].startswith('https://uploads.github.com/')]
                self.assertEqual(uploads, [self.files[1].name])
                self.assertEqual((self.args.work / 'control' / ap.PROOF).read_bytes(), receipt)
                first_retargets = [call for call in client.calls if any(item.startswith('target_commitish=') for item in call)
                                   and call[:3] == ('api', '--method', 'PATCH')]
                self.assertEqual(len(first_retargets), int(initial is not None))
                if first_retargets:
                    self.assertIn(f'tag_name={client.tag}', first_retargets[0])
                self.assertFalse(any(call[:3] == ('api', '--method', 'PATCH')
                                     and any(item.startswith('target_commitish=') for item in call) for call in retry.calls))
                self.assertFalse(any(call[:4] == ('api', '--method', 'POST', f'repos/{retry.repo}/releases')
                                     for call in retry.calls))

    def test_writer_never_adopts_an_ineligible_draft(self):
        for change in ({'body': 'manual notes'}, {'author': {'id': 7, 'login': 'human', 'type': 'User'}},
                       {'target_commitish': 'b' * 40}):
            client = TransportGitHub(dict(draft(), **change))
            client.ancestor = False
            with self.subTest(change=change), patch.object(ap, 'optional_release', OPTIONAL_RELEASE), \
                 patch.object(ap, 'repair', repair), self.assertRaises(ValueError):
                ap.publish(self.args, client)
            self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))

    def test_nonempty_draft_wrong_target_bytes_or_unknown_asset_stops_before_writes(self):
        prepared = self.work
        for reason in ('target', 'bytes', 'unknown'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as runner:
                self.args.work = Path(runner) / 'work'
                shutil.copytree(prepared, self.args.work)
                client = TransportGitHub(draft('b' * 40 if reason == 'target' else 'a' * 40))
                raw = b'corrupt' if reason == 'bytes' else self.files[0].read_bytes()
                name = 'unexpected.tar.gz' if reason == 'unknown' else self.files[0].name
                asset = {'id': 9, 'name': name, 'size': len(raw),
                         'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
                client.value['assets'] = [asset]
                client.bytes[9] = raw
                with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair), \
                     self.assertRaises(ValueError):
                    ap.publish(self.args, client)
                self.assertEqual(client.value['assets'], [asset])
                self.assertTrue(client.value['draft'])
                self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))
                self.assertFalse(any('/compare/' in item for call in client.calls for item in call))

    def test_draft_metadata_drift_during_writer_stops_before_upload(self):
        client = TransportGitHub(draft())
        def changed_before_repair(client, *args, **kwargs):
            client.value['target_commitish'] = 'b' * 40
            return repair(client, *args, **kwargs)
        with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), \
             patch.object(ap, 'repair', side_effect=changed_before_repair), self.assertRaises(ValueError):
            ap.publish(self.args, client)
        self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))
        self.assertTrue(client.value['draft'])
        self.assertEqual(client.value['assets'], [])

    def test_finalization_uses_verified_remote_facts_after_local_payload_cleanup(self):
        client=TransportGitHub(draft())
        def uploaded(client,files,*args,**kwargs):
            state=repair(client,files,*args,**kwargs)
            for path in files:path.unlink()
            return state
        with patch.object(ap,'optional_release',OPTIONAL_RELEASE),patch.object(ap,'repair',side_effect=uploaded):
            self.assertEqual(ap.publish(self.args,client),'published')
        self.assertFalse(client.value['draft'])
        self.assertEqual({asset['name'] for asset in client.value['assets']},{path.name for path in self.files[:2]})


if __name__ == '__main__':
    unittest.main()
