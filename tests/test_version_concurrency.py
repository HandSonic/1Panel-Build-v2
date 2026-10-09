"""Protect release identity while allowing independent versions to overlap."""
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def key(template, **values):
    return re.sub(r'\$\{\{\s*([\w.]+)\s*\}\}', lambda m: str(values[m[1]]), template).lower()


class VersionConcurrency(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / '.github/workflows/build.yml').read_text())
        self.jobs = self.workflow['jobs']
        self.context = {'github.repository': 'HandSonic/1Panel-Build-v2', 'github.run_id': 10,
                        'github.run_attempt': 1, 'github.ref': 'refs/heads/master',
                        'inputs.version': 'v2.1.10', 'needs.build.outputs.version': 'v2.1.10'}

    def test_independent_read_only_runs_do_not_share_a_pending_slot(self):
        concurrency = self.workflow['concurrency']
        self.assertFalse(concurrency['cancel-in-progress'])
        first = key(concurrency['group'], **self.context)
        for change in ({'github.run_id': 11}, {'github.run_attempt': 2}):
            self.assertNotEqual(first, key(concurrency['group'], **(self.context | change)))

    def test_same_release_writer_serializes_across_refs_and_runs(self):
        concurrency = self.jobs['publication_repair']['concurrency']
        self.assertFalse(concurrency['cancel-in-progress'])
        self.assertEqual(concurrency['queue'], 'max')
        template = concurrency['group']
        first = key(template, **self.context)
        self.assertEqual(first, key(template, **(self.context | {'github.ref': 'refs/heads/repair', 'github.run_id': 99})))
        self.assertEqual(first, key(template, **(self.context | {'inputs.version': 'V2.1.10'})))
        self.assertNotEqual(first, key(template, **(self.context | {'inputs.version': 'v2.1.9'})))
        self.assertEqual(self.jobs['publication_repair']['env']['RELEASE_TAG'], '${{ inputs.version }}')

    def test_candidate_retries_share_own_tag_but_not_canonical_or_other_runs(self):
        concurrency = self.jobs['candidate']['concurrency']
        self.assertEqual(concurrency['queue'], 'max')
        self.assertFalse(concurrency['cancel-in-progress'])
        template = concurrency['group']
        first = key(template, **self.context)
        self.assertEqual(first, key(template, **(self.context | {'github.run_attempt': 2})))
        self.assertNotEqual(first, key(template, **(self.context | {'github.run_id': 11})))
        self.assertNotEqual(first, key(self.jobs['publication_repair']['concurrency']['group'], **self.context))
        script = '\n'.join(step.get('run', '') for step in self.jobs['candidate']['steps'])
        self.assertIn('tag="$VERSION-build-${GITHUB_SHA:0:12}-${GITHUB_RUN_ID}"', script)
        self.assertIn('--draft', script)
        self.assertIn('Candidate already exists; refusing overwrite.', script)
        self.assertNotIn('--latest', script)

    def test_all_writers_are_locked_and_resource_limit_remains(self):
        writers = {name for name, job in self.jobs.items() if job.get('permissions', {}).get('contents') == 'write'}
        self.assertEqual(writers, {'candidate', 'publication_repair'})
        for name in writers:
            self.assertIn('concurrency', self.jobs[name])
        self.assertEqual(self.jobs['compile']['strategy']['max-parallel'], 3)
        self.assertFalse(self.jobs['compile']['strategy']['fail-fast'])
        self.assertEqual(self.jobs['publication_repair']['needs'], 'publication_prepare')
        self.assertIn("inputs.operation == 'promote-existing'", self.jobs['publication_repair']['if'])
        self.assertEqual(self.jobs['publication_repair']['env']['EXPECTED_VALIDATION_RECEIPT_SHA256'], '${{ needs.publication_prepare.outputs.receipt_sha256 }}')
