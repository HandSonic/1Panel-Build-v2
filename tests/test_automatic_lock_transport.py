import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import aggregate_shards
import automatic_frontend_lock as automatic
import discover_inputs
import package_release
import package_metadata
import resolved_contract
import validate_artifacts
from semantic_configuration import production
from test_automatic_frontend_lock import contract_fixture
from test_discovered_inputs import FakeVendor
import test_matrix
import test_release


class AutomaticLockTransportTests(unittest.TestCase):
    binaries = test_release.ReleaseTests.binaries
    build = test_release.ReleaseTests.build
    shards = test_matrix.MatrixTests.shards
    aggregate = test_matrix.MatrixTests.aggregate

    def setUp(self):
        test_release.ReleaseTests.setUp(self)
        self.inputs = self.root / 'input-checkout'
        self.config = self.inputs / 'config'
        self.config.mkdir(parents=True)
        self.contract, self.lock = contract_fixture()
        self.contract.update(version='v2.3.2')
        self.contract['source']['commit'] = self.entry['source_commit']
        self.contract['installer'].update(commit=self.entry['installer_commit'], original_version='v2.3.2')
        self.contract['installer']['resources'] = {name: discover_inputs.facts((self.root / name).read_bytes())
                                                    for name in self.entry['installer_sha256']}
        self.contract['toolchain'] = {tool: self.entry[tool + '_version'] for tool in ('go', 'node', 'npm')}
        self.contract['resources']['geoip'] = dict(url=self.entry['geoip_url'],
                                                  **discover_inputs.facts((self.root / 'GeoIP.mmdb').read_bytes()))
        for component, raw in self.configs.items():
            path = f'{component}/cmd/server/conf/app.yaml'
            facts = discover_inputs.facts(raw)
            self.contract['source']['files'][path] = facts
            self.contract['configuration'][component] = dict(path=path, source_sha256=facts['sha256'],
                                                             source_bytes=facts['bytes'],
                                                             normalized_sha256=automatic.sha(production(raw, 'v2.3.2', component, 'stable')))
        self.persist()
        for handle in (patch.object(package_release, 'CONFIG', self.config),
                       patch.object(aggregate_shards, 'INPUT_ROOT', self.inputs),
                       patch.dict(os.environ, RESOLVED_CONTRACT_SHA256=self.entry['resolved_contract_sha256'])):
            handle.start(); self.addCleanup(handle.stop)

    def persist(self):
        (self.config / 'resolved-source.json').write_bytes(resolved_contract.canonical(self.contract))
        if automatic.required_sidecars(self.contract):
            (self.config / automatic.SIDECAR).write_bytes(self.lock)
        elif (self.config / automatic.SIDECAR).exists():
            (self.config / automatic.SIDECAR).unlink()
        self.entry['resolved_contract_sha256'] = resolved_contract.digest(self.contract)

    def test_shards_and_final_dist_preserve_exact_generated_lock(self):
        arches = ['amd64', 'arm64']
        shards = self.shards(arches)
        for arch in arches:
            self.assertEqual((shards / f'shard-{arch}-123-1' / automatic.SIDECAR).read_bytes(), self.lock)
        with patch.object(automatic, 'generate', side_effect=AssertionError('Never resolve while aggregating')):
            self.aggregate(shards, arches)
        dist = self.root / 'result/dist'
        self.assertEqual((dist / automatic.SIDECAR).read_bytes(), self.lock)
        records = validate_artifacts.validate_dist(dist, 'v2.3.2', arches)
        self.assertEqual(len(records), len(arches))
        self.assertEqual({row['resolved_contract_sha256'] for row in records}, {self.entry['resolved_contract_sha256']})
        self.assertEqual((dist / 'resolved-source.json').read_bytes(), resolved_contract.canonical(self.contract))

    def test_repreparing_the_same_legacy_artifact_resumes_a_partial_publication(self):
        from release_asset_repair import repair
        from test_release_repair import FakeGitHub
        self.build(['amd64'])
        dist = self.root / 'dist'
        archive = dist / '1panel-v2.3.2-linux-amd64.tar.gz'
        prefix = archive.name.removesuffix('.tar.gz') + '/'
        # Recreate the valid pre-metadata format from a complete package fixture.
        with tarfile.open(archive) as original:
            members = [(member, original.extractfile(member).read() if member.isfile() else None)
                       for member in original.getmembers()]
        with tarfile.open(archive, 'w:gz') as legacy:
            for member, body in members:
                if member.name.startswith(prefix + package_metadata.DIRECTORY + '/'):
                    continue
                if member.name == prefix + 'manifest.json':
                    manifest = json.loads(body)
                    manifest.pop('publication_format')
                    manifest['files'] = {name: facts for name, facts in manifest['files'].items()
                                         if not name.startswith(package_metadata.DIRECTORY + '/')}
                    body = resolved_contract.canonical(manifest)
                    member.size = len(body)
                legacy.addfile(member, io.BytesIO(body) if body is not None else None)
        summary = json.loads((dist / 'build-manifest.json').read_bytes())
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        summary['artifacts'][0].update(sha256=sha, size=archive.stat().st_size)
        (dist / 'build-manifest.json').write_bytes(resolved_contract.canonical(summary))
        line = sha + '  ' + archive.name + '\n'
        (dist / (archive.name + '.sha256')).write_text(line)
        (dist / 'checksums.txt').write_text(line)
        self.assertEqual(len(validate_artifacts.validate_dist(dist, 'v2.3.2', ['amd64'])), 1)
        prepared = []
        for index in range(2):
            target = self.root / f'prepared-{index}'
            shutil.copytree(dist, target)
            with patch('gzip.time.time', return_value=1700000000 + index):
                self.assertTrue(package_metadata.embed_legacy(target, 'v2.3.2'))
            self.assertEqual(len(validate_artifacts.validate_dist(target, 'v2.3.2', ['amd64'], require_embedded=True)), 1)
            prepared.append(target)
        self.assertEqual({path.name: path.read_bytes() for path in prepared[0].iterdir()},
                         {path.name: path.read_bytes() for path in prepared[1].iterdir()})
        client = FakeGitHub()
        client.assets = {1: (archive.name, (prepared[0] / archive.name).read_bytes())}
        files = [prepared[1] / (archive.name + suffix) for suffix in ('', '.sha256')]
        result = repair(client, files, self.root / 'recovery-journal.json')
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(client.calls, [('upload', archive.name + '.sha256')])
        self.assertEqual(client.assets[1][1], (prepared[0] / archive.name).read_bytes())

    def test_incomplete_or_tampered_shard_pair_fails_before_aggregate_output(self):
        shards = self.shards(['amd64', 'arm64'])
        shard = shards / 'shard-arm64-123-1'
        payload = shard / automatic.SIDECAR
        for case in ('missing', 'changed', 'symlink', 'directory', 'oversize', 'extra', 'contract'):
            with self.subTest(case=case):
                if case == 'missing': payload.unlink()
                if case == 'changed': payload.write_bytes(self.lock + b' ')
                if case == 'symlink': payload.unlink(); payload.symlink_to(self.config / automatic.SIDECAR)
                if case == 'directory': payload.unlink(); payload.mkdir()
                if case == 'oversize':
                    with payload.open('wb') as output: output.truncate(automatic.MAX_LOCK + 1)
                if case == 'extra': (shard / 'unrequested.json').write_bytes(b'{}')
                if case == 'contract':
                    changed = copy.deepcopy(self.contract); changed['source']['commit'] = 'f' * 40
                    (shard / 'resolved-source.json').write_bytes(resolved_contract.canonical(changed))
                with self.assertRaises(ValueError): self.aggregate(shards, ['amd64', 'arm64'])
                self.assertFalse((self.root / 'result/dist').exists())
                if payload.is_symlink(): payload.unlink()
                elif payload.is_dir(): payload.rmdir()
                payload.write_bytes(self.lock)
                (shard / 'unrequested.json').unlink(missing_ok=True)
                (shard / 'resolved-source.json').write_bytes(resolved_contract.canonical(self.contract))

    def test_final_dist_checks_payload_after_all_archives_still_validate(self):
        self.build(['amd64'])
        payload = self.root / 'dist' / automatic.SIDECAR
        payload.write_bytes(self.lock + b' ')
        with self.assertRaisesRegex(ValueError, 'lock bytes mismatch'):
            validate_artifacts.validate_dist(self.root / 'dist', 'v2.3.2', ['amd64'])

    def test_partial_matrix_uses_successful_authenticated_pair(self):
        shards = self.shards(['amd64', 'arm64'])
        (shards / 'shard-arm64-123-1' / automatic.SIDECAR).write_bytes(b'failed attempt leftovers')
        outcomes = [{'architecture': arch, 'status': 'success' if arch == 'amd64' else 'failure',
                     'stage': 'compile', 'reason': '' if arch == 'amd64' else 'GitHub job failure',
                     'job_id': index, 'job_url': f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123/job/{index}'}
                    for index, arch in enumerate(('amd64', 'arm64'), 1)]
        with patch.object(aggregate_shards.subprocess, 'check_output', return_value=b'SOURCE_COMMIT=synthetic\n'),\
             patch.dict(os.environ, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', PRODUCER_HEAD_SHA='a' * 40):
            aggregate_shards.aggregate(shards, self.root / 'result/dist', 'v2.3.2', ['amd64', 'arm64'], '123-1', outcomes)
        dist = self.root / 'result/dist'
        manifest = json.loads((dist / 'build-manifest.json').read_bytes())
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(manifest['requested_architectures'], ['amd64', 'arm64'])
        self.assertEqual(manifest['outcomes'], outcomes)
        self.assertEqual([row['architecture'] for row in manifest['artifacts']], ['amd64'])
        self.assertEqual((dist / automatic.SIDECAR).read_bytes(), self.lock)
        self.assertEqual(len(validate_artifacts.validate_dist(dist, 'v2.3.2', ['amd64'])), 1)

    def test_source_lock_contract_neither_emits_nor_accepts_automatic_sidecar(self):
        source = FakeVendor().files['frontend/package-lock.json']
        self.contract['source']['absent'] = []
        self.contract['source']['files']['frontend/package-lock.json'] = discover_inputs.facts(source)
        self.contract['frontend_lock'] = {'kind': 'source', 'sha256': automatic.sha(source),
                                          'manifest_sha256': self.contract['frontend_lock']['manifest_sha256']}
        self.persist()
        self.build(['amd64'])
        dist = self.root / 'dist'
        self.assertFalse((dist / automatic.SIDECAR).exists())
        (dist / automatic.SIDECAR).write_bytes(self.lock)
        with self.assertRaisesRegex(ValueError, 'Unexpected resolved'):
            validate_artifacts.validate_dist(dist, 'v2.3.2', ['amd64'])

    def test_finalize_refuses_invalid_cached_lock_before_transport_writes(self):
        self.binaries('amd64'); package_release.package(self.root, 'v2.3.2', 'amd64')
        (self.config / automatic.SIDECAR).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'lock bytes mismatch'):
            package_release.finalize(self.root, 'v2.3.2', ['amd64'])
        self.assertFalse((self.root / 'dist/resolved-source.json').exists())
        self.assertFalse((self.root / 'dist' / automatic.SIDECAR).exists())

    def test_lineage_inventory_keeps_same_lock_and_checks_actual_records(self):
        from publication_lineage import generation_id
        self.build(['amd64'])
        dist = self.root / 'dist'
        path = dist / 'build-manifest.json'
        current = json.loads(path.read_bytes())
        current.update(schema_version=2, requested_architectures=['amd64'], producer_run_id=123,
                       producer_run_attempt=1, producer_head_sha='a' * 40,
                       outcomes=[{'architecture': 'amd64', 'status': 'success', 'stage': 'compile',
                                  'reason': '', 'job_id': 1,
                                  'job_url': 'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123/job/1'}])
        for corrupt in (False, True):
            if corrupt:current['artifacts'][0]['sha256'] = '0' * 64
            key = generation_id(current)
            legacy = {'schema_version': 3, 'version': 'v2.3.2',
                      'requested_architectures': ['amd64'],
                      'resolved_contract_sha256': self.entry['resolved_contract_sha256'],
                      'current_generation': key, 'generations': {key: current},
                      'artifact_generations': {'amd64': key}, 'artifacts': current['artifacts']}
            path.write_text(json.dumps(legacy))
            if corrupt:
                with self.assertRaisesRegex(ValueError, 'Publication lineage manifest mismatch'):
                    validate_artifacts.validate_dist(dist, 'v2.3.2', ['amd64'])
            else:
                self.assertEqual(len(validate_artifacts.validate_dist(dist, 'v2.3.2', ['amd64'])), 1)
                self.assertEqual((dist / automatic.SIDECAR).read_bytes(), self.lock)


if __name__ == '__main__':
    unittest.main()
