"""Synthetic resource layouts; no installer, service or packaged binary executes."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import discover_inputs as discovery
import download_resources
import installer_resources as layouts
import package_release
import publication_contract
import resolved_contract
import validate_artifacts
import test_release
from test_discovered_inputs import fixture

INSTALLER = b'''#!/bin/bash
function Init_Panel(){
    cp ./1panel-core.service /etc/systemd/system
    cp ./1panel-agent.service /etc/systemd/system
}
function main(){
    Init_Panel
}
main
'''


def root_resources(version='v2.765.43'):
    result = {name: ('synthetic ' + name + '\n').encode() for name in layouts.ROOT_SYSTEMD}
    result['install.sh'] = INSTALLER
    result['1pctl'] = ('ORIGINAL_VERSION=' + version + '\n').encode()
    return result


class ArchiveLayoutTests(unittest.TestCase):
    def vendor(self, resources=None, mutation=None):
        resources = dict(root_resources() if resources is None else resources)
        resources.update({'GeoIP.mmdb': b'synthetic geo', '1panel-core': b'never executed', '1panel-agent': b'never executed'})
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name, raw in resources.items():
                info = tarfile.TarInfo('1panel-v2.765.43-linux-amd64/' + name); info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        raw = buffer.getvalue()
        class Vendor:
            def read(self, url, **kwargs):
                digest = '0' * 64 if mutation == 'hash' else hashlib.sha256(raw).hexdigest()
                return (digest + '  1panel-v2.765.43-linux-amd64.tar.gz\n').encode()
            def open(self, url): return io.BytesIO(raw)
        return Vendor(), raw

    def test_root_systemd_archive_keeps_exact_original_resource_paths_and_bytes(self):
        vendor, raw = self.vendor()
        resources, geo = discovery.official_archive(vendor, 'v2.765.43', 'stable')
        self.assertEqual(resources, root_resources())
        self.assertEqual(geo['archive']['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(geo['archive']['bytes'], len(raw))
        self.assertEqual(geo['sha256'], hashlib.sha256(b'synthetic geo').hexdigest())
        output = io.BytesIO()
        replay, repeated = discovery.official_archive(vendor, 'v2.765.43', 'stable', geo['archive'], output)
        self.assertEqual(replay, resources); self.assertEqual(repeated, geo)
        self.assertEqual(output.getvalue(), b'synthetic geo')

    def test_modern_layout_and_matching_redundant_root_copies_remain_exact(self):
        modern = {name: ('modern ' + name).encode() for name in layouts.MODERN}
        for copies in (False, True):
            values = dict(modern)
            if copies:
                values.update({name: values['initscript/' + name] for name in layouts.ROOT_SERVICES})
            vendor, _ = self.vendor(values)
            with self.subTest(copies=copies):
                resources, _ = discovery.official_archive(vendor, 'v2.765.43', 'stable')
                self.assertEqual(resources, modern)

    def test_incomplete_mixed_conflicting_and_inactive_interfaces_fail(self):
        cases = {}
        absent = root_resources(); del absent['1panel-agent.service']; cases['missing-pair'] = absent
        mixed = root_resources(); mixed['initscript/1panel-core.init'] = b'unsupported mix'; cases['mixed'] = mixed
        lang = root_resources(); del lang['lang/en.sh']; cases['missing-language'] = lang
        inactive = root_resources(); inactive['install.sh'] = INSTALLER.replace(b'    Init_Panel\n', b'    # Init_Panel\n'); cases['inactive'] = inactive
        unknown = root_resources(); unknown['install.sh'] = INSTALLER.replace(b'./1panel-core.service', b'./services/1panel-core.service'); cases['unknown-reference'] = unknown
        conflicting = {name: b'modern' for name in layouts.MODERN}
        conflicting.update({name: b'different' for name in layouts.ROOT_SERVICES}); cases['conflicting-copies'] = conflicting
        for case, values in cases.items():
            vendor, _ = self.vendor(values)
            with self.subTest(case=case), self.assertRaises(ValueError):
                discovery.official_archive(vendor, 'v2.765.43', 'stable')

    def test_full_hash_and_pinned_size_still_gate_legacy_layout(self):
        vendor, _ = self.vendor(mutation='hash')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            discovery.official_archive(vendor, 'v2.765.43', 'stable')
        vendor, _ = self.vendor()
        _, geo = discovery.official_archive(vendor, 'v2.765.43', 'stable')
        bad = dict(geo['archive']); bad['bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'integrity'):
            discovery.official_archive(vendor, 'v2.765.43', 'stable', bad)

    def test_layout_does_not_bypass_archive_traversal_or_member_bounds(self):
        for name, payload in [('../outside', b'bad'), ('1panel-core.service', b'x' * (512 * 1024 + 1))]:
            values = root_resources(); values[name] = payload
            vendor, _ = self.vendor(values)
            with self.subTest(name=name), self.assertRaises(ValueError):
                discovery.official_archive(vendor, 'v2.765.43', 'stable')


class SourceLayoutBindingTests(unittest.TestCase):
    def test_policy_fingerprint_binds_layout_and_interface_validators(self):
        contract = fixture()
        for name in ('scripts/installer_resources.py', 'scripts/service_layout.py', 'scripts/download_resources.py'):
            def fake_digest(path, changed=False):
                return {'sha256': ('b' if changed and str(path).endswith(name) else 'a') * 64}
            with patch('resolved_contract.runtime_contract', return_value=contract),\
                 patch.object(publication_contract, 'digest', side_effect=lambda path: fake_digest(path)):
                before = publication_contract.policy_fingerprint('upstream-matrix', contract['version'])
            with patch('resolved_contract.runtime_contract', return_value=contract),\
                 patch.object(publication_contract, 'digest', side_effect=lambda path: fake_digest(path, True)):
                after = publication_contract.policy_fingerprint('upstream-matrix', contract['version'])
            with self.subTest(file=name): self.assertNotEqual(before, after)

    def test_complete_resources_match_one_immutable_commit_without_path_aliases(self):
        resources = root_resources()
        source = dict(resources); source['1pctl'] = b'ORIGINAL_VERSION=version\n'
        incomplete = dict(source); del incomplete['1panel-agent.service']
        wrong = dict(source); wrong['1panel-core.service'] += b'changed'
        calls = []
        class Vendor:
            def commit(self, repo, ref): return 'a' * 40
            def api(self, repo, path): return [{'sha': 'b' * 40}, {'sha': 'c' * 40}]
            def source(self, repo, commit, path, optional=False):
                calls.append((commit, path, optional))
                return {'a' * 40: incomplete, 'b' * 40: wrong, 'c' * 40: source}[commit].get(path)
        with patch.object(discovery, 'official_archive', return_value=(resources, {'fixture': True})):
            installer, geo = discovery.installer_inputs(Vendor(), 'v2.765.43', 'stable')
        self.assertEqual(installer['commit'], 'c' * 40)
        self.assertEqual(installer['resources'], {name: discovery.facts(raw) for name, raw in source.items()})
        self.assertEqual(installer['original_version'], 'version')
        self.assertEqual(geo, {'fixture': True})
        self.assertFalse(any(path.startswith('initscript/') for _, path, _ in calls))

    def test_contract_accepts_only_complete_known_resource_sets(self):
        contract = fixture()
        contract['installer']['resources'] = {name: discovery.facts(raw) for name, raw in root_resources().items()}
        self.assertEqual(resolved_contract.source_contract(resolved_contract.canonical(contract), resolved_contract.digest(contract),
                                                         contract['version'], contract['mode']), contract)
        for kind in ('missing', 'mixed', 'unknown'):
            bad = copy.deepcopy(contract)
            if kind == 'missing': del bad['installer']['resources']['1panel-agent.service']
            if kind == 'mixed': bad['installer']['resources']['initscript/1panel-core.service'] = discovery.facts(b'other')
            if kind == 'unknown': bad['installer']['resources']['services/1panel-core.service'] = bad['installer']['resources'].pop('1panel-core.service')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                resolved_contract.source_contract(resolved_contract.canonical(bad), resolved_contract.digest(bad), bad['version'], bad['mode'])


class RootLayoutBuildTests(unittest.TestCase):
    binaries = test_release.ReleaseTests.binaries
    build = test_release.ReleaseTests.build

    def setUp(self):
        test_release.ReleaseTests.setUp(self)
        values = root_resources('v2.3.2')
        for name, raw in values.items():
            target = self.root / name; target.parent.mkdir(exist_ok=True, parents=True); target.write_bytes(raw)
        self.entry['installer_sha256'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in values.items()}
        for name in ('install.sh', '1pctl'): (self.root / name).chmod(0o755)

    def test_download_and_package_preserve_root_resources_without_fabricated_init_files(self):
        destination = self.root / 'downloaded'
        def download(url, path, digest):
            relative = 'GeoIP.mmdb' if url == self.entry['geoip_url'] else url.split(self.entry['installer_commit'] + '/', 1)[1]
            path.write_bytes((self.root / relative).read_bytes())
        with patch.object(download_resources, 'download', side_effect=download):
            download_resources.main('v2.3.2', destination)
        self.assertFalse((destination / 'initscript').exists())
        for name in layouts.ROOT_SERVICES | {'install.sh', '1pctl'}:
            self.assertEqual((destination / name).read_bytes(), (self.root / name).read_bytes())
        self.build(['amd64'])
        path = self.root / 'dist/1panel-v2.3.2-linux-amd64.tar.gz'
        with tarfile.open(path) as archive:
            names = {name.split('/', 1)[1] for name in archive.getnames() if '/' in name}
            self.assertFalse(any(name.startswith('initscript/') for name in names))
        self.assertEqual(len(validate_artifacts.validate_dist(self.root / 'dist', 'v2.3.2', ['amd64'])), 1)

    def test_changed_service_and_unreachable_copy_fail_existing_byte_and_interface_gates(self):
        (self.root / '1panel-core.service').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Installer resource mismatch'): self.build(['amd64'])
        values = root_resources('v2.3.2')
        (self.root / '1panel-core.service').write_bytes(values['1panel-core.service'])
        raw = INSTALLER.replace(b'    cp ./1panel-core.service', b'    return 0\n    cp ./1panel-core.service')
        (self.root / 'install.sh').write_bytes(raw)
        self.entry['installer_sha256']['install.sh'] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError, 'Unconditional termination'): self.build(['amd64'])


if __name__ == '__main__':
    unittest.main()
