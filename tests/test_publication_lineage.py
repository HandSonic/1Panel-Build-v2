import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import matrix_contract
import publication_lineage as lineage


VERSION = 'v2.99.0'
CONTRACT_BYTES = b'{"synthetic_source_contract":"same immutable inputs"}\n'
CONTRACT_SHA = hashlib.sha256(CONTRACT_BYTES).hexdigest()


def producer_fixture(successful=('amd64',), run_id=10, attempt=2,
                     commit='a' * 40, contract_sha=CONTRACT_SHA):
    """A small exact-attempt API inventory, with distinguishable producer bytes."""
    requested = ['amd64', 'arm64']
    jobs = []
    for index, name in enumerate(['tests', 'prepare', 'build'] +
                                 [f'compile ({a})' for a in requested], 1):
        arch = name.removeprefix('compile (').removesuffix(')')
        jobs.append({
            'id': run_id * 100 + index, 'name': name, 'run_id': run_id,
            'run_attempt': attempt, 'head_sha': commit, 'status': 'completed',
            'conclusion': 'success' if index <= 3 or arch in successful else 'failure',
            'html_url': f'https://github.com/{matrix_contract.REPOSITORY}/actions/runs/{run_id}/job/{run_id * 100 + index}',
        })
    artifacts = []
    for arch in requested:
        if arch not in successful:
            continue
        data = f'archive {arch} from {run_id}/{attempt}'.encode()
        artifacts.append({
            'architecture': arch, 'file': f'1panel-{VERSION}-linux-{arch}.tar.gz',
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'build_repository_commit': commit,
            'resolved_contract_sha256': contract_sha,
        })
    manifest = {
        'schema_version': 2, 'version': VERSION, 'requested_architectures': requested,
        'producer_run_id': run_id, 'producer_run_attempt': attempt,
        'producer_head_sha': commit,
        'outcomes': matrix_contract.collect(jobs, requested, run_id, attempt),
        'artifacts': artifacts,
    }
    run = {
        'event': 'schedule', 'id': run_id, 'run_attempt': attempt, 'head_sha': commit,
        'status': 'completed',
        'conclusion': 'success' if len(artifacts) == len(requested) else 'failure',
        'path': '.github/workflows/build.yml',
    }
    return manifest, run, jobs


class PublicationLineageTests(unittest.TestCase):
    def test_new_partial_release_binds_exact_producer_rows(self):
        current, _, _ = producer_fixture()
        result = lineage.compose(None, current, CONTRACT_SHA)
        key = lineage.generation_id(current)
        self.assertEqual(lineage.validate(result, VERSION), ['amd64'])
        self.assertEqual(result['current_generation'], key)
        self.assertEqual(result['generations'], {key: current})
        self.assertEqual(result['artifact_generations'], {'amd64': key})
        self.assertEqual(result['artifacts'], current['artifacts'])

    def test_append_keeps_original_rows_even_when_current_rebuilds_them(self):
        previous, _, _ = producer_fixture()
        current, _, _ = producer_fixture(('amd64', 'arm64'), run_id=20, commit='b' * 40)
        previous_copy, current_copy = copy.deepcopy(previous), copy.deepcopy(current)
        result = lineage.compose(previous, current, CONTRACT_SHA)
        self.assertEqual(lineage.validate(result), ['amd64', 'arm64'])
        self.assertEqual(result['artifacts'], [previous['artifacts'][0], current['artifacts'][1]])
        self.assertNotEqual(result['artifacts'][0], current['artifacts'][0])
        self.assertEqual(result['artifact_generations'], {
            'amd64': lineage.generation_id(previous),
            'arm64': lineage.generation_id(current),
        })
        self.assertEqual(result['current_generation'], lineage.generation_id(current))
        self.assertEqual(previous, previous_copy)
        self.assertEqual(current, current_copy)

    def test_current_generation_is_retained_without_claiming_old_artifacts(self):
        original, _, _ = producer_fixture()
        older_current, _, _ = producer_fixture(run_id=20)
        latest, _, _ = producer_fixture(run_id=30)
        previous = lineage.compose(original, older_current, CONTRACT_SHA)
        result = lineage.compose(previous, latest, CONTRACT_SHA)
        self.assertEqual(result['artifacts'], original['artifacts'])
        self.assertEqual(result['current_generation'], lineage.generation_id(latest))
        self.assertEqual(result['artifact_generations'], {'amd64': lineage.generation_id(original)})
        self.assertEqual(set(result['generations']), {
            lineage.generation_id(original), lineage.generation_id(latest),
        })
        self.assertNotIn(lineage.generation_id(older_current), result['generations'])

    def test_generation_identity_is_stable_across_object_key_order(self):
        current, _, _ = producer_fixture()
        reordered = dict(reversed(list(current.items())))
        self.assertEqual(lineage.generation_id(current), lineage.generation_id(reordered))
        changed = copy.deepcopy(current)
        changed['artifacts'][0]['sha256'] = 'f' * 64
        self.assertNotEqual(lineage.generation_id(current), lineage.generation_id(changed))

    def test_mixed_source_contract_rejected_for_legacy_and_lineage_inputs(self):
        previous, _, _ = producer_fixture()
        current, _, _ = producer_fixture(('arm64',), run_id=20, contract_sha='f' * 64)
        for old in (previous, lineage.compose(None, previous, CONTRACT_SHA)):
            with self.subTest(schema=old['schema_version']), self.assertRaises(ValueError):
                lineage.compose(old, current, 'f' * 64)
        with self.assertRaises(ValueError):
            lineage.compose(None, previous, 'f' * 64)

    def test_version_and_requested_inventory_changes_require_repair(self):
        previous, _, _ = producer_fixture()
        for mutation in ('version', 'requested'):
            current, _, _ = producer_fixture(('arm64',), run_id=20)
            if mutation == 'version':
                current['version'] = 'v2.99.1'
            else:
                current['requested_architectures'].reverse()
                current['outcomes'].reverse()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                lineage.compose(previous, current, CONTRACT_SHA)

    def test_lineage_mutations_cannot_forge_selection_or_producer_rows(self):
        previous, _, _ = producer_fixture()
        current, _, _ = producer_fixture(('amd64', 'arm64'), run_id=20)
        valid = lineage.compose(previous, current, CONTRACT_SHA)
        cases = (
            'extra-field', 'schema', 'version', 'contract', 'current-missing',
            'current-type', 'generation-key', 'producer-attempt', 'producer-job',
            'generation-requested', 'row-contract', 'selected-hash', 'selected-commit',
            'selected-file', 'selected-origin', 'selected-missing', 'selected-extra',
            'row-missing', 'row-duplicate', 'row-order', 'unreferenced', 'nested-lineage',
        )
        for case in cases:
            value = copy.deepcopy(valid)
            # Persisted JSON has no aliases between the selected rows and the
            # producer rows; mutation tests must preserve that separation.
            value['artifacts'] = copy.deepcopy(value['artifacts'])
            old_key = lineage.generation_id(previous)
            current_key = lineage.generation_id(current)
            if case == 'extra-field': value['untrusted'] = True
            elif case == 'schema': value['schema_version'] = 2
            elif case == 'version': value['version'] = 'v2.99.1'
            elif case == 'contract': value['resolved_contract_sha256'] = 'not-a-hash'
            elif case == 'current-missing': value['current_generation'] = 'f' * 64
            elif case == 'current-type': value['current_generation'] = None
            elif case == 'generation-key': value['generations']['f' * 64] = value['generations'].pop(current_key)
            elif case == 'producer-attempt': value['generations'][old_key]['producer_run_attempt'] += 1
            elif case == 'producer-job': value['generations'][old_key]['outcomes'][0]['job_id'] += 1
            elif case == 'generation-requested': value['generations'][old_key]['requested_architectures'].reverse()
            elif case == 'row-contract': value['generations'][old_key]['artifacts'][0]['resolved_contract_sha256'] = 'f' * 64
            elif case == 'selected-hash': value['artifacts'][0]['sha256'] = 'f' * 64
            elif case == 'selected-commit': value['artifacts'][0]['build_repository_commit'] = 'f' * 40
            elif case == 'selected-file': value['artifacts'][0]['file'] = 'different.tar.gz'
            elif case == 'selected-origin': value['artifact_generations']['amd64'] = current_key
            elif case == 'selected-missing': del value['artifact_generations']['amd64']
            elif case == 'selected-extra': value['artifact_generations']['riscv64'] = current_key
            elif case == 'row-missing': value['artifacts'].pop()
            elif case == 'row-duplicate': value['artifacts'].append(copy.deepcopy(value['artifacts'][0]))
            elif case == 'row-order': value['artifacts'].reverse()
            elif case == 'unreferenced':
                extra, _, _ = producer_fixture(run_id=30)
                value['generations'][lineage.generation_id(extra)] = extra
            elif case == 'nested-lineage': value['generations'][old_key] = copy.deepcopy(valid)
            with self.subTest(case=case), self.assertRaises(ValueError):
                lineage.validate(value, VERSION)


    def test_zero_success_current_generation_cannot_hide_execution_identity(self):
        previous,_,_=producer_fixture(('amd64','arm64'))
        empty,_,_=producer_fixture((),run_id=20,commit='b'*40)
        with self.assertRaisesRegex(ValueError,'successful row'):
            lineage.compose(previous,empty,CONTRACT_SHA)


if __name__ == '__main__':
    unittest.main()
