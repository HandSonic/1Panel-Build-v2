"""Compilation scope is conservative and never depends on remote release state."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from ci_scope import decide, requires_compile


class CompilationScopeTests(unittest.TestCase):
    def test_publication_docs_and_test_changes_only_need_unit_ci(self):
        needed = requires_compile('pull_request', [
            'scripts/automatic_publication.py', 'scripts/release_asset_repair.py',
            'scripts/public_packages.py', 'tests/test_automatic_publication.py',
            'docs-publication-runbook.md'])
        self.assertFalse(needed)
        jobs = yaml.safe_load((ROOT / '.github/workflows/build.yml').read_text())['jobs']
        expression = jobs['prepare']['if'].replace('needs.tests.outputs.build_required', repr(str(needed).lower()))
        expression = expression.replace('github.event_name', repr('pull_request')).replace('inputs.operation', repr(''))
        self.assertFalse(eval(expression.replace('&&', ' and ').replace('||', ' or '), {'__builtins__': {}}))

    def test_build_unknown_and_workflow_changes_compile_even_when_mixed(self):
        for path in ('Dockerfile', 'scripts/package_release.py', 'config/inputs.json',
                     '.github/workflows/build.yml', 'scripts/ci_scope.py', 'unknown'):
            with self.subTest(path=path):
                self.assertTrue(requires_compile('pull_request', ['README.md', path]))

    def test_schedules_tags_and_manual_builds_always_compile(self):
        for event in ('schedule', 'push', 'workflow_dispatch'):
            self.assertTrue(requires_compile(event, ['README.md']))
        self.assertTrue(requires_compile('pull_request', []))

    def test_real_go_smoke_source_requires_compilation(self):
        self.assertTrue(requires_compile('pull_request', ['tests/go_sqlite_compat/sqlite_compat_test.go']))

    def test_build_input_renamed_into_docs_still_requires_compilation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(['git', '-c', 'user.name=CI Test', '-c',
                    'user.email=ci-test@example.invalid', *args], cwd=root, stderr=subprocess.DEVNULL)
            git('init')
            (root / 'Dockerfile').write_text('FROM scratch\n' * 10)
            git('add', '.')
            git('commit', '-m', 'initial')
            base = git('rev-parse', 'HEAD').decode().strip()
            (root / 'docs').mkdir()
            os.rename(root / 'Dockerfile', root / 'docs/Dockerfile')
            git('add', '-A')
            git('commit', '-m', 'move build input')
            head = git('rev-parse', 'HEAD').decode().strip()
            self.assertTrue(decide({'GITHUB_EVENT_NAME': 'pull_request', 'PR_BASE_SHA': base,
                                   'PR_HEAD_SHA': head}, lambda command: subprocess.check_output(command, cwd=root)))

    def test_exact_full_pr_diff_and_unknown_diff_default_to_compile(self):
        env = {'GITHUB_EVENT_NAME': 'pull_request', 'PR_BASE_SHA': 'a' * 40, 'PR_HEAD_SHA': 'b' * 40}
        read = Mock(return_value=b'README.md\0scripts/automatic_publication.py\0')
        self.assertFalse(decide(env, read))
        self.assertEqual(read.call_args.args[0], ['git', 'diff', '--no-renames', '--name-only', '-z', 'a' * 40 + '...' + 'b' * 40])
        self.assertTrue(decide(dict(env, PR_BASE_SHA='invalid'), read))
        read.side_effect = subprocess.CalledProcessError(1, 'git')
        self.assertTrue(decide(env, read))

    def test_workflow_keeps_tests_and_uses_scope_only_for_compile_ancestors(self):
        jobs = yaml.safe_load((ROOT / '.github/workflows/build.yml').read_text())['jobs']
        self.assertNotIn('if', jobs['tests'])
        self.assertEqual(jobs['tests']['outputs']['build_required'], '${{ steps.scope.outputs.build_required }}')
        self.assertIn("needs.tests.outputs.build_required == 'true'", jobs['prepare']['if'])
        checkout = next(step for step in jobs['tests']['steps'] if step.get('uses') == 'actions/checkout@v4')
        self.assertEqual(checkout['with']['fetch-depth'], 0)
