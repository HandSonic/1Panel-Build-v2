"""Small, non-executable build inputs carried by ordinary installation archives."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile

FORMAT = 'package-only-v1'
DIRECTORY = 'build-metadata'


def embed_legacy(directory, version):
    """Add data-only inputs to already validated CI packages; never rebuild payloads."""
    from automatic_frontend_lock import MAX_LOCK, read_regular, read_payload, required_sidecars
    from resolved_contract import MAX_CONTROL, canonical, object_bytes, source_contract
    directory = Path(directory)
    summary_path = directory / 'build-manifest.json'
    summary = json.loads(summary_path.read_text())
    raw = read_regular(directory / 'resolved-source.json', MAX_CONTROL)
    sha = hashlib.sha256(raw).hexdigest()
    contract = source_contract(raw, sha, version, object_bytes(raw)['mode'])
    if raw != canonical(contract):
        raise ValueError('Noncanonical migration source contract')
    read_payload(directory, contract)
    additions = {'resolved-source.json': raw}
    additions.update({name: read_regular(directory / name, MAX_LOCK) for name in required_sidecars(contract)})
    changed = False
    for row in summary['artifacts']:
        arch = row['architecture']
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        if row['file'] != name or row.get('resolved_contract_sha256') != sha:
            raise ValueError('Migration archive identity/source mismatch')
        path = directory / name
        if read(path, version, arch) is not None:
            continue
        prefix = name.removesuffix('.tar.gz') + '/'
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            output = Path(temporary) / name
            with tarfile.open(path, 'r:gz') as old, tarfile.open(output, 'w:gz') as new:
                members = old.getmembers()
                if any(PurePosixPath(m.name) == PurePosixPath(prefix + DIRECTORY) and not m.isdir() for m in members):
                    raise ValueError('Legacy payload conflicts with metadata directory')
                manifests = [m for m in members if m.name == prefix + 'manifest.json']
                if len(manifests) != 1 or not manifests[0].isfile() or not 0 < manifests[0].size <= MAX_CONTROL:
                    raise ValueError('Invalid legacy manifest')
                manifest = object_bytes(old.extractfile(manifests[0]).read())
                if manifest.get('resolved_contract_sha256') != sha:
                    raise ValueError('Legacy manifest source mismatch')
                seen = set()
                for member in members:
                    p = PurePosixPath(member.name)
                    if member.name in seen or p.is_absolute() or '..' in p.parts or p.parts[0] != prefix[:-1]:
                        raise ValueError('Unsafe or duplicate migration member')
                    seen.add(member.name)
                    if member.isdir():
                        new.addfile(member)
                        continue
                    if not member.isfile() or not member.name.startswith(prefix):
                        raise ValueError('Nonregular migration member')
                    rel = member.name[len(prefix):]
                    if rel == 'manifest.json':
                        continue
                    body = old.extractfile(member).read()
                    if manifest['files'].get(rel) != {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}:
                        raise ValueError('Migration payload differs from original manifest')
                    # Preserve every original payload byte and tar permission unchanged.
                    new.addfile(member, io.BytesIO(body))
                actual = {m.name[len(prefix):] for m in members if m.isfile() and m.name != prefix + 'manifest.json'}
                if set(manifest['files']) != actual:
                    raise ValueError('Migration manifest file set mismatch')
                for filename, body in additions.items():
                    rel = DIRECTORY + '/' + filename
                    info = tarfile.TarInfo(prefix + rel)
                    info.mode = 0o644
                    info.size = len(body)
                    new.addfile(info, io.BytesIO(body))
                    manifest['files'][rel] = {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}
                manifest['publication_format'] = FORMAT
                body = canonical(manifest)
                info = tarfile.TarInfo(prefix + 'manifest.json')
                info.mode = 0o644
                info.size = len(body)
                new.addfile(info, io.BytesIO(body))
            read(output, version, arch)
            os.replace(output, path)
        row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        row['size'] = path.stat().st_size
        (directory / (name + '.sha256')).write_text(row['sha256'] + '  ' + name + '\n')
        changed = True
    if changed:
        summary_path.write_bytes(canonical(summary))
        (directory / 'checksums.txt').write_text(''.join((directory / (row['file'] + '.sha256')).read_text() for row in summary['artifacts']))
    return changed


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
