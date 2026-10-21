#!/usr/bin/env python3
"""Publish new canonical releases and append verified missing branches without replacements."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
from manual_publication import check_identity, github_json
from publication_contract import PROOF, make_proof, validate_payloads
from release_asset_repair import (GitHub, digest, repair, release_snapshot as snapshot,
                                  Journal, optional_release, require_owned_draft,
                                  finalize_release)


def draft_identity(release):
    author=release.get('author') or {}
    return {**snapshot(release),
            **{key:release.get(key) for key in ('name','target_commitish','prerelease')},
            'author':{key:author.get(key) for key in ('id','login','type')}}


def check_automatic_draft(release,client,commit):
    """Resume exact verified bytes; only an empty draft may change its target."""
    require_owned_draft(release,client)
    target=release.get('target_commitish')
    if not isinstance(target,str) or not re.fullmatch('[0-9a-f]{40}',target):
        raise ValueError('Automatic draft target must be an immutable commit')
    if target!=commit:
        if release.get('assets'):
            raise ValueError('Nonempty automatic draft requires this exact build commit')
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
    from matrix_contract import verify_current_jobs, verify_jobs
    run_id=int(os.environ['GITHUB_RUN_ID']);current_attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])
    attempt=manifest.get('producer_run_attempt')
    if type(attempt) is not int or not 0 < attempt <= current_attempt:
        raise ValueError('Invalid current producer attempt')
    run=github_json(f'repos/{GitHubRepo}/actions/runs/{run_id}/attempts/{attempt}')
    if run.get('event') not in ('push','schedule','workflow_dispatch'):
        raise ValueError('Automatic publication requires a trusted non-PR execution')
    jobs=[];page=1
    while True:
        result=github_json(f'repos/{GitHubRepo}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}')
        jobs.extend(result['jobs'])
        if len(jobs)>=result['total_count']:break
        page+=1
        if page>10:raise ValueError('Excessive job inventory')
    if attempt==current_attempt:
        verify_current_jobs(manifest,run,jobs,commit,run_id,attempt)
    else:
        verify_jobs(manifest,run,jobs,commit,allow_publication_failures=True)


GitHubRepo='HandSonic/1Panel-Build-v2'


def prepare(args):
    contract=check_identity(args.version,args.version,args.repository)
    work=Path(args.work);source=Path(args.input)
    if work.exists():raise ValueError('Automatic preparation directory already exists')
    from resolved_contract import activate_release
    activate_release(source,args.version)
    files=validate_payloads(source,contract,args.version)
    work.mkdir();control=work/'control';control.mkdir();destination=work/'release';destination.mkdir()
    for path in files:shutil.copyfile(path,destination/path.name)
    proof=make_proof(files,contract,args.version,args.version,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    (control/PROOF).write_text(json.dumps(proof,indent=2,sort_keys=True)+'\n')


def publish(args,client=None):
    contract=check_identity(args.version,args.version,args.repository)
    work=Path(args.work);control=work/'control'
    proof_path=control/PROOF
    if digest(proof_path)['sha256']!=os.environ.get('EXPECTED_VALIDATION_RECEIPT_SHA256'):
        raise ValueError('Automatic receipt differs from successful preparation')
    from resolved_contract import activate_release
    activate_release(work/'release',args.version)
    # Preparation validated these bytes; authenticate the transfer without
    # unpacking the same archives again.
    files=sorted((work/'release').iterdir(),key=lambda path:path.name)
    proof=json.loads(proof_path.read_text())
    expected=make_proof(files,contract,args.version,args.version,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],
                        workflow_run_attempt=proof.get('workflow_run_attempt'))
    if proof!=expected:raise ValueError('Automatic publication contract changed')
    manifest=json.loads((work/'release'/'build-manifest.json').read_text())
    current=manifest['generations'][manifest['current_generation']] if manifest['schema_version']==3 else manifest
    current_producer(current,os.environ['GITHUB_SHA'])
    # Discover the current remote state inside the serialized writer. Earlier
    # writers or a partial upload may have completed since preparation.
    client=client or GitHub(args.repository,args.version)
    before=optional_release(client)
    if before is not None:client.bind_release(before['id'])
    from public_packages import package_files, metadata_inventory
    published=package_files(files)
    preserved={}
    expected_snapshot=snapshot(before)
    if before is not None and before.get('draft') is False:
        pairs,actual=metadata_inventory(before,args.version)
        preserved={asset['name']:asset for asset in before['assets']}
        complete_names={asset['name'] for pair in pairs.values() for asset in pair}
        derived={}
        for path in published:
            if path.name in actual and path.name not in complete_names:
                old=actual[path.name]
                if path.name.endswith('.tar.gz'):
                    old_sha=old.get('digest')
                    if not isinstance(old_sha,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',old_sha):
                        raise ValueError('Existing archive has no valid server SHA-256; explicit repair required')
                    directory=control/'public-checksums';directory.mkdir(exist_ok=True)
                    sidecar=directory/(path.name+'.sha256')
                    sidecar.write_text(old_sha[7:]+'  '+path.name+'\n')
                    derived[sidecar.name]=sidecar
                else:
                    facts=digest(path)
                    if old.get('size')!=facts['bytes'] or old.get('digest')!='sha256:'+facts['sha256']:
                        raise ValueError('Incomplete public pair differs from this build; explicit repair required')
        published=[derived.get(path.name,path) for path in published if path.name not in actual]
        if not published:return 'unchanged'
    elif before is not None:
        check_automatic_draft(before,client,os.environ['GITHUB_SHA'])
    journal=control/'automatic-journal.json'
    if journal.exists() or (control/'creation-journal.json').exists():raise ValueError('Existing automatic journal requires recovery review')
    created=None
    if before is None or before.get('draft') is True:
        creation=Journal(control/'creation-journal.json',{'phase':'creating' if before is None else 'resuming',
                         'tag':client.tag,'repository':client.repo})
        if before is None:
            created=json.loads(client.run('api','--method','POST',f'repos/{client.repo}/releases','-f',f'tag_name={client.tag}',
                       '-f',f'target_commitish={os.environ["GITHUB_SHA"]}','-f',f'name={client.tag}',
                       '-F','draft=true','-F',f'prerelease={str("-" in client.tag).lower()}'))
            require_owned_draft(created,client)
            if created.get('assets')!=[] or created.get('target_commitish')!=os.environ['GITHUB_SHA']:
                raise ValueError('New draft identity is not empty and exact')
        else:
            created=client.release()
            if draft_identity(created)!=draft_identity(before):
                raise ValueError('Automatic draft changed before recovery')
            creation.state.update(release_id=created['id']);creation.save()
            if created['target_commitish']!=os.environ['GITHUB_SHA']:
                wanted=draft_identity(created);wanted['target_commitish']=os.environ['GITHUB_SHA']
                created=json.loads(client.run('api','--method','PATCH',f'repos/{client.repo}/releases/{created["id"]}',
                                             '-f',f'target_commitish={os.environ["GITHUB_SHA"]}',
                                             '-f',f'tag_name={client.tag}'))
                if draft_identity(created)!=wanted:raise ValueError('Resumed draft metadata changed')
        creation.state.update(phase='created',release_id=created['id']);creation.save()
        client.bind_release(created['id'])
        if draft_identity(client.release())!=draft_identity(created):raise ValueError('Created draft differs from bound identity')
        expected_snapshot=snapshot(created)
    state=repair(client,published,journal,expected_snapshot=expected_snapshot,preserved_assets=preserved,expected_draft=created)
    if state['phase']!='complete':raise ValueError('Automatic upload did not complete')
    if created is not None:
        current_producer(current,os.environ['GITHUB_SHA'])
        finalize_release(client,created,published,creation,fallback_assets=state.get('verified_legacy_assets'))
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
