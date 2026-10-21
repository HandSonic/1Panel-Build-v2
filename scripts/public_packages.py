"""Select package payloads and inspect remote attachment metadata without downloads."""
import re

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

    Automatic gap checks trust the repository's asset names. They deliberately
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
