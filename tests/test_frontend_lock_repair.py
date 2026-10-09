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
