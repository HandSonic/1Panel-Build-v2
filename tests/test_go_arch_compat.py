import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import go_arch_compat as compat


class Compat(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'go.mod').write_text('module fixture\ngo 1.23\n')
        (self.root / 'go.sum').write_text('original checksums\n')

    def graph(self, sqlite='v1.23.1', libc='v1.22.5', replacement=False):
        value = [{'Path': name, 'Version': version} for name, version in [('modernc.org/sqlite', sqlite), ('modernc.org/libc', libc)]]
        if replacement:
            value[0]['Replace'] = {'Path': '../local-sqlite'}
        return '\n'.join(json.dumps(v) for v in value)

    def test_other_architectures_do_not_touch_graph(self):
        for arch in ('amd64', 'arm64', 'arm', 'ppc64le', 's390x', 'riscv64'):
            with patch.object(compat, 'run') as run, compat.module_for_arch(self.root, arch) as modfile:
                self.assertIsNone(modfile)
                run.assert_not_called()

    def test_supported_graph_stays_original(self):
        with patch.object(compat, 'run', return_value=self.graph('v1.36.0', 'v1.61.0')) as run, patch.object(compat, 'capabilities', return_value=[]), compat.module_for_arch(self.root, 'loong64') as modfile:
            self.assertIsNone(modfile)
            self.assertEqual(run.call_count, 1)

    def test_missing_driver_does_not_introduce_one(self):
        with patch.object(compat, 'run', return_value='{"Path":"example.org/new-driver","Version":"v1.0.0"}'), patch.object(compat, 'capabilities') as probe, compat.module_for_arch(self.root, 'loong64') as modfile:
            self.assertIsNone(modfile)
            probe.assert_not_called()

    def test_old_graph_gets_paired_temporary_pins_and_cleanup(self):
        calls = []
        def run(args, cwd, arch, modfile):
            calls.append(args)
            self.assertNotEqual(modfile, self.root / 'go.mod')
            self.assertTrue(modfile.exists())
            if args[:2] == ['mod', 'edit']:
                modfile.write_text('patched temporary graph\n')
                return ''
            return self.graph() if len(calls) == 1 else self.graph('v1.29.10', 'v1.49.3')
        with patch.object(compat, 'run', side_effect=run), patch.object(compat, 'capabilities', side_effect=[['modernc.org/sqlite/lib'], []]):
            with compat.module_for_arch(self.root, 'loong64') as modfile:
                self.assertTrue(modfile.exists())
                self.assertEqual(modfile.read_text(), 'patched temporary graph\n')
                self.assertEqual((self.root / 'go.mod').read_text(), 'module fixture\ngo 1.23\n')
                self.assertEqual((self.root / 'go.sum').read_text(), 'original checksums\n')
            self.assertFalse(modfile.exists())
        self.assertEqual(calls[1], ['mod', 'edit', '-require=modernc.org/sqlite@v1.29.10', '-require=modernc.org/libc@v1.49.3'])
        self.assertEqual(list(self.root.glob('.go-loong64-*')), [])

    def test_newer_libc_is_never_downgraded(self):
        with patch.object(compat, 'run', side_effect=[self.graph(libc='v1.60.0'), '', self.graph('v1.29.10', 'v1.60.0')]) as run, patch.object(compat, 'capabilities', side_effect=[['modernc.org/sqlite/lib'], []]), compat.module_for_arch(self.root, 'loong64'):
            self.assertEqual(run.call_args_list[1].args[0], ['mod', 'edit', '-require=modernc.org/sqlite@v1.29.10'])

    def test_fail_closed_and_cleanup(self):
        cases = [
            (self.graph(replacement=True), ['modernc.org/sqlite/lib'], 'replacement'),
            (self.graph('v1.36.0', 'v1.61.0'), ['modernc.org/sqlite/lib'], 'at or above'),
        ]
        for graph, missing, message in cases:
            with self.subTest(message=message), patch.object(compat, 'run', return_value=graph), patch.object(compat, 'capabilities', return_value=missing), self.assertRaisesRegex(RuntimeError, message):
                with compat.module_for_arch(self.root, 'loong64'):
                    self.fail('must not build')
            self.assertEqual(list(self.root.glob('.go-loong64-*')), [])

    def test_missing_support_after_upgrade_fails(self):
        with patch.object(compat, 'run', side_effect=[self.graph(), '', self.graph('v1.29.10', 'v1.49.3')]), patch.object(compat, 'capabilities', return_value=['modernc.org/sqlite/lib']), self.assertRaisesRegex(RuntimeError, 'still lacks'):
            with compat.module_for_arch(self.root, 'loong64'):
                self.fail('must not build')

    def test_original_transitive_floor_is_preserved_in_overlay(self):
        before = self.graph() + '\n{"Path":"other","Version":"v1.9.0"}'
        lower = self.graph('v1.29.10', 'v1.49.3') + '\n{"Path":"other","Version":"v1.8.0"}'
        preserved = self.graph('v1.29.10', 'v1.49.3') + '\n{"Path":"other","Version":"v1.9.0"}'
        with patch.object(compat, 'run', side_effect=[before, '', lower, '', preserved]) as run, patch.object(compat, 'capabilities', side_effect=[['modernc.org/sqlite/lib'], []]):
            with compat.module_for_arch(self.root, 'loong64') as modfile:
                self.assertIsNotNone(modfile)
            self.assertEqual(run.call_args_list[3].args[0], ['mod', 'edit', '-require=other@v1.9.0'])
        self.assertEqual((self.root / 'go.sum').read_text(), 'original checksums\n')


    def test_probe_only_treats_build_constraints_as_missing_arch(self):
        value = '\n'.join(json.dumps({'ImportPath': name, 'GoFiles': ['valid.go']}) for name in compat.PACKAGES)
        with patch.object(compat, 'run', return_value=value):
            self.assertEqual(compat.capabilities(self.root, 'temporary.mod'), [])
        bad = json.loads(value.splitlines()[0]); bad['Error'] = {'Err': 'network timeout'}
        with patch.object(compat, 'run', return_value=json.dumps(bad) + '\n' + value.splitlines()[1]), self.assertRaisesRegex(RuntimeError, 'network timeout'):
            compat.capabilities(self.root, 'temporary.mod')
        bad['Error']['Err'] = 'build constraints exclude all Go files in module'
        with patch.object(compat, 'run', return_value=json.dumps(bad) + '\n' + value.splitlines()[1]):
            self.assertEqual(compat.capabilities(self.root, 'temporary.mod'), [compat.PACKAGES[0]])

    def test_semver_never_mistakes_same_pseudoversion_for_downgrade(self):
        for version in ('v1.49.3', 'v1.49.3+incompatible', 'v0.0.0-20240101000000-abcdef', 'v1.2.3-rc.1'):
            self.assertFalse(compat.older(version, version))
        self.assertTrue(compat.older('v1.29.10-rc.1', 'v1.29.10'))
        self.assertTrue(compat.older('v1.29.9', 'v1.29.10'))
        self.assertFalse(compat.older('v1.29.11', 'v1.29.10'))
        self.assertFalse(compat.older('v1.29.10', 'v1.29.10-rc.1'))
        with self.assertRaises(ValueError):
            compat.older('latest', 'v1.29.10')

    def test_cli_environment_is_isolated_from_workspace_and_go_flags(self):
        import subprocess
        with patch.dict(compat.os.environ, {'GOFLAGS': '-mod=vendor', 'GOWORK': '/unrelated/go.work', 'GOARM': '7'}), patch.object(compat.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as process:
            compat.run(['list', '-m', '-json', 'all'], self.root, 'loong64', 'isolated.mod')
            args, kwargs = process.call_args
            self.assertEqual(args[0][:4], ['go', 'list', '-modfile=isolated.mod', '-mod=mod'])
            self.assertEqual(kwargs['env']['GOWORK'], 'off')
            self.assertEqual(kwargs['env']['GOFLAGS'], '')
            self.assertEqual(kwargs['env']['GOARCH'], 'loong64')
            self.assertEqual(kwargs['env']['GOARM'], '')
            compat.run(['mod', 'edit', '-require=modernc.org/libc@v1.49.3'], self.root, 'loong64', 'isolated.mod')
            self.assertEqual(process.call_args.kwargs['env']['GOFLAGS'], '-modfile=isolated.mod')

    def test_smoke_never_introduces_absent_driver(self):
        import io
        from contextlib import redirect_stdout
        import verify_go_sqlite_compat as smoke
        output = io.StringIO()
        with patch.object(smoke, 'run', return_value='{"Path":"example.org/new-driver","Version":"v1.0.0"}'), patch.object(smoke, 'command') as command, patch.object(smoke, 'module_for_arch') as overlay, redirect_stdout(output):
            smoke.verify(self.root)
            command.assert_not_called()
            overlay.assert_not_called()
        self.assertIn('NOT RUN', output.getvalue())
        self.assertIn('no test-only driver will be introduced', output.getvalue())
        self.assertNotIn('checks passed', output.getvalue())
        self.assertEqual(list(self.root.glob('.sqlite-compat-*')), [])
        self.assertEqual((self.root / 'go.mod').read_text(), 'module fixture\ngo 1.23\n')

    def test_unchanged_sqlite_graph_runs_one_smoke_per_engine(self):
        import io
        from contextlib import redirect_stdout
        import verify_go_sqlite_compat as smoke
        graph = self.graph() + '\n' + '\n'.join(json.dumps({'Path': name}) for name in (
            'github.com/glebarez/sqlite', 'github.com/glebarez/go-sqlite', 'gorm.io/gorm'))
        for qemu in (None, 'qemu-loongarch64'):
            with self.subTest(qemu=qemu), patch.object(smoke, 'run', return_value=graph), patch.object(smoke, 'command') as command, patch.object(smoke, 'module_for_arch') as overlay, redirect_stdout(io.StringIO()):
                overlay.return_value.__enter__.return_value = None
                smoke.verify(self.root, qemu)
                calls = command.call_args_list
                self.assertEqual(len(calls), 3 if qemu else 2)
                self.assertIn('-c', calls[0].args[0])
                self.assertEqual(calls[0].args[2]['GOARCH'], 'loong64')
                self.assertEqual(calls[1].args[0][:2], ['go', 'test'])
                self.assertEqual(calls[1].args[2]['SQLITE_COMPAT_PHASE'], 'baseline')
                if qemu:
                    self.assertEqual(calls[2].args[0][0], qemu)
                    self.assertEqual(calls[2].args[2]['SQLITE_COMPAT_PHASE'], 'baseline')
                    self.assertNotEqual(calls[1].args[2]['SQLITE_COMPAT_DB'], calls[2].args[2]['SQLITE_COMPAT_DB'])
            self.assertEqual(list(self.root.glob('.sqlite-compat-*')), [])



class WorkflowIntegration(unittest.TestCase):
    def test_loong64_requires_runtime_smoke_before_packaging(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/build.yml').read_text()
        smoke = workflow.index('      - name: Verify SQLite compatibility on native and loong64 engines')
        build = workflow.index('      - name: Build and validate every requested architecture')
        self.assertLess(smoke, build)
        block = workflow[smoke:build]
        self.assertIn("if: matrix.arch == 'loong64'", block)
        self.assertIn('--qemu qemu-loongarch64', block)
        self.assertNotIn('continue-on-error', block)
