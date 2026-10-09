#!/usr/bin/env python3
"""Independent architecture publication contracts and remote receipts."""
import hashlib,json,os,re,subprocess,sys,tempfile
from pathlib import Path
from release_asset_repair import digest

ROOT=Path(__file__).resolve().parents[1]
ARCHES=['amd64','arm64','armv7','ppc64le','s390x','loong64','riscv64']
REPOS={'upstream-matrix':'HandSonic/1Panel-Build-v2'}
PROOF='release-validation.json'


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
               'scripts/release_asset_repair.py','scripts/aggregate_shards.py']
        facts={p:digest(root/p)['sha256'] for p in paths}
        facts['resolved_contract_sha256']=contract_digest(runtime_contract(version,root))
        return hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()
    raise ValueError('Unknown publication contract')


def expected_names(contract,version,root=ROOT,accepted=None):
    if contract=='upstream-matrix':
        names={f'1panel-{version}-linux-{a}.tar.gz' for a in (ARCHES if accepted is None else accepted)}
        result=names|{name+'.sha256' for name in names}|{'checksums.txt','build-manifest.json','build-inputs.env'}
        result.add('resolved-source.json')
        return result
    raise ValueError('Unknown publication contract')


def validate_payloads(directory,contract,version,root=ROOT):
    directory=Path(directory)
    if contract=='upstream-matrix':
        from matrix_contract import validate
        accepted=validate(json.loads((directory/'build-manifest.json').read_text()),version)
        if not accepted:raise ValueError('No successful architecture payloads')
        # Every accepted archive is independently checked.
        subprocess.run([sys.executable,str(root/'scripts/validate_artifacts.py'),str(directory),version,' '.join(accepted)],check=True)
        from embedded_configuration import validate_archive
        for arch in accepted:validate_archive(directory/f'1panel-{version}-linux-{arch}.tar.gz',version,arch,root)
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
        from matrix_contract import validate
        manifest=json.loads(next(p for p in files if p.name=='build-manifest.json').read_text())
        proof['successful_architectures']=validate(manifest,version)
        for key in ('requested_architectures','producer_run_id','producer_run_attempt','producer_head_sha','outcomes'):
            proof[key]=manifest[key]
    return proof


def verify_receipt(proof,checksums,assets,run,contract,version,tag,repository,root=ROOT):
    if type(proof.get('workflow_run_attempt')) is not int or proof['workflow_run_attempt']<=0:raise ValueError('Invalid validation attempt identity')
    if contract=='upstream-matrix':
        from matrix_contract import validate
        synthetic={'schema_version':2,'version':version,'artifacts':[{'architecture':a} for a in proof.get('successful_architectures',[])]}
        for key in ('requested_architectures','producer_run_id','producer_run_attempt','producer_head_sha','outcomes'):synthetic[key]=proof.get(key)
        if not validate(synthetic,version):raise ValueError('Empty successful publication inventory')
    expected=expected_names(contract,version,root,proof.get('successful_architectures') if contract=='upstream-matrix' else None)
    identity={'schema':2 if contract=='upstream-matrix' else 1,'contract':contract,'version':version,'release_tag':tag,'repository':repository,
              'policy_fingerprint':policy_fingerprint(contract,version,root)}
    if any(proof.get(k)!=v for k,v in identity.items()):raise ValueError('Repair-needed: validation receipt identity is missing or stale')
    if set(proof.get('files',{}))!=expected:raise ValueError('Repair-needed: receipt matrix mismatch')
    if run.get('id')!=proof.get('workflow_run_id') or run.get('run_attempt')!=proof.get('workflow_run_attempt') or run.get('head_sha')!=proof.get('workflow_commit') or run.get('status')!='completed' or run.get('conclusion')!='success':
        raise ValueError('Repair-needed: receipt workflow is not a completed successful exact commit')
    allowed={'.github/workflows/build.yml'}
    if run.get('path','').split('@')[0] not in allowed:raise ValueError('Repair-needed: unrecognized receipt workflow')
    by_name={a['name']:a for a in assets}
    if len(by_name)!=len(assets):raise ValueError('Repair-needed: duplicate remote asset names')
    canonical=expected|{PROOF}
    if not canonical<=set(by_name):raise ValueError('Repair-needed: incomplete canonical asset matrix')
    for name in set(by_name)-canonical:
        # Known noncanonical recovery names are retained deliberately, never deleted.
        recoverable=canonical
        if contract=='upstream-matrix':recoverable=canonical|expected_names(contract,version,root,proof['requested_architectures'])
        if not any(re.fullmatch(re.escape(base)+r'\.(backup|staged)-[0-9a-f]{12}',name) for base in recoverable):
            raise ValueError('Repair-needed: unexpected remote canonical asset')
    for name,facts in proof['files'].items():
        a=by_name[name]
        if a.get('digest')!='sha256:'+facts['sha256'] or a.get('size')!=facts['bytes'] or facts['bytes']<=0:
            raise ValueError(f'Repair-needed: remote digest/size mismatch: {name}')
    if digest_bytes(checksums)!=proof['files']['checksums.txt']:raise ValueError('Repair-needed: checksum file content mismatch')
    sums={}
    for line in checksums.decode().splitlines():
        match=re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)',line)
        if not match or match[2] in sums:raise ValueError('Repair-needed: malformed flat checksums')
        sums[match[2]]=match[1]
    archives={n for n in expected if n.endswith('.tar.gz')}
    if set(sums)!=archives or any(sums[n]!=proof['files'][n]['sha256'] for n in archives):raise ValueError('Repair-needed: checksum matrix mismatch')
    return True


def digest_bytes(data):return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def verify_validation_log(client,proof,receipt_sha):
    run_id=proof.get('workflow_run_id')
    if type(run_id) is not int or run_id<=0 or not re.fullmatch('[0-9a-f]{40}',proof.get('workflow_commit','')):
        raise ValueError('Repair-needed: invalid workflow identity')
    if type(proof.get('workflow_run_attempt')) is not int or proof['workflow_run_attempt']<=0:raise ValueError('Invalid validation attempt identity')
    jobs=json.loads(client.run('api',f'repos/{client.repo}/actions/runs/{run_id}/attempts/{proof["workflow_run_attempt"]}/jobs?per_page=100'))['jobs']
    for job in jobs:
        if job.get('name') not in ['build','publication_prepare'] or job.get('conclusion')!='success':continue
        if type(job.get('id')) is not int or job.get('run_id')!=run_id or job.get('run_attempt')!=proof['workflow_run_attempt'] or job.get('head_sha')!=proof['workflow_commit']:continue
        logs=client.run('api',f'repos/{client.repo}/actions/jobs/{job['id']}/logs')
        hashes=re.findall(r'VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})',logs)
        if receipt_sha in hashes:return True
    raise ValueError('Repair-needed: published receipt is not bound to a successful validation job')

def existing_release_state(client,contract,version,tag,root=ROOT):
    release=client.release()
    if release.get('draft'):return 'draft'
    with tempfile.TemporaryDirectory() as temporary:
        directory=Path(temporary)
        client.download(PROOF,directory);client.download('checksums.txt',directory)
        proof_bytes=(directory/PROOF).read_bytes();proof=json.loads(proof_bytes)
        assets=release['assets'];asset=next(a for a in assets if a['name']==PROOF)
        if asset.get('digest')!='sha256:'+hashlib.sha256(proof_bytes).hexdigest() or asset['size']!=len(proof_bytes):raise ValueError('Repair-needed: receipt byte mismatch')
        verify_validation_log(client,proof,hashlib.sha256(proof_bytes).hexdigest())
        run=json.loads(client.run('api',f'repos/{client.repo}/actions/runs/{proof["workflow_run_id"]}/attempts/{proof["workflow_run_attempt"]}'))
        if run.get('event') in ['pull_request','pull_request_target']:
            raise ValueError('Repair-needed: read-only PR tests are not package publication validation')
        verification_root=root
        if contract=='upstream-matrix' and 'resolved-source.json' in proof.get('files',{}):
            from resolved_contract import apply_contract
            client.download('resolved-source.json',directory)
            supplied=directory/'resolved-source.json'
            if digest(supplied)!=proof['files']['resolved-source.json']:raise ValueError('Repair-needed: source contract differs from published receipt')
            import shutil
            verification_root=directory/'policy'
            verification_root.mkdir()
            shutil.copytree(root/'config',verification_root/'config')
            shutil.copytree(root/'scripts',verification_root/'scripts')
            shutil.copyfile(root/'Dockerfile',verification_root/'Dockerfile')
            apply_contract(supplied,proof['files']['resolved-source.json']['sha256'],verification_root,version)
            client.download('build-manifest.json',directory)
            manifest_path=directory/'build-manifest.json'
            if digest(manifest_path)!=proof['files']['build-manifest.json']:raise ValueError('Published matrix differs from receipt')
            manifest=json.loads(manifest_path.read_text())
            from matrix_contract import verify_jobs,validate
            if validate(manifest,version)!=proof.get('successful_architectures'):raise ValueError('Published accepted inventory differs from receipt')
            for key in ('requested_architectures','producer_run_id','producer_run_attempt','producer_head_sha','outcomes'):
                if manifest[key]!=proof.get(key):raise ValueError('Published matrix identity differs from receipt')
            producer=manifest['producer_run_id'];attempt=manifest['producer_run_attempt']
            producer_run=json.loads(client.run('api',f'repos/{client.repo}/actions/runs/{producer}/attempts/{attempt}'))
            pages=json.loads(client.run('api','--paginate','--slurp',f'repos/{client.repo}/actions/runs/{producer}/attempts/{attempt}/jobs?per_page=100'))
            commits={r['build_repository_commit'] for r in manifest['artifacts']}
            if len(commits)!=1:raise ValueError('Mixed producer commits')
            built_commit=commits.pop()
            execution=json.loads(client.run('api',f'repos/{client.repo}/git/commits/{built_commit}')) if producer_run.get('event')=='pull_request' else None
            verify_jobs(manifest,producer_run,[j for page in pages for j in page['jobs']],built_commit,execution)
        verify_receipt(proof,(directory/'checksums.txt').read_bytes(),assets,run,contract,version,tag,client.repo,verification_root)
    return 'verified'
