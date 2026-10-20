#!/usr/bin/env python3
"""Publish new canonical releases and append verified missing branches without replacements."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from manual_publication import check_identity, github_json
from publication_contract import (PROOF, ROOT, digest_bytes, existing_release_state,
                                  make_proof, matrix_validate, validate_payloads)
from publication_lineage import compose
from release_asset_repair import (GitHub, digest, repair, release_snapshot as snapshot,
                                  Journal, asset_identity, optional_release,
                                  finalize_release, verify_new_release)


def draft_identity(release):
    author=release.get('author') or {}
    return {**snapshot(release),
            **{key:release.get(key) for key in ('name','target_commitish','prerelease')},
            'author':{key:author.get(key) for key in ('id','login','type')}}


def empty_automatic_draft(release,client,commit,*,allow_packages=False):
    """Authenticate owned draft metadata; nonempty recovery also validates payloads."""
    if (type(release.get('id')) is not int or release['id']<=0 or
        release.get('draft') is not True or release.get('tag_name')!=client.tag or
        release.get('name')!=client.tag or release.get('prerelease') is not ('-' in client.tag) or
        (release.get('body') or '')!='' or (not allow_packages and release.get('assets')!=[]) or
        draft_identity(release)['author']!={'id':41898282,'login':'github-actions[bot]','type':'Bot'}):
        raise ValueError('Only an empty automatic release draft can be resumed; recovery review required')
    target=release.get('target_commitish')
    if not isinstance(target,str) or not re.fullmatch('[0-9a-f]{40}',target):
        raise ValueError('Automatic draft target must be an immutable commit')
    if target!=commit:
        ancestry=json.loads(client.run('api',f'repos/{client.repo}/compare/{target}...{commit}?per_page=1'))
        if (ancestry.get('status') not in ('ahead','identical') or
            ancestry.get('base_commit',{}).get('sha')!=target or
            ancestry.get('merge_base_commit',{}).get('sha')!=target):
            raise ValueError('Automatic draft target is not an ancestor of this build')


def eligible(event, ref, default_branch, operation, candidate):
    if event=='schedule':return ref=='refs/heads/'+default_branch
    if event=='push':return ref.startswith('refs/tags/v')
    if event=='workflow_dispatch':return operation=='build' and candidate is False
    return False


def operation_allowed(action,event,ref,default_branch,mode,candidate):
    if action not in ('prepare','publish') or type(candidate) is not bool:return False
    if action=='prepare' and event=='workflow_dispatch' and mode=='validate-automatic' and not candidate:return True
    return eligible(event,ref,default_branch,mode,candidate)


def current_producer(manifest,commit):
    from matrix_contract import verify_current_jobs
    run_id=int(os.environ['GITHUB_RUN_ID']);attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])
    run=github_json(f'repos/{GitHubRepo}/actions/runs/{run_id}/attempts/{attempt}')
    jobs=[];page=1
    while True:
        result=github_json(f'repos/{GitHubRepo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
        jobs.extend(result['jobs'])
        if len(jobs)>=result['total_count']:break
        page+=1
        if page>10:raise ValueError('Excessive job inventory')
    verify_current_jobs(manifest,run,jobs,commit,run_id,attempt)


GitHubRepo='HandSonic/1Panel-Build-v2'


def prepare(args):
    contract=check_identity(args.version,args.version,args.repository)
    work=Path(args.work);source=Path(args.input)
    if work.exists():raise ValueError('Automatic preparation directory already exists')
    from resolved_contract import activate_release
    activate_release(source,args.version)
    files=validate_payloads(source,contract,args.version)
    current=json.loads((source/'build-manifest.json').read_text())
    current_producer(current,os.environ['GITHUB_SHA'])
    contract_sha=digest(source/'resolved-source.json')['sha256']
    client=GitHub(args.repository,args.version)
    before=optional_release(client)
    work.mkdir();control=work/'control';control.mkdir();destination=work/'release';destination.mkdir()
    for path in files:shutil.copyfile(path,destination/path.name)
    proof=make_proof(files,contract,args.version,args.version,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    (control/PROOF).write_text(json.dumps(proof,indent=2,sort_keys=True)+'\n')
    preserved={}
    operation='create'
    if before is not None and before.get('draft') is True:
        empty_automatic_draft(before,client,os.environ['GITHUB_SHA'],allow_packages=True)
        client.bind_release(before['id'])
        if before['assets']:
            from public_packages import verify_draft
            verified,_=verify_draft(client,args.version,ROOT,contract_sha)
            if draft_identity(verified)!=draft_identity(before):raise ValueError('Draft changed during verification')
            preserved={a['name']:asset_identity(a) for a in before['assets']}
        operation='resume'
    elif before is not None:
        client.bind_release(before['id'])
        from public_packages import verify_existing
        verified,records=verify_existing(client,args.version,ROOT)
        if snapshot(verified)!=snapshot(before):raise ValueError('Existing release changed during verification')
        if any(row['resolved_contract_sha256']!=contract_sha for row in records.values()):
            raise ValueError('Existing source/toolchain/lock contract requires explicit repair')
        if set(records)>=set(current['requested_architectures']):
            operation='unchanged'
            print('AUTOMATIC_PUBLICATION_UNCHANGED=1')
        else:
            if any(row.get('publication_format')!='package-only-v1' for row in records.values()):
                raise ValueError('Legacy partial release requires explicit package-format migration')
            operation='append'
        names={row['file']+suffix for row in records.values() for suffix in ('','.sha256')}
        preserved={a['name']:asset_identity(a) for a in before['assets'] if a['name'] in names}
    plan={'schema':1,'operation':operation,'version':args.version,
          'before':snapshot(before),'receipt_sha256':digest(control/PROOF)['sha256'],
          'preserved':preserved}
    if operation=='resume':plan['draft_identity']=draft_identity(before)
    (control/'automatic-plan.json').write_text(json.dumps(plan,indent=2,sort_keys=True)+'\n')


def publish(args,client=None):
    contract=check_identity(args.version,args.version,args.repository)
    work=Path(args.work);control=work/'control'
    plan=json.loads((control/'automatic-plan.json').read_text())
    if plan.get('schema')!=1 or plan.get('version')!=args.version or plan.get('operation') not in ('create','resume','append','unchanged'):
        raise ValueError('Invalid automatic publication plan')
    client=client or GitHub(args.repository,args.version)
    observed=optional_release(client)
    # A read-only preparation token may not see drafts. The serialized writer
    # can adopt the same owned state after ownership/ancestry checks; nonempty
    # payloads are independently validated below before any write. Keep the
    # downloaded plan unchanged on disk.
    if (plan['operation']=='create' and plan['before'] is None and
        observed is not None and observed.get('draft') is True):
        empty_automatic_draft(observed,client,os.environ['GITHUB_SHA'],allow_packages=True)
        plan={**plan,'operation':'resume','before':snapshot(observed),
              'draft_identity':draft_identity(observed),
              'preserved':{a['name']:asset_identity(a) for a in observed['assets']}}
    if snapshot(observed)!=plan['before']:raise ValueError('Release changed after validation; retry discovery under the writer lock')
    if plan['operation']=='resume':
        if observed is None or draft_identity(observed)!=plan.get('draft_identity'):
            raise ValueError('Automatic draft identity changed after validation')
        empty_automatic_draft(observed,client,os.environ['GITHUB_SHA'],allow_packages=True)
        client.bind_release(observed['id'])
    elif observed is not None:
        client.bind_release(observed['id'])
    if plan['operation']=='unchanged':return 'unchanged'
    proof_path=control/PROOF
    if digest(proof_path)['sha256']!=os.environ.get('EXPECTED_VALIDATION_RECEIPT_SHA256') or digest(proof_path)['sha256']!=plan['receipt_sha256']:
        raise ValueError('Automatic receipt differs from successful preparation')
    from resolved_contract import activate_release
    activate_release(work/'release',args.version)
    files=validate_payloads(work/'release',contract,args.version)
    proof=json.loads(proof_path.read_text())
    expected=make_proof(files,contract,args.version,args.version,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    if proof!=expected:raise ValueError('Automatic publication contract changed')
    manifest=json.loads((work/'release'/'build-manifest.json').read_text())
    current=manifest['generations'][manifest['current_generation']] if manifest['schema_version']==3 else manifest
    current_producer(current,os.environ['GITHUB_SHA'])
    # Existing successful architecture packages are never automatically replaced.
    from public_packages import package_files, recovery_assets, require_embedded, inventory, verify_draft
    require_embedded(files,args.version)
    published=package_files(files)
    actual={}
    if plan['operation']=='append':
        current_release=client.release()
        pairs=inventory(current_release,args.version)
        actual={asset['name']:asset for pair in pairs.values() for asset in pair}
        if {name:asset_identity(asset) for name,asset in plan['preserved'].items()}!={name:asset_identity(asset) for name,asset in actual.items()}:raise ValueError('Preserved package inventory changed')
        published=[path for path in published if path.name not in actual]
    elif plan['operation']=='resume' and observed['assets']:
        contract_sha=digest(work/'release'/'resolved-source.json')['sha256']
        verified,records=verify_draft(client,args.version,ROOT,contract_sha)
        if draft_identity(verified)!=plan['draft_identity']:raise ValueError('Draft changed during package verification')
        actual={asset['name']:asset for asset in verified['assets']}
        if {name:asset_identity(asset) for name,asset in actual.items()}!=plan['preserved']:
            raise ValueError('Prepared draft package inventory changed')
        # Retain authenticated original bytes even when rebuilt candidates differ.
        preserved_names={row['file']+suffix for row in records.values() for suffix in ('','.sha256')}
        published=[path for path in published if path.name not in preserved_names]
        for record in records.values():
            name=record['file']+'.sha256'
            if name not in actual:
                sidecars=control/'draft-sidecars';sidecars.mkdir(exist_ok=True)
                path=sidecars/name
                if path.exists():raise ValueError('Draft sidecar workspace already exists')
                path.write_text(record['sha256']+'  '+record['file']+'\n')
                published.append(path)
    journal=control/'automatic-journal.json'
    if journal.exists() or (control/'creation-journal.json').exists():raise ValueError('Existing automatic journal requires recovery review')
    expected_snapshot=plan['before']
    created=None
    if plan['operation'] in ('create','resume'):
        creation=Journal(control/'creation-journal.json',{'phase':'creating','tag':client.tag,'repository':client.repo})
        if plan['operation']=='create':
            created=json.loads(client.run('api','--method','POST',f'repos/{client.repo}/releases','-f',f'tag_name={client.tag}',
                       '-f',f'target_commitish={os.environ["GITHUB_SHA"]}','-f',f'name={client.tag}',
                       '-F','draft=true','-F',f'prerelease={str("-" in client.tag).lower()}'))
        else:
            client.bind_release(observed['id'])
            created=client.release()
            if draft_identity(created)!=plan['draft_identity']:
                raise ValueError('Automatic draft changed before recovery')
            creation.state.update(phase='resuming',release_id=created['id']);creation.save()
            # A fixed build may descend from the failed build. Publish its tag
            # against the actual verified build commit, not the older attempt.
            if created['target_commitish']!=os.environ['GITHUB_SHA']:
                wanted=draft_identity(created);wanted['target_commitish']=os.environ['GITHUB_SHA']
                created=json.loads(client.run('api','--method','PATCH',f'repos/{client.repo}/releases/{created["id"]}',
                                             '-f',f'target_commitish={os.environ["GITHUB_SHA"]}'))
                if draft_identity(created)!=wanted:raise ValueError('Resumed draft metadata changed')
        if (type(created.get('id')) is not int or created['id']<=0 or not created.get('draft') or
            created.get('tag_name')!=client.tag or (plan['operation']=='create' and created.get('assets')!=[])):
            raise ValueError('New draft identity is not empty and exact')
        creation.state.update(phase='created',release_id=created['id']);creation.save()
        client.bind_release(created['id'])
        if draft_identity(client.release())!=draft_identity(created):raise ValueError('Created draft differs from POST identity')
        expected_snapshot=snapshot(created)
    ordered=published
    recovery={}
    if plan['operation']=='append':
        current_release=client.release()
        recovery=recovery_assets(current_release,args.version)
        recovery.update(actual)
    elif plan['operation']=='resume':
        recovery=actual
    state=repair(client,ordered,journal,expected_snapshot=expected_snapshot,preserved_assets=recovery,expected_draft=created)
    if state['phase']!='complete':raise ValueError('Automatic upload did not complete')
    if plan['operation'] in ('create','resume'):
        current_producer(current,os.environ['GITHUB_SHA'])
        finalize_release(client,created,ordered,creation,preserved_assets=recovery)
    return 'published'


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['prepare','publish'])
    parser.add_argument('--repository',required=True);parser.add_argument('--version',required=True)
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--input',type=Path)
    args=parser.parse_args()
    event=os.environ.get('GITHUB_EVENT_NAME');operation=os.environ.get('PUBLICATION_OPERATION','build')
    candidate=os.environ.get('PUBLISH_CANDIDATE','false')
    if candidate not in ('true','false'):raise SystemExit('Invalid candidate selection')
    if not operation_allowed(args.operation,event,os.environ.get('GITHUB_REF',''),os.environ.get('DEFAULT_BRANCH','master'),operation,candidate=='true'):
        raise SystemExit('Canonical publication requires a normal schedule, tag or non-candidate build; validation never writes')
    if args.operation=='prepare':prepare(args)
    else:print(publish(args))
