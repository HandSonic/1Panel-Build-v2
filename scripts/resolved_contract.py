#!/usr/bin/env python3
"""Validate immutable source contracts shared by producer and installer pipelines.

This module is deliberately transport-independent. Its caller must authenticate
the expected custom contract digest using the verified upstream release receipt.
Vendor responses are collected once from the canonical endpoints; the resulting
plan is immutable input to later download, validation, native and publication
jobs. Archive contents still require the existing independent byte validators.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

ARCHES = ('amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'loong64', 'riscv64')
NATIVE = ('amd64', 'arm64')
SOURCES = ('official', 'custom', 'enterprise-original', 'enterprise-docker')
INSTALLER_REQUIRED = {'install.sh', '1pctl'} | {
    f'initscript/1panel-{part}.{kind}' for part in ('core', 'agent')
    for kind in ('service', 'init', 'openrc', 'procd')} | {
    f'lang/{language}.sh' for language in ('en', 'fa', 'pt-BR', 'ru', 'zh')}
VERSION = re.compile(r'v2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:beta|dev)\.[0-9]+))?')
MAX_CONTROL = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def object_bytes(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_CONTROL, 'Invalid control size')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate control field: ' + key)
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    require(isinstance(value, dict), 'Control must be an object')
    return value


def exact_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), 'Unsupported ' + label + ' fields')


def hash_value(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA-256')
    return value


def commit_value(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'Immutable commit required')
    return value


def safe_path(value):
    require(isinstance(value, str) and value and '\\' not in value, 'Invalid input path')
    p = PurePosixPath(value)
    require(not p.is_absolute() and '..' not in p.parts and str(p) == value and value != '.',
            'Unsafe input path')
    return value


def file_facts(value):
    exact_keys(value, ('sha256', 'bytes'), 'file facts')
    hash_value(value['sha256'])
    require(type(value['bytes']) is int and value['bytes'] > 0, 'Invalid file size')


def file_map(value):
    require(isinstance(value, dict) and value, 'Missing immutable input files')
    for name, facts in value.items():
        safe_path(name)
        file_facts(facts)


def version_identity(version, mode):
    require(isinstance(version, str), 'Explicit version required')
    match = VERSION.fullmatch(version)
    require(match is not None and mode in ('stable', 'beta', 'dev'), 'Unsupported version or channel')
    suffix = match[3]
    require((mode == 'stable' and suffix is None) or
            (mode != 'stable' and suffix is not None and re.match(re.escape(mode) + r'(?:[.-]|$)', suffix)),
            'Version/channel mismatch')
    return match


def source_contract(raw, expected_digest, version, mode):
    """Validate the shared upper/lower resolved-input schema, independent of version."""
    version_identity(version, mode)
    value = object_bytes(raw)
    require(digest(value) == hash_value(expected_digest), 'Resolved source contract digest mismatch')
    exact_keys(value, ('schema', 'kind', 'version', 'mode', 'edition', 'architectures',
                       'source', 'installer', 'toolchain', 'resources', 'configuration',
                       'frontend_lock'), 'source contract')
    require(type(value['schema']) is int and value['schema'] == 1 and
            value['kind'] == '1panel-resolved-build-inputs' and value['version'] == version and
            value['mode'] == mode and value['edition'] == 'community', 'Source contract identity mismatch')
    require(isinstance(value['architectures'], list) and
            len(value['architectures']) == len(ARCHES) and set(value['architectures']) == set(ARCHES),
            'Complete supported custom architecture set required')
    source = value['source']
    exact_keys(source, ('repository', 'commit', 'files', 'absent'), 'source identity')
    require(source['repository'] == '1Panel-dev/1Panel', 'Unexpected application source repository')
    commit_value(source['commit'])
    file_map(source['files'])
    require(isinstance(source['absent'], list) and len(source['absent']) == len(set(source['absent'])),
            'Invalid source absence list')
    for path in source['absent']:
        safe_path(path)
        require(path not in source['files'], 'Input cannot be both present and absent')
    installer = value['installer']
    exact_keys(installer, ('repository', 'commit', 'resources', 'original_version'), 'installer identity')
    require(installer['repository'] == '1Panel-dev/installer', 'Unexpected installer repository')
    commit_value(installer['commit'])
    file_map(installer['resources'])
    require(INSTALLER_REQUIRED <= set(installer['resources']), 'Incomplete installer resource contract')
    require(isinstance(installer['original_version'], str) and
            (installer['original_version'] == 'version' or VERSION.fullmatch(installer['original_version'])),
            'Invalid original installer version')
    exact_keys(value['toolchain'], ('go', 'node', 'npm'), 'toolchain')
    for tool, pinned in value['toolchain'].items():
        require(isinstance(pinned, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pinned),
                'Exact ' + tool + ' toolchain version required')
    exact_keys(value['resources'], ('geoip',), 'resources')
    geoip = value['resources']['geoip']
    file_facts({k: geoip[k] for k in ('sha256', 'bytes')})
    require(geoip['bytes'] <= 64 * 1024 * 1024, 'GeoIP size exceeds bound')
    if 'archive' in geoip:
        exact_keys(geoip, ('archive', 'sha256', 'bytes'), 'GeoIP resource')
        archive = geoip['archive']
        exact_keys(archive, ('url', 'sha256', 'bytes', 'member'), 'GeoIP archive')
        file_facts({k: archive[k] for k in ('sha256', 'bytes')})
        name = '1panel-' + value['version'] + '-linux-amd64'
        require(archive['url'] == 'https://resource.fit2cloud.com/1panel/package/v2/' + value['mode'] + '/' + value['version'] + '/release/' + name + '.tar.gz'
                and archive['member'] == name + '/GeoIP.mmdb'
                and archive['bytes'] <= 512 * 1024 * 1024, 'Invalid version-bound GeoIP archive')
    else:
        exact_keys(geoip, ('url', 'sha256', 'bytes'), 'GeoIP resource')
        url = urlsplit(geoip['url'])
        require(url.scheme == 'https' and url.hostname in ('github.com', 'raw.githubusercontent.com', 'resource.fit2cloud.com')
                and url.username is None and url.password is None and url.port in (None, 443)
                and not url.fragment, 'Unsupported GeoIP resource origin')
    exact_keys(value['configuration'], ('core', 'agent'), 'configuration')
    for profile in value['configuration'].values():
        exact_keys(profile, ('path', 'source_sha256', 'source_bytes', 'normalized_sha256'), 'configuration profile')
        safe_path(profile['path'])
        require(type(profile['source_bytes']) is int and profile['source_bytes'] > 0, 'Invalid source config size')
        require(source['files'].get(profile['path']) ==
                {'sha256': hash_value(profile['source_sha256']), 'bytes': profile['source_bytes']},
                'Configuration is not bound to immutable source bytes')
        hash_value(profile['normalized_sha256'])
    lock = value['frontend_lock']
    require(isinstance(lock, dict) and lock.get('kind') in ('source', 'derived'), 'Unsupported frontend lock origin')
    fields = ('kind', 'sha256', 'manifest_sha256') + (('recipe_sha256',) if lock['kind'] == 'derived' else ())
    exact_keys(lock, fields, 'frontend lock')
    for key in fields[1:]:
        hash_value(lock[key])
    require(source['files'].get('frontend/package.json', {}).get('sha256') == lock['manifest_sha256'],
            'Frontend dependency manifest is not bound to source')
    if lock['kind'] == 'source':
        require(source['files'].get('frontend/package-lock.json', {}).get('sha256') == lock['sha256'],
                'Frontend lock is not bound to source')
    elif 'frontend/package-lock.json' not in source['files']:
        require('frontend/package-lock.json' in source['absent'], 'Derived lock requires explicit source-lock absence')
    return value



def apply_contract(path, expected_digest, root, version):
    """Install a verified generated contract into this disposable CI checkout only."""
    from pathlib import Path
    root, path = Path(root), Path(path)
    raw = path.read_bytes()
    mode = object_bytes(raw).get('mode')
    contract = source_contract(raw, expected_digest, version, mode)
    inputs = contract['source']
    required = {'core/go.mod', 'agent/go.mod', 'frontend/package.json', 'frontend/package-lock.json'}
    absent = inputs['absent']
    require(absent in ([], ['frontend/package-lock.json']), 'Unsupported dependency absence contract')
    require(set(inputs['files']) == required - set(absent) | {f'{c}/cmd/server/conf/app.yaml' for c in ('core', 'agent')}, 'Unexpected source input set')
    from discover_inputs import INSTALLER_FILES
    require(set(contract['installer']['resources']) == set(INSTALLER_FILES), 'Incomplete installer resources')
    for component, profile in contract['configuration'].items():
        require(profile['path'] == f'{component}/cmd/server/conf/app.yaml' and type(profile['source_bytes']) is int and 0 < profile['source_bytes'] <= 65536, 'Unsupported embedded source contract')
    sources_path = root / 'config/sources.json'
    sources = object_bytes(sources_path.read_bytes())
    profiles_path = root / 'config/embedded-configs.json'
    profiles = object_bytes(profiles_path.read_bytes())
    recipes_path = root / 'config/frontend-lock-repairs.json'
    recipes = object_bytes(recipes_path.read_bytes())
    lock = contract['frontend_lock']
    recipe = None
    if lock['kind'] == 'derived':
        require(absent == ['frontend/package-lock.json'], 'Derived contract must bind original lock absence')
        matches = [r for v, r in recipes['versions'].items() if sources.get(v, {}).get('source_files_absent') == ['frontend/package-lock.json'] and sources[v].get('source_files_sha256', {}).get('frontend/package.json') == lock['manifest_sha256'] and digest(r) == lock['recipe_sha256'] and r.get('original_absent') and r.get('derived_sha256') == lock['sha256']]
        require(bool(matches), 'Derived contract lacks an existing verified repair recipe')
        recipe = matches[0]
        lock_file = root / 'config' / recipe['reviewed_lock_file']
        require(not lock_file.is_symlink() and lock_file.is_file() and hashlib.sha256(lock_file.read_bytes()).hexdigest() == lock['sha256'], 'Derived lock bytes mismatch')
    else:
        require(not absent, 'Source lock cannot be absent')
    entry = {'source_commit': inputs['commit'], 'installer_commit': contract['installer']['commit'],
             'go_version': contract['toolchain']['go'], 'node_version': contract['toolchain']['node'], 'npm_version': contract['toolchain']['npm'],
             'mode': mode, 'geoip_sha256': contract['resources']['geoip']['sha256'],
             'installer_sha256': {name: row['sha256'] for name, row in contract['installer']['resources'].items()},
             'installer_original_version': contract['installer']['original_version'],
             'source_files_sha256': {name: row['sha256'] for name, row in inputs['files'].items() if name in required},
             'resolved_contract_sha256': expected_digest}
    geoip = contract['resources']['geoip']
    entry['geoip_archive' if 'archive' in geoip else 'geoip_url'] = geoip.get('archive', geoip.get('url'))
    entry['geoip_bytes'] = geoip['bytes']
    if absent:
        entry['source_files_absent'] = absent
        entry['frontend_repair_lock_sha256'] = lock['sha256']
    profile = {'source_commit': inputs['commit'], 'mode': mode, 'components': {name: {'source_acquisition': 'immutable_vendor_https', 'source_path': row['path'], 'source_bytes': row['source_bytes'], 'source_sha256': row['source_sha256'], 'normalized_sha256': row['normalized_sha256']} for name, row in contract['configuration'].items()}}
    if version in sources and sources[version] != entry:
        require(sources[version].get('resolved_contract_sha256') == expected_digest, 'Refusing to replace a checked-in historical input contract')
    if version in profiles and profiles[version] != profile:
        require(version in sources and sources[version].get('resolved_contract_sha256') == expected_digest, 'Refusing to replace a checked-in historical configuration profile')
    # All checks precede writes; the parent contract digest is passed across jobs.
    sources[version], profiles[version] = entry, profile
    if recipe is not None:
        recipes['versions'][version] = recipe
    sources_path.write_bytes(canonical(sources) + b'\n')
    profiles_path.write_bytes(canonical(profiles) + b'\n')
    recipes_path.write_bytes(canonical(recipes) + b'\n')
    (root / 'config/resolved-source.json').write_bytes(canonical(contract))
    return contract


def activate_release(directory, version, root=None):
    from pathlib import Path
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    directory = Path(directory)
    path = directory / 'resolved-source.json'
    if not path.exists():
        return None
    manifest = object_bytes((directory / 'build-manifest.json').read_bytes())
    require(manifest.get('version') == version, 'Contract release version mismatch')
    digests = {row.get('resolved_contract_sha256') for row in manifest.get('artifacts', [])}
    require(len(digests) == 1 and None not in digests, 'Missing or mixed artifact contract binding')
    expected = digests.pop()
    apply_contract(path, expected, root, version)
    return expected


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['apply', 'activate'])
    parser.add_argument('path', type=Path)
    parser.add_argument('version')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if args.operation == 'apply':
        apply_contract(args.path, args.sha256, Path(__file__).resolve().parents[1], args.version)
    else:
        activate_release(args.path, args.version)
