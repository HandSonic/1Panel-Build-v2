"""Draft releases remain hidden by tag, but publish and recover by their exact ID."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
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
    def test_tag_404_discovers_only_matching_draft(self):
        client = Mock()
        client.release.side_effect = missing()
        client.drafts.return_value = [draft()]
        self.assertEqual(OPTIONAL_RELEASE(client), draft())
        client.drafts.assert_called_once_with()

    def test_missing_release_and_no_drafts_allows_create(self):
        client = Mock()
        client.release.side_effect = missing()
        client.drafts.return_value = []
        self.assertIsNone(OPTIONAL_RELEASE(client))

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

    def test_published_release_needs_no_draft_lookup(self):
        client = Mock()
        client.release.return_value = dict(draft(), draft=False)
        self.assertFalse(OPTIONAL_RELEASE(client)['draft'])
        client.drafts.assert_not_called()


class EmptyDraftEligibility(unittest.TestCase):
    def test_empty_bot_draft_at_current_commit_needs_no_compare(self):
        client = TransportGitHub(draft())
        ap.empty_automatic_draft(client.value, client, 'a' * 40)
        self.assertEqual(client.calls, [])

    def test_ancestor_target_is_eligible_for_explicit_retarget(self):
        client = TransportGitHub(draft('b' * 40))
        ap.empty_automatic_draft(client.value, client, 'a' * 40)
        self.assertTrue(any('/compare/' in item for call in client.calls for item in call))

    def test_nonempty_foreign_or_changed_drafts_require_review(self):
        changes = [dict(assets=[{'id': 3, 'name': 'package.tar.gz'}]),
                   dict(body='Human release notes'), dict(name='Human draft'),
                   dict(author={'id': 123, 'login': 'someone', 'type': 'User'}),
                   dict(target_commitish='master'), dict(prerelease=True),
                   dict(draft=False), dict(id=True), dict(tag_name='v2.98.0')]
        for change in changes:
            release = dict(draft(), **change)
            client = TransportGitHub(release)
            with self.subTest(change=change), self.assertRaises(ValueError):
                ap.empty_automatic_draft(release, client, 'a' * 40)
            self.assertEqual(client.calls, [])

    def test_diverged_target_is_not_automatically_adopted(self):
        client = TransportGitHub(draft('b' * 40))
        client.ancestor = False
        with self.assertRaises(ValueError):
            ap.empty_automatic_draft(client.value, client, 'a' * 40)


class DraftTransactions(unittest.TestCase):
    setUp = fixtures.AutomaticTransaction.setUp
    save_plan = fixtures.AutomaticTransaction.save_plan
    producer = fixtures.AutomaticTransaction.producer
    repair = fixtures.AutomaticTransaction.repair

    def test_real_adapter_creates_uploads_reads_back_and_publishes_by_id(self):
        client = TransportGitHub()
        with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair):
            self.assertEqual(ap.publish(self.args, client), 'published')
        self.assertFalse(client.value['draft'])
        self.assertEqual(client.release_id, 17)
        self.assertEqual(len(client.value['assets']), 2)
        post = next(i for i, call in enumerate(client.calls)
                    if call[:4] == ('api', '--method', 'POST', f'repos/{client.repo}/releases'))
        self.assertFalse(any('/releases/tags/' in item for call in client.calls[post + 1:] for item in call))

    def resume_plan(self, release):
        self.plan.update(operation='resume', before=ap.snapshot(release),
                         draft_identity=ap.draft_identity(release))
        self.save_plan()

    def test_failed_empty_draft_resumes_without_creating_another_release(self):
        release = draft('b' * 40)
        self.resume_plan(release)
        client = TransportGitHub(release)
        with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair):
            self.assertEqual(ap.publish(self.args, client), 'published')
        self.assertEqual(client.value['id'], 17)
        self.assertEqual(client.value['tag_name'], client.tag)
        self.assertFalse(client.value['draft'])
        self.assertEqual(client.value['target_commitish'], os.environ['GITHUB_SHA'])
        self.assertEqual(len(client.value['assets']), 2)
        retarget = next(call for call in client.calls
                        if call[:3] == ('api', '--method', 'PATCH')
                        and f'target_commitish={os.environ["GITHUB_SHA"]}' in call)
        self.assertIn(f'tag_name={client.tag}', retarget)
        self.assertFalse(any(call[:4] == ('api', '--method', 'POST', f'repos/{client.repo}/releases')
                             for call in client.calls))
        journal = json.loads((self.work / 'control' / 'creation-journal.json').read_text())
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['release_id'], 17)

    def test_writer_recovers_draft_hidden_from_readonly_preparation(self):
        # The read-only preparation token may not see draft releases. The
        # write token must discover and bind the unique eligible draft instead
        # of treating its existence as permission to create a duplicate.
        client = TransportGitHub(draft('b' * 40))
        self.assertIsNone(self.plan['before'])
        self.assertEqual(self.plan['operation'], 'create')
        with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), patch.object(ap, 'repair', repair):
            self.assertEqual(ap.publish(self.args, client), 'published')
        self.assertEqual(client.release_id, 17)
        self.assertFalse(client.value['draft'])
        self.assertEqual(len(client.value['assets']), 2)
        self.assertFalse(any(call[:4] == ('api', '--method', 'POST', f'repos/{client.repo}/releases')
                             for call in client.calls))

    def test_writer_never_adopts_ineligible_draft_hidden_from_preparation(self):
        for change in ({'body': 'manual notes'}, {'author': {'id': 7, 'login': 'human', 'type': 'User'}},
                       {'assets': [{'id': 9, 'name': 'existing.tar.gz', 'size': 3, 'digest': 'sha256:' + 'a' * 64}]}):
            client = TransportGitHub(dict(draft(), **change))
            with self.subTest(change=change), patch.object(ap, 'optional_release', OPTIONAL_RELEASE), self.assertRaises(ValueError):
                ap.publish(self.args, client)
            self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))

    def test_draft_metadata_drift_after_prepare_stops_before_write(self):
        self.resume_plan(draft())
        client = TransportGitHub(dict(draft(), target_commitish='b' * 40))
        with patch.object(ap, 'optional_release', OPTIONAL_RELEASE), self.assertRaises(ValueError):
            ap.publish(self.args, client)
        self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))
        self.assertTrue(client.value['draft'])
        self.assertEqual(client.value['assets'], [])


class DraftPreparation(unittest.TestCase):
    setUp = fixtures.PrepareControlFlow.setUp
    payload = fixtures.PrepareControlFlow.payload

    def test_empty_draft_plan_does_not_use_public_package_verification(self):
        self.payload(self.source, ('amd64',), 11, 'b' * 40)
        client = TransportGitHub(draft('b' * 40))
        with patch.object(ap, 'GitHub', return_value=client), patch('public_packages.verify_existing') as verify:
            ap.prepare(self.args)
        verify.assert_not_called()
        plan = json.loads((self.args.work / 'control' / 'automatic-plan.json').read_text())
        self.assertEqual(plan['operation'], 'resume')
        self.assertEqual(plan['before'], ap.snapshot(client.value))
        self.assertEqual(plan['draft_identity'], ap.draft_identity(client.value))
        self.assertFalse(any('POST' in call or 'PATCH' in call for call in client.calls))


if __name__ == '__main__':
    unittest.main()
