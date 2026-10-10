#!/usr/bin/env python3
"""Resolve missing locks once; transport and replay only authenticated bytes.

The exact generator validates all engines using npm's semver before the real
frontend build repeats npm ci --engine-strict. Transport needs no npm runtime:
the authenticated contract proves that generator gate, while every replay checks
source manifest bytes, graph identity, registry origins, integrity, size and hash.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from resolved_contract import exact_keys, hash_value, require, commit_value

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = 'resolved-frontend-package-lock.json'
MAX_MANIFEST = 65536
MAX_LOCK = 4 * 1024 * 1024
GENERATOR = {'node': '22.22.1', 'npm': '10.9.4',
             'registry': 'https://registry.npmjs.org', 'lifecycle_scripts': False}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def data_object(raw, bound, label):
    require(isinstance(raw, bytes) and 0 < len(raw) <= bound, 'Invalid ' + label + ' size')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'Duplicate ' + label + ' field: ' + key)
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    require(isinstance(value, dict), label + ' must be an object')
    return value


def read_regular(path, bound):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValueError('Missing resolved lock input: ' + path.name) from error
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= bound,
            'Linked, nonregular or oversize resolved lock input: ' + path.name)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError('Resolved lock input changed while opening: ' + path.name) from error
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        require(stat.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino, opened.st_size) ==
                (info.st_dev, info.st_ino, info.st_size), 'Resolved lock input changed while opening')
        raw = source.read(bound + 1)
    require(0 < len(raw) <= bound and len(raw) == info.st_size, 'Resolved lock input changed while reading')
    return raw


def validate_descriptor(lock, source):
    exact_keys(lock, ('kind', 'sha256', 'bytes', 'manifest_sha256', 'manifest', 'generator'), 'resolved frontend lock')
    require(lock['kind'] == 'resolved', 'Expected automatically resolved lock')
    hash_value(lock['sha256']); hash_value(lock['manifest_sha256'])
    require(type(lock['bytes']) is int and 0 < lock['bytes'] <= MAX_LOCK, 'Invalid resolved lock size')
    exact_keys(lock['generator'], GENERATOR, 'lock generator')
    require(lock['generator'] == GENERATOR and lock['generator']['lifecycle_scripts'] is False,
            'Unsupported automatic lock generator')
    require(source['repository'] == '1Panel-dev/1Panel', 'Unexpected automatic lock source')
    commit_value(source['commit'])
    require(source['absent'] == ['frontend/package-lock.json'] and 'frontend/package-lock.json' not in source['files'],
            'Automatic lock requires immutable source-lock absence')
    require(isinstance(lock['manifest'], str), 'Missing resolved source manifest bytes')
    raw = lock['manifest'].encode('utf-8')
    manifest = data_object(raw, MAX_MANIFEST, 'frontend manifest')
    require(sha(raw) == lock['manifest_sha256'] and source['files']['frontend/package.json'] ==
            {'sha256': sha(raw), 'bytes': len(raw)}, 'Resolved manifest differs from immutable source')
    return manifest


def validate_payload(raw, contract):
    descriptor = contract['frontend_lock']
    manifest = validate_descriptor(descriptor, contract['source'])
    require(len(raw) == descriptor['bytes'] and sha(raw) == descriptor['sha256'], 'Resolved frontend lock bytes mismatch')
    lock = data_object(raw, MAX_LOCK, 'frontend lock')
    require(type(lock.get('lockfileVersion')) is int and lock['lockfileVersion'] == 3 and
            isinstance(lock.get('packages'), dict) and isinstance(lock['packages'].get(''), dict),
            'Unsupported resolved lock schema')
    root = lock['packages']['']
    for key in ('name', 'version'):
        require(lock.get(key) == manifest.get(key) and root.get(key) == manifest.get(key), 'Resolved lock root identity differs')
    for key in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
        require(root.get(key, {}) == manifest.get(key, {}), 'Resolved lock root manifest differs: ' + key)
    from frontend_lock_origins import validate_lock_origins
    validate_lock_origins(lock)
    # Bundled children inherit only a declared, pinned containing archive. The
    # immutable producer also inspected its actual member manifests; transport
    # authenticates those generated bytes rather than downloading archives again.
    return raw


def required_sidecars(contract):
    return (SIDECAR,) if contract['frontend_lock']['kind'] == 'resolved' else ()


def read_payload(directory, contract):
    path = Path(directory) / SIDECAR
    if not required_sidecars(contract):
        require(not path.exists() and not path.is_symlink(), 'Unexpected resolved frontend lock sidecar')
        return None
    return validate_payload(read_regular(path, MAX_LOCK), contract)


def write_payload(path, raw):
    """No replacing another run's inputs, including symlinks or partial payloads."""
    path = Path(path)
    require(not path.parent.is_symlink(), 'Linked resolved input directory rejected')
    if path.exists() or path.is_symlink():
        require(read_regular(path, MAX_LOCK) == raw, 'Refusing to replace a different resolved frontend lock')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as output:
            output.write(raw)


def copy_payload(contract, source_directory, destination_directory):
    raw = read_payload(source_directory, contract)
    if raw is not None:
        write_payload(Path(destination_directory) / SIDECAR, raw)


def apply(frontend, contract, root=ROOT):
    frontend = Path(frontend)
    raw = read_payload(Path(root) / 'config', contract)
    manifest = read_regular(frontend / 'package.json', MAX_MANIFEST)
    require(manifest.decode('utf-8') == contract['frontend_lock']['manifest'], 'Frontend manifest changed before lock replay')
    target = frontend / 'package-lock.json'
    require(not target.exists() and not target.is_symlink(), 'Original lock absence no longer holds')
    with target.open('xb') as output:
        output.write(raw)


def generate(manifest_raw, source_commit, manifest_sha256):
    """Network resolution is confined to this producer function, never replay."""
    commit_value(source_commit)
    hash_value(manifest_sha256)
    require(sha(manifest_raw) == manifest_sha256, 'Resolver manifest checksum mismatch')
    data_object(manifest_raw, MAX_MANIFEST, 'frontend manifest')
    manifest_text = manifest_raw.decode('utf-8')
    node, npm = shutil.which('node'), shutil.which('npm')
    require(node is not None and npm is not None, 'Pinned Node/npm automatic lock resolver is unavailable')
    node, npm_cli = Path(node).resolve(), Path(npm).resolve()
    npm_dir = npm_cli.parents[1]
    npm_package = data_object(read_regular(npm_dir / 'package.json', MAX_MANIFEST), MAX_MANIFEST, 'npm package')
    require(npm_package.get('name') == 'npm' and npm_package.get('version') == GENERATOR['npm']
            and npm_cli == npm_dir / 'bin/npm-cli.js', 'Automatic lock resolver requires exact npm ' + GENERATOR['npm'])
    with tempfile.TemporaryDirectory(prefix='1panel-lock-') as temporary:
        work = Path(temporary)
        for name in ('home', 'tmp', 'npm-cache'):
            (work / name).mkdir()
        for name in ('empty-user.npmrc', 'empty-global.npmrc'):
            (work / name).write_bytes(b'')
        (work / 'package.json').write_bytes(manifest_raw)
        # Never forward tokens, NODE_OPTIONS, proxies or npm settings from CI.
        env = {'PATH': str(node.parent) + os.pathsep + '/usr/bin:/bin',
               'HOME': str(work / 'home'), 'TMPDIR': str(work / 'tmp'), 'LANG': 'C.UTF-8',
               'LOCK_NPM_DIR': str(npm_dir), 'NPM_CONFIG_USERCONFIG': str(work / 'empty-user.npmrc'),
               'NPM_CONFIG_GLOBALCONFIG': str(work / 'empty-global.npmrc'),
               'NPM_CONFIG_CACHE': str(work / 'npm-cache'), 'NPM_CONFIG_REGISTRY': GENERATOR['registry'],
               'NPM_CONFIG_IGNORE_SCRIPTS': 'true', 'NPM_CONFIG_ENGINE_STRICT': 'true',
               'NPM_CONFIG_AUDIT': 'false', 'NPM_CONFIG_FUND': 'false', 'NPM_CONFIG_LOGLEVEL': 'error'}
        def run(command):
            try:
                result = subprocess.run(command, cwd=work, env=env, check=True,
                                        capture_output=True, text=True, timeout=600)
            except subprocess.CalledProcessError as error:
                raise ValueError('Automatic frontend lock resolution failed: ' + (error.stderr or '')[-4000:]) from error
            except subprocess.TimeoutExpired as error:
                raise ValueError('Automatic frontend lock resolution timed out') from error
            require(read_regular(work / 'package.json', MAX_MANIFEST) == manifest_raw,
                    'Resolver changed immutable source manifest')
            require(not (work / 'node_modules').exists() and not (work / 'node_modules').is_symlink(),
                    'Lock-only resolver unexpectedly materialized dependencies')
            return result.stdout.strip()
        require(run([str(node), '--version']) == 'v' + GENERATOR['node'],
                'Automatic lock resolver requires exact Node ' + GENERATOR['node'])
        sandbox = [str(node), '--permission', '--allow-fs-read=*', '--allow-fs-write=' + str(work)]
        validator = str(ROOT / 'scripts/automatic_frontend_lock.mjs')
        run(sandbox + [validator, 'preflight'])
        flags = ['--ignore-scripts', '--engine-strict', '--no-audit', '--no-fund']
        run(sandbox + [str(npm_cli), 'install', '--package-lock-only'] + flags)
        raw = read_regular(work / 'package-lock.json', MAX_LOCK)
        run(sandbox + [validator, 'validate'])
        descriptor = {'kind': 'resolved', 'sha256': sha(raw), 'bytes': len(raw),
                      'manifest_sha256': manifest_sha256, 'manifest': manifest_text, 'generator': dict(GENERATOR)}
        source = {'repository': '1Panel-dev/1Panel', 'commit': source_commit,
                  'files': {'frontend/package.json': {'sha256': manifest_sha256, 'bytes': len(manifest_raw)}},
                  'absent': ['frontend/package-lock.json']}
        validate_payload(raw, {'frontend_lock': descriptor, 'source': source})
        from verify_bundled_archives import verify_bundled_archives
        verify_bundled_archives(json.loads(raw))
        run(sandbox + [str(npm_cli), 'ci', '--dry-run'] + flags)
        require(read_regular(work / 'package-lock.json', MAX_LOCK) == raw, 'npm ci changed resolved lock bytes')
        return raw, descriptor
