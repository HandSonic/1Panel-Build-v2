#!/usr/bin/env python3
"""Skip a scheduled rebuild only after validating the complete public matrix."""
import argparse
from pathlib import Path
import re
import subprocess

from public_packages import verify_existing
from release_asset_repair import GitHub
from validate_artifacts import architectures

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'HandSonic/1Panel-Build-v2'


def build_required(repository, version, arches, root=ROOT):
    if repository != REPOSITORY or not re.fullmatch(r'v2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:beta|dev)\.(?:0|[1-9][0-9]*))?', version):
        raise ValueError('Unexpected scheduled release identity')
    requested = architectures(arches)
    client = GitHub(repository, version)
    try:
        release = client.release()
    except subprocess.CalledProcessError as error:
        # Only this initial tag lookup can classify a published release as absent.
        # Asset errors and races inside verification must still fail the check.
        if error.returncode == 1 and '(HTTP 404)' in (error.stderr or ''):
            return True
        raise
    client.bind_release(release['id'])
    _, records = verify_existing(client, version, root)
    return not set(requested) <= set(records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--arches', required=True)
    args = parser.parse_args()
    print(str(build_required(args.repository, args.version, args.arches)).lower())
