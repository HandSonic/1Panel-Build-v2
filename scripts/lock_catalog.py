#!/usr/bin/env python3
"""Select reviewed dependency repairs by input bytes, never by release version."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def select(manifest_sha256, original_sha256, root=ROOT):
    catalog = json.loads((root / 'config/repair-locks.json').read_text())
    if catalog.get('schema') != 1 or set(catalog) != {'schema', 'additions', 'recipes'}:
        raise ValueError('Unsupported content-addressed repair catalogue')
    if not isinstance(catalog['recipes'],list) or not isinstance(catalog['additions'],dict):
        raise ValueError('Invalid repair catalogue types')
    for row in catalog['recipes']:
        required={'manifest_sha256','original_sha256','derived_sha256'}
        if row.get('original_sha256') is None:required.add('reviewed_lock_file')
        if set(row)!=required or any(not isinstance(row[k],str) or not re.fullmatch('[0-9a-f]{64}',row[k]) for k in ('manifest_sha256','derived_sha256')):
            raise ValueError('Invalid content-addressed repair selector')
        if row['original_sha256'] is not None and (not isinstance(row['original_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',row['original_sha256'])):
            raise ValueError('Invalid original-lock digest')
    matches = [r for r in catalog['recipes'] if r.get('manifest_sha256') == manifest_sha256
               and r.get('original_sha256') == original_sha256]
    if len(matches) > 1:
        raise ValueError('Ambiguous manifest and source-lock repair')
    return (matches[0], catalog['additions']) if matches else (None, None)


def derive(manifest, original, recipe, additions, root=ROOT):
    if sha(manifest) != recipe['manifest_sha256']:
        raise ValueError('Repair manifest checksum mismatch')
    if (None if original is None else sha(original)) != recipe['original_sha256']:
        raise ValueError('Repair original-lock checksum or absence mismatch')
    if original is None:
        expected = 'frontend-locks/' + recipe['derived_sha256'] + '.package-lock.json'
        if recipe.get('reviewed_lock_file') != expected:
            raise ValueError('Unexpected content-addressed lock path')
        path = root / 'config' / expected
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or linked reviewed repair lock')
        result = path.read_bytes()
    else:
        if 'reviewed_lock_file' in recipe:
            raise ValueError('Unexpected replacement for a present source lock')
        value = json.loads(original)
        for name, package in additions.items():
            if name in value['packages']:
                raise ValueError('Repair cannot replace an existing dependency')
            value['packages'][name] = package
        result = (json.dumps(value, indent=2) + '\n').encode()
    if sha(result) != recipe['derived_sha256']:
        raise ValueError('Derived repair lock checksum mismatch')
    return result


def apply(frontend, contract, root=ROOT):
    lock = contract['frontend_lock']
    if lock['kind'] == 'source':
        return
    manifest_path = frontend / 'package.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Missing or linked frontend manifest')
    manifest = manifest_path.read_bytes()
    source_lock = contract['source']['files'].get('frontend/package-lock.json')
    original_sha = source_lock['sha256'] if source_lock else None
    recipe, additions = select(lock['manifest_sha256'], original_sha, root)
    if recipe is None or sha(canonical(recipe)) != lock['recipe_sha256'] or recipe['derived_sha256'] != lock['sha256']:
        raise ValueError('Unbound content-addressed repair recipe')
    target = frontend / 'package-lock.json'
    if original_sha is None:
        if contract['source']['absent'] != ['frontend/package-lock.json'] or target.exists() or target.is_symlink():
            raise ValueError('Original lock absence no longer holds')
        original = None
    else:
        if contract['source']['absent'] or target.is_symlink() or not target.is_file():
            raise ValueError('Missing or linked original source lock')
        original = target.read_bytes()
    result = derive(manifest, original, recipe, additions, root)
    if original is None:
        with target.open('xb') as output:
            output.write(result)
    else:
        target.write_bytes(result)
