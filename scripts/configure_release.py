#!/usr/bin/env python3
"""Normalize only the exact reviewed configuration schema for this version."""
import pathlib,sys
from resolve_inputs import resolve
from embedded_configuration import expected_bytes

def configure(root,version):
    entry=resolve(version);pending=[]
    for component in ('core','agent'):
        file=root/component/'cmd/server/conf/app.yaml'
        original,normalized,source_commit=expected_bytes(version,component)
        if source_commit!=entry['source_commit']:raise ValueError('Source commit does not match the reviewed per-version configuration')
        if file.read_bytes() not in (original,normalized):raise ValueError(f'{file}: source YAML differs from reviewed per-version schema')
        pending.append((file,normalized))
    for file,normalized in pending:file.write_bytes(normalized)
if __name__=='__main__':configure(pathlib.Path(sys.argv[1]),sys.argv[2])
