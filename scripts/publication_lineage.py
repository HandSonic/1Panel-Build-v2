#!/usr/bin/env python3
"""Canonical inventories retain the exact producer of each accepted architecture."""
import hashlib
import json
import re

FIELDS = {'schema_version', 'version', 'requested_architectures', 'resolved_contract_sha256',
          'current_generation', 'generations', 'artifact_generations', 'artifacts'}


def generation_id(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def validate(value, version=None):
    from matrix_contract import validate as validate_matrix
    if not isinstance(value, dict) or set(value) != FIELDS or value.get('schema_version') != 3:
        raise ValueError('Invalid publication lineage fields')
    if version is not None and value['version'] != version:
        raise ValueError('Publication lineage version mismatch')
    contract = value['resolved_contract_sha256']
    if not isinstance(contract, str) or not re.fullmatch('[0-9a-f]{64}', contract):
        raise ValueError('Invalid common source contract digest')
    generations = value['generations']
    if not isinstance(generations, dict) or not 1 <= len(generations) <= 8:
        raise ValueError('Invalid generation inventory')
    current = value['current_generation']
    if not isinstance(current, str) or current not in generations:
        raise ValueError('Missing current generation')
    available = {}
    for key, manifest in generations.items():
        validate_matrix(manifest, value['version'])
        commits={row.get('build_repository_commit') for row in manifest['artifacts']}
        if len(commits)!=1 or not isinstance(next(iter(commits)),str) or not re.fullmatch('[0-9a-f]{40}',next(iter(commits))):
            raise ValueError('Each generation requires a successful row with one exact executed commit')
        if key != generation_id(manifest) or manifest['requested_architectures'] != value['requested_architectures']:
            raise ValueError('Generation digest or requested inventory mismatch')
        for row in manifest['artifacts']:
            if row.get('resolved_contract_sha256') != contract:
                raise ValueError('Generations have different source contracts')
        available[key] = {row['architecture']: row for row in manifest['artifacts']}
    selection = value['artifact_generations']
    if not isinstance(selection, dict) or not selection or not isinstance(value['artifacts'], list):
        raise ValueError('Missing selected publication artifacts')
    arches = [a for a in value['requested_architectures'] if a in selection]
    if set(arches) != set(selection) or [r.get('architecture') for r in value['artifacts']] != arches:
        raise ValueError('Selected architecture inventory mismatch')
    for row in value['artifacts']:
        key = selection[row['architecture']]
        if not isinstance(key, str) or key not in available or available[key].get(row['architecture']) != row:
            raise ValueError('Artifact does not equal its original successful producer row')
    if set(generations) != set(selection.values()) | {current}:
        raise ValueError('Unreferenced generation')
    return arches
