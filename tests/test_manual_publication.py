import hashlib,json,os,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import manual_publication as manual

class ManualPublicationTests(unittest.TestCase):
 def test_existing_entry_manual_write_gate_and_readonly_prepare(self):
  name='build-offline-v2.yml' if (ROOT/'.github/workflows/build-offline-v2.yml').exists() else 'build.yml'
  text=(ROOT/'.github/workflows'/name).read_text()
  prepare=text.split('  publication_prepare:',1)[1].split('  publication_repair:',1)[0]
  repair=text.split('  publication_repair:',1)[1]
  self.assertIn('contents: read',prepare);self.assertNotIn('contents: write',prepare)
  expected='repair-existing' if name.startswith('build-offline') else 'promote-existing'
  gate=next(line.strip() for line in repair.splitlines() if line.strip().startswith('if:'))
  self.assertEqual(gate,f"if: github.event_name == 'workflow_dispatch' && inputs.operation == '{expected}'")
  self.assertIn('EXPECTED_VALIDATION_RECEIPT_SHA256',repair)
 def test_push_and_validate_only_cannot_execute_publication(self):
  with tempfile.TemporaryDirectory() as t:
   for event,operation in [('push','repair-existing'),('schedule','repair-existing'),('pull_request','repair-existing'),('workflow_dispatch','validate-repair')]:
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
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'original.zip'
   with zipfile.ZipFile(archive,'w') as z:z.writestr('build-manifest.json',json.dumps(manifest))
   data=archive.read_bytes();sha=hashlib.sha256(data).hexdigest();commit='a'*40
   asset={'expired':False,'workflow_run':{'id':10},'name':f'verified-1panel-v2.3.2-{commit}','digest':'sha256:'+sha,'size_in_bytes':len(data)}
   def download(args,**kwargs):kwargs['stdout'].write(data);return SimpleNamespace(returncode=0)
   with patch.object(manual,'github_json',side_effect=[run,asset,{'jobs':jobs,'total_count':len(jobs)},run]),patch.object(manual.subprocess,'run',side_effect=download),patch.object(manual,'validate_upstream_input') as validate:
    result=manual.fetch_ci_bundle(root/'input','v2.3.2','10','22',sha,commit,'upstream-matrix')
    self.assertEqual(result['build_repository_commit'],commit);validate.assert_called_once()
   with patch.object(manual,'github_json',return_value=dict(run,conclusion='cancelled')):
    with self.assertRaises(ValueError):manual.fetch_ci_bundle(root/'bad','v2.3.2','10','22',sha,commit,'upstream-matrix')
 def test_wrong_version_tag_or_repository_rejected(self):
  for version,tag,repo in [('v2.3.2','v2.3.1',manual.REPOS['upstream-matrix']),('../../escape','../../escape',manual.REPOS['upstream-matrix']),('v2.3.2','v2.3.2','other/repo')]:
   with self.assertRaises(ValueError):manual.check_identity(version,tag,repo)

if __name__=='__main__':unittest.main()
