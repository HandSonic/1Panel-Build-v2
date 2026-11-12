#!/usr/bin/env python3
"""Supply the official KVM binary before compiling an Agent that embeds it."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

from validate_artifacts import validate_elf


MAX_HELPER_BYTES = 64 * 1024 * 1024


def prepare(root, arch):
    if arch not in ('amd64', 'arm64'):
        return
    directory = root / 'agent/utils/vm/helper'
    declaration = directory / f'embedded_{arch}.go'
    name = f'1panel-kvm-{arch}'
    if not declaration.is_file() or f'//go:embed bin/{name}' not in declaration.read_text().splitlines():
        return

    # This is the vendor resource used by prepare.sh in the pinned Agent source.
    url = f'https://resource.fit2cloud.com/1panel/resource/kvm/{name}'
    target = directory / 'bin' / name
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.kvm-', dir=target.parent) as work:
        download = Path(work) / name
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--retry', '3',
                        '--connect-timeout', '15', '--max-time', '180',
                        '--max-filesize', str(MAX_HELPER_BYTES), '--output', str(download), url],
                       check=True)
        if download.stat().st_size > MAX_HELPER_BYTES:
            raise ValueError(f'KVM helper exceeds the size limit: {name}')
        data = download.read_bytes()
        validate_elf(data, arch)
        download.chmod(0o755)
        download.replace(target)
    print(f'Official KVM helper {name}: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}', flush=True)


if __name__ == '__main__':
    prepare(Path.cwd(), sys.argv[1])
