import copy
import hashlib
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
    def test_lineage_mutations_cannot_forge_selection_or_producer_rows(self):
        previous, _, _ = producer_fixture()
        current, _, _ = producer_fixture(('amd64', 'arm64'), run_id=20)
        old_key = lineage.generation_id(previous)
        current_key = lineage.generation_id(current)
        valid = {'schema_version': 3, 'version': VERSION,
                 'requested_architectures': current['requested_architectures'],
                 'resolved_contract_sha256': CONTRACT_SHA, 'current_generation': current_key,
                 'generations': {old_key: previous, current_key: current},
                 'artifact_generations': {'amd64': old_key, 'arm64': current_key},
                 'artifacts': copy.deepcopy([previous['artifacts'][0], current['artifacts'][1]])}
        self.assertEqual(lineage.validate(valid, VERSION), ['amd64', 'arm64'])
        cases = (
            'current-missing', 'producer-attempt', 'producer-job', 'row-contract',
            'selected-hash', 'selected-commit', 'selected-origin', 'row-missing',
        )
        for case in cases:
            value = copy.deepcopy(valid)
            if case == 'current-missing': value['current_generation'] = 'f' * 64
            elif case == 'producer-attempt': value['generations'][old_key]['producer_run_attempt'] += 1
            elif case == 'producer-job': value['generations'][old_key]['outcomes'][0]['job_id'] += 1
            elif case == 'row-contract': value['generations'][old_key]['artifacts'][0]['resolved_contract_sha256'] = 'f' * 64
            elif case == 'selected-hash': value['artifacts'][0]['sha256'] = 'f' * 64
            elif case == 'selected-commit': value['artifacts'][0]['build_repository_commit'] = 'f' * 40
            elif case == 'selected-origin': value['artifact_generations']['amd64'] = current_key
            elif case == 'row-missing': value['artifacts'].pop()
            with self.subTest(case=case), self.assertRaises(ValueError):
                lineage.validate(value, VERSION)

if __name__ == '__main__':
    unittest.main()
