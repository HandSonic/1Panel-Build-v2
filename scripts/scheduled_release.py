#!/usr/bin/env python3
"""Plan daily builds from official versions and public attachment names only."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from public_packages import ARCHES, metadata_inventory
from release_asset_repair import GitHub

REPOSITORY = 'HandSonic/1Panel-Build-v2'
SOURCE = '1Panel-dev/1Panel'
STABLE = re.compile(r'v2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def latest_version(client):
    releases = json.loads(client.run('api', f'repos/{SOURCE}/releases?per_page=100'))
    candidates = []
    for release in releases:
        version = release.get('tag_name', '')
        match = STABLE.fullmatch(version)
        if match and not release.get('draft') and not release.get('prerelease'):
            candidates.append((tuple(map(int, match.groups())), version))
    if not candidates:
        raise ValueError('No official stable release found')
    return max(candidates)[1]


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
        present = {}
    else:
        present, _ = metadata_inventory(release, version)
    missing = [arch for arch in requested if arch not in present]
    return {'version': version, 'arches': ' '.join(missing),
            'build_required': 'true' if missing else 'false'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--version')
    parser.add_argument('--arches', default=' '.join(ARCHES))
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    result = plan(args.repository, args.version, args.arches)
    if args.github_output:
        with args.github_output.open('a') as output:
            for key, value in result.items():
                print(f'{key}={value}', file=output)
    print(json.dumps(result, sort_keys=True))
