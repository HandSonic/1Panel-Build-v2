"""Small, non-executable build inputs carried by ordinary installation archives."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

FORMAT = 'package-only-v1'
DIRECTORY = 'build-metadata'


def install(stage, config, entry):
    from automatic_frontend_lock import copy_payload, read_regular, write_payload
    from resolved_contract import MAX_CONTROL, source_contract
    raw = read_regular(Path(config) / 'resolved-source.json', MAX_CONTROL)
    value = source_contract(raw, entry['resolved_contract_sha256'], entry['version'], entry['mode'])
    target = Path(stage) / DIRECTORY
    target.mkdir()
    write_payload(target / 'resolved-source.json', raw)
    copy_payload(value, Path(config), target)
    for path in target.iterdir():
        path.chmod(0o644)


def read(path, version, arch):
    """Read only bounded data; absence means legacy, malformed new data never does."""
    from automatic_frontend_lock import MAX_LOCK, required_sidecars, validate_payload
    from resolved_contract import MAX_CONTROL, canonical, object_bytes, source_contract
    prefix = f'1panel-{version}-linux-{arch}/'
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive member')
        by_name = {m.name: m for m in members}
        def body(name, limit):
            member = by_name.get(prefix + name)
            if member is None or not member.isfile() or not 0 < member.size <= limit:
                raise ValueError('Missing or invalid package metadata: ' + name)
            if name.startswith(DIRECTORY + '/') and member.mode & 0o111:
                raise ValueError('Package metadata must not be executable')
            return archive.extractfile(member).read()
        manifest = object_bytes(body('manifest.json', MAX_CONTROL))
        marker = manifest.get('publication_format')
        if marker is None:
            if any(name.startswith(prefix + DIRECTORY + '/') for name in names):
                raise ValueError('Unmarked package metadata')
            return None
        if marker != FORMAT:
            raise ValueError('Unsupported package publication format')
        if manifest.get('version') != version or manifest.get('architecture') != arch:
            raise ValueError('Package metadata identity mismatch')
        raw = body(DIRECTORY + '/resolved-source.json', MAX_CONTROL)
        value = source_contract(raw, manifest.get('resolved_contract_sha256'), version, manifest.get('mode'))
        if raw != canonical(value):
            raise ValueError('Noncanonical package source contract')
        files = {'resolved-source.json': raw}
        for name in required_sidecars(value):
            files[name] = body(DIRECTORY + '/' + name, MAX_LOCK)
            validate_payload(files[name], value)
        expected = {prefix + DIRECTORY + '/' + name for name in files}
        actual = {m.name for m in members if m.name.startswith(prefix + DIRECTORY + '/') and not m.isdir()}
        if actual != expected:
            raise ValueError('Unexpected package metadata member')
        for name, raw in files.items():
            if manifest.get('files', {}).get(DIRECTORY + '/' + name) != {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}:
                raise ValueError('Package metadata is not manifest-bound')
        return manifest, files
