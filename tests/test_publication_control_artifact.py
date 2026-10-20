"""The small readback artifact must not alter the full publication binding."""
from pathlib import Path
import unittest
import yaml

class PublicationControlArtifact(unittest.TestCase):
    def test_artifact_only_mode_runs_source_but_never_publication_jobs(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        jobs = data['jobs']
        def enabled(expression, candidate=False):
            expression = expression.removeprefix('${{').removesuffix('}}').strip()
            for old, new in {
                'github.event_name': repr('workflow_dispatch'),
                'github.ref': repr('refs/heads/master'),
                'inputs.operation': repr('build-only'),
                'inputs.publish_candidate': repr(candidate),
                'needs.tests.result': repr('success'),
                'needs.tests.outputs.build_required': repr('true'),
                'needs.build.result': repr('success'),
                'needs.publication_prepare.result': repr('success'),
                'cancelled()': 'False',
                "startsWith('refs/heads/master', 'refs/tags/v')": 'False',
            }.items():
                expression = expression.replace(old, new)
            expression = expression.replace('!=', '<>').replace('!', ' not ').replace('<>', '!=')
            return eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}})
        self.assertTrue(enabled(jobs['prepare']['if']))
        for candidate in (False, True):
            for job in ('candidate', 'publication_prepare', 'publication_repair', 'automatic_publication'):
                self.assertFalse(enabled(jobs[job]['if'], candidate), job)

    def test_id_selected_automatic_input_uses_flat_validated_directory(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        steps = data['jobs']['publication_prepare']['steps']
        selected = [step for step in steps if step.get('uses') == 'actions/download-artifact@v4']
        self.assertEqual(len(selected), 1)
        download = selected[0]
        self.assertEqual(download['with']['artifact-ids'], '${{ needs.build.outputs.artifact_id }}')
        self.assertEqual(download['with']['path'], 'automatic-input')
        self.assertIs(download['with']['merge-multiple'], True)
        prepare = next(step for step in steps if step.get('name') == 'Prepare normal automatic canonical publication')
        self.assertIn('--input automatic-input', prepare['run'])
        self.assertEqual(download['if'], prepare['if'])

    def test_exact_control_only_paths_after_receipt_binding(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        job = data['jobs']['publication_prepare']
        steps = job['steps']
        receipt = next(i for i, step in enumerate(steps) if step.get('id') == 'receipt')
        full = next(i for i, step in enumerate(steps) if step.get('id') == 'upload')
        small = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload small publication control evidence')
        self.assertLess(receipt, full)
        self.assertLess(full, small)
        self.assertEqual(steps[small]['uses'], 'actions/upload-artifact@v4')
        args = steps[small]['with']
        self.assertEqual(args['name'], 'publication-control-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(args['path'].splitlines(), [
            'publication-work/control/release-validation.json',
            'publication-work/release/checksums.txt',
            'publication-work/release/build-manifest.json',
            'publication-work/release/build-inputs.env',
            'publication-work/release/resolved-source.json',
            'publication-work/release/resolved-frontend-package-lock.json',
        ])
        self.assertEqual(args['if-no-files-found'], 'error')
        self.assertEqual(args['retention-days'], 14)
        self.assertEqual(steps[full]['with']['name'], 'verified-publication-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(job['outputs']['artifact_id'], '${{ steps.upload.outputs.artifact-id }}')
        self.assertEqual(job['outputs']['receipt_sha256'], '${{ steps.receipt.outputs.sha256 }}')
        self.assertEqual(job['permissions']['contents'], 'read')
        repair = data['jobs']['publication_repair']
        self.assertEqual(repair['permissions']['contents'], 'write')
        self.assertEqual(repair['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')

    def test_automatic_writer_requires_successful_preparation_and_receipt_binding(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        job = data['jobs']['automatic_publication']
        self.assertIn("needs.publication_prepare.result == 'success'", job['if'])
        self.assertIn('!cancelled()', job['if'])
        self.assertEqual(job['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
        self.assertEqual(job['permissions']['contents'], 'write')
        self.assertNotIn('id-token', job['permissions'])
        commands = '\n'.join(step.get('run', '') for step in job['steps'])
        self.assertIn('automatic_publication.py publish', commands)
        self.assertNotIn('gh release delete', commands)

    def test_repair_writer_overrides_skipped_ancestors_but_fails_closed(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        job = data['jobs']['publication_repair']
        self.assertEqual(job['needs'], 'publication_prepare')
        expression = job['if'].removeprefix('${{').removesuffix('}}').strip()
        self.assertIn('!cancelled()', expression)
        for cancelled in (False, True):
            for prepared in ('success', 'failure', 'cancelled', 'skipped'):
                for event in ('workflow_dispatch', 'schedule', 'push', 'pull_request'):
                    for operation in ('promote-existing', 'validate-promotion', 'validate-automatic', 'build-only', 'build'):
                        evaluated = expression.replace('cancelled()', repr(cancelled)).replace('needs.publication_prepare.result', repr(prepared)).replace('github.event_name', repr(event)).replace('inputs.operation', repr(operation))
                        enabled = eval(evaluated.replace('!', ' not ').replace('&&', ' and '), {'__builtins__': {}})
                        expected = not cancelled and prepared == 'success' and event == 'workflow_dispatch' and operation == 'promote-existing'
                        self.assertEqual(enabled, expected)
        self.assertEqual(job['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
