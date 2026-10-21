"""Protect release identity while allowing independent versions to overlap."""
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def key(template, **values):
    return re.sub(r'\$\{\{\s*([\w.]+(?:\s*\|\|\s*[\w.]+)?)\s*\}\}',
                  lambda m: str(next((values.get(name.strip()) for name in m[1].split('||')
                                      if values.get(name.strip())), '')), template).lower()


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
        self.assertNotEqual(first, key(template, **(self.context | {'inputs.version': 'v2.1.9'})))

    def test_candidate_retries_share_own_tag_but_not_canonical_or_other_runs(self):
        concurrency = self.jobs['candidate']['concurrency']
        self.assertEqual(concurrency['queue'], 'max')
        self.assertFalse(concurrency['cancel-in-progress'])
        template = concurrency['group']
        first = key(template, **self.context)
        self.assertEqual(first, key(template, **(self.context | {'github.run_attempt': 2})))
        self.assertNotEqual(first, key(template, **(self.context | {'github.run_id': 11})))
        self.assertNotEqual(first, key(template, **(self.context | {'needs.build.outputs.version': 'v2.1.9'})))
        self.assertNotEqual(first, key(self.jobs['publication_repair']['concurrency']['group'], **self.context))

    def test_automatic_and_repair_writers_share_exact_canonical_lock(self):
        auto = self.jobs['automatic_publication']
        self.assertFalse(auto['concurrency']['cancel-in-progress'])
        self.assertEqual(auto['concurrency']['queue'], 'max')
        context = self.context | {'needs.publication_prepare.outputs.version': 'v2.1.10',
                                  'needs.prepare.outputs.version': 'v2.1.10'}
        auto_key = key(auto['concurrency']['group'], **context)
        repair_key = key(self.jobs['publication_repair']['concurrency']['group'], **context)
        self.assertEqual(auto_key, repair_key)
        checksum_context = context | {'needs.publication_prepare.outputs.version': '',
                                      'needs.check_update.outputs.version': 'v2.1.10'}
        self.assertEqual(key(auto['concurrency']['group'], **checksum_context), repair_key)
