import os
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import patch
import yaml
import test_release
import aggregate_shards
import matrix_contract
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


    def test_aggregate_parses_each_package_once(self):
        shards = self.shards(['amd64'])
        with patch.object(validate_artifacts, 'validate_package', wraps=validate_artifacts.validate_package) as parse:
            self.aggregate(shards, ['amd64'])
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(len(validate_artifacts.validate_dist(self.root/'result/dist', 'v2.3.2', ['amd64'])), 1)

    def test_changed_copy_fails_before_summary(self):
        shards = self.shards(['amd64'])
        copyfile = shutil.copyfile
        destination = self.root/'result/dist'
        for suffix in ('.tar.gz', '.sha256'):
            def changed_copy(source, target):
                result = copyfile(source, target)
                if str(target).endswith(suffix):
                    raw = Path(target).read_bytes()
                    Path(target).write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                return result
            with self.subTest(suffix=suffix), patch.object(aggregate_shards.shutil, 'copyfile', side_effect=changed_copy):
                with self.assertRaisesRegex(ValueError, 'Copied shard differs'):
                    self.aggregate(shards, ['amd64'])
            self.assertFalse((destination/'build-manifest.json').exists())
            shutil.rmtree(destination)

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

    def recorded_shards(self, failed=()):
        shards=self.shards(['amd64','arm64'])
        temporary=self.root/'recording/dist';temporary.parent.mkdir()
        with patch.dict(os.environ,GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',PRODUCER_HEAD_SHA='a'*40):
            for arch in ['amd64','arm64']:
                shard=shards/f'shard-{arch}-123-1'
                shard.rename(temporary)
                matrix_contract.record(temporary,'v2.3.2',arch,'123-1','failure' if arch in failed else 'success')
                temporary.rename(shard)
        return shards

    def recorded_jobs(self):
        return [{'id':i,'name':name,'run_id':123,'run_attempt':1,'head_sha':'a'*40,
                 'status':'queued','conclusion':None,
                 'html_url':f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123/job/{i}'}
                for i,name in enumerate(['compile (amd64)','compile (arm64)','build'],1)]

    def test_stale_api_status_does_not_discard_recorded_successful_shards(self):
        import json
        shards=self.recorded_shards();jobs=self.recorded_jobs()
        destination=self.root/'result/dist'
        env=dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',PRODUCER_HEAD_SHA='a'*40,
                 GITHUB_REPOSITORY='HandSonic/1Panel-Build-v2',MATRIX_RESULT='success',
                 GITHUB_OUTPUT=str(self.root/'output'),GITHUB_STEP_SUMMARY='')
        with patch.dict(os.environ,env),patch.object(matrix_contract.time,'sleep'),\
             patch.object(aggregate_shards.subprocess,'check_output',return_value=b'SOURCE_COMMIT=synthetic\n'),\
             patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs,'total_count':3}) as api:
            aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        manifest=json.loads((destination/'build-manifest.json').read_text())
        self.assertEqual([r['architecture'] for r in manifest['artifacts']],['amd64','arm64'])
        self.assertEqual(manifest['outcome_source'],'package-step')
        self.assertEqual(api.call_count,1)
        shutil.rmtree(destination)
        with patch.dict(os.environ,env),patch.object(matrix_contract.time,'sleep'),\
             patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs[:1]+jobs[2:],'total_count':2}) as api:
            with self.assertRaisesRegex(ValueError,'did not settle.*arm64'):
                aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        self.assertFalse(destination.exists())
        self.assertEqual(api.call_count,13)

    def test_partial_rerun_aggregates_original_report_when_api_copies_successful_job(self):
        import copy
        import json
        shards=self.recorded_shards();jobs=self.recorded_jobs()
        old=jobs[0]
        old.update(status='completed',conclusion='success',created_at='2026-10-12T10:54:44Z',
                   started_at='2026-10-12T10:54:46Z',completed_at='2026-10-12T10:55:58Z',
                   runner_id=1000013085,runner_name='GitHub Actions 1000013085',
                   runner_group_id=0,runner_group_name='GitHub Actions',labels=['ubuntu-latest'],
                   steps=[{'name':'Package','status':'completed','conclusion':'success'}])
        inherited=copy.deepcopy(old)
        inherited.update(id=88,run_attempt=2,created_at='2026-10-13T01:53:43Z',runner_group_id=None,
                         html_url='https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123/job/88')
        jobs.extend([inherited]);jobs[1]['run_attempt']=2;jobs[2]['run_attempt']=2
        report=shards/'shard-arm64-123-1'/matrix_contract.BRANCH_RESULT
        result=json.loads(report.read_text());result['producer_run_attempt']=2
        report.write_text(json.dumps(result))
        destination=self.root/'result/dist'
        env=dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2',PRODUCER_HEAD_SHA='a'*40,
                 GITHUB_REPOSITORY='HandSonic/1Panel-Build-v2',MATRIX_RESULT='success',
                 GITHUB_OUTPUT=str(self.root/'output'),GITHUB_STEP_SUMMARY='')
        with patch.dict(os.environ,env),\
             patch.object(aggregate_shards.subprocess,'check_output',return_value=b'SOURCE_COMMIT=synthetic\n'),\
             patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs,'total_count':len(jobs)}):
            aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        manifest=json.loads((destination/'build-manifest.json').read_text())
        self.assertEqual(manifest['producer_run_attempt'],2)
        self.assertEqual([row['job_id'] for row in manifest['outcomes']],[1,2])
        self.assertEqual((self.root/'output').read_text(),'complete=true\n')

    def test_recorded_partial_build_rejects_old_success_and_infrastructure_failure(self):
        import json
        shards=self.recorded_shards(failed=['arm64']);jobs=self.recorded_jobs()
        destination=self.root/'result/dist';output=self.root/'output'
        self.assertEqual({p.name for p in (shards/'shard-arm64-123-1').iterdir()},{matrix_contract.BRANCH_RESULT})
        env=dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',PRODUCER_HEAD_SHA='a'*40,
                 GITHUB_REPOSITORY='HandSonic/1Panel-Build-v2',MATRIX_RESULT='success',GITHUB_OUTPUT=str(output),GITHUB_STEP_SUMMARY='')
        with patch.dict(os.environ,env),\
             patch.object(aggregate_shards.subprocess,'check_output',return_value=b'SOURCE_COMMIT=synthetic\n'),\
             patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs,'total_count':3}):
            aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        manifest=json.loads((destination/'build-manifest.json').read_text())
        self.assertEqual([r['architecture'] for r in manifest['artifacts']],['amd64'])
        self.assertEqual(output.read_text(),'complete=false\n')
        shutil.rmtree(destination)
        for result in ('failure','cancelled'):
            with patch.dict(os.environ,env|{'MATRIX_RESULT':result}),self.assertRaisesRegex(ValueError,'infrastructure'):
                aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        jobs[0]['run_attempt']=2;jobs[2]['run_attempt']=2
        with patch.dict(os.environ,env|{'GITHUB_RUN_ATTEMPT':'2'}),\
             patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs,'total_count':3}),\
             self.assertRaisesRegex(ValueError,'older execution: amd64'):
            aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
        self.assertFalse(destination.exists())
        jobs[0]['run_attempt']=1;jobs[2]['run_attempt']=1
        path=shards/'shard-arm64-123-1'/matrix_contract.BRANCH_RESULT
        original=json.loads(path.read_text())
        for field,value in [('architecture','amd64'),('producer_run_id',124),
                            ('producer_run_attempt',2),('build_repository_commit','b'*40),
                            ('generation','123-2'),('reason',''),('status','unknown')]:
            path.write_text(json.dumps(original|{field:value}))
            with self.subTest(field=field),patch.dict(os.environ,env),\
                 patch.object(aggregate_shards,'github_json',return_value={'jobs':jobs,'total_count':3}),\
                 self.assertRaises(ValueError):
                aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])
            self.assertFalse(destination.exists())
        path.write_text(json.dumps(original))
        (path.parent/'leftover.tar.gz').write_bytes(b'old package')
        with patch.dict(os.environ,env),self.assertRaisesRegex(ValueError,'Failed architecture shard contains'):
            aggregate_shards.main([str(shards),str(destination),'v2.3.2','amd64 arm64','123-1'])

    def test_workflow_gates_and_pinned_cache(self):
        workflow = yaml.safe_load((ROOT/'.github/workflows/build.yml').read_text())
        jobs = workflow['jobs']
        cache = next(step['with'] for step in jobs['compile']['steps']
                     if step.get('uses') == 'actions/cache@v4')
        self.assertNotIn('restore-keys', cache)
        template = cache['key']
        dimensions = {'runner.os': 'Linux', 'needs.prepare.outputs.version': 'v2.3.2',
                      'needs.prepare.outputs.go_version': '1.26.1', 'matrix.arch': 'amd64'}
        def namespace(values):
            return re.sub(r'\$\{\{\s*([\w.]+)\s*\}\}',
                          lambda match: values[match[1]], template)
        before = namespace(dimensions)
        for dimension in dimensions:
            self.assertNotEqual(before, namespace(dimensions | {dimension: 'different'}), dimension)
        hash_args = re.search(r'hashFiles\((.*?)\)', template).group(1)
        self.assertLessEqual({
            'prepared-source/core/go.mod', 'prepared-source/core/go.sum',
            'prepared-source/agent/go.mod', 'prepared-source/agent/go.sum',
            'scripts/go_arch_compat.py',
        }, set(re.findall(r"'([^']+)'", hash_args)))

    def test_shard_transport_names_bind_the_prepared_generation(self):
        workflow = (ROOT/'.github/workflows/build.yml').read_text()
        prepare = workflow.split('  prepare:\n')[1].split('  compile:\n')[0]
        compile_job = workflow.split('  compile:\n')[1].split('  build:\n')[0]
        aggregate_job = workflow.split('  build:\n')[1].split('  candidate:\n')[0]
        self.assertIn('artifact_id: ${{ steps.source_upload.outputs.artifact-id }}', prepare)
        self.assertIn('artifact-ids: ${{ needs.prepare.outputs.artifact_id }}', compile_job)
        self.assertNotIn('github.run_attempt', compile_job)
        self.assertIn('name: verified-1panel-${{ needs.prepare.outputs.version }}-${{ github.sha }}-${{ github.run_attempt }}', aggregate_job)
        self.assertIn('overwrite: true', compile_job)
        self.assertNotIn('overwrite: true', aggregate_job)
        # Transfer names retain the prepared-source generation across retries.
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
        producer['artifact_id'] = '4'
        self.assertFalse(fnmatch.fnmatch('shard-amd64-123-1', render(pattern, 4)))
