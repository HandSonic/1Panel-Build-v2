#!/usr/bin/env python3
import hashlib,json,os,pathlib,re,shutil,sys,tarfile,tempfile
from resolve_inputs import resolve
from validate_artifacts import architectures,validate_dist,validate_elf

def package(root,version,arch):
    entry=resolve(version); commit=os.environ['BUILD_REPOSITORY_COMMIT']
    if not re.fullmatch('[0-9a-f]{40}',commit): raise ValueError('Build repository commit must be immutable')
    name=f'1panel-{version}-linux-{arch}'; dist=root/'dist'; dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        stage=pathlib.Path(tmp)/name; stage.mkdir()
        for rel in entry['installer_sha256']:
            target=stage/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(root/rel,target)
        for name2 in ('GeoIP.mmdb','1panel-core.service','1panel-agent.service'): shutil.copy2(root/name2,stage/name2)
        for binary in ('1panel-core','1panel-agent'):
            source=root/'build'/binary; validate_elf(source.read_bytes(),arch); shutil.copy2(source,stage/binary)
        for optional in ('LICENSE','README.md'):
            if (root/optional).is_file(): shutil.copy2(root/optional,stage/optional)
        manifest={'schema_version':1,'version':version,'architecture':arch,'edition':'community','build_repository_commit':commit}
        for key in ('source_commit','installer_commit','mode','go_version','node_version','npm_version'):manifest[key]=entry[key]
        if entry.get('resolved_contract_sha256'): manifest['resolved_contract_sha256']=entry['resolved_contract_sha256']
        manifest['files']={str(p.relative_to(stage)):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size} for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        archive=dist/(name+'.tar.gz')
        with tarfile.open(str(archive)+'.tmp','w:gz') as tf:tf.add(stage,arcname=name)
        os.replace(str(archive)+'.tmp',archive)
        (dist/(archive.name+'.sha256')).write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')

def finalize(root,version,arches):
    dist=root/'dist'; records=[]
    for arch in arches:
        file=f'1panel-{version}-linux-{arch}.tar.gz'; p=dist/file
        entry=resolve(version)
        records.append({'architecture':arch,'file':file,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size,'source_commit':entry['source_commit'],'installer_commit':entry['installer_commit'],'build_repository_commit':os.environ['BUILD_REPOSITORY_COMMIT']})
    entry=resolve(version)
    if entry.get('resolved_contract_sha256'):
        for row in records: row['resolved_contract_sha256']=entry['resolved_contract_sha256']
        shutil.copyfile(pathlib.Path(__file__).resolve().parents[1]/'config/resolved-source.json',dist/'resolved-source.json')
    (dist/'checksums.txt').write_text(''.join((dist/(r['file']+'.sha256')).read_text() for r in records))
    (dist/'build-manifest.json').write_text(json.dumps({'schema_version':1,'version':version,'artifacts':records},indent=2)+'\n')
    validate_dist(dist,version,arches)
if __name__=='__main__':
    root=pathlib.Path(sys.argv[2]); version=sys.argv[3]
    if sys.argv[1]=='package':package(root,version,sys.argv[4])
    elif sys.argv[1]=='finalize':finalize(root,version,architectures(' '.join(sys.argv[4:])))
    else:raise ValueError('Unknown command')
