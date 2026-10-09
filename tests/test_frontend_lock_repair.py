import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import repair_frontend_lock as subject
import lock_catalog
import resolved_contract
from test_discovered_inputs import FakeVendor, fixture, install_fixture


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RepairFixture(unittest.TestCase):
    absent = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.front = self.root / 'frontend'
        self.front.mkdir()
        self.target = self.front / 'package-lock.json'
        vendor = FakeVendor()
        self.value = fixture(vendor=vendor)
        self.package = vendor.files['frontend/package.json']
        (self.front / 'package.json').write_bytes(self.package)
        self.original = vendor.files['frontend/package-lock.json']
        self.additions = {'node_modules/missing': {'version': '9.0.2', 'integrity': 'synthetic-pinned'}}
        data = json.loads(self.original)
        data['packages'].update(self.additions)
        self.derived = (json.dumps(data, indent=2) + '\n').encode()
        self.recipe = {'manifest_sha256': digest(self.package), 'original_sha256': None if self.absent else digest(self.original), 'derived_sha256': digest(self.derived)}
        if self.absent:
            self.value['source']['absent'] = ['frontend/package-lock.json']
            del self.value['source']['files']['frontend/package-lock.json']
            self.recipe['reviewed_lock_file'] = 'frontend-locks/' + digest(self.derived) + '.package-lock.json'
            self.lock = self.root / 'config' / self.recipe['reviewed_lock_file']
            self.lock.parent.mkdir(parents=True)
            self.lock.write_bytes(self.derived)
        else:
            self.target.write_bytes(self.original)
        self.catalog = {'schema': 1, 'recipes': [self.recipe], 'additions': self.additions}
        self.write_inputs()
        # Any accidental old version-registry read must fail rather than pass silently.
        for name in ('sources.json', 'embedded-configs.json', 'frontend-lock-repairs.json'):
            (self.root / 'config' / name).write_text('poisoned legacy registry')
        for handle in [patch.object(subject, 'runtime_contract', side_effect=lambda version: resolved_contract.runtime_contract(version, self.root)),
                       patch.object(subject, 'apply', side_effect=lambda front, value: lock_catalog.apply(front, value, self.root))]:
            handle.start()
            self.addCleanup(handle.stop)

    def write_inputs(self, rebind=True):
        if rebind:
            self.value['frontend_lock'] = {'kind': 'derived', 'sha256': self.recipe['derived_sha256'], 'manifest_sha256': self.recipe['manifest_sha256'], 'recipe_sha256': resolved_contract.digest(self.recipe)}
        install_fixture(self.root, self.value)
        (self.root / 'config/repair-locks.json').write_text(json.dumps(self.catalog))

    def repair(self):
        subject.repair(self.front, self.value['version'])


class FrontendLockRepair(RepairFixture):
    def test_only_addition_and_exact_derived_bytes(self):
        self.repair()
        self.assertEqual(self.target.read_bytes(), self.derived)
        original = json.loads(self.original)['packages']
        for name, expected in original.items():
            self.assertEqual(json.loads(self.target.read_bytes())['packages'][name], expected)

    def test_source_lock_needs_no_repair_recipe(self):
        self.value = fixture()
        self.catalog['recipes'] = []
        self.write_inputs(rebind=False)
        self.repair()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_unlisted_version_reuses_exact_manifest_and_source_lock(self):
        self.value = fixture(version='v2.654.321')
        self.write_inputs()
        self.repair()
        self.assertEqual(self.target.read_bytes(), self.derived)
        self.assertNotIn('versions', self.catalog)
        self.assertFalse(any('version' in name for name in self.recipe))

    def test_bad_source_or_recipe_fails_without_writing(self):
        baseline_value, baseline_recipe, baseline_catalog = copy.deepcopy((self.value, self.recipe, self.catalog))
        for case in ('source', 'manifest', 'binding', 'derived', 'existing', 'recipe-digest', 'absent', 'replacement', 'no-recipe', 'ambiguous'):
            with self.subTest(case=case):
                self.value, self.recipe, self.catalog = copy.deepcopy((baseline_value, baseline_recipe, baseline_catalog))
                self.catalog['recipes'] = [self.recipe]
                self.target.write_bytes(self.original)
                (self.front / 'package.json').write_bytes(self.package)
                if case == 'source': self.target.write_bytes(b'changed')
                if case == 'manifest': (self.front / 'package.json').write_bytes(b'changed')
                if case == 'binding': self.recipe['original_sha256'] = '0' * 64
                if case == 'derived': self.recipe['derived_sha256'] = '0' * 64
                if case == 'existing': self.catalog['additions'] = {'node_modules/example': {'version': '2.0.0'}}
                if case == 'absent': self.value['source']['absent'] = ['frontend/package-lock.json']
                if case == 'replacement': self.recipe['reviewed_lock_file'] = 'frontend-locks/unexpected.json'
                if case == 'no-recipe': self.catalog['recipes'] = []
                if case == 'ambiguous': self.catalog['recipes'].append(copy.deepcopy(self.recipe))
                self.write_inputs()
                if case == 'recipe-digest':
                    self.value['frontend_lock']['recipe_sha256'] = '0' * 64
                    self.write_inputs(rebind=False)
                before = self.target.read_bytes()
                with self.assertRaises(ValueError): self.repair()
                self.assertEqual(self.target.read_bytes(), before)

    def test_symlinked_manifest_and_source_lock_rejected(self):
        for filename in ('package.json', 'package-lock.json'):
            with self.subTest(filename=filename):
                target = self.front / filename
                actual = self.front / (filename + '.actual')
                target.rename(actual)
                target.symlink_to(actual)
                before = actual.read_bytes()
                with self.assertRaises(ValueError): self.repair()
                self.assertEqual(actual.read_bytes(), before)
                target.unlink()
                actual.rename(target)

    def test_missing_original_lock_rejected(self):
        self.target.unlink()
        with self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.target.exists())

    def test_pipeline_checks_original_then_repairs_before_ci(self):
        text = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
        self.assertLess(text.index('verify_source_inputs.py'), text.index('repair_frontend_lock.py'))
        self.assertLess(text.index('repair_frontend_lock.py'), text.index('npm ci --engine-strict'))


class AbsentFrontendLockRepair(RepairFixture):
    absent = True

    def test_creates_exact_content_addressed_lock_once(self):
        self.repair()
        self.assertEqual(self.target.read_bytes(), self.derived)
        self.assertEqual(self.lock.name, digest(self.derived) + '.package-lock.json')
        with self.assertRaises(ValueError): self.repair()
        self.assertEqual(self.target.read_bytes(), self.derived)

    def test_manifest_derived_binding_absence_and_path_checked_before_create(self):
        baseline_value, baseline_recipe, baseline_catalog = copy.deepcopy((self.value, self.recipe, self.catalog))
        for case in ('manifest', 'derived', 'binding', 'absence', 'path', 'wrong-digest-path', 'original-sha', 'recipe-digest', 'manifest-sha'):
            with self.subTest(case=case):
                self.value, self.recipe, self.catalog = copy.deepcopy((baseline_value, baseline_recipe, baseline_catalog))
                self.catalog['recipes'] = [self.recipe]
                (self.front / 'package.json').write_bytes(self.package)
                self.lock.write_bytes(self.derived)
                if case == 'manifest': (self.front / 'package.json').write_bytes(b'changed')
                if case == 'derived': self.lock.write_bytes(b'changed')
                if case == 'absence': self.value['source']['absent'] = []
                if case == 'path': self.recipe['reviewed_lock_file'] = '../outside'
                if case == 'wrong-digest-path': self.recipe['reviewed_lock_file'] = 'frontend-locks/' + '0' * 64 + '.package-lock.json'
                if case == 'original-sha': self.recipe['original_sha256'] = '0' * 64
                if case == 'manifest-sha': self.recipe['manifest_sha256'] = '0' * 64
                self.write_inputs()
                if case in ('binding', 'recipe-digest'):
                    self.value['frontend_lock']['sha256' if case == 'binding' else 'recipe_sha256'] = '0' * 64
                    self.write_inputs(rebind=False)
                with self.assertRaises(ValueError): self.repair()
                self.assertFalse(self.target.exists())

    def test_dangling_existing_lock_is_rejected(self):
        self.target.symlink_to(self.root / 'nonexistent')
        with self.assertRaises(ValueError): self.repair()
        self.assertTrue(self.target.is_symlink())

    def test_missing_or_linked_reviewed_payload_rejected(self):
        self.lock.rename(self.lock.with_suffix('.actual'))
        with self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.target.exists())
        self.lock.symlink_to(self.lock.with_suffix('.actual'))
        with self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.target.exists())

    def test_exclusive_create_preserves_competing_file(self):
        original_open = Path.open
        competing = b'concurrent source lock'
        def racing_open(path, mode='r', *args, **kwargs):
            if path == self.target and mode == 'xb':
                with original_open(path, 'wb') as output: output.write(competing)
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', racing_open), self.assertRaises(FileExistsError): self.repair()
        self.assertEqual(self.target.read_bytes(), competing)
