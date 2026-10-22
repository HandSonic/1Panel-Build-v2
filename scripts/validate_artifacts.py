#!/usr/bin/env python3
"""Validate release bytes without executing packaged binaries or installer scripts."""
import argparse,gzip,hashlib,json,pathlib,re,struct,tarfile
from resolve_inputs import resolve
from embedded_configuration import validate_binary
from installer_resources import service_copies, resource_layout, validate_root_interface
ARCHES={'amd64':(2,1,62),'arm64':(2,1,183),'armv7':(1,1,40),'ppc64le':(2,1,21),'s390x':(2,2,22),'loong64':(2,1,258),'riscv64':(2,1,243)}
def architectures(value):
    arches=value.replace(',',' ').replace(';',' ').split()
    if not arches or len(arches)!=len(set(arches)) or any(a not in ARCHES for a in arches): raise ValueError('Invalid or duplicate architecture list')
    return arches

def validate_elf(data, arch):
    if len(data)<20 or data[:4]!=b'\x7fELF': raise ValueError('Missing ELF header')
    found=(data[4],data[5],int.from_bytes(data[18:20],'little' if data[5]==1 else 'big'))
    if found!=ARCHES[arch]: raise ValueError(f'Wrong ELF target {found}; expected {ARCHES[arch]}')
    endian='<' if data[5]==1 else '>'
    header_format=endian+('HHIIIIIHHHHHH' if data[4]==1 else 'HHIQQQIHHHHHH')
    header_size=16+struct.calcsize(header_format)
    if len(data)<header_size: raise ValueError('Truncated ELF header')
    kind,_,version,entry,phoff,_,_,ehsize,phentsize,phnum,_,_,_=struct.unpack_from(header_format,data,16)
    if data[6]!=1 or version!=1 or kind not in (2,3) or ehsize!=header_size:
        raise ValueError('Unsupported executable ELF header')
    program_format=endian+('IIIIIIII' if data[4]==1 else 'IIQQQQQQ')
    if (not phnum or phentsize!=struct.calcsize(program_format) or phoff<header_size or
        phoff+phnum*phentsize>len(data)):
        raise ValueError('Invalid ELF program header table')
    executable_entry=False
    for index in range(phnum):
        row=struct.unpack_from(program_format,data,phoff+index*phentsize)
        if data[4]==1:
            segment,offset,address,_,filesize,memorysize,flags,_=row
        else:
            segment,flags,offset,address,_,filesize,memorysize,_=row
        if segment!=1:continue  # PT_LOAD
        if filesize>memorysize or (filesize and offset+filesize>len(data)):
            raise ValueError('ELF load segment exceeds file or memory bounds')
        if flags & 1 and address<=entry<address+filesize:executable_entry=True
    if not executable_entry:
        raise ValueError('ELF entry point is not in an executable load segment')

def validate_package(path, version, arch, *, require_embedded=False):
    entry=resolve(version); root=f'1panel-{version}-linux-{arch}'
    from package_metadata import DIRECTORY, _read_members, bounded_members, check_archive_size
    from automatic_frontend_lock import MAX_LOCK
    from resolved_contract import MAX_CONTROL
    required=set(entry['installer_sha256'])|{'1panel-core','1panel-agent','1panel-core.service','1panel-agent.service','GeoIP.mmdb','manifest.json'}
    check_archive_size(path)
    data={};members=[]
    with gzip.open(path,'rb') as stream, tarfile.open(fileobj=stream,mode='r|') as tf:
        for member in bounded_members(tf):
            members.append(member)
            p=pathlib.PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!=root: raise ValueError('Unsafe archive path')
            if member.isdir(): continue
            if not member.isfile() or len(p.parts)<2: raise ValueError('Links/nonregular archive member prohibited')
            rel=str(p.relative_to(root))
            if rel in data: raise ValueError('Duplicate archive member')
            if member.size<=0: raise ValueError(f'Empty file {rel}')
            if (rel=='manifest.json' or rel.startswith(DIRECTORY+'/')):
                limit=MAX_CONTROL if rel in ('manifest.json',DIRECTORY+'/resolved-source.json') else MAX_LOCK
                if member.size>limit:raise ValueError('Missing or invalid package metadata: '+rel)
            if rel in ('1panel-core','1panel-agent','1pctl','install.sh') and not member.mode & 0o111: raise ValueError(f'Not executable: {rel}')
            data[rel]=tf.extractfile(member).read()
        # tar stops at its end marker. Drain this same gzip stream so its CRC
        # and trailer remain checked without decompressing the archive again.
        while stream.read(1024*1024):pass
    embedded=_read_members(members,lambda member:data[str(pathlib.PurePosixPath(member.name).relative_to(root))],version,arch)
    if require_embedded and embedded is None:
        raise ValueError('Package-only publication requires embedded build inputs')
    if not required<=data.keys(): raise ValueError(f'Missing files: {required-data.keys()}')
    manifest=json.loads(data.pop('manifest.json'))
    expected={'schema_version':1,'version':version,'architecture':arch,'edition':'community','source_commit':entry['source_commit'],'installer_commit':entry['installer_commit'],'mode':entry['mode'],'go_version':entry['go_version'],'node_version':entry['node_version'],'npm_version':entry['npm_version']}
    if entry.get('resolved_contract_sha256'): expected['resolved_contract_sha256']=entry['resolved_contract_sha256']
    for key,value in expected.items():
        if manifest.get(key)!=value: raise ValueError(f'Manifest mismatch {key}')
    if not re.fullmatch('[0-9a-f]{40}',manifest.get('build_repository_commit','')): raise ValueError('Missing build commit')
    if set(manifest['files'])!=set(data): raise ValueError('Manifest file set mismatch')
    for rel,content in data.items():
        if manifest['files'][rel]!={'sha256':hashlib.sha256(content).hexdigest(),'size':len(content)}: raise ValueError(f'Manifest digest mismatch {rel}')
    for binary in ('1panel-core','1panel-agent'):
        validate_elf(data[binary],arch)
        validate_binary(data[binary],version,binary.removeprefix('1panel-'),entry['source_commit'])
    if hashlib.sha256(data['GeoIP.mmdb']).hexdigest()!=entry['geoip_sha256']: raise ValueError('GeoIP mismatch')
    for rel,digest in entry['installer_sha256'].items():
        if rel=='1pctl':
            normalized,count=re.subn(rb'(?m)^ORIGINAL_VERSION=[^\n]*',b'ORIGINAL_VERSION='+entry['installer_original_version'].encode(),data[rel])
            if count!=1 or hashlib.sha256(normalized).hexdigest()!=digest: raise ValueError('1pctl content mismatch')
            continue
        if hashlib.sha256(data[rel]).hexdigest()!=digest: raise ValueError(f'Installer resource mismatch {rel}')
    if f'ORIGINAL_VERSION={version}'.encode() not in data['1pctl']: raise ValueError('1pctl version mismatch')
    if resource_layout(entry['installer_sha256']) == 'root-systemd':
        validate_root_interface(data['install.sh'], entry['installer_sha256'])
    for source, target in service_copies(entry['installer_sha256']):
        if data[target] != data[source]: raise ValueError('Service copy mismatch')
    return manifest

def validate_dist(dist,version,arches, *, require_embedded=False):
    from package_metadata import check_archive_size
    expected=set(); records=[]
    for arch in arches:
        name=f'1panel-{version}-linux-{arch}.tar.gz'; expected|={name,name+'.sha256'}
        path=dist/name; check_archive_size(path)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if (dist/(name+'.sha256')).read_text()!=f'{digest}  {name}\n': raise ValueError('Checksum must match bytes and relative basename')
        manifest=validate_package(path,version,arch,require_embedded=require_embedded)
        records.append({'architecture':arch,'file':name,'sha256':digest,'size':path.stat().st_size,'source_commit':manifest['source_commit'],'installer_commit':manifest['installer_commit'],'build_repository_commit':manifest['build_repository_commit']})
    entry=resolve(version)
    if entry.get('resolved_contract_sha256'):
        for row in records: row['resolved_contract_sha256']=entry['resolved_contract_sha256']
        contract_file=dist/'resolved-source.json'
        from automatic_frontend_lock import read_regular, read_payload, required_sidecars
        from resolved_contract import MAX_CONTROL, object_bytes, source_contract
        raw=read_regular(contract_file,MAX_CONTROL)
        if hashlib.sha256(raw).hexdigest()!=entry['resolved_contract_sha256']: raise ValueError('Resolved contract bytes mismatch')
        contract=source_contract(raw,entry['resolved_contract_sha256'],version,object_bytes(raw)['mode'])
        read_payload(dist,contract)
        expected.update(required_sidecars(contract))
        expected.add('resolved-source.json')
    expected|={'checksums.txt','build-manifest.json'}
    # CI may attach the resolved inputs alongside the immutable manifests.
    if (dist/'build-inputs.env').is_file(): expected.add('build-inputs.env')
    if {p.name for p in dist.iterdir()}!=expected: raise ValueError('Unexpected or missing release files')
    aggregate=''.join((dist/(r['file']+'.sha256')).read_text() for r in records)
    if (dist/'checksums.txt').read_text()!=aggregate: raise ValueError('Aggregate checksums mismatch')
    aggregate_manifest = json.loads((dist/'build-manifest.json').read_text())
    if aggregate_manifest.get('schema_version') == 2:
        from matrix_contract import validate
        if validate(aggregate_manifest, version) != list(arches) or aggregate_manifest['artifacts'] != records:
            raise ValueError('Partial release manifest mismatch')
    elif aggregate_manifest.get('schema_version') == 3:
        from publication_lineage import validate
        if validate(aggregate_manifest, version) != list(arches) or aggregate_manifest['artifacts'] != records:
            raise ValueError('Publication lineage manifest mismatch')
    elif aggregate_manifest != {'schema_version':1,'version':version,'artifacts':records}:
        raise ValueError('Release manifest mismatch')
    return records
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=pathlib.Path)
    parser.add_argument('version')
    parser.add_argument('architectures',nargs='+')
    parser.add_argument('--require-embedded',action='store_true')
    args=parser.parse_args()
    validate_dist(args.directory,args.version,architectures(' '.join(args.architectures)),require_embedded=args.require_embedded)
    print('All requested artifacts verified')
