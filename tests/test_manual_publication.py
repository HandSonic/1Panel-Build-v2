import hashlib,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import manual_publication as manual

class ManualPublicationTests(unittest.TestCase):
 def test_push_and_validate_only_cannot_execute_publication(self):
  with tempfile.TemporaryDirectory() as t:
   for event,operation in [('push','promote-existing'),('schedule','promote-existing'),('pull_request','promote-existing'),('workflow_dispatch','validate-promotion')]:
    env=dict(os.environ,GITHUB_EVENT_NAME=event,PUBLICATION_OPERATION=operation)
    result=subprocess.run([sys.executable,str(ROOT/'scripts/manual_publication.py'),'publish','--repository',manual.REPOS['upstream-matrix'],'--version','v2.3.2','--tag','v2.3.2','--work',t],env=env,capture_output=True,text=True)
    self.assertNotEqual(result.returncode,0);self.assertIn('explicitly selected manual',result.stderr)
 def test_zip_rejects_traversal_directories_and_symlinks(self):
  for name,mode in [('../escape',0),('/absolute',0),('nested/file',0),('./file',0),('file/.',0),('link',0o120777)]:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as t:
    root=Path(t);archive=root/'input.zip'
    with zipfile.ZipFile(archive,'w') as z:
     info=zipfile.ZipInfo(name);info.external_attr=mode<<16;z.writestr(info,b'content')
    with self.assertRaises(ValueError):manual.extract_verified_zip(archive,root/'output')
    self.assertFalse((root/'output').exists())
 def test_selected_ci_identity_and_byte_hash_are_bound(self):
  from test_matrix_contract import MatrixContractTests
  manifest,run,jobs=MatrixContractTests().fixture();manifest['version']='v2.3.2'
  for row in manifest['artifacts']:row['build_repository_commit']='a'*40
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'original.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('build-manifest.json',json.dumps(manifest))
   data=archive.read_bytes();sha=hashlib.sha256(data).hexdigest();commit='a'*40
   asset={'expired':False,'workflow_run':{'id':10},'name':f'verified-1panel-v2.3.2-{commit}','digest':'sha256:'+sha,'size_in_bytes':len(data)}
   def download(args,**kwargs):kwargs['stdout'].write(data);return SimpleNamespace(returncode=0)
   for suffix in ('','-2','-3'):
    selected=dict(asset,name=asset['name']+suffix)
    with patch.object(manual,'github_json',side_effect=[run,selected,{'jobs':jobs,'total_count':len(jobs)},run]),patch.object(manual.subprocess,'run',side_effect=download):
     if suffix=='-3':
      with self.assertRaisesRegex(ValueError,'producer attempt'):
       manual.fetch_ci_bundle(root/'mismatch','v2.3.2','10','22',sha,commit,'upstream-matrix')
     else:
      result=manual.fetch_ci_bundle(root/('input'+suffix),'v2.3.2','10','22',sha,commit,'upstream-matrix')
      self.assertEqual(result['build_repository_commit'],commit)
   for invalid in (dict(run,id=11),dict(run,path='.github/workflows/other.yml')):
    with patch.object(manual,'github_json',return_value=invalid):
     with self.assertRaisesRegex(ValueError,'Unexpected upstream workflow'):
      manual.fetch_ci_bundle(root/'bad','v2.3.2','10','22',sha,commit,'upstream-matrix')
 def test_wrong_version_tag_or_repository_rejected(self):
  for version,tag,repo in [('v2.3.2','v2.3.1',manual.REPOS['upstream-matrix']),('../../escape','../../escape',manual.REPOS['upstream-matrix']),('v2.3.2','v2.3.2','other/repo')]:
   with self.assertRaises(ValueError):manual.check_identity(version,tag,repo)
 def test_merged_pr_bundle_uses_the_exact_successful_checkout_log(self):
  from test_matrix_contract import MergedPRMatrixTests
  manifest,run,jobs,commit,pr,lines=MergedPRMatrixTests().fixture()
  manifest['version']='v2.4.1'
  for row in manifest['artifacts']:row['build_repository_commit']=commit['sha']
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);archive=root/'original.zip'
   with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('build-manifest.json',json.dumps(manifest))
   data=archive.read_bytes();sha=hashlib.sha256(data).hexdigest()
   metadata={'expired':False,'workflow_run':{'id':10},'name':f'verified-1panel-v2.4.1-{commit["sha"]}-2',
             'digest':'sha256:'+sha,'size_in_bytes':len(data)}
   def api(path):
    responses={f'repos/{manual.UPSTREAM}/actions/runs/10':run,
               f'repos/{manual.UPSTREAM}/actions/artifacts/22':metadata,
               f'repos/{manual.UPSTREAM}/actions/runs/10/jobs?filter=all&per_page=100&page=1':{'jobs':jobs,'total_count':len(jobs)},
               f'repos/{manual.UPSTREAM}/actions/runs/10/attempts/2':run,
               f'repos/{manual.UPSTREAM}/git/commits/{commit["sha"]}':commit,
               f'repos/{manual.UPSTREAM}/pulls/28':pr}
    return responses[path]
   def download(command,**kwargs):kwargs['stdout'].write(data);return SimpleNamespace(returncode=0)
   with patch.object(manual,'github_json',side_effect=api),patch.object(manual.subprocess,'run',side_effect=download),\
        patch.object(manual,'github_job_log',return_value='\n'.join(lines)+'\n') as logs:
    result=manual.fetch_ci_bundle(root/'input','v2.4.1','10','22',sha,commit['sha'],'upstream-matrix')
   self.assertEqual(result['build_repository_commit'],commit['sha']);logs.assert_called_once_with(3)
 def test_checkout_log_transport_only_retries_the_gh_terminal_control_guard(self):
  guarded=subprocess.CalledProcessError(1,['gh'],stderr='the response contains terminal escape sequences')
  with patch.object(manual.subprocess,'run',side_effect=[guarded,SimpleNamespace(stdout='checkout log')]) as request:
   self.assertEqual(manual.github_job_log(3),'checkout log')
  self.assertEqual(request.call_count,2)
  self.assertIn('--allow-escape-sequences',request.call_args_list[1].args[0])
  with patch.object(manual.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['gh'],stderr='HTTP 403')) as request:
   with self.assertRaises(subprocess.CalledProcessError):manual.github_job_log(3)
  self.assertEqual(request.call_count,1)
 def test_proof_retry_keeps_original_attempt_and_rejects_future_identity(self):
  import publication_contract as contract
  from test_matrix_contract import MatrixContractTests
  manifest,_,_=MatrixContractTests().fixture()
  with tempfile.TemporaryDirectory() as t:
   path=Path(t)/'build-manifest.json';path.write_text(json.dumps(manifest))
   arguments=([path],'upstream-matrix',manifest['version'],manifest['version'],manual.UPSTREAM,'10','a'*40)
   with patch.object(contract,'policy_fingerprint',return_value='f'*64),patch.dict(os.environ,GITHUB_RUN_ATTEMPT='2'):
    proof=contract.make_proof(*arguments)
   with patch.object(contract,'policy_fingerprint',return_value='f'*64),patch.dict(os.environ,GITHUB_RUN_ATTEMPT='3'):
    self.assertEqual(contract.make_proof(*arguments,workflow_run_attempt=proof['workflow_run_attempt']),proof)
    for invalid in (0,True,4):
     with self.subTest(attempt=invalid),self.assertRaises(ValueError):
      contract.make_proof(*arguments,workflow_run_attempt=invalid)
 def test_zip_checks_crc_during_its_only_extraction(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'input.zip';body=b'CRC payload'
   with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('payload',body)
   with patch.object(zipfile.ZipFile,'testzip',side_effect=AssertionError('Do not decompress the ZIP twice')):
    manual.extract_verified_zip(archive,root/'valid')
    self.assertEqual((root/'valid/payload').read_bytes(),body)
    raw=bytearray(archive.read_bytes());raw[raw.index(body)]^=1;archive.write_bytes(raw)
    with self.assertRaises(zipfile.BadZipFile):manual.extract_verified_zip(archive,root/'corrupt')
if __name__=='__main__':unittest.main()
