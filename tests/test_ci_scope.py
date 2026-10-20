"""A small PR cannot suppress integration when a build input also changed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ci_scope


class CIScope(unittest.TestCase):
    def test_publication_docs_and_python_unit_tests_use_tests_only(self):
        self.assertFalse(ci_scope.build_required([
            'README.md', 'README_zh.md', 'docs-publication-runbook.md', 'docs/release.md',
            'tests/test_draft_publication.py', *ci_scope.PUBLICATION_ONLY]))

    def test_unknown_build_workflow_and_shared_validation_inputs_require_build(self):
        for path in ('Dockerfile', '.github/workflows/build.yml', 'config/repair-locks.json',
                     'scripts/build_release.sh', 'scripts/publication_contract.py',
                     'scripts/publication_lineage.py', 'scripts/ci_scope.py',
                     'scripts/scheduled_release.py', 'tests/go_sqlite_compat/sqlite_compat_test.go',
                     'docs/example.py', 'new-file'):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.build_required(['README.md', path]))
        self.assertTrue(ci_scope.build_required([]))

    def test_only_prs_can_use_changed_file_scope(self):
        for event in ('schedule', 'push', 'workflow_dispatch', ''):
            with self.subTest(event=event):
                self.assertTrue(ci_scope.event_requires_build(event, {}, lambda *args: b'README.md\0'))

    def test_pr_scope_uses_merge_result_when_the_base_has_advanced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)
            def commit(message):
                git('add', '.'); git('commit', '-m', message)
                return git('rev-parse', 'HEAD').decode().strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'CI scope test')
            git('config', 'user.email', 'ci-scope@example.invalid')
            (root / 'scripts').mkdir()
            (root / 'scripts/build_release.sh').write_text('original\n')
            (root / 'README.md').write_text('original\n')
            commit('base')
            git('checkout', '-b', 'docs')
            (root / 'README.md').write_text('docs-only PR\n')
            head = commit('docs')
            git('checkout', 'main')
            (root / 'scripts/build_release.sh').write_text('base-only change\n')
            base = commit('base advanced')
            git('merge', '--no-ff', 'docs', '-m', 'synthetic merge')
            merge = git('rev-parse', 'HEAD').decode().strip()
            event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
            diff = lambda left, right: git('diff', '--name-only', '--no-renames', '-z', left, right)
            self.assertTrue(ci_scope.build_required(diff(base, head).decode().split('\0')[:-1]))
            self.assertFalse(ci_scope.event_requires_build('pull_request', event, diff, merge_sha=merge))
            (root / 'docs').mkdir()
            git('mv', 'scripts/build_release.sh', 'docs/build.md')
            renamed = commit('move build source to docs')
            self.assertTrue(ci_scope.event_requires_build('pull_request', event, diff, merge_sha=renamed))

    def test_unavailable_or_malformed_diff_keeps_integration(self):
        event = {'pull_request': {'base': {'sha': 'a' * 40}, 'head': {'sha': 'b' * 40}}}
        def unavailable(*args):
            raise subprocess.CalledProcessError(128, ['git', 'diff'])
        for invalid in ({}, {'pull_request': {'base': {'sha': 'main'}, 'head': {'sha': 'b' * 40}}}):
            self.assertTrue(ci_scope.event_requires_build('pull_request', invalid, merge_sha='c' * 40))
        self.assertTrue(ci_scope.event_requires_build('pull_request', event, lambda *args: b'README.md\0'))
        for diff in (unavailable, lambda *args: b'', lambda *args: b'\xff\0'):
            self.assertTrue(ci_scope.event_requires_build('pull_request', event, diff, merge_sha='c' * 40))

    def test_workflow_does_not_treat_scope_script_failure_as_tests_only(self):
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text())
        command = next(step['run'] for step in workflow['jobs']['tests']['steps'] if step.get('id') == 'scope')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/ci_scope.py').write_text('raise RuntimeError("scope failed")\n')
            result = subprocess.run(['bash', '-c', command], cwd=root, capture_output=True, text=True,
                                    env=dict(os.environ, GITHUB_OUTPUT=str(root / 'output')))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'output').exists())
