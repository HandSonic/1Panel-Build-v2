#!/usr/bin/env python3
"""Resolve reviewed immutable inputs; never substitute latest for historical releases."""
import json, pathlib, re, shlex, sys
SOURCE_FILES = frozenset(('core/go.mod', 'agent/go.mod', 'frontend/package.json', 'frontend/package-lock.json'))
def resolve(version):
    from resolved_contract import runtime_contract, digest
    contract = runtime_contract(version)
    source, tools, installer = contract['source'], contract['toolchain'], contract['installer']
    entry = {'source_commit': source['commit'], 'installer_commit': installer['commit'],
             'go_version': tools['go'], 'node_version': tools['node'], 'npm_version': tools['npm'],
             'mode': contract['mode'], 'geoip_sha256': contract['resources']['geoip']['sha256'],
             'geoip_bytes': contract['resources']['geoip']['bytes'],
             'installer_sha256': {p: row['sha256'] for p, row in installer['resources'].items()},
             'installer_original_version': installer['original_version'],
             'source_files_sha256': {p: row['sha256'] for p, row in source['files'].items() if p in SOURCE_FILES},
             'source_files_absent': source['absent'], 'resolved_contract_sha256': digest(contract)}
    geoip = contract['resources']['geoip']
    entry['geoip_archive' if 'archive' in geoip else 'geoip_url'] = geoip.get('archive', geoip.get('url'))
    if contract['frontend_lock']['kind'] == 'derived':
        entry['frontend_repair_lock_sha256'] = contract['frontend_lock']['sha256']
    return entry
if __name__ == '__main__':
    try:
        entry = resolve(sys.argv[1])
        for name, key in [('SOURCE_COMMIT','source_commit'),('INSTALLER_REF','installer_commit'),('GO_VERSION','go_version'),('NODE_VERSION','node_version'),('NPM_VERSION','npm_version'),('GEOIP_SHA256','geoip_sha256')]:
            print(f'{name}={shlex.quote(entry[key])}')
    except (IndexError, KeyError, ValueError) as exc:
        sys.exit(str(exc))
