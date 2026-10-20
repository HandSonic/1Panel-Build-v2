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
from test_ci_contract import ROOT, github_block


def release(*arches):
    return {'id': 17, 'tag_name': 'v2.4.0', 'draft': False, 'prerelease': False,
            'assets': [{'name': f'1panel-v2.4.0-linux-{arch}.tar.gz{suffix}'}
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
                                               'build_required': 'true' if missing else 'false'})
        self.client.download.assert_not_called()
        self.client.download_asset.assert_not_called()

    def test_only_explicit_release_not_found_builds_all(self):
        self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr='gh: Not Found (HTTP 404)')
        self.assertEqual(self.plan()['arches'], 'amd64 arm64 riscv64')
        for status in (401, 403, 500):
            self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr=f'gh: error (HTTP {status})')
            with self.subTest(status=status), self.assertRaises(subprocess.CalledProcessError):
                self.plan()

    def test_latest_official_stable_uses_numeric_version_and_excludes_other_channels(self):
        self.client.run.return_value = json.dumps([
            {'tag_name': 'v2.9.9'}, {'tag_name': 'v2.10.0'},
            {'tag_name': 'v2.11.0', 'draft': True},
            {'tag_name': 'v2.12.0', 'prerelease': True},
            {'tag_name': 'v2.13.0-beta.1', 'prerelease': True}])
        self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr='(HTTP 404)')
        self.assertEqual(scheduled.plan(scheduled.REPOSITORY)['version'], 'v2.10.0')

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

    def test_prepare_uses_only_preflight_missing_subset_without_checking_again(self):
        self.run_prepare('schedule', 'amd64 arm64', 'riscv64', 'riscv64')

    def test_manual_build_keeps_requested_architectures(self):
        self.run_prepare('workflow_dispatch', 'amd64 arm64', '', 'amd64 arm64')

    def run_prepare(self, event, requested, missing, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()\n')
            (root / 'scripts/discover_inputs.py').write_text('import sys; assert "--latest" not in sys.argv; print("a" * 64)\n')
            (root / 'scripts/resolved_contract.py').write_text('')
            (root / 'scripts/resolve_inputs.py').write_text('print("SOURCE_COMMIT=fixture\\nGO_VERSION=1.26.1")\n')
            (root / 'scripts/scheduled_release.py').write_text('raise RuntimeError("preflight must not run again")\n')
            env = dict(os.environ, INPUT_VERSION='v2.4.0', INPUT_ARCHES=requested, INPUT_CHANNEL='stable',
                       SCHEDULED_VERSION='v2.4.0', SCHEDULED_ARCHES=missing,
                       GITHUB_EVENT_NAME=event, GITHUB_REF='refs/heads/master',
                       GITHUB_OUTPUT=str(root / 'outputs'), GITHUB_ENV=str(root / 'env'))
            result = subprocess.run(['bash', '-c', github_block('Resolve immutable build inputs')],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = (root / 'outputs').read_text()
            self.assertIn(f'arches={expected}\n', outputs)
            matrix = json.loads(next(line[7:] for line in outputs.splitlines() if line.startswith('matrix=')))
            self.assertEqual(matrix['arch'], expected.split())
