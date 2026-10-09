#!/usr/bin/env python3
"""Verify reviewed dependency inputs before compatibility patches or installation."""
import hashlib
from pathlib import Path
import sys
from resolve_inputs import resolve, SOURCE_FILES


def verify(root, version):
    entry = resolve(version)
    expected = entry.get('source_files_sha256', {})
    if set(expected) != SOURCE_FILES:
        raise ValueError('Incomplete reviewed dependency input hashes')
    for relative, digest in expected.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or linked dependency input: ' + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Dependency input checksum mismatch: ' + relative)


if __name__ == '__main__':
    verify(Path(sys.argv[1]), sys.argv[2])
