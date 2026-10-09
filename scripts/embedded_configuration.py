#!/usr/bin/env python3
"""Immutable run-contract Go-embedded configuration checks; never execute binaries."""
import hashlib,json,re,sys,tarfile
from pathlib import Path
from functools import lru_cache
from urllib.request import build_opener, HTTPRedirectHandler
from semantic_configuration import production
ROOT=Path(__file__).resolve().parents[1]

class NoVendorRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Vendor configuration redirect rejected')

def open_vendor_source(url):
    return build_opener(NoVendorRedirect()).open(url,timeout=30)

@lru_cache(maxsize=64)
def fetch_vendor_source(commit,component,source_path,source_bytes,source_sha256):
    if not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('Immutable source commit required')
    if component not in ('core','agent') or source_path!=f'{component}/cmd/server/conf/app.yaml':
        raise ValueError('Unexpected vendor configuration path')
    if type(source_bytes) is not int or not 0<source_bytes<=65536:raise ValueError('Invalid vendor source size')
    if not re.fullmatch(r'[0-9a-f]{64}',source_sha256):raise ValueError('Invalid vendor source digest')
    url=f'https://raw.githubusercontent.com/1Panel-dev/1Panel/{commit}/{source_path}'
    try:
        with open_vendor_source(url) as response:
            if response.geturl()!=url:raise ValueError('Vendor configuration redirect rejected')
            data=response.read(source_bytes+1)
    except Exception as exc:
        raise ValueError('Pinned vendor configuration unavailable or invalid') from exc
    if len(data)!=source_bytes or hashlib.sha256(data).hexdigest()!=source_sha256:
        raise ValueError('Pinned vendor configuration size or digest mismatch')
    return data

def expected_bytes(version,component,root=ROOT):
    from resolved_contract import runtime_contract
    contract=runtime_contract(version,root)
    entry={'source_commit':contract['source']['commit'],'mode':contract['mode']}
    profile=contract['configuration'][component]
    original=fetch_vendor_source(entry['source_commit'],component,profile['path'],profile['source_bytes'],profile['source_sha256'])
    normalized=production(original,version,component,entry['mode'])
    if hashlib.sha256(normalized).hexdigest()!=profile['normalized_sha256']:raise ValueError('Reviewed normalized YAML changed')
    return original,normalized,entry['source_commit']

def validate_binary(data,version,component,source_commit,root=ROOT):
    original,normalized,expected_commit=expected_bytes(version,component,root)
    if source_commit!=expected_commit:raise ValueError('Binary configuration source commit mismatch')
    if normalized not in data:raise ValueError(f'{version}/{component}: exact normalized production configuration not embedded')
    if original!=normalized and original in data:raise ValueError(f'{version}/{component}: unmodified development configuration remains embedded')
    return hashlib.sha256(normalized).hexdigest()

def validate_archive(path,version,arch,root=ROOT):
    prefix=f'1panel-{version}-linux-{arch}/'
    with tarfile.open(path,'r:gz') as archive:
        manifest=json.load(archive.extractfile(prefix+'manifest.json'))
        return {c:validate_binary(archive.extractfile(prefix+'1panel-'+c).read(),version,c,manifest['source_commit'],root) for c in ['core','agent']}

if __name__=='__main__':
    print(json.dumps(validate_archive(Path(sys.argv[1]),sys.argv[2],sys.argv[3]),sort_keys=True))
