#!/usr/bin/env python3
"""Verify reviewed dependency inputs before compatibility patches or installation."""
import hashlib
import re
from pathlib import Path
import sys
from resolve_inputs import resolve, SOURCE_FILES


def verify_go_requirements(data, selected, relative):
    """Check the exact hashed module, rather than a separate audit report."""
    chosen = tuple(map(int, selected.split('.')))
    directives = {}
    for line in data.decode('utf-8').splitlines():
        fields = line.split('//', 1)[0].split()
        if not fields or fields[0] not in ('go', 'toolchain'):
            continue
        if len(fields) != 2 or fields[0] in directives:
            raise ValueError('Invalid Go directive: ' + relative)
        directives[fields[0]] = fields[1]
    if 'go' not in directives:
        raise ValueError('Missing Go requirement: ' + relative)
    for kind, value in directives.items():
        if kind == 'toolchain':
            if value == 'default':
                continue
            if not value.startswith('go'):
                raise ValueError('Invalid Go toolchain: ' + relative)
            value = value[2:]
        if not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', value):
            raise ValueError('Unsupported Go requirement: ' + relative)
        required = tuple(map(int, value.split('.')))
        required += (0,) * (3 - len(required))
        if chosen < required:
            raise ValueError('Pinned Go compiler is below module requirement: ' + relative)


def verify(root, version):
    entry = resolve(version)
    expected = entry.get('source_files_sha256', {})
    absent = entry.get('source_files_absent', [])
    if absent not in ([], ['frontend/package-lock.json']):
        raise ValueError('Unsupported absent source input')
    if set(expected) != SOURCE_FILES - set(absent):
        raise ValueError('Incomplete reviewed dependency input hashes')
    for relative in absent:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise ValueError('Unexpected source input: ' + relative)
    for relative, digest in expected.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or linked dependency input: ' + relative)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Dependency input checksum mismatch: ' + relative)
        if relative in ('core/go.mod', 'agent/go.mod'):
            verify_go_requirements(data, entry['go_version'], relative)


if __name__ == '__main__':
    verify(Path(sys.argv[1]), sys.argv[2])
