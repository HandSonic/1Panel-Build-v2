import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import repair_frontend_lock as subject


class FrontendLockRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'package-lock.json'
        self.original = b'{"lockfileVersion":3,"packages":{"existing":{"version":"1.0.0"}}}'
        self.target.write_bytes(self.original)
        self.additions = {'node_modules/missing': {'version': '9.0.2', 'integrity': 'pinned'}}
        derived = json.loads(self.original)
        derived['packages'].update(self.additions)
        self.derived = (json.dumps(derived, indent=2) + '\n').encode()
        digest = lambda data: hashlib.sha256(data).hexdigest()
        self.recipe = {'original_sha256': digest(self.original), 'derived_sha256': digest(self.derived)}
        self.config = {'versions': {'v2.1.13': self.recipe}, 'additions': self.additions}
        self.path = self.root / 'repairs.json'
        self.path.write_text(json.dumps(self.config))
        self.entry = {'source_files_sha256': {'frontend/package-lock.json': digest(self.original)}}
        for handle in [patch.object(subject, 'REPAIRS', self.path), patch.object(subject, 'resolve', return_value=self.entry)]:
            handle.start()
            self.addCleanup(handle.stop)

    def test_only_addition_and_exact_derived_bytes(self):
        subject.repair(self.root, 'v2.1.13')
        self.assertEqual(self.target.read_bytes(), self.derived)
        self.assertEqual(json.loads(self.target.read_bytes())['packages']['existing'], {'version': '1.0.0'})

    def test_unconfigured_version_is_unchanged(self):
        subject.repair(self.root, 'v2.3.2')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_bad_source_or_recipe_fails_without_writing(self):
        for case in ('source', 'binding', 'derived', 'existing'):
            with self.subTest(case=case):
                self.target.write_bytes(self.original)
                config = json.loads(json.dumps(self.config))
                if case == 'source': self.target.write_bytes(b'changed')
                if case == 'binding': config['versions']['v2.1.13']['original_sha256'] = '0' * 64
                if case == 'derived': config['versions']['v2.1.13']['derived_sha256'] = '0' * 64
                if case == 'existing': config['additions'] = {'existing': {'version': '2.0.0'}}
                self.path.write_text(json.dumps(config))
                before = self.target.read_bytes()
                with self.assertRaises(ValueError): subject.repair(self.root, 'v2.1.13')
                self.assertEqual(self.target.read_bytes(), before)

    def test_symlink_rejected(self):
        self.target.rename(self.root / 'actual')
        self.target.symlink_to(self.root / 'actual')
        with self.assertRaises(ValueError): subject.repair(self.root, 'v2.1.13')
        self.assertEqual((self.root / 'actual').read_bytes(), self.original)

    def test_pipeline_checks_original_then_repairs_before_ci(self):
        text = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertLess(text.index('verify_source_inputs.py'), text.index('repair_frontend_lock.py'))
        self.assertLess(text.index('repair_frontend_lock.py'), text.index('npm ci --engine-strict'))


class AbsentFrontendLockRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.front = self.root / 'frontend'
        self.front.mkdir()
        self.target = self.front / 'package-lock.json'
        self.package = b'{"name":"test","version":"1"}'
        (self.front / 'package.json').write_bytes(self.package)
        self.derived = b'{"lockfileVersion":3,"packages":{}}\n'
        self.lock = self.root / 'frontend-locks/v2.1.10.package-lock.json'
        self.lock.parent.mkdir()
        self.lock.write_bytes(self.derived)
        digest = lambda data: hashlib.sha256(data).hexdigest()
        self.recipe = {'original_absent': True, 'derived_sha256': digest(self.derived), 'reviewed_lock_file': 'frontend-locks/v2.1.10.package-lock.json'}
        self.config = {'versions': {'v2.1.10': self.recipe}, 'additions': {}}
        self.path = self.root / 'repairs.json'
        self.path.write_text(json.dumps(self.config))
        self.entry = {'source_files_absent': ['frontend/package-lock.json'], 'frontend_repair_lock_sha256': digest(self.derived), 'source_files_sha256': {'frontend/package.json': digest(self.package)}}
        for handle in [patch.object(subject, 'REPAIRS', self.path), patch.object(subject, 'resolve', return_value=self.entry)]:
            handle.start()
            self.addCleanup(handle.stop)

    def test_creates_exact_pinned_lock_once(self):
        subject.repair(self.front, 'v2.1.10')
        self.assertEqual(self.target.read_bytes(), self.derived)
        with self.assertRaises(ValueError): subject.repair(self.front, 'v2.1.10')
        self.assertEqual(self.target.read_bytes(), self.derived)

    def test_manifest_and_derived_integrity_before_create(self):
        for case in ('manifest', 'derived', 'binding', 'absence', 'path'):
            with self.subTest(case=case):
                (self.front / 'package.json').write_bytes(self.package)
                self.lock.write_bytes(self.derived)
                entry = json.loads(json.dumps(self.entry))
                config = json.loads(json.dumps(self.config))
                if case == 'manifest': (self.front / 'package.json').write_bytes(b'changed')
                if case == 'derived': self.lock.write_bytes(b'changed')
                if case == 'binding': entry['frontend_repair_lock_sha256'] = '0' * 64
                if case == 'absence': entry['source_files_absent'] = []
                if case == 'path': config['versions']['v2.1.10']['reviewed_lock_file'] = '../outside'
                self.path.write_text(json.dumps(config))
                with patch.object(subject, 'resolve', return_value=entry), self.assertRaises(ValueError):
                    subject.repair(self.front, 'v2.1.10')
                self.assertFalse(self.target.exists())

    def test_dangling_existing_lock_is_rejected(self):
        self.target.symlink_to(self.root / 'nonexistent')
        with self.assertRaises(ValueError): subject.repair(self.front, 'v2.1.10')
        self.assertTrue(self.target.is_symlink())

    def test_content_addressed_lock_reuses_exact_bytes(self):
        name = 'frontend-locks/' + self.recipe['derived_sha256'] + '.package-lock.json'
        self.lock.rename(self.root / name)
        self.recipe['reviewed_lock_file'] = name
        self.path.write_text(json.dumps(self.config))
        subject.repair(self.front, 'v2.1.10')
        self.assertEqual(self.target.read_bytes(), self.derived)

    def test_content_addressed_wrong_digest_path_rejected(self):
        name = 'frontend-locks/' + '0' * 64 + '.package-lock.json'
        self.lock.rename(self.root / name)
        self.recipe['reviewed_lock_file'] = name
        self.path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            subject.repair(self.front, 'v2.1.10')
        self.assertFalse(self.target.exists())
