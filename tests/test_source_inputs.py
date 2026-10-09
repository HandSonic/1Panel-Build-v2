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
        self.entry = {'source_files_sha256': {}}
        for name in source.SOURCE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
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

    def test_docker_checks_before_patch_and_enforces_engines(self):
        text = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertLess(text.index('verify_source_inputs.py'), text.index('patch_backend_xpack_compat.mjs'))
        self.assertIn('npm ci --engine-strict', text)

class ReviewedLocks(unittest.TestCase):
    def test_all_enabled_sources_have_exact_evidenced_inputs(self):
        import json
        import resolve_inputs
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / 'config/source-input-evidence.json').read_text())
        locks = json.loads(resolve_inputs.LOCK.read_text())
        for version in locks:
            entry = resolve_inputs.resolve(version)
            files = evidence['versions'][version]['frontend']['files']
            self.assertEqual(entry['source_files_sha256'], {item['path']: item['sha256'] for item in files})
            self.assertEqual(entry['source_commit'], evidence['versions'][version]['frontend']['source_commit'])
            self.assertEqual(entry['node_version'], evidence['toolchain']['node_version'])
            self.assertEqual(entry['npm_version'], evidence['toolchain']['npm_version'])

    def test_incomplete_hashes_and_nonexact_toolchain_rejected(self):
        import json
        import resolve_inputs
        original = json.loads(resolve_inputs.LOCK.read_text())
        for case in ('missing', 'digest', 'node', 'npm', 'go'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                data = json.loads(json.dumps(original))
                entry = data['v2.3.2']
                if case == 'missing': del entry['source_files_sha256']['core/go.mod']
                elif case == 'digest': entry['source_files_sha256']['core/go.mod'] = 'bad'
                else: entry[case + '_version'] = 'latest'
                path = Path(directory) / 'sources.json'
                path.write_text(json.dumps(data))
                with patch.object(resolve_inputs, 'LOCK', path):
                    with self.assertRaises(ValueError): resolve_inputs.resolve('v2.3.2')

class HistoricalInputCohorts(unittest.TestCase):
    def test_enabled_go_versions_satisfy_both_components(self):
        import json
        import resolve_inputs
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / 'config/source-input-evidence.json').read_text())
        def version(value):
            return tuple(int(part) for part in value.removeprefix('go').split('.'))
        for name in json.loads(resolve_inputs.LOCK.read_text()):
            selected = version(resolve_inputs.resolve(name)['go_version'])
            for component, requirements in evidence['versions'][name]['go_requirements'].items():
                self.assertGreaterEqual(selected, version(requirements['go']), (name, component))
                if requirements.get('toolchain', 'default') != 'default':
                    self.assertGreaterEqual(selected, version(requirements['toolchain']), (name, component))

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
            entry = {'source_files_sha256': {}, 'source_files_absent': ['frontend/package-lock.json']}
            for name in source.SOURCE_FILES - {'frontend/package-lock.json'}:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
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
