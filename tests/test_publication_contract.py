import copy,hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import publication_contract as contracts
from test_matrix_contract import MatrixContractTests

class ReceiptTests(unittest.TestCase):
 def fixture(self,root):
  manifest,producer,jobs=MatrixContractTests().fixture();manifest['version']='v2.99.0'
  names=contracts.expected_names('upstream-matrix','v2.99.0',root,['amd64']);files=[];sums=[]
  for name in sorted(names-{'checksums.txt'}):
   p=root/name;p.write_bytes(json.dumps(manifest).encode() if name=='build-manifest.json' else ('verified '+name).encode());files.append(p)
   if name.endswith('.tar.gz'):sums.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+name)
  checksum=root/'checksums.txt';checksum.write_text('\n'.join(sums)+'\n');files.append(checksum)
  with patch.object(contracts,'policy_fingerprint',return_value='reviewed-policy'),patch.dict(os.environ,GITHUB_RUN_ATTEMPT='1'):
   proof=contracts.make_proof(files,'upstream-matrix','v2.99.0','v2.99.0',contracts.REPOS['upstream-matrix'],123,'a'*40,root)
  assets=[{'id':i,'name':p.name,'size':p.stat().st_size,'digest':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()} for i,p in enumerate(files)]
  assets.append({'id':100,'name':contracts.PROOF,'size':1,'digest':'unused-in-pure-receipt-check'})
  run={'id':123,'run_attempt':1,'head_sha':'a'*40,'status':'completed','conclusion':'success','path':'.github/workflows/build.yml'}
  return proof,checksum.read_bytes(),assets,run
 def check(self,root,values):
  with patch.object(contracts,'policy_fingerprint',return_value='reviewed-policy'):
   return contracts.verify_receipt(*values,'upstream-matrix','v2.99.0','v2.99.0',contracts.REPOS['upstream-matrix'],root)
 def test_exact_partial_receipt_is_verified(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);values=self.fixture(root);self.assertTrue(self.check(root,values))
   self.assertEqual(values[0]['requested_architectures'],['amd64','arm64'])
   self.assertEqual(values[0]['successful_architectures'],['amd64'])
 def test_missing_successful_payload_is_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);proof,checksums,assets,run=self.fixture(root)
   with self.assertRaises(ValueError):self.check(root,(proof,checksums,assets[:1],run))
 def test_bad_digest_stale_policy_wrong_commit_and_run_fail(self):
  for case in ['digest','policy','commit','run','attempt','checksum','extra','missing-proof','wrong-contract','failed-canonical','hidden-outcome']:
   with self.subTest(case=case),tempfile.TemporaryDirectory() as t:
    root=Path(t);p,c,a,r=self.fixture(root)
    if case=='digest':a[0]['digest']='sha256:'+'0'*64
    if case=='policy':p['policy_fingerprint']='old-policy'
    if case=='commit':r['head_sha']='b'*40
    if case=='run':r['conclusion']='failure'
    if case=='attempt':r['run_attempt']=2
    if case=='checksum':c+=b'bad\n'
    if case=='extra':a.append({'name':'unreviewed-package.tar.gz','size':1})
    if case=='missing-proof':a.pop()
    if case=='wrong-contract':p['contract']='upstream7'
    if case=='failed-canonical':a.append({'name':'1panel-v2.99.0-linux-arm64.tar.gz','size':1})
    if case=='hidden-outcome':p['outcomes'].pop()
    with self.assertRaises(ValueError):self.check(root,(p,c,a,r))
 def test_retired_failed_branch_backups_are_noncanonical(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p,c,a,r=self.fixture(root)
   a.append({'name':'1panel-v2.99.0-linux-arm64.tar.gz.backup-012345abcdef','size':10})
   self.assertTrue(self.check(root,(p,c,a,r)))
 def test_expected_names_follow_accepted_inventory(self):
  names=contracts.expected_names('upstream-matrix','v2.99.0',accepted=['amd64'])
  self.assertEqual(sum(n.endswith('.tar.gz') for n in names),1)
  self.assertIn('build-manifest.json',names);self.assertIn('resolved-source.json',names)

class ValidationLogTests(unittest.TestCase):
 def test_receipt_hash_must_exist_in_exact_successful_validation_job(self):
  class Client:
   repo=contracts.REPOS['upstream-matrix']
   def run(self,*args):
    if '/jobs?' in args[-1]:return json.dumps({'jobs':[{'id':17,'name':'publication_prepare','conclusion':'success','run_id':123,'run_attempt':1,'head_sha':'a'*40}]})
    return 'timestamp VERIFIED_RELEASE_RECEIPT_SHA256='+'b'*64
  proof={'workflow_run_id':123,'workflow_run_attempt':1,'workflow_commit':'a'*40}
  self.assertTrue(contracts.verify_validation_log(Client(),proof,'b'*64))
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),proof,'c'*64)
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),dict(proof,workflow_run_attempt=2),'b'*64)
 def test_untrusted_run_path_rejected_before_api_request(self):
  class Client:
   repo=contracts.REPOS['upstream-matrix']
   def run(self,*args):raise AssertionError('API must not be called')
  with self.assertRaises(ValueError):contracts.verify_validation_log(Client(),{'workflow_run_id':'../../user','workflow_commit':'a'*40},'b'*64)
