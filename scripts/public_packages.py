"""Select package payloads and inspect remote attachment metadata without downloads."""
ARCHES = ('amd64', 'arm64', 'armv7', 'ppc64le', 's390x', 'loong64', 'riscv64')


def package_files(files):
    return [path for path in files if path.name.endswith(('.tar.gz', '.tar.gz.sha256'))]


def metadata_inventory(release, version):
    """List complete architecture pairs and individual canonical members.

    Automatic gap checks trust the repository's asset names. They deliberately
    do not download packages or require historical digest/size metadata.
    """
    if release.get('draft') is not False or release.get('tag_name') != version or release.get('prerelease') is not ('-' in version):
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
