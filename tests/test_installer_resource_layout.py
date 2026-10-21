"""Synthetic resource layouts; no installer, service or packaged binary executes."""
import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import discover_inputs as discovery
import download_resources
import installer_resources as layouts
import resolved_contract
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
    def vendor(self, resources=None):
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
                digest = hashlib.sha256(raw).hexdigest()
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


    def test_oversize_installer_resource_is_rejected(self):
        values = root_resources(); values['1panel-core.service'] = b'x' * (512 * 1024 + 1)
        vendor, _ = self.vendor(values)
        with self.assertRaises(ValueError):
            discovery.official_archive(vendor, 'v2.765.43', 'stable')


class SourceLayoutBindingTests(unittest.TestCase):

    def test_complete_resources_match_one_immutable_commit_without_path_aliases(self):
        resources = root_resources()
        source = dict(resources); source['1pctl'] = b'ORIGINAL_VERSION=version\n'
        incomplete = dict(source); del incomplete['1panel-agent.service']
        wrong = dict(source); wrong['1panel-core.service'] += b'changed'
        class Vendor:
            def commit(self, repo, ref): return 'a' * 40
            def api(self, repo, path): return [{'sha': 'b' * 40}, {'sha': 'c' * 40}]
            def source(self, repo, commit, path, optional=False):
                return {'a' * 40: incomplete, 'b' * 40: wrong, 'c' * 40: source}[commit].get(path)
        with patch.object(discovery, 'official_archive', return_value=(resources, {'fixture': True})):
            installer, geo = discovery.installer_inputs(Vendor(), 'v2.765.43', 'stable')
        self.assertEqual(installer['commit'], 'c' * 40)
        self.assertEqual(installer['resources'], {name: discovery.facts(raw) for name, raw in source.items()})
        self.assertEqual(installer['original_version'], 'version')
        self.assertEqual(geo, {'fixture': True})

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
