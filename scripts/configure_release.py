#!/usr/bin/env python3
"""Normalize only the exact reviewed configuration schema for this version."""
import pathlib,sys
from resolve_inputs import resolve
from embedded_configuration import expected_bytes
from semantic_configuration import semantic,production

def configure(root,version):
    entry=resolve(version);pending=[]
    for component in ('core','agent'):
        file=root/component/'cmd/server/conf/app.yaml'
        original,normalized,source_commit=expected_bytes(version,component)
        if source_commit!=entry['source_commit']:raise ValueError('Source commit does not match the reviewed per-version configuration')
        actual=file.read_bytes()
        production(actual,version,component,entry['mode'])  # Reject structural/type ambiguity first.
        if semantic(actual) not in (semantic(original),semantic(normalized)):
            raise ValueError(f'{file}: source YAML differs from reviewed per-version schema')
        # Canonical reviewed bytes retain the exact independently pinned binary check.
        # Formatting-only source variations are accepted without weakening source semantics.
        pending.append((file,normalized))
    for file,normalized in pending:file.write_bytes(normalized)
if __name__=='__main__':configure(pathlib.Path(sys.argv[1]),sys.argv[2])
