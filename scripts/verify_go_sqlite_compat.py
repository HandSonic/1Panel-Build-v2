#!/usr/bin/env python3
"""Exercise the selected source's real GORM/SQLite graph before packaging.

Native old -> overlaid -> old engine roundtrip, plus linux/loong64 compilation
and optional mandatory execution under the supplied QEMU user-mode emulator.
All test databases, modfiles, checksums and binaries are disposable.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from go_arch_compat import module_for_arch, objects, run

TEST = Path(__file__).resolve().parents[1] / 'tests/go_sqlite_compat/sqlite_compat_test.go'


def command(args, cwd, env):
    print('+ ' + ' '.join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def verify(cwd, qemu=None):
    cwd = Path(cwd).resolve()
    originals = {name: hashlib.sha256((cwd / name).read_bytes()).hexdigest() for name in ('go.mod', 'go.sum') if (cwd / name).exists()}
    base_env = dict(os.environ, CGO_ENABLED='0', GOOS='linux', GOARM='', GOFLAGS='', GOWORK='off')
    base_env.pop('GOARCH', None)
    try:
        with tempfile.TemporaryDirectory(prefix='.sqlite-compat-', dir=cwd) as directory:
            directory = Path(directory)
            original = directory / 'original.mod'
            shutil.copyfile(cwd / 'go.mod', original)
            if (cwd / 'go.sum').exists():
                shutil.copyfile(cwd / 'go.sum', original.with_suffix('.sum'))
            selected_modules = {item['Path'] for item in objects(run(['list', '-m', '-json', 'all'], cwd, 'loong64', original))}
            required = {'github.com/glebarez/sqlite', 'github.com/glebarez/go-sqlite', 'gorm.io/gorm', 'modernc.org/sqlite', 'modernc.org/libc'}
            if not required <= selected_modules:
                print(f'{cwd.name}: SQLite smoke NOT RUN: source graph lacks {sorted(required - selected_modules)}; no test-only driver will be introduced.', flush=True)
                return
            with module_for_arch(cwd, 'loong64') as overlay:
                selected = overlay or original
                binary = directory / 'sqlite-loong64.test'
                command(['go', 'test', f'-modfile={selected}', '-mod=mod', '-c', '-o', binary, TEST], cwd, dict(base_env, GOARCH='loong64'))
                for target in ('native', 'loong64') if qemu else ('native',):
                    database = directory / (target + '.db')
                    for phase in ('baseline', 'upgrade', 'rollback'):
                        env = dict(base_env, SQLITE_COMPAT_DB=str(database), SQLITE_COMPAT_PHASE=phase)
                        if target == 'loong64' and phase == 'upgrade':
                            command([qemu, binary, '-test.v', '-test.timeout=120s'], cwd, env)
                        else:
                            modfile = selected if phase == 'upgrade' else original
                            command(['go', 'test', f'-modfile={modfile}', '-mod=mod', '-count=1', '-timeout=120s', '-v', TEST], cwd, env)
                if not qemu:
                    print('loong64 smoke binary compiled; loong64 execution NOT RUN (supply --qemu).', flush=True)
    finally:
        for name, expected in originals.items():
            actual = hashlib.sha256((cwd / name).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(f'Source {cwd / name} changed during compatibility verification')
    print(f'{cwd.name}: SQLite compatibility checks passed; source module files unchanged.', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--qemu', help='Executable for required loong64 smoke execution (e.g. qemu-loongarch64)')
    args = parser.parse_args()
    if args.qemu and not shutil.which(args.qemu):
        raise SystemExit(f'Required emulator is unavailable: {args.qemu}')
    for component in ('core', 'agent'):
        verify(args.source / component, args.qemu)


if __name__ == '__main__':
    main()
