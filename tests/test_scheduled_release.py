"""Daily checks use metadata and pass only missing architectures to the build."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scheduled_release as scheduled
from workflow_helpers import ROOT, github_jobs, step_by_id


def release(*arches):
    return {'id': 17, 'tag_name': 'v2.4.0', 'draft': False, 'prerelease': False,
            'assets': [{'name': f'1panel-v2.4.0-linux-{arch}.tar.gz{suffix}', 'state': 'uploaded', 'size': 100}
                       for arch in arches for suffix in ('', '.sha256')]}


class ScheduledRelease(unittest.TestCase):
    def setUp(self):
        self.client_patch = patch.object(scheduled, 'GitHub')
        self.client = self.client_patch.start().return_value
        self.client.download.side_effect = AssertionError('Existing packages must not be downloaded')
        self.client.download_asset.side_effect = AssertionError('Existing assets must not be downloaded')
        self.addCleanup(self.client_patch.stop)

    def plan(self):
        return scheduled.plan(scheduled.REPOSITORY, 'v2.4.0', 'amd64 arm64 riscv64')

    def test_complete_release_stops_and_partial_release_selects_only_missing_arches(self):
        for present, missing in ((('amd64', 'arm64', 'riscv64'), ''),
                                 (('amd64', 'arm64'), 'riscv64'),
                                 (('arm64',), 'amd64 riscv64'), ((), 'amd64 arm64 riscv64')):
            self.client.release.return_value = release(*present)
            with self.subTest(present=present):
                self.assertEqual(self.plan(), {'version': 'v2.4.0', 'arches': missing,
                                               'build_required': 'true' if missing else 'false',
                                               'checksum_required': 'false'})
        self.client.download.assert_not_called()
        self.client.download_asset.assert_not_called()

    def test_incomplete_upload_never_counts_as_a_complete_pair(self):
        for suffix in ('', '.sha256'):
            for change in ({'state': 'starter', 'size': 0}, {'state': 'starter'},
                           {'size': 0}, {'state': None}, {'size': True}):
                current = release('amd64', 'arm64', 'riscv64')
                name = '1panel-v2.4.0-linux-amd64.tar.gz' + suffix
                asset = next(row for row in current['assets'] if row['name'] == name)
                asset.update(change, id=91)
                self.client.release.return_value = current
                with self.subTest(suffix=suffix, change=change), self.assertRaisesRegex(ValueError, r'Incomplete public asset.*id=91'):
                    self.plan()
        self.client.download.assert_not_called()
        self.client.download_asset.assert_not_called()

    def test_only_explicit_release_not_found_builds_all(self):
        self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr='gh: Not Found (HTTP 404)')
        self.assertEqual(self.plan()['arches'], 'amd64 arm64 riscv64')
        for status in (401, 403, 500):
            self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr=f'gh: error (HTTP {status})')
            with self.subTest(status=status), self.assertRaises(subprocess.CalledProcessError):
                self.plan()

    def test_latest_uses_one_official_request_and_requires_stable_v2(self):
        latest = {'tag_name': 'v2.4.0', 'draft': False, 'prerelease': False}
        self.client.run.return_value = json.dumps(latest)
        self.assertEqual(scheduled.latest_version(self.client), 'v2.4.0')
        self.client.run.assert_called_once_with('api', f'repos/{scheduled.SOURCE}/releases/latest')
        for invalid in (dict(latest, tag_name='v3.0.0'), dict(latest, tag_name='v2.4.0-beta.1'),
                        dict(latest, draft=True), dict(latest, prerelease=True), [], {}):
            self.client.run.return_value = json.dumps(invalid)
            with self.subTest(release=invalid), self.assertRaises(ValueError):
                scheduled.latest_version(self.client)

    def test_archive_only_needs_checksum_not_rebuild_and_orphan_checksum_stops(self):
        current = release('amd64', 'arm64', 'riscv64')
        name = '1panel-v2.4.0-linux-amd64.tar.gz'
        current['assets'] = [asset for asset in current['assets'] if asset['name'] != name + '.sha256']
        current['assets'][0]['digest'] = 'sha256:' + 'a' * 64
        self.client.release.return_value = current
        self.assertEqual(self.plan(), {'version': 'v2.4.0', 'arches': '',
                                      'build_required': 'false', 'checksum_required': 'true'})
        current['assets'] = [asset for asset in current['assets'] if 'linux-arm64.' not in asset['name']]
        self.assertEqual(self.plan()['arches'], 'arm64')
        for invalid in (None, '', 'sha256:short'):
            current['assets'][0]['digest'] = invalid
            with self.subTest(digest=invalid), self.assertRaisesRegex(ValueError, 'server SHA-256'):
                self.plan()
        current = release('amd64')
        current['assets'].pop(0)
        self.client.release.return_value = current
        with self.assertRaisesRegex(ValueError, 'without its archive'):
            self.plan()
        self.client.download.assert_not_called()
        self.client.download_asset.assert_not_called()

    def test_preflight_has_no_third_party_runtime_dependency(self):
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/scheduled_release.py'), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_daily_gate_stops_before_tests_when_complete_and_keeps_other_events(self):
        jobs = yaml.safe_load((ROOT / '.github/workflows/build.yml').read_text())['jobs']
        expression = jobs['tests']['if'].removeprefix('${{').removesuffix('}}').strip()
        for event, required, expected in (('schedule', 'false', False), ('schedule', 'true', True),
                                          ('schedule', '', False), ('pull_request', '', True),
                                          ('push', '', True), ('workflow_dispatch', '', True)):
            code = expression.replace('!cancelled()', 'True')
            code = code.replace('github.event_name', repr(event))
            code = code.replace('needs.check_update.outputs.build_required', repr(required))
            code = code.replace('&&', ' and ').replace('||', ' or ')
            with self.subTest(event=event, required=required):
                self.assertEqual(eval(code, {'__builtins__': {}}), expected)
        self.assertEqual(jobs['tests']['needs'], 'check_update')
        writer = jobs['automatic_publication']
        self.assertEqual(writer['needs'], ['check_update', 'publication_prepare'])
        gate = writer['if'].removeprefix('${{').removesuffix('}}').strip()
        for event, prepared, checked, checksum, expected in (
                ('schedule', 'skipped', 'success', 'true', True),
                ('schedule', 'skipped', 'success', 'false', False),
                ('schedule', 'skipped', 'failure', 'true', False),
                ('schedule', 'success', 'success', 'false', True),
                ('workflow_dispatch', 'skipped', 'skipped', 'true', False)):
            code = gate.replace('!cancelled()', 'True')
            for old, new in {'github.event_name': repr(event), 'github.ref': repr('refs/heads/master'),
                             'needs.publication_prepare.result': repr(prepared),
                             'needs.check_update.result': repr(checked),
                             'needs.check_update.outputs.checksum_required': repr(checksum),
                             'inputs.operation': repr('build-only'), 'inputs.publish_candidate': 'False',
                             "startsWith('refs/heads/master', 'refs/tags/v')": 'False'}.items():
                code = code.replace(old, new)
            self.assertEqual(eval(code.replace('!', ' not ').replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}), expected)
        steps = writer['steps']
        publish = steps.index(step_by_id(writer, 'publish'))
        checksum = steps.index(step_by_id(writer, 'repair_checksums'))
        self.assertLess(publish, checksum)
        # Repairing an existing attachment's checksum is independent of new
        # package publication, including when that preceding step failed.
        condition = steps[checksum]['if'].removeprefix('${{').removesuffix('}}').strip()
        for event, required, succeeded, cancelled, expected in (
                ('schedule', 'true', True, False, True),
                ('schedule', 'true', False, False, True),
                ('schedule', 'true', False, True, False),
                ('schedule', 'false', False, False, False),
                ('workflow_dispatch', 'true', True, False, False)):
            code = condition
            if not any(name + '()' in code for name in ('always', 'success', 'failure', 'cancelled')):
                code = 'success() && (' + code + ')'
            for old, new in {'cancelled()': repr(cancelled), 'success()': repr(succeeded),
                             'github.event_name': repr(event),
                             'needs.check_update.outputs.checksum_required': repr(required)}.items():
                code = code.replace(old, new)
            with self.subTest(event=event, checksum=required, succeeded=succeeded, cancelled=cancelled):
                self.assertEqual(eval(code.replace('!', ' not ').replace('&&', ' and '), {'__builtins__': {}}), expected)
        for step in steps:
            if step.get('uses') == 'actions/download-artifact@v4':
                self.assertEqual(step['if'], "needs.publication_prepare.result == 'success'")

    def test_prepare_uses_only_preflight_missing_subset_without_checking_again(self):
        self.run_prepare('schedule', 'amd64 arm64', 'riscv64', 'riscv64')

    def test_manual_build_keeps_requested_architectures(self):
        for requested in ('amd64 arm64', ' amd64,\narm64; ', 'amd64\tarm64'):
            with self.subTest(requested=requested):
                self.run_prepare('workflow_dispatch', requested, '', 'amd64 arm64')

    def run_prepare(self, event, requested, missing, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/validate_artifacts.py').write_text('def architectures(value): return value.replace(",", " ").replace(";", " ").split()\n')
            (root / 'scripts/discover_inputs.py').write_text('import sys; assert "--latest" not in sys.argv; print("a" * 64)\n')
            (root / 'scripts/resolved_contract.py').write_text('')
            (root / 'scripts/resolve_inputs.py').write_text('print("SOURCE_COMMIT=fixture\\nGO_VERSION=1.26.1")\n')
            (root / 'scripts/scheduled_release.py').write_text('raise RuntimeError("preflight must not run again")\n')
            env = dict(os.environ, INPUT_VERSION='v2.4.0', INPUT_ARCHES=requested, INPUT_CHANNEL='stable',
                       SCHEDULED_VERSION='v2.4.0', SCHEDULED_ARCHES=missing,
                       GITHUB_EVENT_NAME=event, GITHUB_REF='refs/heads/master',
                       GITHUB_OUTPUT=str(root / 'outputs'), GITHUB_ENV=str(root / 'env'))
            result = subprocess.run(['bash', '-c', step_by_id(github_jobs()['prepare'], 'inputs')['run']],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = (root / 'outputs').read_text()
            self.assertIn(f'arches={expected}\n', outputs)
            matrix = json.loads(next(line[7:] for line in outputs.splitlines() if line.startswith('matrix=')))
            self.assertEqual(matrix['arch'], expected.split())
