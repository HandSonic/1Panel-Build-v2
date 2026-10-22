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


def aggregate(shards, destination, version, arches, run_suffix, outcomes=None, *, outcome_source=None):
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
        shard_records = validate_dist(shard, version, [arch], branch_result=outcome_source == 'package-step')
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
        if outcome_source is not None:
            manifest['outcome_source'] = outcome_source
        validate(manifest, version)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        # The shard checks already covered the aggregate package bytes. Only these
        # matrix fields changed, and validate() checked their final contract.


def github_json(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', endpoint], text=True, timeout=30))


def main(argv=None):
    from matrix_contract import BRANCH_RESULT, collect_recorded, read_jobs, retry_pending
    shards, destination, version, requested, generation = sys.argv[1:] if argv is None else argv
    requested = architectures(requested)
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    if os.environ['MATRIX_RESULT'] != 'success':
        raise ValueError('Architecture infrastructure, result recording or upload failed; refusing partial infrastructure success')
    root = Path(shards)
    expected = {f'shard-{arch}-{generation}' for arch in requested}
    if {p.name for p in root.iterdir()} != expected:
        raise ValueError('Missing, extra or unexpected architecture result shards')
    results = {}
    for arch in requested:
        shard = root / f'shard-{arch}-{generation}'
        path = shard / BRANCH_RESULT
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError('Missing or oversized architecture result: ' + arch)
        results[arch] = json.loads(path.read_text())
        if not isinstance(results[arch], dict):
            raise ValueError('Invalid architecture result: ' + arch)
        if results[arch].get('status') == 'failure' and {p.name for p in shard.iterdir()} != {BRANCH_RESULT}:
            raise ValueError('Failed architecture shard contains package files: ' + arch)
    def snapshot():
        jobs = read_jobs(github_json, os.environ['GITHUB_REPOSITORY'], run_id)
        return collect_recorded(jobs, results, requested, run_id, attempt,
                                os.environ['PRODUCER_HEAD_SHA'], os.environ['BUILD_REPOSITORY_COMMIT'],
                                generation, version)
    # needs.compile proves all infrastructure/report/upload steps passed.
    # Package failures are explicit artifact records. REST supplies identities,
    # never a second scheduling barrier or a live completion verdict.
    outcomes = retry_pending(snapshot)
    for row in outcomes:
        if row['status'] == 'failure':
            print(f'::warning title=Architecture omitted::{version} / {row["architecture"]}: {row["reason"]}; {row["job_url"]}')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            for row in outcomes:
                summary.write(f'- {version} / {row["architecture"]}: {row["status"]}; {row["reason"]} {row["job_url"]}\n')
    aggregate(root, Path(destination), version, requested, generation, outcomes, outcome_source='package-step')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('complete=' + str(all(row['status'] == 'success' for row in outcomes)).lower() + '\n')


if __name__ == '__main__':
    main()
