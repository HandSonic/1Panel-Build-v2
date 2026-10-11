import copy,hashlib,json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import publication_contract as contracts
from test_matrix_contract import MatrixContractTests

class ReceiptLogDiagnostics(unittest.TestCase):
 def test_only_controlled_failure_facts_are_disclosed(self):
  for stderr,status,category in [
   ('HTTP 403 token=secret https://private.invalid/?sig=secret','403','unclassified'),
   ('HTTP 404','404','unclassified'),
   ('HTTP 503 stream error: INTERNAL_ERROR','503','http2'),
   ('dial: i/o timeout https://private.invalid','unknown','timeout'),
   ('connection reset by peer','unknown','connection'),
   ('no such host','unknown','dns'),
   ('TLS handshake timeout','unknown','timeout'),
   ('certificate verify failed','unknown','tls'),
   ('unrecognized secret value','unknown','unclassified'),
  ]:
   with self.subTest(stderr=stderr):
    error=subprocess.CalledProcessError(1,['gh','api'],stderr=stderr)
    self.assertEqual(contracts.validation_log_failure_note(error),f'Receipt log fetch failed: exit=1; http_status={status}; network={category}')
 def test_failed_log_read_still_raises_without_retry_or_receipt_acceptance(self):
  proof={'workflow_run_id':123,'workflow_run_attempt':1,'workflow_commit':'a'*40}
  job={'id':456,'name':'publication_prepare','conclusion':'success','run_id':123,'run_attempt':1,'head_sha':'a'*40}
  error=subprocess.CalledProcessError(1,['gh','api','repos/example/project/actions/jobs/456/logs'],stderr='HTTP 403 secret=do-not-print')
  class Client:
   repo='example/project'
   def __init__(self):self.calls=[]
   def run(self,*args):
    self.calls.append(args)
    if args == ('--version',):return 'gh version 2.92.0 (2026-05-13)'
    if len(self.calls)==1:return json.dumps({'jobs':[job]})
    raise error
  client=Client()
  with self.assertRaises(subprocess.CalledProcessError) as caught:
   contracts.verify_validation_log(client,proof,'b'*64)
  self.assertIs(caught.exception,error)
  self.assertEqual(len(client.calls),3)
  self.assertEqual(error.__notes__,['Receipt log fetch failed: exit=1; http_status=403; network=unclassified'])
  self.assertNotIn('secret', '\n'.join(error.__notes__))

class ReceiptTests(unittest.TestCase):
 def setUp(self):
  # A synthetic per-run contract isolates receipt tests from checkout state.
  self.runtime_patch=patch('resolved_contract.runtime_contract',return_value={'frontend_lock':{'kind':'source'}})
  self.runtime_patch.start();self.addCleanup(self.runtime_patch.stop)
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
 def test_legacy_arm_recovery_pair_needs_verified_armv7_and_exact_suffix(self):
  for mutation in ('none','missing-sidecar','different-suffix','wrong-version','canonical','staged','unknown','no-armv7'):
   with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as t:
    root=Path(t);proof,checksums,assets,run=self.fixture(root)
    if mutation!='no-armv7':
     proof=json.loads(json.dumps(proof).replace('amd64','armv7'))
     checksums=checksums.replace(b'amd64',b'armv7')
     assets=json.loads(json.dumps(assets).replace('amd64','armv7'))
     proof['files']['checksums.txt']=contracts.digest_bytes(checksums)
     checksum_asset=next(a for a in assets if a['name']=='checksums.txt')
     checksum_asset.update(size=len(checksums),digest='sha256:'+hashlib.sha256(checksums).hexdigest())
    base='1panel-v2.99.0-linux-arm.tar.gz';suffix='.backup-012345abcdef'
    names=[base+suffix,base+'.sha256'+suffix]
    if mutation=='missing-sidecar':names.pop()
    elif mutation=='different-suffix':names[1]=base+'.sha256.backup-fedcba543210'
    elif mutation=='wrong-version':names=[n.replace('v2.99.0','v2.98.0') for n in names]
    elif mutation=='canonical':names=[base,base+'.sha256']
    elif mutation=='staged':names=[n.replace('.backup-','.staged-') for n in names]
    elif mutation=='unknown':names=[n.replace('-arm.','-unknown.') for n in names]
    assets.extend({'name':n,'size':10} for n in names)
    if mutation=='none':self.assertTrue(self.check(root,(proof,checksums,assets,run)))
    else:
     with self.assertRaises(ValueError):self.check(root,(proof,checksums,assets,run))
 def test_expected_names_follow_accepted_inventory(self):
  names=contracts.expected_names('upstream-matrix','v2.99.0',accepted=['amd64'])
  self.assertEqual(sum(n.endswith('.tar.gz') for n in names),1)
  self.assertIn('build-manifest.json',names);self.assertIn('resolved-source.json',names)
 def test_partial_producer_failure_is_accepted_only_for_its_exact_attempt(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p,c,a,r=self.fixture(root)
   p['workflow_run_id']=p['producer_run_id'];p['workflow_run_attempt']=p['producer_run_attempt']
   r.update(id=p['producer_run_id'],run_attempt=p['producer_run_attempt'],conclusion='failure')
   self.assertTrue(self.check(root,(p,c,a,r)))
   for key in ('id','run_attempt'):
    bad=dict(r);bad[key]+=1
    with self.subTest(key=key),self.assertRaises(ValueError):self.check(root,(p,c,a,bad))
   bad=dict(r,conclusion='cancelled')
   with self.assertRaises(ValueError):self.check(root,(p,c,a,bad))
 def test_resolved_lock_sidecar_is_part_of_exact_receipt_inventory(self):
  with patch('resolved_contract.runtime_contract',return_value={'frontend_lock':{'kind':'resolved'}}):
   names=contracts.expected_names('upstream-matrix','v2.99.0',accepted=['amd64'])
   self.assertIn('resolved-frontend-package-lock.json',names)

class ValidationLogTests(unittest.TestCase):
 def test_receipt_hash_must_exist_in_exact_successful_validation_job(self):
  class Client:
   repo=contracts.REPOS['upstream-matrix']
   def run(self,*args):
    if args == ('--version',):return 'gh version 2.92.0 (2026-05-13)'
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

class SafeReceiptTransportTests(unittest.TestCase):
 def client(self, version='gh version 2.92.0 (2026-05-13)', logs='safe receipt'):
  class Client:
   repo='example/project'
   def __init__(self):self.calls=[]
   def run(self,*args):
    self.calls.append(args)
    return version if args == ('--version',) else logs
  return Client()
 def test_exact_safe_command(self):
  client=self.client()
  self.assertEqual(contracts.sanitized_validation_log(client,456,2),'safe receipt')
  self.assertEqual(client.calls,[('--version',),('run','view','--repo','example/project','--job','456','--attempt','2','--log')])
 def test_unpatched_unknown_and_prerelease_cli_fail_before_fetch(self):
  for version in ('gh version 2.91.9','gh version 2.92.0-rc1','malformed','gh version 1.99.0'):
   client=self.client(version)
   with self.subTest(version=version),self.assertRaises(ValueError):contracts.sanitized_validation_log(client,456,2)
   self.assertEqual(client.calls,[('--version',)])
 def test_controls_fail_closed_without_being_rendered(self):
  for control in ('\x1b[31m','\x00','\x07','\x7f','\x9b'):
   with self.subTest(control=repr(control)),self.assertRaises(ValueError):
    contracts.sanitized_validation_log(self.client(logs='receipt'+control),456,2)
 def test_invalid_identity_never_fetches(self):
  for job,attempt in ((True,1),(0,1),('456',1),(456,False),(456,0)):
   client=self.client()
   with self.subTest(job=job,attempt=attempt),self.assertRaises(ValueError):contracts.sanitized_validation_log(client,job,attempt)
   self.assertEqual(client.calls,[])
