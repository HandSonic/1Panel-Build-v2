#!/usr/bin/env python3
"""Validate isolated matrix outputs before reconstructing the release contract."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from package_release import finalize
from validate_artifacts import architectures, validate_dist


def aggregate(shards, destination, version, arches, run_suffix):
    if destination.name != 'dist':
        raise ValueError('Aggregate destination must be named dist')
    expected = {f'shard-{arch}-{run_suffix}' for arch in arches}
    if {p.name for p in shards.iterdir()} != expected:
        raise ValueError('Missing, extra or unexpected architecture shards')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Aggregate destination must be empty')
    inputs = subprocess.check_output([sys.executable, str(Path(__file__).with_name('resolve_inputs.py')), version])
    # Validate everything first; never merge shard manifests with conflicting names.
    for arch in arches:
        shard = shards / f'shard-{arch}-{run_suffix}'
        records = validate_dist(shard, version, [arch])
        if any(r['build_repository_commit'] != os.environ['BUILD_REPOSITORY_COMMIT'] for r in records):
            raise ValueError('Shard build commit mismatch')
        if (shard / 'build-inputs.env').read_bytes() != inputs:
            raise ValueError('Shard resolved inputs mismatch')
    destination.mkdir(parents=True, exist_ok=True)
    for arch in arches:
        shard = shards / f'shard-{arch}-{run_suffix}'
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        for file in (name, name + '.sha256'):
            shutil.copyfile(shard / file, destination / file)
    (destination / 'build-inputs.env').write_bytes(inputs)
    # finalize expects a root containing dist; use its unchanged contract writer.
    finalize(destination.parent, version, arches)


if __name__ == '__main__':
    aggregate(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], architectures(sys.argv[4]), sys.argv[5])
