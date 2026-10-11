#!/usr/bin/env python3
"""Build-only modernc compatibility for source graphs without linux/loong64.

No 1Panel release lookup is used. Original go.mod/go.sum remain immutable and
all other targets use the original graph. Pins are deliberately paired: modernc
SQLite requires the libc version in its own go.mod (see upstream documentation).
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PINS = {'modernc.org/sqlite': 'v1.29.10', 'modernc.org/libc': 'v1.49.3'}
PACKAGES = ('modernc.org/sqlite/lib', 'modernc.org/libc/errno')


def objects(text):
    decoder = json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        item, end = decoder.raw_decode(text)
        yield item
        text = text[end:]


def older(version, minimum):
    # SemVer comparison includes prereleases/pseudo-versions, ignores metadata,
    # and fails closed on unfamiliar syntax rather than risking a downgrade.
    pattern = r'v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?'
    match, floor = re.fullmatch(pattern, version), re.fullmatch(pattern, minimum)
    if not match or not floor:
        raise ValueError(f'Cannot safely compare module versions: {version}, {minimum}')
    current, required = tuple(map(int, match.group(1, 2, 3))), tuple(map(int, floor.group(1, 2, 3)))
    if current != required:
        return current < required
    a, b = match.group(4), floor.group(4)
    if a == b:
        return False
    if a is None or b is None:
        return a is not None
    for left, right in zip(a.split('.'), b.split('.')):
        if left == right:
            continue
        if left.isdigit() and right.isdigit():
            return int(left) < int(right)
        if left.isdigit() != right.isdigit():
            return left.isdigit()
        return left < right
    return len(a.split('.')) < len(b.split('.'))


def run(args, cwd, arch, modfile):
    env = dict(os.environ, CGO_ENABLED='0', GOOS='linux', GOARCH=arch, GOARM='', GOWORK='off')
    # The explicit -modfile must apply even if callers supplied GOFLAGS.
    env['GOFLAGS'] = ''
    command = ['go', *args]
    if args[0] in ('list', 'build', 'test'):
        command[2:2] = [f'-modfile={modfile}', '-mod=mod']
    else:
        env['GOFLAGS'] = f'-modfile={modfile}'
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(command)} failed:\n{result.stderr}")
    return result.stdout


def capabilities(cwd, modfile):
    packages = list(objects(run(['list', '-e', '-json', *PACKAGES], cwd, 'loong64', modfile)))
    if {p.get('ImportPath') for p in packages} != set(PACKAGES):
        raise RuntimeError('Incomplete modernc architecture capability probe')
    missing = []
    for package in packages:
        error = package.get('Error', {}).get('Err', '')
        if error:
            if 'build constraints exclude all Go files' not in error:
                raise RuntimeError(f"Cannot probe {package['ImportPath']}: {error}")
            missing.append(package['ImportPath'])
        elif not package.get('GoFiles'):
            raise RuntimeError(f"Empty architecture probe for {package['ImportPath']}")
    return missing


@contextmanager
def module_for_arch(cwd, arch):
    cwd = Path(cwd).resolve()
    if arch != 'loong64':
        yield None
        return
    # A disposable copy also isolates go list's dependency/sum resolution.
    with tempfile.TemporaryDirectory(prefix='.go-loong64-', dir=cwd) as directory:
        modfile = Path(directory) / 'compat.mod'
        shutil.copyfile(cwd / 'go.mod', modfile)
        if (cwd / 'go.sum').exists():
            shutil.copyfile(cwd / 'go.sum', modfile.with_suffix('.sum'))
        modules = {m['Path']: m for m in objects(run(['list', '-m', '-json', 'all'], cwd, arch, modfile))}
        if 'modernc.org/sqlite' not in modules:
            # Source switched drivers; never introduce a new SQLite dependency.
            yield None
            return
        if 'modernc.org/libc' not in modules:
            raise RuntimeError('modernc SQLite graph is missing libc')
        missing = capabilities(cwd, modfile)
        if not missing:
            yield None
            return
        if any(modules[name].get('Replace') for name in PINS):
            raise RuntimeError('Unsupported loong64 modernc replacement; refusing to overwrite a source replacement')
        requirements = [f'-require={name}@{pin}' for name, pin in PINS.items()
                        if older(modules[name]['Version'], pin)]
        if not requirements:
            raise RuntimeError(f'modernc graph at or above compatibility pins still lacks loong64: {missing}')
        print(f'loong64: {", ".join(missing)} unavailable; applying build-only {" ".join(requirements)}', file=sys.stderr)
        # Raising direct requirements uses MVS; go get module@pin can downgrade
        # other dependencies. Never use @latest or a per-1Panel-version table.
        run(['mod', 'edit', *requirements], cwd, arch, modfile)
        # Resolve once, then verify that MVS did not lower any selected dependency.
        selected = {m['Path']: m for m in objects(run(['list', '-m', '-json', 'all'], cwd, arch, modfile))}
        for name, before in modules.items():
            after = selected.get(name)
            if after and before.get('Version') and after.get('Version') and older(after['Version'], before['Version']):
                raise RuntimeError(f'Compatibility overlay would downgrade {name}')
        remaining = capabilities(cwd, modfile)
        if remaining:
            raise RuntimeError(f'Compatibility overlay still lacks linux/loong64: {remaining}')
        for name in PINS:
            print(f"loong64: selected {name}@{selected[name]['Version']}", file=sys.stderr)
        yield modfile


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: go_arch_compat.py ARCH OUTPUT')
    arch, output = sys.argv[1:]
    with module_for_arch(Path.cwd(), arch) as modfile:
        if modfile is None:
            subprocess.run(['go', 'build', '-trimpath', '-buildvcs=false', '-ldflags', '-s -w', '-o', output, './cmd/server/main.go'],
                           env=dict(os.environ, CGO_ENABLED='0', GOOS='linux', GOARCH=arch), check=True)
        else:
            run(['build', '-trimpath', '-buildvcs=false', '-ldflags', '-s -w', '-o', output, './cmd/server/main.go'], Path.cwd(), arch, modfile)


if __name__ == '__main__':
    main()
