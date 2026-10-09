#!/usr/bin/env python3
"""Manual preparation and recoverable publication of validated architecture outputs."""
import argparse,hashlib,json,os,re,shutil,stat,subprocess,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath
from publication_contract import ARCHES,PROOF,REPOS,ROOT,contract_for_repo,make_proof,policy_fingerprint,validate_payloads
from release_asset_repair import GitHub,digest,repair

UPSTREAM=REPOS['upstream-matrix']

def github_json(endpoint):
    result=subprocess.run(['gh','api',endpoint],check=True,capture_output=True,text=True)
    return json.loads(result.stdout)


def check_identity(version,tag,repository):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?',version):raise ValueError('Invalid application version')
    if tag!=version:raise ValueError('Existing-release repair tag must equal the application version')
    return contract_for_repo(repository)


def extract_verified_zip(path,destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('Refusing to overwrite an existing input directory')
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        if len(names)!=len(set(names)) or sum(i.file_size for i in infos)>2*1024**3:raise ValueError('Duplicate or oversized CI ZIP')
        for info in infos:
            name=PurePosixPath(info.filename);kind=(info.external_attr>>16)&0o170000
            if name.is_absolute() or '..' in name.parts or '\\' in info.filename or len(name.parts)!=1 or info.filename!=name.name or kind not in (0,stat.S_IFREG):
                raise ValueError('Only flat regular CI artifact files are accepted')
        if archive.testzip() is not None:raise ValueError('CI ZIP CRC mismatch')
        destination.mkdir(parents=True)
        for info in infos:
            with archive.open(info) as src,(destination/info.filename).open('wb') as dst:shutil.copyfileobj(src,dst)


def validate_upstream_input(directory,version,expected_commit,contract):
    records=json.loads((Path(directory)/'build-manifest.json').read_text())['artifacts']
    if not records or any(r['build_repository_commit']!=expected_commit for r in records):raise ValueError('CI artifact built-commit mismatch')
    if contract=='upstream-matrix':
        from resolved_contract import activate_release
        activate_release(directory,version)
    validator_root=ROOT
    from matrix_contract import validate
    accepted=validate(json.loads((Path(directory)/'build-manifest.json').read_text()),version)
    subprocess.run([sys.executable,str(validator_root/'scripts/validate_artifacts.py'),str(directory),version,' '.join(accepted)],check=True)
    records=json.loads((Path(directory)/'build-manifest.json').read_text())['artifacts']
    if not records or any(r['build_repository_commit']!=expected_commit for r in records):raise ValueError('CI artifact built-commit mismatch')
    from embedded_configuration import validate_archive
    for row in records:validate_archive(Path(directory)/row['file'],version,row['architecture'])


def fetch_ci_bundle(directory,version,run_id,artifact_id,expected_sha,expected_commit,contract):
    if not str(run_id).isdigit() or not str(artifact_id).isdigit() or not re.fullmatch('[0-9a-f]{64}',expected_sha) or not re.fullmatch('[0-9a-f]{40}',expected_commit):
        raise ValueError('Exact run/artifact IDs, ZIP hash and built commit are required')
    run=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}')
    if run.get('status')!='completed' or run.get('conclusion') not in ('success','failure'):raise ValueError('Selected upstream workflow is cancelled or nonterminal')
    if run.get('path','').split('@')[0]!='.github/workflows/build.yml':raise ValueError('Unexpected upstream workflow')
    metadata=github_json(f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}')
    if metadata.get('expired') or metadata.get('workflow_run',{}).get('id')!=int(run_id):raise ValueError('Artifact/run mismatch or expired artifact')
    if metadata.get('name')!=f'verified-1panel-{version}-{expected_commit}' or metadata.get('digest')!='sha256:'+expected_sha:
        raise ValueError('Selected artifact identity/digest mismatch')
    with tempfile.TemporaryDirectory(dir=Path(directory).parent) as temp:
        archive=Path(temp)/'upstream.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh','api',f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}/zip'],stdout=stream,check=True)
        if digest(archive)!= {'bytes':metadata['size_in_bytes'],'sha256':expected_sha}:raise ValueError('Downloaded CI ZIP differs from GitHub metadata')
        extract_verified_zip(archive,directory)
    manifest=json.loads((Path(directory)/'build-manifest.json').read_text())
    from matrix_contract import verify_jobs
    attempt=manifest.get('producer_run_attempt')
    if type(attempt) is not int or attempt<=0:raise ValueError('Invalid producer attempt')
    jobs=[];page=1
    while True:
        data=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
        jobs.extend(data['jobs'])
        if len(jobs)>=data['total_count']:break
        page+=1
        if page>10:raise ValueError('Excessive producer job inventory')
    run=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}/attempts/{attempt}')
    execution=github_json(f'repos/{UPSTREAM}/git/commits/{expected_commit}') if run.get('event')=='pull_request' else None
    verify_jobs(manifest,run,jobs,expected_commit,execution)
    validate_upstream_input(directory,version,expected_commit,contract)
    return {'repository':UPSTREAM,'run_id':int(run_id),'artifact_id':int(artifact_id),'artifact_sha256':expected_sha,
            'build_repository_commit':expected_commit,'run_url':f'https://github.com/{UPSTREAM}/actions/runs/{run_id}'}


def prepare(args):
    contract=check_identity(args.version,args.tag,args.repository)
    work=Path(args.work)
    if work.exists() and any(work.iterdir()):raise ValueError('Preparation work directory must be empty')
    work.mkdir(parents=True,exist_ok=True)
    control=work/'control';control.mkdir();source=None;provenance=None
    if args.upstream_source=='verified-ci':
        source=work/'upstream-input'
        provenance=fetch_ci_bundle(source,args.version,args.run_id,args.artifact_id,args.artifact_sha256,args.build_commit,contract)
    elif contract=='upstream-matrix':raise ValueError('Upstream promotion requires a verified CI input, never old release bytes')
    destination=work/'release'
    shutil.move(source,destination)
    files=validate_payloads(destination,contract,args.version)
    proof=make_proof(files,contract,args.version,args.tag,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    proof['upstream_input']=provenance or {'source_kind':'verified-public-release'}
    (control/PROOF).write_text(json.dumps(proof,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'contract':contract,'version':args.version,'package_count':sum(p.name.endswith('.tar.gz') for p in files),'publication_file_count':len(files)+1,'state':'validated_no_release_writes'}))


def publish(args):
    contract=check_identity(args.version,args.tag,args.repository)
    work=Path(args.work)
    if contract=='upstream-matrix':
        from resolved_contract import activate_release
        activate_release(work/'release',args.version)
    files=validate_payloads(work/'release',contract,args.version)
    proof_path=work/'control'/PROOF
    if digest(proof_path)['sha256']!=os.environ.get('EXPECTED_VALIDATION_RECEIPT_SHA256'):
        raise ValueError('Downloaded validation receipt differs from the read-only preparation job')
    proof=json.loads(proof_path.read_text())
    expected=make_proof(files,contract,args.version,args.tag,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    if any(proof.get(k)!=v for k,v in expected.items()):raise ValueError('Prepared publication receipt changed')
    client=GitHub(args.repository,args.tag)
    release=client.release()
    if release.get('draft'):raise ValueError('Existing-release repair requires an existing public release')
    # Recovery metadata switches before checksums; the public checksum file is last.
    files=[p for p in files if p.name!='checksums.txt']+[proof_path]+[p for p in files if p.name=='checksums.txt']
    journal=work/'control'/'repair-journal.json'
    if journal.exists():raise ValueError('Review the existing journal before retrying')
    retired=[]
    if contract=='upstream-matrix':
        for row in proof['outcomes']:
            if row['status']=='failure':
                name=f'1panel-{args.version}-linux-{row["architecture"]}.tar.gz'
                retired.extend([name,name+'.sha256'])
    state=repair(client,files,journal,retire=retired)
    print(json.dumps({'state':state['phase'],'repository':args.repository,'tag':args.tag}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['prepare','publish'])
    parser.add_argument('--repository',required=True);parser.add_argument('--version',required=True);parser.add_argument('--tag',required=True)
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--upstream-source',choices=['release','verified-ci'],default='verified-ci')
    parser.add_argument('--run-id',default='');parser.add_argument('--artifact-id',default='');parser.add_argument('--artifact-sha256',default='');parser.add_argument('--build-commit',default='')
    args=parser.parse_args()
    if args.operation=='prepare':prepare(args)
    else:
        if os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or os.environ.get('PUBLICATION_OPERATION')!={'upstream-matrix':'promote-existing'}[contract_for_repo(args.repository)]:
            raise SystemExit('Publication is permitted only by the explicitly selected manual promotion/repair mode')
        publish(args)
