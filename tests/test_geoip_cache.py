"""Discovery reuses authenticated GeoIP bytes without another official archive."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import discover_inputs as discovery
import download_resources
import geoip_cache as cache
import resolved_contract
from test_discovered_inputs import FakeVendor, fixture
import test_release


class Vendor(FakeVendor):
    def __init__(self, version='v2.99.0', mutation=None, geoip=b'geo'):
        super().__init__()
        self.installer = {name: b'# synthetic ' + name.encode() for name in discovery.INSTALLER_FILES}
        self.installer['1pctl'] = b'ORIGINAL_VERSION=version\n'
        self.opens = 0
        self.name = f'1panel-{version}-linux-amd64'
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            contents = dict(self.installer, **{'GeoIP.mmdb': geoip, '1panel-core': b'unused binary'})
            contents['1pctl'] = ('ORIGINAL_VERSION=' + version + '\n').encode()
            for name, body in contents.items():
                member = tarfile.TarInfo(self.name + '/' + name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        self.compressed = stream.getvalue()
        if mutation == 'crc':
            self.compressed = self.compressed[:-8] + bytes([self.compressed[-8] ^ 1]) + self.compressed[-7:]
        self.sha = '0' * 64 if mutation == 'digest' else hashlib.sha256(self.compressed).hexdigest()
        if mutation == 'installer':
            self.installer['install.sh'] = b'different installer'

    def read(self, url, **kwargs):
        if url.startswith('https://resource.fit2cloud.com/'):
            return (self.sha + '  ' + self.name + '.tar.gz\n').encode()
        return super().read(url, **kwargs)

    def open(self, url):
        self.opens += 1
        return io.BytesIO(self.compressed)

    def source(self, repo, commit, path, optional=False):
        return self.installer.get(path) if repo == discovery.INSTALLER else super().source(repo, commit, path, optional)

    def api(self, repo, path):
        return [] if repo == discovery.INSTALLER else super().api(repo, path)


class GeoIPCacheTests(unittest.TestCase):
    def test_discovery_publishes_cache_only_after_archive_installer_and_contract_checks(self):
        for mutation in (None, 'digest', 'crc', 'installer', 'existing-contract'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'resolved-source.json'
                if mutation == 'existing-contract':
                    output.write_bytes(b'{}')
                vendor = Vendor(mutation=mutation)
                with patch.object(discovery, 'select_node', return_value={'node': '22.22.1', 'npm': '10.9.4'}):
                    if mutation is None:
                        sha = discovery.write_discovery(vendor, output, 'v2.99.0')
                        contract = json.loads(output.read_bytes())
                        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), sha)
                        self.assertEqual(cache.read(output.parent, contract['resources']['geoip']), b'geo')
                        self.assertEqual(vendor.opens, 1)
                        self.assertEqual(resolved_contract.source_contract(output.read_bytes(), sha, 'v2.99.0', 'stable'), contract)
                    else:
                        with self.assertRaises((ValueError, OSError)):
                            discovery.write_discovery(vendor, output, 'v2.99.0')
                        self.assertFalse((output.parent / cache.CACHE).exists())
                        self.assertEqual(output.read_bytes() if output.exists() else None,
                                         b'{}' if mutation == 'existing-contract' else None)

    def test_apply_accepts_absent_cache_and_copies_only_matching_regular_bytes(self):
        contract = fixture()
        for kind in ('present', 'absent', 'changed', 'symlink', 'oversize'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'input'
                source.mkdir()
                path = source / 'resolved-source.json'
                path.write_bytes(resolved_contract.canonical(contract))
                cached = source / cache.CACHE
                if kind in ('present', 'changed'):
                    cached.write_bytes(b'geo' if kind == 'present' else b'bad')
                if kind == 'symlink':
                    cached.symlink_to(path)
                if kind == 'oversize':
                    with cached.open('wb') as output:
                        output.truncate(cache.MAX_GEOIP + 1)
                destination = root / 'checkout'
                args = (path, resolved_contract.digest(contract), destination, 'v2.99.0')
                if kind in ('present', 'absent'):
                    resolved_contract.apply_contract(*args)
                    resolved_contract.apply_contract(*args)
                    self.assertEqual((destination / 'config' / cache.CACHE).exists(), kind == 'present')
                else:
                    with self.assertRaises(ValueError):
                        resolved_contract.apply_contract(*args)
                    self.assertFalse(destination.exists())

    def test_resource_cache_hit_avoids_archive_and_absence_preserves_both_download_paths(self):
        case = test_release.ReleaseTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        config = case.root / 'config'
        config.mkdir()
        geoip = (case.root / 'GeoIP.mmdb').read_bytes()
        case.entry['geoip_bytes'] = len(geoip)
        vendor = Vendor('v2.3.2', geoip=geoip)
        _, facts = discovery.official_archive(vendor, 'v2.3.2', 'stable')
        case.entry['geoip_archive'] = facts['archive']
        vendor.opens = 0
        downloads = []
        def download(url, destination, expected):
            downloads.append(url)
            rel = 'GeoIP.mmdb' if url == case.entry['geoip_url'] else url.split(case.entry['installer_commit'] + '/', 1)[1]
            destination.write_bytes((case.root / rel).read_bytes())
        with patch.object(download_resources, 'CONFIG', config), patch.object(download_resources, 'download', side_effect=download), patch.object(discovery, 'Vendor', return_value=vendor):
            cache.write(config, geoip, facts)
            download_resources.main('v2.3.2', case.root / 'hit')
            self.assertEqual(vendor.opens, 0)
            (config / cache.CACHE).unlink()
            download_resources.main('v2.3.2', case.root / 'miss')
            self.assertEqual(vendor.opens, 1)
            del case.entry['geoip_archive']
            download_resources.main('v2.3.2', case.root / 'legacy')
            self.assertEqual(downloads.count(case.entry['geoip_url']), 1)
        for name in ('hit', 'miss', 'legacy'):
            self.assertEqual((case.root / name / 'GeoIP.mmdb').read_bytes(), geoip)
