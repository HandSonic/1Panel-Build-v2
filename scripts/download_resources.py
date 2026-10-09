#!/usr/bin/env python3
import hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile
from resolve_inputs import resolve

def download(url, dest, expected):
    subprocess.run(['curl','--fail','--location','--silent','--show-error','--retry','3','--connect-timeout','20','--max-time','180','--output',str(dest),url],check=True)
    if not dest.is_file() or dest.stat().st_size == 0: raise ValueError(f'Empty download: {url}')
    if hashlib.sha256(dest.read_bytes()).hexdigest() != expected: raise ValueError(f'Checksum mismatch: {url}')

def main(version, destination):
    entry = resolve(version)
    if os.getenv('INSTALLER_REF', entry['installer_commit']) != entry['installer_commit']:
        raise ValueError('INSTALLER_REF must match reviewed lock')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.resources-',dir=destination) as tmp:
        stage=pathlib.Path(tmp)
        for rel, digest in entry['installer_sha256'].items():
            target=stage/rel; target.parent.mkdir(parents=True,exist_ok=True)
            download(f"https://raw.githubusercontent.com/1Panel-dev/installer/{entry['installer_commit']}/{rel}",target,digest)
        if 'geoip_archive' in entry:
            from discover_inputs import Vendor, official_archive
            with (stage/'GeoIP.mmdb').open('wb') as output:
                _, geoip = official_archive(Vendor(), version, entry['mode'], expected=entry['geoip_archive'], geoip_destination=output)
            if geoip['sha256'] != entry['geoip_sha256'] or geoip['bytes'] != entry['geoip_bytes']:
                raise ValueError('Version-bound GeoIP member mismatch')
        else:
            download(entry['geoip_url'],stage/'GeoIP.mmdb',entry['geoip_sha256'])
        for part in ('core','agent'): shutil.copy2(stage/f'initscript/1panel-{part}.service',stage/f'1panel-{part}.service')
        for file in stage.rglob('*'):
            if file.is_file():
                target=destination/file.relative_to(stage); target.parent.mkdir(parents=True,exist_ok=True)
                os.replace(file,target)
    for name in ('1pctl','install.sh'): (destination/name).chmod(0o755)
    (destination/'resource-provenance.json').write_text(json.dumps(entry,indent=2)+'\n')
if __name__ == '__main__':
    main(sys.argv[1],pathlib.Path(sys.argv[2]))
