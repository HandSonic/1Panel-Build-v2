"""Static/shell execution contract checks; no Docker, network or remote writes."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from workflow_helpers import ROOT, github_jobs, step_by_id
class CIContract(unittest.TestCase):
 def test_package_failure_reports_without_masking_infrastructure_failures(self):
  job=github_jobs()['compile']
  package=step_by_id(job,'package')
  record=step_by_id(job,'outcome_record')
  upload=step_by_id(job,'shard_upload')
  self.assertFalse(job.get('continue-on-error',False));self.assertTrue(package.get('continue-on-error',False))
  for step in job['steps']:
   if step is not package:self.assertFalse(step.get('continue-on-error',False))
  self.assertFalse(job['strategy']['fail-fast'])
  self.assertLess(job['steps'].index(package),job['steps'].index(record))
  self.assertLess(job['steps'].index(record),job['steps'].index(upload))
  self.assertEqual(record['env']['BRANCH_RESULT'],'${{ steps.package.outcome }}')
  self.assertEqual(job['env']['PRODUCER_HEAD_SHA'],'${{ github.event.pull_request.head.sha || github.sha }}')
  self.assertEqual(job['env']['SHARD_GENERATION'],'${{ github.run_id }}-${{ needs.prepare.outputs.artifact_id }}')
  for result,cancelled,record_result,expected_record,expected_upload in (
    ('success',False,'success',True,True),('failure',False,'success',True,True),
    ('skipped',False,'success',True,True),('failure',False,'failure',True,False),
    ('success',True,'skipped',False,False),('failure',True,'skipped',False,False)):
   def runs(step):
    code=step.get('if','success()').removeprefix('${{').removesuffix('}}').strip()
    if not any(name+'()' in code for name in ('always','success','failure','cancelled')):code='success() && ('+code+')'
    code=code.replace('success()',str(result in ('success','failure'))).replace('cancelled()',str(cancelled))
    code=code.replace('steps.outcome_record.outcome',repr(record_result))
    return eval(code.replace('!',' not ').replace('&&',' and '),{'__builtins__':{}})
   with self.subTest(result=result,cancelled=cancelled,record_result=record_result):
    self.assertEqual(runs(record),expected_record);self.assertEqual(runs(upload),expected_upload)
 def test_partial_matrix_stays_red_after_successful_artifacts_can_be_published(self):
  jobs=github_jobs()
  status=jobs['matrix_status']
  self.assertEqual(set(status['needs']),{'build','candidate','automatic_publication','publication_repair'})
  self.assertEqual(jobs['build']['outputs']['complete'],'${{ steps.aggregate.outputs.complete }}')
  self.assertEqual(status['env']['MATRIX_COMPLETE'],'${{ needs.build.outputs.complete }}')
  step_by_id(jobs['build'],'aggregate')
  condition=status['if'].removeprefix('${{').removesuffix('}}').strip()
  for result,cancelled,expected in (('success',False,True),('failure',False,False),
                                  ('skipped',False,False),('success',True,False)):
   code=condition.replace('cancelled()',str(cancelled)).replace('needs.build.result',repr(result))
   with self.subTest(result=result,cancelled=cancelled):
    self.assertEqual(eval(code.replace('!',' not ').replace('&&',' and '),{'__builtins__':{}}),expected)
  for complete,expected in (('true',0),('false',1),('',1)):
   result=subprocess.run(['bash','-c',step_by_id(status,'report')['run']],env=dict(os.environ,MATRIX_COMPLETE=complete),capture_output=True,text=True)
   with self.subTest(complete=complete):self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
 def test_record_step_passes_exact_branch_identity(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'bin').mkdir()
   python=root/'bin/python3'
   python.write_text('#!'+sys.executable+'\nimport json,os,pathlib,sys\npathlib.Path(os.environ["ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))\n')
   python.chmod(0o755)
   env=dict(os.environ,PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],
            VERSION='v2.3.2',TARGET_ARCHES='amd64',SHARD_GENERATION='123-456',
            BRANCH_RESULT='failure',ARGUMENTS=str(root/'arguments'))
   result=subprocess.run(['bash','-c',step_by_id(github_jobs()['compile'],'outcome_record')['run']],
                         cwd=root,env=env,capture_output=True,text=True)
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual(json.loads((root/'arguments').read_text()),
                    ['scripts/matrix_contract.py','record','prepared-source/dist','v2.3.2','amd64','123-456','failure'])
 def test_unsupported_arch_stops_before_resolution(self):
  with tempfile.TemporaryDirectory() as d:
   r=subprocess.run(['bash','-c',step_by_id(github_jobs()['prepare'],'inputs')['run']],cwd=d,env=dict(os.environ,PYTHONPATH=str(ROOT/'scripts'),INPUT_VERSION='v2.3.2',INPUT_ARCHES='amd64 wrongarch',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/main'),capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0);self.assertFalse((Path(d)/'build-inputs.env').exists())
 def test_tag_selected_not_latest_fallback(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'config').mkdir();(p/'config/sources.json').write_text('{"v2.3.2":{}}');(p/'scripts').mkdir();(p/'scripts/resolve_inputs.py').write_text('import sys; print("SOURCE_COMMIT="+sys.argv[1]+"\\nGO_VERSION=1.26.1")')
   (p/'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()')
   (p/'scripts/discover_inputs.py').write_text('import sys; assert \"--latest\" not in sys.argv; print(\"a\"*64)')
   (p/'scripts/resolved_contract.py').write_text('')
   e=dict(os.environ,INPUT_VERSION='',INPUT_ARCHES='amd64',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/tags/v2.3.2',GITHUB_REF_NAME='v2.3.2',GITHUB_ENV=str(p/'env'),GITHUB_OUTPUT=str(p/'out'))
   r=subprocess.run(['bash','-c',step_by_id(github_jobs()['prepare'],'inputs')['run']],cwd=d,env=e,capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('SOURCE_COMMIT=v2.3.2',(p/'build-inputs.env').read_text())
if __name__=='__main__':unittest.main()
