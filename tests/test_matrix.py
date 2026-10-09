import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_release
import aggregate_shards
import validate_artifacts

ROOT = Path(__file__).resolve().parents[1]

class MatrixTests(unittest.TestCase):
    setUp = test_release.ReleaseTests.setUp
    binaries = test_release.ReleaseTests.binaries
    build = test_release.ReleaseTests.build

    def shards(self, arches):
        inputs = b'SOURCE_COMMIT=synthetic\n'
        shards = self.root/'shards'
        shards.mkdir()
        for arch in arches:
            self.build([arch])
            (self.root/'dist/build-inputs.env').write_bytes(inputs)
            shutil.move(self.root/'dist', shards/f'shard-{arch}-123-1')
        return shards

    def aggregate(self, shards, arches):
        with patch.object(aggregate_shards.subprocess,'check_output',return_value=b'SOURCE_COMMIT=synthetic\n'):
            aggregate_shards.aggregate(shards, self.root/'result/dist', 'v2.3.2', arches, '123-1')

    def test_full_matrix_validates_fourteen_binaries(self):
        arches = list(validate_artifacts.ARCHES)
        shards = self.shards(arches)
        with patch.object(validate_artifacts, 'validate_binary', wraps=validate_artifacts.validate_binary) as check:
            self.aggregate(shards, arches)
            # Every binary checked at both shard and aggregate stages.
            self.assertEqual(check.call_count, 28)
        self.assertEqual(len(validate_artifacts.validate_dist(self.root/'result/dist', 'v2.3.2', arches)), 7)

    def test_missing_extra_and_retry_shard_fail_closed(self):
        for name in ['shard-arm64-123-1', 'shard-amd64-123-2']:
            with self.subTest(name=name):
                shards = self.shards(['amd64'])
                (shards/name).mkdir()
                with self.assertRaisesRegex(ValueError, 'unexpected architecture shards'):
                    self.aggregate(shards, ['amd64'])
                shutil.rmtree(shards)
        shards = self.shards(['amd64'])
        with self.assertRaisesRegex(ValueError, 'unexpected architecture shards'):
            self.aggregate(shards, ['amd64', 'arm64'])

    def test_other_build_commit_rejected(self):
        shards = self.shards(['amd64'])
        with patch.dict(os.environ, BUILD_REPOSITORY_COMMIT='b'*40):
            with self.assertRaisesRegex(ValueError, 'build commit mismatch'):
                self.aggregate(shards, ['amd64'])
        self.assertFalse((self.root/'result/dist').exists())

    def test_mismatched_inputs_rejected(self):
        shards = self.shards(['amd64'])
        (shards/'shard-amd64-123-1/build-inputs.env').write_text('wrong')
        with self.assertRaisesRegex(ValueError, 'resolved inputs mismatch'):
            self.aggregate(shards, ['amd64'])

    def test_corrupted_shard_rejected_before_output(self):
        shards = self.shards(['amd64'])
        next(shards.rglob('*.sha256')).write_text('wrong')
        with self.assertRaises(ValueError):
            self.aggregate(shards, ['amd64'])
        self.assertFalse((self.root/'result/dist').exists())

    def test_failed_branch_omitted_with_explicit_outcome(self):
        import json
        shards=self.shards(['amd64','arm64'])
        # A failed-attempt branch may leave an earlier shard; it must never be promoted.
        outcomes=[{'architecture':a,'status':'success' if a=='amd64' else 'failure',
                   'stage':'compile','reason':'' if a=='amd64' else 'GitHub job failure',
                   'job_id':i,'job_url':f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123/job/{i}'}
                  for i,a in enumerate(['amd64','arm64'],1)]
        with patch.object(aggregate_shards.subprocess,'check_output',return_value=b'SOURCE_COMMIT=synthetic\n'),patch.dict(os.environ,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',PRODUCER_HEAD_SHA='a'*40):
            aggregate_shards.aggregate(shards,self.root/'result/dist','v2.3.2',['amd64','arm64'],'123-1',outcomes)
        manifest=json.loads((self.root/'result/dist/build-manifest.json').read_text())
        self.assertEqual(manifest['requested_architectures'],['amd64','arm64'])
        self.assertEqual([r['architecture'] for r in manifest['artifacts']],['amd64'])
        self.assertFalse((self.root/'result/dist/1panel-v2.3.2-linux-arm64.tar.gz').exists())
        self.assertEqual(len(validate_artifacts.validate_dist(self.root/'result/dist','v2.3.2',['amd64'])),1)

    def test_workflow_gates_and_pinned_cache(self):
        workflow = (ROOT/'.github/workflows/build.yml').read_text()
        self.assertIn('max-parallel: 3', workflow)
        self.assertIn('fail-fast: false', workflow)
        self.assertIn('needs: [prepare, compile]', workflow)
        self.assertIn('candidate:\n    needs: build', workflow)
        self.assertIn('needs: publication_prepare', workflow)
        self.assertNotIn('restore-keys:', workflow)
        self.assertIn("${{ matrix.arch }}-${{ hashFiles('config/**', 'scripts/**'", workflow)
        self.assertIn('go${{ needs.prepare.outputs.go_version }}', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        build_section = workflow.split('  prepare:\n')[1].split('  candidate:\n')[0]
        self.assertNotIn('contents: write', build_section)

    def test_shard_transport_names_bind_the_prepared_generation(self):
        workflow = (ROOT/'.github/workflows/build.yml').read_text()
        prepare = workflow.split('  prepare:\n')[1].split('  compile:\n')[0]
        compile_job = workflow.split('  compile:\n')[1].split('  build:\n')[0]
        aggregate_job = workflow.split('  build:\n')[1].split('  candidate:\n')[0]
        self.assertIn('artifact_id: ${{ steps.source_upload.outputs.artifact-id }}', prepare)
        self.assertIn('artifact-ids: ${{ needs.prepare.outputs.artifact_id }}', compile_job)
        self.assertNotIn('github.run_attempt', compile_job)
        self.assertNotIn('github.run_attempt', aggregate_job)
        self.assertIn('overwrite: true', compile_job)
        self.assertNotIn('overwrite: true', aggregate_job)
        # Transfer names retain the prepared-source generation. This does not
        # assert that API provenance accepts inherited jobs from another attempt.
        producer = {'artifact_id': '1'}
        def render(value, attempt):
            return (value.replace('${{ github.run_id }}', '123')
                    .replace('${{ needs.prepare.outputs.artifact_id }}', producer['artifact_id'])
                    .replace('${{ github.run_attempt }}', str(attempt))
                    .replace('${{ matrix.arch }}', 'amd64'))
        name = next(line.split('name: ', 1)[1] for line in compile_job.splitlines() if 'name: shard-' in line)
        pattern = next(line.split('pattern: ', 1)[1] for line in aggregate_job.splitlines() if 'pattern: shard-' in line)
        import fnmatch
        self.assertEqual(render(name, 2), 'shard-amd64-123-1')
        self.assertTrue(fnmatch.fnmatch(render(name, 2), render(pattern, 3)))
        shards = self.shards(['amd64'])
        self.aggregate(shards, ['amd64'])
        producer['artifact_id'] = '4'
        self.assertFalse(fnmatch.fnmatch('shard-amd64-123-1', render(pattern, 4)))
