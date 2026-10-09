#!/usr/bin/env python3
"""Resolve a vendor release into immutable build inputs, without a version allowlist.

The output is a per-run artifact. It is not a repository status/audit document.
Network identity is fixed here; callers cannot supply arbitrary download hosts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from semantic_configuration import production

ROOT = Path(__file__).resolve().parents[1]
ARCHES = ['amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'loong64', 'riscv64']
SOURCE = '1Panel-dev/1Panel'
INSTALLER = '1Panel-dev/installer'
VERSION = re.compile(r'v2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(beta|dev)\.(0|[1-9][0-9]*))?')
DEPENDENCIES = ['core/go.mod', 'agent/go.mod', 'frontend/package.json', 'frontend/package-lock.json']
INSTALLER_FILES = ['install.sh', '1pctl'] + [f'initscript/1panel-{part}.{ext}' for part in ['core', 'agent'] for ext in ['init', 'openrc', 'procd', 'service']] + [f'lang/{lang}.sh' for lang in ['en', 'fa', 'pt-BR', 'ru', 'zh']]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def facts(data):
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def identity(version, mode=None):
    m = VERSION.fullmatch(version)
    if not m:
        raise ValueError('Unsupported explicit v2 release version')
    actual = m[3] or 'stable'
    if mode is not None and actual != mode:
        raise ValueError('Version/channel mismatch')
    return actual


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Input discovery redirect rejected')


class Vendor:
    def read(self, url, limit=2 * 1024 * 1024, optional=False):
        allowed = ('https://api.github.com/repos/1Panel-dev/', 'https://raw.githubusercontent.com/1Panel-dev/', 'https://go.dev/dl/?mode=json', 'https://nodejs.org/dist/index.json', 'https://raw.githubusercontent.com/nodejs/Release/main/schedule.json', 'https://resource.fit2cloud.com/1panel/package/v2/')
        if not url.startswith(allowed):
            raise ValueError('Unsupported discovery origin')
        try:
            headers = {'User-Agent': '1Panel-build-input-discovery', 'Accept': 'application/vnd.github+json'}
            if url.startswith('https://api.github.com/repos/1Panel-dev/') and os.environ.get('GH_TOKEN'):
                headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
            with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=30) as response:
                if response.geturl() != url:
                    raise ValueError('Discovery redirect rejected')
                data = response.read(limit + 1)
        except HTTPError as exc:
            if optional and exc.code == 404:
                return None
            raise ValueError('Official input unavailable (HTTP %s)' % exc.code) from exc
        if not 0 < len(data) <= limit:
            raise ValueError('Invalid official input size')
        return data

    def open(self, url):
        if not url.startswith('https://resource.fit2cloud.com/1panel/package/v2/'):
            raise ValueError('Unsupported archive origin')
        response = build_opener(NoRedirect()).open(Request(url, headers={'User-Agent': '1Panel-build-input-discovery'}), timeout=45)
        if response.geturl() != url:
            response.close()
            raise ValueError('Official resource redirect rejected')
        return response

    def digest(self, url, limit):
        sha, size = hashlib.sha256(), 0
        with self.open(url) as stream:
            while True:
                part = stream.read(1024 * 1024)
                if not part:
                    break
                size += len(part)
                if size > limit:
                    raise ValueError('Official resource exceeds bound')
                sha.update(part)
        if not size:
            raise ValueError('Empty official resource')
        return {'sha256': sha.hexdigest(), 'bytes': size}

    def api(self, repo, path):
        if repo not in (SOURCE, INSTALLER):
            raise ValueError('Unsupported source repository')
        return json.loads(self.read(f'https://api.github.com/repos/{repo}/{path}'))

    def source(self, repo, commit, path, optional=False):
        if repo not in (SOURCE, INSTALLER) or not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in Path(path).parts or path.startswith('/'):
            raise ValueError('Invalid immutable source identity')
        return self.read(f'https://raw.githubusercontent.com/{repo}/{commit}/{path}', optional=optional)

    def commit(self, repo, ref):
        obj = self.api(repo, 'git/ref/' + ref)['object']
        for _ in range(4):
            sha = obj.get('sha', '')
            if not re.fullmatch('[0-9a-f]{40}', sha):
                raise ValueError('Invalid official commit identity')
            if obj.get('type') == 'commit':
                return sha
            if obj.get('type') != 'tag':
                break
            obj = self.api(repo, 'git/tags/' + sha)['object']
        raise ValueError('Unsupported source tag chain')


def latest(vendor, mode):
    if mode not in ('stable', 'beta', 'dev'):
        raise ValueError('Unsupported release channel')
    releases = vendor.api(SOURCE, 'releases?per_page=100')
    candidates = []
    for release in releases:
        v = release.get('tag_name', '')
        if release.get('draft') or not VERSION.fullmatch(v):
            continue
        if identity(v) != mode or bool(release.get('prerelease')) != (mode != 'stable'):
            continue
        m = VERSION.fullmatch(v)
        candidates.append(((int(m[1]), int(m[2]), int(m[4] or 0)), v))
    if not candidates:
        raise ValueError('No official release found for requested channel')
    return max(candidates)[1]


def go_requirement(data):
    directives = {}
    for line in data.decode('utf-8').splitlines():
        fields = line.split('//', 1)[0].split()
        if not fields or fields[0] not in ('go', 'toolchain'):
            continue
        if len(fields) != 2 or fields[0] in directives:
            raise ValueError('Unsupported Go module requirement')
        directives[fields[0]] = fields[1]
    if 'go' not in directives:
        raise ValueError('Missing Go module language requirement')
    values = [directives['go']]
    if directives.get('toolchain') not in (None, 'default'):
        if not directives['toolchain'].startswith('go'):
            raise ValueError('Unsupported Go toolchain requirement')
        values.append(directives['toolchain'][2:])
    if any(not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', value) for value in values):
        raise ValueError('Unsupported Go version requirement')
    return max(tuple(map(int, (v + '.0').split('.')[:3])) for v in values)


def select_go(vendor, modules):
    minimum = max(go_requirement(data) for data in modules)
    releases = json.loads(vendor.read('https://go.dev/dl/?mode=json&include=all', limit=8 * 1024 * 1024))
    versions = [row['version'][2:] for row in releases if row.get('stable') and re.fullmatch(r'go\d+\.\d+\.\d+', row.get('version', ''))]
    candidates = [v for v in versions if tuple(map(int, v.split('.'))) >= minimum]
    if not candidates:
        raise ValueError('No official stable Go compiler satisfies both modules')
    # Use the newest patch of the lowest available compatible minor.
    minor = min(tuple(map(int, v.split('.')[:2])) for v in candidates)
    return max((v for v in candidates if tuple(map(int, v.split('.')[:2])) == minor), key=lambda v: tuple(map(int, v.split('.'))))


def resolve_source(vendor, version=None, mode='stable', root=ROOT):
    version = version or latest(vendor, mode)
    identity(version, mode)
    commit = vendor.commit(SOURCE, 'tags/' + version)
    files, raw, absent = {}, {}, []
    for path in DEPENDENCIES:
        data = vendor.source(SOURCE, commit, path, optional=path.endswith('package-lock.json'))
        if data is None:
            absent.append(path)
        else:
            files[path] = facts(data)
            raw[path] = data
    # These inputs can change npm resolution despite identical package.json.
    for path in ('package.json', '.npmrc', 'npm-shrinkwrap.json', 'frontend/.npmrc', 'frontend/npm-shrinkwrap.json'):
        if vendor.source(SOURCE, commit, path, optional=True) is not None:
            raise ValueError('Unsupported additional npm resolution input: ' + path)
    package = json.loads(raw['frontend/package.json'])
    manager = package.get('packageManager')
    if package.get('workspaces') or (manager is not None and not re.fullmatch(r'npm@[0-9]+\.[0-9]+\.[0-9]+', manager)):
        raise ValueError('Unsupported frontend workspace/package manager contract')
    configuration = {}
    for part in ('core', 'agent'):
        path = f'{part}/cmd/server/conf/app.yaml'
        data = vendor.source(SOURCE, commit, path)
        if len(data) > 65536:
            raise ValueError('Embedded configuration too large')
        files[path] = facts(data)
        normalized = production(data, version, part, mode)
        configuration[part] = {'path': path, 'source_sha256': files[path]['sha256'], 'source_bytes': len(data), 'normalized_sha256': facts(normalized)['sha256']}
    from lock_catalog import select, derive
    manifest_sha = files['frontend/package.json']['sha256']
    original = raw.get('frontend/package-lock.json')
    recipe, additions = select(manifest_sha, None if original is None else facts(original)['sha256'], root)
    if recipe is None:
        if original is None:
            raise ValueError('Source lacks a lock and no manifest-bound repair exists; candidate dependency resolution required')
        lock = {'kind': 'source', 'sha256': facts(original)['sha256'], 'manifest_sha256': manifest_sha}
    else:
        raw['frontend/package-lock.json'] = derive(raw['frontend/package.json'], original, recipe, additions, root)
        lock = {'kind': 'derived', 'sha256': recipe['derived_sha256'], 'manifest_sha256': manifest_sha,
                'recipe_sha256': hashlib.sha256(canonical(recipe)).hexdigest()}
    repair = recipe
    parsed_lock = json.loads(raw['frontend/package-lock.json'])
    if parsed_lock.get('lockfileVersion') not in (2, 3) or not isinstance(parsed_lock.get('packages'), dict):
        raise ValueError('Unsupported npm lock schema')
    if parsed_lock['packages'].get('', {}).get('dependencies', {}) != package.get('dependencies', {}) or parsed_lock['packages'].get('', {}).get('devDependencies', {}) != package.get('devDependencies', {}):
        raise ValueError('Source lock differs from frontend manifest')
    validate_lock_origins(parsed_lock)
    return {'version': version, 'mode': mode, 'source': {'repository': SOURCE, 'commit': commit, 'files': files, 'absent': absent}, 'configuration': configuration, 'frontend_lock': lock, 'go': select_go(vendor, [raw['core/go.mod'], raw['agent/go.mod']])}, raw, repair




def validate_lock_origins(lock):
    """Bundled entries inherit only an explicitly declared, integrity-pinned archive."""
    packages=lock['packages']
    package_name=r'(?:@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+'
    def tarball(item):
        url=item.get('resolved','')
        if not isinstance(url,str) or not re.fullmatch(r'https://registry\.npmjs\.org/[^?#\s]+/-/[^/?#\s]+\.tgz',url):
            raise ValueError('Dependency must use the official npm registry tarball')
        if not isinstance(item.get('integrity'),str) or not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==',item['integrity']):
            raise ValueError('Dependency requires pinned SHA-512 integrity')
    for path,item in packages.items():
        if path=='':continue
        if not isinstance(item,dict) or item.get('link') or not re.fullmatch('node_modules/'+package_name+'(?:/node_modules/'+package_name+')*',path) or '..' in Path(path).parts:
            raise ValueError('Unsupported frontend dependency source')
        if item.get('inBundle') is not True:
            tarball(item)
            continue
        # Bundled contents are shipped inside an ancestor tarball, not fetched by
        # a missing child URL. Require the complete explicit bundle ancestry.
        child=path
        while True:
            if '/node_modules/' not in child:
                raise ValueError('Bundled dependency has no pinned containing package')
            parent,direct=child.rsplit('/node_modules/',1)
            owner=packages.get(parent)
            if not isinstance(owner,dict) or owner.get('link'):
                raise ValueError('Bundled dependency parent is absent or linked')
            if owner.get('inBundle') is True:
                child=parent
                continue
            declared=owner.get('bundleDependencies',owner.get('bundledDependencies'))
            if not isinstance(declared,list) or direct not in declared:
                raise ValueError('Containing tarball does not declare this bundled dependency')
            tarball(owner)
            break
        # If npm retains a separate origin too, do not ignore an invalid one.
        if 'resolved' in item or 'integrity' in item:tarball(item)


def select_node(vendor, package, lock):
    import os
    import subprocess
    releases = json.loads(vendor.read('https://nodejs.org/dist/index.json', limit=4 * 1024 * 1024))
    # Exclude end-of-life majors by using the official schedule, not a frozen version list.
    schedule = json.loads(vendor.read('https://raw.githubusercontent.com/nodejs/Release/main/schedule.json'))
    from datetime import date
    today = date.today().isoformat()
    active = {int(name[1:]) for name, value in schedule.items() if value.get('lts') and value['lts'] <= today < value['end']}
    releases = [row for row in releases if int(row['version'].split('.')[0][1:]) in active]
    env = dict(os.environ)
    import shutil
    npm_dir = Path(shutil.which('npm')).resolve().parents[1]
    if json.loads((npm_dir / 'package.json').read_text()).get('name') != 'npm':
        raise ValueError('Official npm installation required for engine matching')
    env['PROBE_NPM_DIR'] = str(npm_dir)
    result = subprocess.check_output(['node', str(ROOT / 'scripts/select_node_toolchain.mjs')], input=canonical({'packages': [package] + ([{'engines': {'npm': package['packageManager'][4:]}}] if package.get('packageManager') else []) + list(lock['packages'].values()), 'releases': releases}), env=env)
    return json.loads(result)


def official_archive(vendor, version, mode, expected=None, geoip_destination=None):
    """Read all compressed bytes and hash small installer resources without extraction."""
    import tarfile
    from pathlib import PurePosixPath
    base = f'https://resource.fit2cloud.com/1panel/package/v2/{mode}/{version}/release/'
    name = f'1panel-{version}-linux-amd64.tar.gz'
    if expected is None:
        checksums = vendor.read(base + 'checksums.txt', limit=1024 * 1024).decode()
        pins = {}
        for line in checksums.splitlines():
            m = re.fullmatch(r'([0-9a-f]{64}) [ *]([^/\\\s]+)', line)
            if not m or m[2] in pins:
                raise ValueError('Unsupported official checksum manifest')
            pins[m[2]] = m[1]
        if name not in pins:
            raise ValueError('Official amd64 package missing from checksum manifest')
        expected_sha = pins[name]
    else:
        if expected['url'] != base + name or expected['member'] != name[:-7] + '/GeoIP.mmdb':
            raise ValueError('Archive resource identity mismatch')
        expected_sha = expected['sha256']
    class HashedStream:
        def __init__(self, stream):
            self.stream, self.sha, self.size = stream, hashlib.sha256(), 0
        def read(self, n=-1):
            if n < 0:
                n = 1024 * 1024
            data = self.stream.read(min(n, 1024 * 1024))
            self.size += len(data)
            if self.size > 512 * 1024 * 1024:
                raise ValueError('Official archive exceeds bound')
            self.sha.update(data)
            return data
    resources, geoip, seen, logical_size = {}, None, set(), 0
    with vendor.open(base + name) as response:
        stream = HashedStream(response)
        with tarfile.open(fileobj=stream, mode='r|gz') as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or '\\' in member.name or str(path) in seen:
                    raise ValueError('Unsafe or duplicate official archive member')
                seen.add(str(path))
                if len(seen) > 10000 or not path.parts or path.parts[0] != name[:-7]:
                    raise ValueError('Unexpected official archive structure')
                if member.isdir():
                    continue
                if not member.isfile() or member.issparse() or member.size < 0 or member.size > 256 * 1024 * 1024:
                    raise ValueError('Unsupported official archive member')
                logical_size += member.size
                if logical_size > 1024 * 1024 * 1024:
                    raise ValueError('Official archive logical size exceeds bound')
                rel = '/'.join(path.parts[1:])
                payload = archive.extractfile(member)
                if rel == 'GeoIP.mmdb' and member.size > 64 * 1024 * 1024:
                    raise ValueError('GeoIP member exceeds bound')
                sha, size, kept = hashlib.sha256(), 0, bytearray()
                while True:
                    part = payload.read(1024 * 1024)
                    if not part:
                        break
                    sha.update(part); size += len(part)
                    if rel == 'GeoIP.mmdb' and geoip_destination is not None:
                        geoip_destination.write(part)
                    if rel in INSTALLER_FILES:
                        if size > 512 * 1024:
                            raise ValueError('Installer resource exceeds bound')
                        kept.extend(part)
                if size != member.size:
                    raise ValueError('Truncated official member')
                if rel in INSTALLER_FILES:
                    resources[rel] = bytes(kept)
                if rel == 'GeoIP.mmdb':
                    geoip = {'sha256': sha.hexdigest(), 'bytes': size}
        while stream.read(1024 * 1024):
            pass
    if stream.sha.hexdigest() != expected_sha or (expected is not None and stream.size != expected['bytes']) or set(resources) != set(INSTALLER_FILES) or not geoip or not geoip['bytes']:
        raise ValueError('Official archive integrity/resource contract mismatch')
    geoip['archive'] = {'url': base + name, 'sha256': stream.sha.hexdigest(), 'bytes': stream.size, 'member': name[:-7] + '/GeoIP.mmdb'}
    return resources, geoip


def installer_inputs(vendor, version, mode, root=ROOT):
    resources, geoip = official_archive(vendor, version, mode)
    # Current branch is resolved only once. Historical candidates remain exact commits.
    current = vendor.commit(INSTALLER, 'heads/v2')
    commits = [current]
    def candidates():
        seen = set()
        for commit in commits:
            seen.add(commit)
            yield commit
        # Anchor history to the already resolved branch tip; never use a moving ref.
        for page in range(1, 4):
            rows = vendor.api(INSTALLER, f'commits?sha={current}&per_page=100&page={page}')
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError('Unsupported official installer history response')
            for row in rows:
                commit = row.get('sha', '')
                if not re.fullmatch('[0-9a-f]{40}', commit):
                    raise ValueError('Invalid installer history commit')
                if commit not in seen:
                    seen.add(commit)
                    yield commit
            if len(rows) < 100:
                break
    matched = None
    for commit in candidates():
        install = vendor.source(INSTALLER, commit, 'install.sh')
        if install != resources['install.sh']:
            continue
        values = {'install.sh': install}
        for path in INSTALLER_FILES:
            if path not in values:
                values[path] = vendor.source(INSTALLER, commit, path)
        original = re.findall(rb'^ORIGINAL_VERSION=([^\r\n]+)$', values['1pctl'], re.M)
        if len(original) != 1:
            continue
        raw_version = original[0].decode()
        if raw_version != 'version' and not VERSION.fullmatch(raw_version):
            continue
        rendered = re.sub(rb'^ORIGINAL_VERSION=.*$', ('ORIGINAL_VERSION=' + version).encode(), values['1pctl'], flags=re.M)
        if all((rendered if path == '1pctl' else values[path]) == resources[path] for path in INSTALLER_FILES):
            matched = {'repository': INSTALLER, 'commit': commit, 'resources': {path: facts(data) for path, data in values.items()}, 'original_version': raw_version}
            break
    if matched is None:
        raise ValueError('Official installer archive does not match any resolved immutable installer source')
    return matched, geoip



def discover(vendor, version=None, mode='stable', root=ROOT):
    result, raw, repair = resolve_source(vendor, version, mode, root)
    tools = select_node(vendor, json.loads(raw['frontend/package.json']), json.loads(raw['frontend/package-lock.json']))
    installer, geoip = installer_inputs(vendor, result['version'], mode, root)
    contract = {'schema': 1, 'kind': '1panel-resolved-build-inputs', 'version': result['version'], 'mode': mode, 'edition': 'community', 'architectures': ARCHES, 'source': result['source'], 'installer': installer, 'toolchain': dict(go=result['go'], **tools), 'resources': {'geoip': geoip}, 'configuration': result['configuration'], 'frontend_lock': result['frontend_lock']}
    return contract, repair


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version')
    parser.add_argument('--mode', choices=['stable', 'beta', 'dev'])
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    vendor = Vendor()
    mode = args.mode or (identity(args.version) if args.version else 'stable')
    if args.latest:
        print(latest(vendor, mode))
    else:
        if args.output is None:
            parser.error('--output is required for immutable contract discovery')
        contract, _ = discover(vendor, args.version, mode)
        args.output.write_bytes(canonical(contract))
        print(hashlib.sha256(canonical(contract)).hexdigest())
