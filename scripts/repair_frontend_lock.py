#!/usr/bin/env python3
"""Apply reviewed, source-hash-bound additions to incomplete historical npm locks."""
import hashlib
import json
from pathlib import Path
import sys
from resolve_inputs import resolve

REPAIRS = Path(__file__).resolve().parents[1] / 'config/frontend-lock-repairs.json'


def repair(root, version):
    entry = resolve(version)
    config = json.loads(REPAIRS.read_text())
    recipe = config['versions'].get(version)
    if recipe is None:
        return
    target = root / 'package-lock.json'
    if recipe.get('original_absent'):
        if entry.get('source_files_absent') != ['frontend/package-lock.json']:
            raise ValueError('Repair lock requires reviewed source absence')
        if recipe['derived_sha256'] != entry.get('frontend_repair_lock_sha256'):
            raise ValueError('Repair lock is not bound to reviewed source inputs')
        package = root / 'package.json'
        if package.is_symlink() or not package.is_file() or hashlib.sha256(package.read_bytes()).hexdigest() != entry['source_files_sha256']['frontend/package.json']:
            raise ValueError('Repair lock package manifest checksum mismatch')
        if target.exists() or target.is_symlink():
            raise ValueError('Refusing to replace an existing source lock')
        expected_path = 'frontend-locks/' + version + '.package-lock.json'
        if recipe['reviewed_lock_file'] != expected_path:
            raise ValueError('Unexpected reviewed repair lock path')
        source = REPAIRS.parent / expected_path
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing or linked reviewed repair lock')
        derived = source.read_bytes()
        if hashlib.sha256(derived).hexdigest() != recipe['derived_sha256']:
            raise ValueError('Reviewed repair lock checksum mismatch')
        # Exclusive creation also rejects a lock introduced after the absence check.
        with target.open('xb') as handle:
            handle.write(derived)
        print(f"Applied reviewed absent-lock repair: {version} {recipe['derived_sha256']}")
        return
    original_hash = recipe['original_sha256']
    if original_hash != entry['source_files_sha256']['frontend/package-lock.json']:
        raise ValueError('Repair is not bound to the reviewed source lock')
    if target.is_symlink() or not target.is_file():
        raise ValueError('Missing or linked frontend lock')
    original = target.read_bytes()
    if hashlib.sha256(original).hexdigest() != original_hash:
        raise ValueError('Original frontend lock checksum mismatch')
    lock = json.loads(original)
    for name, package in config['additions'].items():
        if name in lock['packages']:
            raise ValueError('Repair must not replace an existing package')
        lock['packages'][name] = package
    derived = (json.dumps(lock, indent=2) + '\n').encode()
    if hashlib.sha256(derived).hexdigest() != recipe['derived_sha256']:
        raise ValueError('Derived frontend lock checksum mismatch')
    target.write_bytes(derived)
    print(f"Applied reviewed frontend lock repair: {version} {recipe['derived_sha256']}")


if __name__ == '__main__':
    repair(Path(sys.argv[1]), sys.argv[2])
