"""Static/shell execution contract checks; no Docker, network or remote writes."""
import os,subprocess,tempfile,unittest
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
def github_block(name):
 s=(ROOT/'.github/workflows/build.yml').read_text().split('- name: '+name,1)[1].split('        run: |\n',1)[1]
 result=[]
 for line in s.splitlines():
  if line and not line.startswith('          '):break
  result.append(line[10:] if line.startswith('          ') else '')
 return '\n'.join(result)+'\n'
class CIContract(unittest.TestCase):
 def test_package_failure_reports_without_masking_infrastructure_failures(self):
  job=yaml.safe_load((ROOT/'.github/workflows/build.yml').read_text())['jobs']['compile']
  package=next(step for step in job['steps'] if step.get('id')=='package')
  record=next(step for step in job['steps'] if step.get('id')=='outcome_record')
  upload=next(step for step in job['steps'] if step.get('uses')=='actions/upload-artifact@v4')
  self.assertFalse(job.get('continue-on-error',False));self.assertTrue(package.get('continue-on-error',False))
  for step in job['steps']:
   if step is not package:self.assertFalse(step.get('continue-on-error',False))
  self.assertFalse(job['strategy']['fail-fast'])
  self.assertLess(job['steps'].index(package),job['steps'].index(record))
  self.assertLess(job['steps'].index(record),job['steps'].index(upload))
  self.assertEqual(record['env']['BRANCH_RESULT'],'${{ steps.package.outcome }}')
  self.assertIn('scripts/matrix_contract.py record prepared-source/dist "$VERSION" "$TARGET_ARCHES" "$SHARD_GENERATION" "$BRANCH_RESULT"',record['run'])
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
  jobs=yaml.safe_load((ROOT/'.github/workflows/build.yml').read_text())['jobs']
  status=jobs['matrix_status']
  self.assertEqual(set(status['needs']),{'build','candidate','automatic_publication','publication_repair'})
  self.assertEqual(jobs['build']['outputs']['complete'],'${{ steps.aggregate.outputs.complete }}')
  self.assertEqual(status['env']['MATRIX_COMPLETE'],'${{ needs.build.outputs.complete }}')
  self.assertTrue(any(step.get('id')=='aggregate' for step in jobs['build']['steps']))
  condition=status['if'].removeprefix('${{').removesuffix('}}').strip()
  for result,cancelled,expected in (('success',False,True),('failure',False,False),
                                  ('skipped',False,False),('success',True,False)):
   code=condition.replace('cancelled()',str(cancelled)).replace('needs.build.result',repr(result))
   with self.subTest(result=result,cancelled=cancelled):
    self.assertEqual(eval(code.replace('!',' not ').replace('&&',' and '),{'__builtins__':{}}),expected)
  for complete,expected in (('true',0),('false',1),('',1)):
   result=subprocess.run(['bash','-c',status['steps'][0]['run']],env=dict(os.environ,MATRIX_COMPLETE=complete),capture_output=True,text=True)
   with self.subTest(complete=complete):self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
 def test_unsupported_arch_stops_before_resolution(self):
  with tempfile.TemporaryDirectory() as d:
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=dict(os.environ,PYTHONPATH=str(ROOT/'scripts'),INPUT_VERSION='v2.3.2',INPUT_ARCHES='amd64 wrongarch',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/main'),capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0);self.assertFalse((Path(d)/'build-inputs.env').exists())
 def test_tag_selected_not_latest_fallback(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'config').mkdir();(p/'config/sources.json').write_text('{"v2.3.2":{}}');(p/'scripts').mkdir();(p/'scripts/resolve_inputs.py').write_text('import sys; print("SOURCE_COMMIT="+sys.argv[1]+"\\nGO_VERSION=1.26.1")')
   (p/'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()')
   (p/'scripts/discover_inputs.py').write_text('import sys; assert \"--latest\" not in sys.argv; print(\"a\"*64)')
   (p/'scripts/resolved_contract.py').write_text('')
   e=dict(os.environ,INPUT_VERSION='',INPUT_ARCHES='amd64',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/tags/v2.3.2',GITHUB_REF_NAME='v2.3.2',GITHUB_ENV=str(p/'env'),GITHUB_OUTPUT=str(p/'out'))
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=e,capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('SOURCE_COMMIT=v2.3.2',(p/'build-inputs.env').read_text())
if __name__=='__main__':unittest.main()
