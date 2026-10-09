import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_source_inputs as source


class SourceInputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = {'source_files_sha256': {}, 'go_version': '1.26.1'}
        for name in source.SOURCE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('module example.org/test\ngo 1.25.7\n' if name.endswith('go.mod') else name)
            self.entry['source_files_sha256'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        patched = patch.object(source, 'resolve', return_value=self.entry)
        patched.start()
        self.addCleanup(patched.stop)

    def test_exact_inputs_pass(self):
        source.verify(self.root, 'v2.3.1')

    def test_missing_extra_and_changed_inputs_fail(self):
        for case in ('missing', 'extra', 'changed', 'linked'):
            with self.subTest(case=case):
                hashes = dict(self.entry['source_files_sha256'])
                file = self.root / 'core/go.mod'
                original = file.read_bytes()
                if case == 'missing': del self.entry['source_files_sha256']['core/go.mod']
                if case == 'extra': self.entry['source_files_sha256']['../outside'] = '0' * 64
                if case == 'changed': file.write_bytes(b'changed')
                if case == 'linked':
                    (self.root / 'original').write_bytes(original)
                    file.unlink()
                    file.symlink_to(self.root / 'original')
                with self.assertRaises(ValueError): source.verify(self.root, 'v2.3.1')
                if file.is_symlink(): file.unlink()
                file.write_bytes(original)
                self.entry['source_files_sha256'] = hashes

    def test_hashed_module_requirement_must_fit_selected_compiler(self):
        for component in ('core/go.mod', 'agent/go.mod'):
            path = self.root / component
            original = path.read_bytes()
            for requirement in ('go 1.27.0', 'go 1.25.7\ntoolchain go1.27.0'):
                with self.subTest(component=component, requirement=requirement):
                    path.write_text('module example.org/test\n' + requirement + '\n')
                    self.entry['source_files_sha256'][component] = hashlib.sha256(path.read_bytes()).hexdigest()
                    with self.assertRaises(ValueError):
                        source.verify(self.root, 'v2.3.1')
            path.write_bytes(original)
            self.entry['source_files_sha256'][component] = hashlib.sha256(original).hexdigest()

    def test_docker_checks_before_patch_and_enforces_engines(self):
        text = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertLess(text.index('verify_source_inputs.py'), text.index('patch_backend_xpack_compat.mjs'))
        self.assertIn('npm ci --engine-strict', text)

class ReviewedLocks(unittest.TestCase):
    def test_all_enabled_sources_resolve_exact_operational_inputs(self):
        import json
        import resolve_inputs
        for version, expected in json.loads(resolve_inputs.LOCK.read_text()).items():
            with self.subTest(version=version):
                entry = resolve_inputs.resolve(version)
                self.assertEqual(entry, expected)
                self.assertEqual(set(entry['source_files_sha256']) | set(entry.get('source_files_absent', [])), source.SOURCE_FILES)

    def test_incomplete_hashes_and_nonexact_toolchain_rejected(self):
        import json
        import resolve_inputs
        original = json.loads(resolve_inputs.LOCK.read_text())
        for case in ('missing', 'digest', 'node', 'npm', 'go', 'source_commit', 'installer_commit'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                data = json.loads(json.dumps(original))
                entry = data['v2.3.2']
                if case == 'missing': del entry['source_files_sha256']['core/go.mod']
                elif case == 'digest': entry['source_files_sha256']['core/go.mod'] = 'bad'
                elif case.endswith('_commit'): entry[case] = 'latest'
                else: entry[case + '_version'] = 'latest'
                path = Path(directory) / 'sources.json'
                path.write_text(json.dumps(data))
                with patch.object(resolve_inputs, 'LOCK', path):
                    with self.assertRaises(ValueError): resolve_inputs.resolve('v2.3.2')

class HistoricalInputCohorts(unittest.TestCase):
    def test_go_requirements_are_checked_from_actual_hashed_modules(self):
        for component in ('core/go.mod', 'agent/go.mod'):
            for text, selected, ok in (
                ('module example.org/test\ngo 1.25.7\n', '1.25.7', True),
                ('go 1.24 // minimum\ntoolchain default\n', '1.24.0', True),
                ('go 1.24\ntoolchain go1.25.7\n', '1.26.1', True),
                ('go 1.25.7\n', '1.25.6', False),
                ('go 1.24\ntoolchain go1.25.7\n', '1.25.6', False),
                ('module example.org/test\n', '1.26.1', False),
                ('go 1.25\ngo 1.24\n', '1.26.1', False),
                ('go latest\n', '1.26.1', False),
                ('go 1.25\ntoolchain latest\n', '1.26.1', False),
            ):
                with self.subTest(component=component, text=text, selected=selected):
                    if ok:
                        source.verify_go_requirements(text.encode(), selected, component)
                    else:
                        with self.assertRaises(ValueError):
                            source.verify_go_requirements(text.encode(), selected, component)

    def test_enabled_configs_bind_same_source_commit(self):
        import json
        import resolve_inputs
        import embedded_configuration
        for name, entry in json.loads(resolve_inputs.LOCK.read_text()).items():
            for component in ('core', 'agent'):
                original, normalized, reviewed_commit = embedded_configuration.expected_bytes(name, component)
                self.assertEqual(reviewed_commit, entry['source_commit'], (name, component))
                self.assertTrue(original)
                self.assertTrue(normalized)

class AbsentSourceLock(unittest.TestCase):
    def test_only_reviewed_lock_absence_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {'source_files_sha256': {}, 'source_files_absent': ['frontend/package-lock.json'], 'go_version': '1.26.1'}
            for name in source.SOURCE_FILES - {'frontend/package-lock.json'}:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('module example.org/test\ngo 1.25.7\n' if name.endswith('go.mod') else name)
                entry['source_files_sha256'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(source, 'resolve', return_value=entry):
                source.verify(root, 'v2.1.10')
                lock = root / 'frontend/package-lock.json'
                lock.write_text('unexpected')
                with self.assertRaises(ValueError): source.verify(root, 'v2.1.10')
                lock.unlink()
                lock.symlink_to(root / 'missing')
                with self.assertRaises(ValueError): source.verify(root, 'v2.1.10')
                lock.unlink()
                entry['source_files_absent'] = ['core/go.mod']
                with self.assertRaises(ValueError): source.verify(root, 'v2.1.10')
