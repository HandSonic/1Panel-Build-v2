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
                     'tests/go_sqlite_compat/sqlite_compat_test.go',
                     'docs/example.py', 'new-file'):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.build_required(['README.md', path]))
        self.assertTrue(ci_scope.build_required([]))

    def test_only_prs_can_use_changed_file_scope(self):
        for event in ('schedule', 'push', 'workflow_dispatch', ''):
            with self.subTest(event=event):
                self.assertTrue(ci_scope.event_requires_build(event, {}, lambda *args: b'README.md\0'))

    def test_schedule_resolves_current_inputs_and_keeps_compilation_enabled(self):
        from test_ci_contract import github_block
        workflow=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/build.yml').read_text())
        jobs=workflow['jobs']
        self.assertTrue(ci_scope.event_requires_build('schedule',{}))
        self.assertNotIn('build_required',jobs['prepare']['outputs'])
        for step in jobs['prepare']['steps']:
            if step.get('id') in ('prepare','source_upload'):self.assertNotIn('if',step)
        for name in ('compile','build'):
            self.assertNotIn('build_required',jobs[name].get('if',''))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'scripts').mkdir()
            (root/'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()\n')
            (root/'scripts/discover_inputs.py').write_text(
                'import sys\nfrom pathlib import Path\n'
                'with Path("discovery-calls").open("a") as out: out.write(" ".join(sys.argv[1:])+"\\n")\n'
                'print("v2.4.0" if "--latest" in sys.argv else "a"*64)\n')
            (root/'scripts/resolved_contract.py').write_text('')
            (root/'scripts/resolve_inputs.py').write_text('print("SOURCE_COMMIT=current-source\\nGO_VERSION=1.26.1")\n')
            env=dict(os.environ,INPUT_VERSION='',INPUT_ARCHES='amd64 arm64',INPUT_CHANNEL='stable',
                GITHUB_EVENT_NAME='schedule',GITHUB_REF='refs/heads/master',
                GITHUB_OUTPUT=str(root/'outputs'),GITHUB_ENV=str(root/'env'))
            result=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],
                cwd=root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((root/'discovery-calls').read_text().splitlines(),
                ['--latest --mode stable','--version v2.4.0 --output resolved-source.json'])
            self.assertIn('SOURCE_COMMIT=current-source',(root/'build-inputs.env').read_text())
            self.assertIn('resolved_contract_sha256='+'a'*64,(root/'outputs').read_text())

    def test_diff_includes_deleted_source_of_a_rename(self):
        event = {'pull_request': {'base': {'sha': 'a' * 40}, 'head': {'sha': 'b' * 40}}}
        self.assertFalse(ci_scope.event_requires_build('pull_request', event, lambda *args: b'README.md\0'))
        self.assertTrue(ci_scope.event_requires_build('pull_request', event,
            lambda *args: b'scripts/build_release.sh\0docs/build.md\0'))

    def test_unavailable_or_malformed_diff_keeps_integration(self):
        event = {'pull_request': {'base': {'sha': 'a' * 40}, 'head': {'sha': 'b' * 40}}}
        def unavailable(*args):
            raise subprocess.CalledProcessError(128, ['git', 'diff'])
        for invalid in ({}, {'pull_request': {'base': {'sha': 'main'}, 'head': {'sha': 'b' * 40}}}):
            self.assertTrue(ci_scope.event_requires_build('pull_request', invalid))
        for diff in (unavailable, lambda *args: b'', lambda *args: b'\xff\0'):
            self.assertTrue(ci_scope.event_requires_build('pull_request', event, diff))

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
