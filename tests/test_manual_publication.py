import hashlib,io,json,os,subprocess,sys,tempfile,unittest,zipfile
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
   with patch.object(manual,'github_json',return_value=dict(run,conclusion='cancelled')):
    with self.assertRaises(ValueError):manual.fetch_ci_bundle(root/'bad','v2.3.2','10','22',sha,commit,'upstream-matrix')
 def test_wrong_version_tag_or_repository_rejected(self):
  for version,tag,repo in [('v2.3.2','v2.3.1',manual.REPOS['upstream-matrix']),('../../escape','../../escape',manual.REPOS['upstream-matrix']),('v2.3.2','v2.3.2','other/repo')]:
   with self.assertRaises(ValueError):manual.check_identity(version,tag,repo)
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
 def test_prepare_validates_the_final_payload_once_with_or_without_migration(self):
  for migrated in (False,True):
   with self.subTest(migrated=migrated),tempfile.TemporaryDirectory() as t:
    work=Path(t)/'work'
    args=SimpleNamespace(version='v2.3.2',tag='v2.3.2',repository=manual.UPSTREAM,work=work,
                         upstream_source='verified-ci',run_id='10',artifact_id='22',artifact_sha256='a'*64,build_commit='b'*40)
    def fetch(directory,*unused):
     directory.mkdir();(directory/'payload').write_bytes(b'verified CI bytes')
     return {'build_repository_commit':args.build_commit}
    with patch.object(manual,'fetch_ci_bundle',side_effect=fetch),\
         patch('resolved_contract.activate_release'),patch('package_metadata.embed_legacy',return_value=migrated),\
         patch.object(manual,'validate_payloads',side_effect=lambda directory,*unused:list(directory.iterdir())) as validate,\
         patch.object(manual,'make_proof',return_value={}),\
         patch.dict(os.environ,GITHUB_RUN_ID='50',GITHUB_SHA='c'*40),patch('sys.stdout',new_callable=io.StringIO):
     manual.prepare(args)
    validate.assert_called_once_with(work/'release','upstream-matrix',args.version)
    self.assertEqual((work/'release/payload').read_bytes(),b'verified CI bytes')

if __name__=='__main__':unittest.main()
