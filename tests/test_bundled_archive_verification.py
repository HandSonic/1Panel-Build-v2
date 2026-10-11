import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automatic_frontend_lock as automatic
import verify_bundled_archives as bundles
from test_automatic_frontend_lock import contract_fixture


class BundledArchiveTests(unittest.TestCase):
    def fixture(self):
        parent = {'name': 'example', 'version': '1.0.0', 'bundleDependencies': ['@scope/child'],
                  'dependencies': {'@scope/child': '^1.0.0'}, 'engines': {'node': '>=22'}}
        child = {'name': '@scope/child', 'version': '1.2.3', 'engines': ['node >=22'],
                 'dependencies': {'nested': '2.0.0'}}
        nested = {'name': 'nested', 'version': '2.0.0', 'engines': {'npm': '>=10'}}
        packages = {'': {'name': 'synthetic', 'version': '1', 'dependencies': {'example': '1.0.0'}},
                    'node_modules/example': {**parent, 'resolved': 'https://registry.npmjs.org/example/-/example-1.0.0.tgz'},
                    'node_modules/example/node_modules/@scope/child': {**child, 'inBundle': True},
                    'node_modules/example/node_modules/@scope/child/node_modules/nested': {**nested, 'inBundle': True}}
        members = {'package/package.json': parent,
                   'package/node_modules/@scope/child/package.json': child,
                   'package/node_modules/@scope/child/node_modules/nested/package.json': nested,
                   'package/index.js': b'this file is never executed'}
        return {'name': 'synthetic', 'version': '1', 'lockfileVersion': 3, 'packages': packages}, members

    def archive(self, members, extra=None):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, value in members.items():
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                entry = tarfile.TarInfo(name); entry.size = len(raw)
                archive.addfile(entry, io.BytesIO(raw))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b'x' * extra.size))
        return output.getvalue()

    def bind(self, lock, raw):
        lock['packages']['node_modules/example']['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()

    def verify(self, lock, raw):
        requests = []
        def opener(url):
            requests.append(url)
            return io.BytesIO(raw)
        bundles.verify_bundled_archives(lock, opener)
        return requests

    def test_actual_parent_and_nested_child_manifests_share_one_verified_archive(self):
        lock, members = self.fixture(); raw = self.archive(members); self.bind(lock, raw)
        self.assertEqual(self.verify(lock, raw), [lock['packages']['node_modules/example']['resolved']])
        # After producer archive verification, runtime replay needs only the
        # authenticated exact lock and source manifest, never archive network.
        contract, _ = contract_fixture()
        payload = json.dumps(lock).encode()
        contract['frontend_lock'].update(sha256=automatic.sha(payload), bytes=len(payload))
        with patch.object(bundles, 'open_registry', side_effect=AssertionError('Offline replay')):
            self.assertEqual(automatic.validate_payload(payload, contract), payload)

    def test_changed_identity_engines_dependencies_or_declaration_reject(self):
        for case in ('parent-name', 'parent-bundle', 'child-name', 'child-version', 'child-engines', 'child-dependencies', 'nested-version'):
            lock, members = self.fixture()
            parent = members['package/package.json']
            child = members['package/node_modules/@scope/child/package.json']
            # Detach archive metadata from the independently pinned lock maps.
            members = copy.deepcopy(members)
            parent = members['package/package.json']; child = members['package/node_modules/@scope/child/package.json']
            if case == 'parent-name': parent['name'] = 'unrelated'
            if case == 'parent-bundle': parent['bundleDependencies'] = ['different']
            if case == 'child-name': child['name'] = 'different'
            if case == 'child-version': child['version'] = '9.0.0'
            if case == 'child-engines': child['engines'] = {'node': '>=30'}
            if case == 'child-dependencies': child['dependencies'] = {'nested': '3.0.0'}
            if case == 'nested-version': members['package/node_modules/@scope/child/node_modules/nested/package.json']['version'] = '9.0.0'
            raw = self.archive(members); self.bind(lock, raw)
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'differs'):
                self.verify(lock, raw)

    def test_absent_unrecorded_unsafe_duplicate_or_linked_members_reject(self):
        for case in ('missing', 'unrecorded', 'traversal', 'absolute', 'duplicate', 'symlink', 'hardlink'):
            lock, members = self.fixture(); extra = None
            if case == 'missing': del members['package/node_modules/@scope/child/package.json']
            if case == 'unrecorded': members['package/node_modules/unrelated/package.json'] = {'name': 'unrelated', 'version': '1.0.0'}
            if case == 'traversal': members['package/../outside'] = b'bad'
            if case == 'absolute': members['/outside'] = b'bad'
            if case in ('duplicate', 'symlink', 'hardlink'):
                extra = tarfile.TarInfo('package/package.json' if case == 'duplicate' else 'package/link')
                if case != 'duplicate':
                    extra.type = tarfile.SYMTYPE if case == 'symlink' else tarfile.LNKTYPE
                    extra.linkname = '/outside'
            raw = self.archive(members, extra); self.bind(lock, raw)
            with self.subTest(case=case), self.assertRaises(ValueError): self.verify(lock, raw)

    def test_parent_pin_membership_and_path_reject_before_fetch(self):
        for case in ('wrong-parent', 'unrelated', 'missing-parent', 'origin', 'weak-integrity', 'child-origin'):
            lock, members = self.fixture(); raw = self.archive(members); self.bind(lock, raw)
            parent = lock['packages']['node_modules/example']
            child = lock['packages']['node_modules/example/node_modules/@scope/child']
            if case == 'wrong-parent': parent['bundleDependencies'] = ['other']
            if case == 'unrelated': lock['packages']['node_modules/unrelated'] = child
            if case == 'missing-parent': del lock['packages']['node_modules/example']
            if case == 'origin': parent['resolved'] = 'https://example.invalid/a.tgz'
            if case == 'weak-integrity': parent['integrity'] = 'sha1-no'
            if case == 'child-origin': child['resolved'] = 'https://example.invalid/b.tgz'
            with self.subTest(case=case), self.assertRaises(ValueError):
                bundles.verify_bundled_archives(lock, lambda _: self.fail('Must reject before network'))

    def test_registry_download_never_follows_redirects_or_unapproved_origins(self):
        with patch.object(bundles, 'build_opener') as build:
            for url in ('http://registry.npmjs.org/example/-/example.tgz',
                        'https://registry.npmjs.org.evil.invalid/example/-/example.tgz',
                        'https://example.invalid/example.tgz'):
                with self.subTest(url=url), self.assertRaises(ValueError): bundles.open_registry(url)
            build.assert_not_called()
        response = io.BytesIO(b'not read')
        response.geturl = lambda: 'https://example.invalid/redirect'
        with patch.object(bundles, 'build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, 'redirect'):
                bundles.open_registry('https://registry.npmjs.org/example/-/example.tgz')
            self.assertTrue(response.closed)
        with self.assertRaisesRegex(ValueError, 'redirect'):
            bundles.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_full_sha512_gzip_trailer_and_stream_bounds_are_checked(self):
        lock, members = self.fixture(); raw = self.archive(members); self.bind(lock, raw)
        bad = copy.deepcopy(lock); bad['packages']['node_modules/example']['integrity'] = 'sha512-' + 'A' * 86 + '=='
        with self.assertRaisesRegex(ValueError, 'SHA-512'): self.verify(bad, raw)
        for truncated in (raw[:-1], raw[:-8]):
            self.bind(lock, truncated)
            with self.assertRaises((ValueError, EOFError, OSError)): self.verify(lock, truncated)
        self.bind(lock, raw)
        for name, bound in [('MAX_COMPRESSED', len(raw) - 1), ('MAX_EXPANDED', 512), ('MAX_MEMBERS', 2),
                            ('MAX_MEMBER', 10), ('MAX_MANIFEST', 10), ('MAX_TOTAL_COMPRESSED', len(raw) - 1),
                            ('MAX_TOTAL_EXPANDED', 512)]:
            with self.subTest(bound=name), patch.object(bundles, name, bound), self.assertRaises(ValueError):
                self.verify(lock, raw)

    def test_bundled_js_gate_still_requires_declared_pinned_ancestor_and_engines(self):
        lock, members = self.fixture(); self.bind(lock, self.archive(members))
        script = '''
import assert from 'node:assert/strict';
import {checkLock} from './scripts/automatic_frontend_lock.mjs';
const lock=JSON.parse(process.env.TEST_BUNDLE_LOCK);
checkLock(lock.packages[''],lock);
for(const kind of ['membership','parent','engine']) {
 const bad=structuredClone(lock);
 if(kind==='membership') bad.packages['node_modules/example'].bundleDependencies=[];
 if(kind==='parent') delete bad.packages['node_modules/example'];
 if(kind==='engine') bad.packages['node_modules/example/node_modules/@scope/child'].engines={node:'>=30'};
 assert.throws(()=>checkLock(bad.packages[''],bad));
}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, 'LOCK_NPM_DIR': str(Path(shutil.which('npm')).resolve().parents[1]),
                                     'TEST_BUNDLE_LOCK': json.dumps(lock)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
