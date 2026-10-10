"""Inspect declared npm bundles without extraction or package execution.

Only the producer downloads these exact integrity-pinned registry archives.
The resulting verified lock is then bound to the immutable run contract.
"""
import base64
import gzip
import hashlib
from pathlib import PurePosixPath
import re
import tarfile
from urllib.request import HTTPRedirectHandler, Request, build_opener

from automatic_frontend_lock import data_object, MAX_MANIFEST
from frontend_lock_origins import validate_lock_origins
from resolved_contract import require

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024
MAX_MEMBERS = 25000
MAX_PARENTS = 32
MAX_TOTAL_COMPRESSED = 256 * 1024 * 1024
MAX_TOTAL_EXPANDED = 512 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Bundled registry archive redirect rejected')


def open_registry(url):
    require(isinstance(url, str) and re.fullmatch(r'https://registry\.npmjs\.org/[^?#\s]+/-/[^/?#\s]+\.tgz', url),
            'Unsupported bundled archive origin')
    response = build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': '1Panel-bundle-verification'}), timeout=30)
    if response.geturl() != url:
        response.close()
        raise ValueError('Bundled registry archive redirect rejected')
    return response


class BoundedReader:
    def __init__(self, stream, limit, label, digest=None):
        self.stream, self.limit, self.label, self.digest, self.size = stream, limit, label, digest, 0

    def read(self, count=-1):
        data = self.stream.read(min(65536, count) if count >= 0 else 65536)
        self.size += len(data)
        require(self.size <= self.limit, 'Bundled archive ' + self.label + ' exceeds bound')
        if self.digest is not None:
            self.digest.update(data)
        return data


def declarations(manifest):
    value = manifest.get('bundleDependencies', manifest.get('bundledDependencies', []))
    if value is True:
        value = list(manifest.get('dependencies', {}))
    elif value is False:
        value = []
    elif isinstance(value, dict):
        value = list(value)
    require(isinstance(value, list) and all(isinstance(item, str) for item in value)
            and len(value) == len(set(value)), 'Invalid actual bundled dependency declaration')
    return sorted(value)


def verify_manifest(raw, item, path):
    manifest = data_object(raw, MAX_MANIFEST, 'bundled package manifest')
    name = item.get('name', path.rsplit('node_modules/', 1)[-1])
    require(isinstance(manifest.get('version'), str) and manifest['version']
            and manifest.get('name') == name and manifest['version'] == item.get('version'),
            'Bundled package identity differs from lock: ' + path)
    for field in ('dependencies', 'optionalDependencies', 'peerDependencies', 'peerDependenciesMeta'):
        actual = manifest.get(field, {})
        require(isinstance(actual, dict), 'Invalid bundled dependency metadata')
        if field == 'dependencies':
            # npm normalizes duplicate optional entries out of dependencies.
            optional = manifest.get('optionalDependencies', {})
            require(isinstance(optional, dict), 'Invalid bundled optional dependencies')
            actual = {key: value for key, value in actual.items() if key not in optional}
        require((actual or {}) == item.get(field, {}), 'Bundled dependency metadata differs: ' + path + ':' + field)
    if 'engines' in manifest and manifest['engines'] is not None:
        require(isinstance(manifest['engines'], (dict, list)), 'Invalid bundled engines')
    for field in ('engines', 'os', 'cpu', 'workspaces'):
        require((manifest.get(field) or None) == (item.get(field) or None),
                'Bundled package metadata differs: ' + path + ':' + field)
    require(declarations(manifest) == declarations(item), 'Actual bundle declaration differs from lock: ' + path)


def verify_bundled_archives(lock, opener=open_registry):
    owners = validate_lock_origins(lock)
    packages = lock['packages']
    grouped = {}
    for child, owner in owners.items():
        grouped.setdefault(owner, []).append(child)
    require(len(grouped) <= MAX_PARENTS, 'Too many containing bundle archives')
    total, total_expanded = 0, 0
    for owner, children in grouped.items():
        parent = packages[owner]
        expected = {'package/package.json': owner}
        expected.update(('package/' + child[len(owner) + 1:] + '/package.json', child) for child in children)
        found, seen = set(), set()
        digest = hashlib.sha512()
        with opener(parent['resolved']) as response:
            compressed = BoundedReader(response, MAX_COMPRESSED, 'compressed size', digest)
            with gzip.GzipFile(fileobj=compressed) as decoded:
                expanded = BoundedReader(decoded, MAX_EXPANDED, 'expanded size')
                with tarfile.open(fileobj=expanded, mode='r|', ignore_zeros=True) as archive:
                    for member in archive:
                        path = PurePosixPath(member.name)
                        require(path.parts and not path.is_absolute() and '..' not in path.parts and '\\' not in member.name
                                and str(path) == member.name.rstrip('/') and path.parts[0] == 'package',
                                'Unsafe bundled archive path')
                        name = str(path)
                        require(name not in seen, 'Duplicate bundled archive member')
                        seen.add(name)
                        require(len(seen) <= MAX_MEMBERS, 'Too many bundled archive members')
                        if member.isdir():
                            continue
                        require(member.isfile() and not member.issparse() and 0 <= member.size <= MAX_MEMBER,
                                'Unsupported bundled archive member')
                        if name in expected:
                            require(0 < member.size <= MAX_MANIFEST, 'Bundled manifest exceeds bound')
                            verify_manifest(archive.extractfile(member).read(MAX_MANIFEST + 1), packages[expected[name]], expected[name])
                            found.add(name)
                        elif re.fullmatch(r'package/(?:node_modules/(?:@[^/]+/)?[^/]+/)+package\.json', name):
                            raise ValueError('Unrecorded bundled package manifest')
                # Read through gzip trailers and all trailing compressed bytes;
                # tar's end marker alone does not establish archive integrity.
                while expanded.read(65536):
                    pass
            while compressed.read(65536):
                pass
        require(found == set(expected), 'Missing actual bundled package manifest')
        require('sha512-' + base64.b64encode(digest.digest()).decode() == parent['integrity'],
                'Containing bundle archive SHA-512 mismatch')
        total += compressed.size
        total_expanded += expanded.size
        require(total <= MAX_TOTAL_COMPRESSED and total_expanded <= MAX_TOTAL_EXPANDED,
                'Total bundled archive verification exceeds bound')
