"""Only a completely verified public matrix can suppress a scheduled build."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scheduled_release as scheduled
from test_ci_contract import github_block


class ScheduledRelease(unittest.TestCase):
    def setUp(self):
        self.client_patch = patch.object(scheduled, 'GitHub')
        self.client = self.client_patch.start().return_value
        self.client.release.return_value = {'id': 17, 'tag_name': 'v2.4.0', 'draft': False}
        self.addCleanup(self.client_patch.stop)
        self.verify_patch = patch.object(scheduled, 'verify_existing')
        self.verify = self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def required(self):
        return scheduled.build_required(scheduled.REPOSITORY, 'v2.4.0', 'amd64 arm64')

    def test_verified_complete_matrix_skips_but_partial_matrix_builds(self):
        for records, expected in (({'amd64': {}, 'arm64': {}}, False), ({'amd64': {}}, True), ({}, True)):
            self.verify.return_value = ({'draft': False}, records)
            self.assertIs(self.required(), expected)
        self.verify.assert_called_with(self.client, 'v2.4.0', scheduled.ROOT)
        self.client.bind_release.assert_called_with(17)

    def test_only_initial_explicit_not_found_is_missing(self):
        self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr='gh: Not Found (HTTP 404)')
        self.assertTrue(self.required())
        self.verify.assert_not_called()
        for status in (401, 403, 500):
            self.client.release.side_effect = subprocess.CalledProcessError(1, ['gh'], stderr=f'gh: error (HTTP {status})')
            with self.subTest(status=status), self.assertRaises(subprocess.CalledProcessError):
                self.required()

    def test_corruption_asset_not_found_and_snapshot_races_never_skip(self):
        for error in (ValueError('Canonical package SHA sidecar mismatch'),
                      ValueError('Release changed during package verification'),
                      subprocess.CalledProcessError(1, ['gh'], stderr='asset not found (HTTP 404)')):
            self.verify.side_effect = error
            with self.subTest(error=str(error)), self.assertRaises(type(error)):
                self.required()

    def test_schedule_skip_occurs_before_contract_resolution_or_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()\n')
            (root / 'scripts/discover_inputs.py').write_text('import sys; assert "--latest" in sys.argv; print("v2.4.0")\n')
            (root / 'scripts/scheduled_release.py').write_text('print("false")\n')
            env = dict(os.environ, INPUT_VERSION='', INPUT_ARCHES='amd64 arm64', INPUT_CHANNEL='stable',
                       GITHUB_EVENT_NAME='schedule', GITHUB_REF='refs/heads/master',
                       GITHUB_REPOSITORY=scheduled.REPOSITORY, GITHUB_OUTPUT=str(root / 'outputs'))
            result = subprocess.run(['bash', '-c', github_block('Resolve immutable build inputs')],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('build_required=false\n', (root / 'outputs').read_text())
            self.assertFalse((root / 'resolved-source.json').exists())
            self.assertFalse((root / 'build-inputs.env').exists())

    def test_manual_build_does_not_call_schedule_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()\n')
            (root / 'scripts/discover_inputs.py').write_text('print("a" * 64)\n')
            (root / 'scripts/resolved_contract.py').write_text('')
            (root / 'scripts/resolve_inputs.py').write_text('print("SOURCE_COMMIT=fixture\\nGO_VERSION=1.26.1")\n')
            (root / 'scripts/scheduled_release.py').write_text('raise RuntimeError("schedule helper called for manual build")\n')
            env = dict(os.environ, INPUT_VERSION='v2.4.0', INPUT_ARCHES='amd64', INPUT_CHANNEL='stable',
                       GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/master',
                       GITHUB_OUTPUT=str(root / 'outputs'), GITHUB_ENV=str(root / 'env'))
            result = subprocess.run(['bash', '-c', github_block('Resolve immutable build inputs')],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('build_required=true\n', (root / 'outputs').read_text())
            self.assertTrue((root / 'build-inputs.env').exists())
