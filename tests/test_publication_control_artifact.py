"""The small readback artifact must not alter the full publication binding."""
from pathlib import Path
import unittest
import yaml

class PublicationControlArtifact(unittest.TestCase):
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
