"""Publication workflow gates and the verified build handoff."""
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
                'needs.check_update.result': repr('skipped'),
                'needs.check_update.outputs.checksum_required': repr('false'),
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

    def test_automatic_input_uses_the_verified_build_artifact_id(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        steps = data['jobs']['publication_prepare']['steps']
        selected = [step for step in steps if step.get('uses') == 'actions/download-artifact@v4']
        self.assertEqual(len(selected), 1)
        download = selected[0]
        self.assertEqual(download['with']['artifact-ids'], '${{ needs.build.outputs.artifact_id }}')

    def test_repair_writer_overrides_skipped_ancestors_but_fails_closed(self):
        data = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        job = data['jobs']['publication_repair']
        self.assertEqual(job['needs'], 'publication_prepare')
        self.assertEqual(job['permissions']['contents'], 'write')
        expression = job['if'].removeprefix('${{').removesuffix('}}').strip()
        for cancelled in (False, True):
            for prepared in ('success', 'failure', 'cancelled', 'skipped'):
                for event in ('workflow_dispatch', 'schedule', 'push', 'pull_request'):
                    for operation in ('promote-existing', 'validate-promotion', 'validate-automatic', 'build-only', 'build'):
                        evaluated = expression.replace('cancelled()', repr(cancelled)).replace('needs.publication_prepare.result', repr(prepared)).replace('github.event_name', repr(event)).replace('inputs.operation', repr(operation))
                        enabled = eval(evaluated.replace('!', ' not ').replace('&&', ' and '), {'__builtins__': {}})
                        expected = not cancelled and prepared == 'success' and event == 'workflow_dispatch' and operation == 'promote-existing'
                        self.assertEqual(enabled, expected)
        self.assertEqual(job['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
