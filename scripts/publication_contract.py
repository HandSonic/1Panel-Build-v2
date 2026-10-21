#!/usr/bin/env python3
"""Validate publication payloads and bind the current CI handoff."""
import hashlib,json,os,re,subprocess,sys
from pathlib import Path
from release_asset_repair import digest

ROOT=Path(__file__).resolve().parents[1]
ARCHES=['amd64','arm64','armv7','ppc64le','s390x','loong64','riscv64']
REPOS={'upstream-matrix':'HandSonic/1Panel-Build-v2'}
PROOF='release-validation.json'
LINEAGE_KEYS=('requested_architectures','resolved_contract_sha256','current_generation','generations','artifact_generations')

def matrix_validate(manifest, version=None):
    if manifest.get('schema_version') == 3:
        from publication_lineage import validate
    else:
        from matrix_contract import validate
    return validate(manifest,version)



def contract_for_repo(repository):
    for name,repo in REPOS.items():
        if repository==repo:return name
    raise ValueError('Unsupported publication repository')


def policy_fingerprint(contract,version,root=ROOT):
    if contract=='upstream-matrix':
        from resolved_contract import runtime_contract, digest as contract_digest
        paths=['scripts/validate_artifacts.py','scripts/resolve_inputs.py','scripts/configure_release.py',
               'scripts/build_release.sh','Dockerfile','scripts/embedded_configuration.py',
               'scripts/discover_inputs.py','scripts/resolved_contract.py','scripts/select_node_toolchain.mjs',
               'scripts/lock_catalog.py','scripts/matrix_contract.py','scripts/semantic_configuration.py',
               'scripts/publication_contract.py','scripts/manual_publication.py',
               'scripts/release_asset_repair.py','scripts/aggregate_shards.py',
               'scripts/automatic_frontend_lock.py','scripts/automatic_frontend_lock.mjs',
               'scripts/installer_resources.py','scripts/service_layout.py','scripts/download_resources.py',
               'scripts/frontend_lock_origins.py','scripts/verify_bundled_archives.py','scripts/publication_lineage.py','scripts/automatic_publication.py']
        facts={p:digest(root/p)['sha256'] for p in paths}
        facts['resolved_contract_sha256']=contract_digest(runtime_contract(version,root))
        return hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()
    raise ValueError('Unknown publication contract')


def expected_names(contract,version,root=ROOT,accepted=None):
    if contract=='upstream-matrix':
        names={f'1panel-{version}-linux-{a}.tar.gz' for a in (ARCHES if accepted is None else accepted)}
        result=names|{name+'.sha256' for name in names}|{'checksums.txt','build-manifest.json','build-inputs.env'}
        result.add('resolved-source.json')
        from automatic_frontend_lock import required_sidecars
        from resolved_contract import runtime_contract
        result.update(required_sidecars(runtime_contract(version, root)))
        return result
    raise ValueError('Unknown publication contract')


def validate_payloads(directory,contract,version,root=ROOT):
    directory=Path(directory)
    if contract=='upstream-matrix':
        accepted=matrix_validate(json.loads((directory/'build-manifest.json').read_text()),version)
        if not accepted:raise ValueError('No successful architecture payloads')
        # Every accepted archive is independently checked.
        subprocess.run([sys.executable,str(root/'scripts/validate_artifacts.py'),str(directory),version,' '.join(accepted),'--require-embedded'],check=True)
        files=[p for p in directory.iterdir() if p.is_file()]
    else:raise ValueError('Unknown publication contract')
    if {p.name for p in files}!=expected_names(contract,version,root,accepted if contract=='upstream-matrix' else None) or len(files)!=len({p.name for p in files}):
        raise ValueError('Publication payload matrix mismatch')
    return sorted(files,key=lambda p:p.name)


def make_proof(files,contract,version,tag,repository,workflow_run_id,workflow_commit,root=ROOT):
    if repository!=REPOS[contract] or not (tag==version or tag.startswith(version+'-')):raise ValueError('Publication target/version mismatch')
    if not str(workflow_run_id).isdigit() or not re.fullmatch('[0-9a-f]{40}',workflow_commit):raise ValueError('Immutable workflow identity required')
    proof={'schema':2 if contract=='upstream-matrix' else 1,'contract':contract,'version':version,'release_tag':tag,'repository':repository,
            'policy_fingerprint':policy_fingerprint(contract,version,root),
            'workflow_run_id':int(workflow_run_id),'workflow_commit':workflow_commit,
            'workflow_run_attempt':int(os.environ['GITHUB_RUN_ATTEMPT']),
            'files':{p.name:digest(p) for p in files}}
    if contract=='upstream-matrix':
        manifest=json.loads(next(p for p in files if p.name=='build-manifest.json').read_text())
        proof['successful_architectures']=matrix_validate(manifest,version)
        proof['schema']=manifest['schema_version']
        keys=LINEAGE_KEYS if proof['schema']==3 else ('requested_architectures','producer_run_id','producer_run_attempt','producer_head_sha','outcomes')
        for key in keys:proof[key]=manifest[key]
    return proof
