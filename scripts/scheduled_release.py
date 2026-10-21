#!/usr/bin/env python3
"""Plan daily builds from official versions and public attachment names only."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from public_packages import ARCHES, metadata_inventory
from release_asset_repair import GitHub, release_snapshot, repair

REPOSITORY = 'HandSonic/1Panel-Build-v2'
SOURCE = '1Panel-dev/1Panel'
STABLE = re.compile(r'v2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def latest_version(client):
    release = json.loads(client.run('api', f'repos/{SOURCE}/releases/latest'))
    version = release.get('tag_name') if isinstance(release, dict) else None
    if (not isinstance(version, str) or not STABLE.fullmatch(version) or
        release.get('draft') is not False or release.get('prerelease') is not False):
        raise ValueError('Official latest release is not a supported stable v2 release')
    return version


def missing_files(release, version, requested):
    _, members = metadata_inventory(release, version)
    archives, checksums = [], []
    for arch in requested:
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        archive, checksum = members.get(name), members.get(name + '.sha256')
        if archive is None:
            if checksum is not None:
                raise ValueError(f'Checksum exists without its archive; explicit repair required: {name}')
            archives.append(arch)
        elif checksum is None:
            digest = archive.get('digest')
            if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                raise ValueError(f'Archive has no valid server SHA-256; explicit repair required: {name}')
            checksums.append(archive)
    return archives, checksums


def plan(repository, version=None, arches=' '.join(ARCHES)):
    if repository != REPOSITORY:
        raise ValueError('Unexpected scheduled repository')
    requested = arches.replace(',', ' ').replace(';', ' ').split()
    if not requested or len(requested) != len(set(requested)) or any(a not in ARCHES for a in requested):
        raise ValueError('Invalid requested architectures')
    client = GitHub(repository, version or '')
    version = version or latest_version(client)
    if not STABLE.fullmatch(version):
        raise ValueError('Unexpected scheduled stable version')
    client.tag = version
    try:
        release = client.release()
    except subprocess.CalledProcessError as error:
        if error.returncode != 1 or '(HTTP 404)' not in (error.stderr or ''):
            raise
        missing, checksums = requested, []
    else:
        missing, checksums = missing_files(release, version, requested)
    return {'version': version, 'arches': ' '.join(missing),
            'build_required': 'true' if missing else 'false',
            'checksum_required': 'true' if checksums else 'false'}


def repair_checksums(repository, version, work):
    if repository != REPOSITORY or not isinstance(version, str) or not STABLE.fullmatch(version):
        raise ValueError('Unexpected scheduled checksum identity')
    work = Path(work)
    work.mkdir(parents=True)
    client = GitHub(repository, version)
    before = client.release()
    client.bind_release(before['id'])
    current = client.release()
    if release_snapshot(current) != release_snapshot(before):
        raise ValueError('Release changed before checksum repair')
    _, missing = missing_files(current, version, ARCHES)
    files = []
    for archive in missing:
        path = work / (archive['name'] + '.sha256')
        path.write_text(archive['digest'][7:] + '  ' + archive['name'] + '\n')
        files.append(path)
    if files:
        repair(client, files, work / 'checksum-journal.json',
               expected_snapshot=release_snapshot(current),
               preserved_assets={asset['name']: asset for asset in current['assets']})
    return {'state': 'complete', 'release_id': before['id'], 'checksum_count': len(files)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--version')
    parser.add_argument('--arches', default=' '.join(ARCHES))
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--repair-checksums', action='store_true')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    if args.repair_checksums:
        if not args.version or args.work is None:
            parser.error('--repair-checksums requires --version and --work')
        result = repair_checksums(args.repository, args.version, args.work)
    else:
        result = plan(args.repository, args.version, args.arches)
    if args.github_output:
        with args.github_output.open('a') as output:
            for key, value in result.items():
                print(f'{key}={value}', file=output)
    print(json.dumps(result, sort_keys=True))
