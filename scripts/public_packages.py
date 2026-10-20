"""Trust canonical GitHub packages plus SHA and complete content validation.

Old CI logs and publication receipts are not a download-time trust dependency.
Controls remain internal CI data; existing remote controls/backups are preserved
until a separately approved cleanup, never deleted by publication.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from release_asset_repair import digest, release_snapshot

ARCHES = ('amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'loong64', 'riscv64')
CONTROLS = {'build-inputs.env', 'build-manifest.json', 'checksums.txt',
            'release-validation.json', 'resolved-source.json', 'resolved-frontend-package-lock.json'}


def package_files(files):
    return [path for path in files if path.name.endswith(('.tar.gz', '.tar.gz.sha256'))]


def recovery_assets(release, version):
    """Preserve recognized existing internal files and recovery bytes verbatim."""
    bases = CONTROLS | {f'1panel-{version}-linux-{a}.tar.gz' + suffix
                        for a in (*ARCHES, 'arm') for suffix in ('', '.sha256')}
    keep = {}
    for asset in release.get('assets', []):
        name = asset['name']
        if name in CONTROLS or any(re.fullmatch(re.escape(base) + r'\.(backup|staged)-[0-9a-f]{12}', name) for base in bases):
            keep[name] = asset
    return keep


def metadata_inventory(release, version):
    """List complete architecture pairs and individual canonical members.

    Scheduled gap checks trust the repository's asset names. They deliberately
    do not download packages or require historical digest/size metadata.
    """
    if release.get('draft') is not False or release.get('tag_name') != version or release.get('prerelease', False) is not ('-' in version):
        raise ValueError('Canonical public release identity mismatch')
    assets = {a['name']: a for a in release['assets']}
    if len(assets) != len(release['assets']):
        raise ValueError('Duplicate release asset names')
    found = {};members = {}
    for arch in ARCHES:
        name = f'1panel-{version}-linux-{arch}.tar.gz'
        for member in (name,name+'.sha256'):
            if member in assets:members[member]=assets[member]
        if name in assets and name + '.sha256' in assets:
            found[arch] = (assets[name], assets[name + '.sha256'])
    return found,members


def inventory(release, version):
    found,members=metadata_inventory(release,version)
    if len(members)!=2*len(found):raise ValueError('Incomplete canonical package/checksum pair')
    assets = {a['name']: a for a in release['assets']}
    allowed = set(recovery_assets(release, version)) | {a['name'] for pair in found.values() for a in pair}
    if set(assets) != allowed:
        raise ValueError('Unexpected canonical release asset')
    return found


def download(client, asset, directory):
    if type(asset.get('id')) is not int or asset['id'] <= 0 or type(asset.get('size')) is not int or not 0 < asset['size'] <= 2 * 1024**3:
        raise ValueError('Invalid canonical asset identity/size')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest', '')):
        raise ValueError('Missing canonical asset SHA-256')
    client.download(asset['name'], directory)
    path = Path(directory) / asset['name']
    if digest(path) != {'bytes': asset['size'], 'sha256': asset['digest'][7:]}:
        raise ValueError('Downloaded canonical asset differs from GitHub snapshot')
    return path


def verify_existing(client, version, root):
    """Independently validate each actual public package; return immutable pins."""
    from package_metadata import read
    from resolved_contract import apply_contract
    from automatic_frontend_lock import required_sidecars
    import json
    if client.repo != 'HandSonic/1Panel-Build-v2' or client.tag != version:
        raise ValueError('Unexpected public package repository or tag')
    release = client.release()
    pairs = inventory(release, version)
    records = {}
    for arch, (asset, sidecar) in pairs.items():
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            archive = download(client, asset, work)
            checksum = download(client, sidecar, work)
            if checksum.read_text() != asset['digest'][7:] + '  ' + asset['name'] + '\n':
                raise ValueError('Canonical package SHA sidecar mismatch')
            embedded = read(archive, version, arch)
            if embedded is None:
                # Legacy format can still supply its source inputs as old assets.
                assets = {row['name']: row for row in release['assets']}
                if 'resolved-source.json' not in assets:
                    raise ValueError('Legacy package has no resolved source inputs')
                raw = download(client, assets['resolved-source.json'], work).read_bytes()
                contract = json.loads(raw)
                for name in required_sidecars(contract):
                    download(client, assets[name], work)
            else:
                manifest, files = embedded
                for name, raw in files.items():
                    (work / name).write_bytes(raw)
                raw = files['resolved-source.json']
            contract_sha = hashlib.sha256(raw).hexdigest()
            policy = work / 'policy'
            policy.mkdir()
            shutil.copytree(Path(root) / 'config', policy / 'config', ignore=shutil.ignore_patterns('resolved-source.json', 'resolved-frontend-package-lock.json'))
            shutil.copytree(Path(root) / 'scripts', policy / 'scripts')
            shutil.copyfile(Path(root) / 'Dockerfile', policy / 'Dockerfile')
            apply_contract(work / 'resolved-source.json', contract_sha, policy, version)
            env = dict(os.environ)
            env.pop('RESOLVED_CONTRACT_SHA256', None)
            subprocess.run([sys.executable, '-c',
                'import sys;from validate_artifacts import validate_package;validate_package(sys.argv[1],sys.argv[2],sys.argv[3])',
                str(archive), version, arch], cwd=policy / 'scripts', env=env, check=True)
            records[arch] = {'file': asset['name'], 'sha256': asset['digest'][7:],
                             'size': asset['size'], 'resolved_contract_sha256': contract_sha,
                             'publication_format': None if embedded is None else embedded[0]['publication_format']}
    if len({row['resolved_contract_sha256'] for row in records.values()}) > 1:
        raise ValueError('Published architectures have conflicting source contracts')
    if release_snapshot(client.release()) != release_snapshot(release):
        raise ValueError('Release changed during package verification')
    return release, records


def require_embedded(files, version):
    from package_metadata import read
    for path in package_files(files):
        if not path.name.endswith('.tar.gz'):
            continue
        matches = [a for a in ARCHES if path.name == f'1panel-{version}-linux-{a}.tar.gz']
        if len(matches) != 1 or read(path, version, matches[0]) is None:
            raise ValueError('Package-only publication requires embedded build inputs')
