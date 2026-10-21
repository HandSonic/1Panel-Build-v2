#!/usr/bin/env python3
"""Validate isolated matrix outputs before reconstructing the release contract."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from package_release import write_summary
from validate_artifacts import architectures, validate_dist
INPUT_ROOT = Path(__file__).resolve().parents[1]


def aggregate(shards, destination, version, arches, run_suffix, outcomes=None):
    requested = list(arches)
    if outcomes is not None:
        arches = [row["architecture"] for row in outcomes if row["status"] == "success"]
        if not arches:
            raise ValueError("No independently successful architecture to publish")
    if outcomes is not None:
        from matrix_contract import validate
        validate({'schema_version':2,'version':version,'requested_architectures':requested,
                  'producer_run_id':int(os.environ['GITHUB_RUN_ID']),
                  'producer_run_attempt':int(os.environ['GITHUB_RUN_ATTEMPT']),
                  'producer_head_sha':os.environ['PRODUCER_HEAD_SHA'],
                  'outcomes':outcomes,'artifacts':[{'architecture':a} for a in arches]},version)
    if destination.name != 'dist':
        raise ValueError('Aggregate destination must be named dist')
    expected = {f'shard-{arch}-{run_suffix}' for arch in arches}
    actual={p.name for p in shards.iterdir()}
    allowed={f'shard-{arch}-{run_suffix}' for arch in requested}
    if (outcomes is None and actual!=expected) or (outcomes is not None and (not expected<=actual or not actual<=allowed)):
        raise ValueError('Missing, extra or unexpected architecture shards')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Aggregate destination must be empty')
    contract_sha=os.environ.get('RESOLVED_CONTRACT_SHA256','')
    if contract_sha:
        from resolved_contract import apply_contract
        for arch in arches:
            path=shards/f'shard-{arch}-{run_suffix}'/'resolved-source.json'
            # This authenticates the contract and its exact required sidecar in
            # each successful shard before the aggregate destination is created.
            apply_contract(path,contract_sha,INPUT_ROOT,version)
    inputs = subprocess.check_output([sys.executable, str(Path(__file__).with_name('resolve_inputs.py')), version])
    # Validate everything first; never merge shard manifests with conflicting names.
    records = []
    for arch in arches:
        shard = shards / f'shard-{arch}-{run_suffix}'
        shard_records = validate_dist(shard, version, [arch])
        if any(r['build_repository_commit'] != os.environ['BUILD_REPOSITORY_COMMIT'] for r in shard_records):
            raise ValueError('Shard build commit mismatch')
        if (shard / 'build-inputs.env').read_bytes() != inputs:
            raise ValueError('Shard resolved inputs mismatch')
        records.extend(shard_records)
    destination.mkdir(parents=True, exist_ok=True)
    for arch in arches:
        shard = shards / f'shard-{arch}-{run_suffix}'
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        for file in (name, name + '.sha256'):
            shutil.copyfile(shard / file, destination / file)
    for row in records:
        path = destination / row['file']
        if (path.stat().st_size != row['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']
                or (destination / (row['file'] + '.sha256')).read_text() != f"{row['sha256']}  {row['file']}\n"):
            raise ValueError('Copied shard differs from validated artifact')
    (destination / 'build-inputs.env').write_bytes(inputs)
    # Package parsing already happened at this job boundary. Authenticate the
    # copied compressed bytes above and reuse the independently checked facts.
    write_summary(destination.parent, version, records)
    if outcomes is not None:
        from matrix_contract import validate
        manifest_path = destination / "build-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(schema_version=2, requested_architectures=requested,
                        producer_run_id=int(os.environ["GITHUB_RUN_ID"]),
                        producer_run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                        producer_head_sha=os.environ['PRODUCER_HEAD_SHA'], outcomes=outcomes)
        validate(manifest, version)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        # The shard checks already covered the aggregate package bytes. Only these
        # matrix fields changed, and validate() checked their final contract.


if __name__ == '__main__':
    from matrix_contract import collect
    requested = architectures(sys.argv[4])
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    pages = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
        f'repos/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{run_id}/jobs?filter=all&per_page=100']))
    jobs = [job for page in pages for job in page['jobs']]
    if any(j.get('head_sha')!=os.environ['PRODUCER_HEAD_SHA'] for j in jobs):raise ValueError('Producer job head differs from workflow event')
    outcomes = collect(jobs, requested, run_id, attempt)
    for row in outcomes:
        if row['status'] == 'failure':
            print(f'::warning title=Architecture omitted::{sys.argv[3]} / {row["architecture"]}: {row["reason"]}; {row["job_url"]}')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            for row in outcomes:
                summary.write(f'- {sys.argv[3]} / {row["architecture"]}: {row["status"]}; {row["reason"]} {row["job_url"]}\n')
    aggregate(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], requested, sys.argv[5], outcomes)
