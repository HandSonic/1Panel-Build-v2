#!/usr/bin/env python3
"""Keep verified candidate packages on the exact newly created draft ID."""
import argparse
import json
import os
from pathlib import Path
import re

from manual_publication import check_identity
from publication_contract import matrix_validate
from public_packages import package_files
from release_asset_repair import (GitHub,Journal,optional_release,repair,
                                  release_snapshot,verify_new_release)

NOTES='Build candidate only. Included artifacts validated; pending promotion and downstream regression.'


def publish(args,client=None):
    check_identity(args.version,args.version,args.repository)
    run_id=os.environ.get('GITHUB_RUN_ID','')
    if (not re.fullmatch('[0-9a-f]{40}',args.target) or
        not run_id.isdigit() or int(run_id)<=0 or
        args.tag!=f'{args.version}-build-{args.target[:12]}-{run_id}'):
        raise ValueError('Candidate requires its exact build commit and run tag')
    source=Path(args.input);control=Path(args.control)
    manifest=json.loads((source/'build-manifest.json').read_text())
    arches=matrix_validate(manifest,args.version)
    expected={f'1panel-{args.version}-linux-{arch}.tar.gz'+suffix
              for arch in arches for suffix in ('','.sha256')}
    files=package_files(sorted(source.iterdir()))
    if not expected or {path.name for path in files}!=expected or any(not path.is_file() for path in files):
        raise ValueError('Candidate package matrix differs from the verified build')
    if control.exists():raise ValueError('Candidate control directory already exists; recovery review required')
    client=client or GitHub(args.repository,args.tag)
    if optional_release(client) is not None:
        raise ValueError('Candidate already exists; refusing overwrite.')
    control.mkdir(parents=True)
    creation=Journal(control/'creation-journal.json',{'phase':'creating','repository':args.repository,
                       'tag':args.tag,'target_commitish':args.target,'run_id':int(run_id)})
    try:
        created=json.loads(client.run('api','--method','POST',f'repos/{client.repo}/releases',
            '-f',f'tag_name={args.tag}','-f',f'target_commitish={args.target}',
            '-f',f'name={args.tag}','-f',f'body={NOTES}','-F','draft=true','-F','prerelease=false'))
        if type(created.get('id')) is int and created['id']>0:
            creation.state['release_id']=created['id'];creation.save()
        if (type(created.get('id')) is not int or created['id']<=0 or
            created.get('tag_name')!=args.tag or created.get('name')!=args.tag or
            created.get('target_commitish')!=args.target or created.get('draft') is not True or
            created.get('prerelease') is not False or created.get('body')!=NOTES or created.get('assets')!=[]):
            raise ValueError('Created candidate draft identity differs from the request')
        client.bind_release(created['id'])
        verify_new_release(client.release(),created,[],True)
        creation.state['phase']='created';creation.save()
        state=repair(client,files,control/'repair-journal.json',
                     expected_snapshot=release_snapshot(created),expected_draft=created)
        if state['phase']!='complete':raise ValueError('Candidate upload did not complete')
        verify_new_release(client.release(),created,files,True)
        creation.state['phase']='complete';creation.save()
    except Exception as error:
        creation.state.update(phase='incomplete',failure=str(error));creation.save()
        raise
    return {'state':'verified_draft','tag':args.tag,'release_id':created['id']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('repository','version','tag','target','input','control'):
        parser.add_argument('--'+name,required=True)
    print(json.dumps(publish(parser.parse_args())))
