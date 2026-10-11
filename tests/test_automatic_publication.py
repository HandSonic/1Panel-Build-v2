"""Reimplemented recovery tests for the automatic canonical transaction.

These tests replace unavailable local tests; they do not claim the old test blob.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automatic_publication as ap


def facts(path):
    data = path.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


class Client:
    repo = 'HandSonic/1Panel-Build-v2'
    tag = 'v2.99.0'

    def __init__(self, release=None):
        self.value = copy.deepcopy(release)
        self.calls = []
        self.substitute = False
        self.lost_response = False
        self.post_drift = False
        self.recovery_failure = False

    def release(self):
        return copy.deepcopy(self.value)

    def run(self, *args):
        self.calls.append(args)
        if 'POST' in args:
            created = {'id': 17, 'tag_name': self.tag, 'draft': True,
                       'body': '', 'assets': []}
            self.value = copy.deepcopy(created)
            if self.substitute:
                self.value['id'] = 18
            return json.dumps(created)
        if 'PATCH' in args:
            if 'draft=false' in args:
                self.value['draft'] = False
                if self.post_drift:
                    self.value['assets'][0]['size'] += 1
                if self.lost_response:
                    raise OSError('response lost after application')
            elif 'draft=true' in args:
                if self.recovery_failure:
                    raise OSError('recovery transport failed')
                self.value['draft'] = True
            return json.dumps(self.value)
        if args == ('api', f'repos/{self.repo}/releases/17'):
            return json.dumps(self.value)
        raise AssertionError(args)


class AutomaticTransaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / 'control').mkdir()
        (self.work / 'release').mkdir()
        self.files = []
        for name, body in [('payload.tar.gz', b'archive'),
                           ('payload.tar.gz.sha256', b'archive checksum'),
                           ('checksums.txt', b'aggregate checksum'),
                           ('build-manifest.json', b'{"schema_version":2}')]:
            p = self.work / 'release' / name
            p.write_bytes(body)
            self.files.append(p)
        self.proof = {'files': {p.name: facts(p) for p in self.files}}
        self.proofpath = self.work / 'control' / ap.PROOF
        self.proofpath.write_text(json.dumps(self.proof))
        self.plan = {'schema': 1, 'version': Client.tag, 'operation': 'create',
                     'before': ap.snapshot(None), 'preserved': {},
                     'receipt_sha256': facts(self.proofpath)['sha256']}
        self.save_plan()
        self.args = types.SimpleNamespace(version=Client.tag,
                repository=Client.repo, work=self.work)
        self.repair_calls = []
        self.producer_calls = 0
        self.before_repair = None
        self.after_repair = None
        for target, replacement in [
            ('check_identity', lambda *a: {}),
            ('validate_payloads', lambda *a: self.files),
            ('make_proof', lambda *a: self.proof),
            ('optional_release', lambda c: c.release()),
            ('current_producer', self.producer),
            ('repair', self.repair),
        ]:
            p = patch.object(ap, target, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch('resolved_contract.activate_release', lambda *a: None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40,
            'GITHUB_RUN_ID': '11', 'GITHUB_RUN_ATTEMPT': '1',
            'EXPECTED_VALIDATION_RECEIPT_SHA256': facts(self.proofpath)['sha256']})
        p.start()
        self.addCleanup(p.stop)

    def save_plan(self):
        (self.work / 'control' / 'automatic-plan.json').write_text(json.dumps(self.plan))

    def producer(self, *args):
        self.producer_calls += 1

    def repair(self, client, ordered, journal, **kwargs):
        self.repair_calls.append((ordered, kwargs))
        if self.before_repair:
            self.before_repair(client)
        if ap.snapshot(client.release()) != kwargs['expected_snapshot']:
            raise ValueError('transaction entry drift')
        client.value['assets'] = [dict(id=100+i, name=p.name,
             size=facts(p)['bytes'], digest='sha256:'+facts(p)['sha256'])
             for i, p in enumerate(ordered)]
        if self.after_repair:
            self.after_repair(client)
        return {'phase': 'complete'}

    def test_create_checksums_last_and_same_owned_id(self):
        c = Client()
        self.assertEqual(ap.publish(self.args, c), 'published')
        self.assertFalse(c.value['draft'])
        self.assertEqual(c.value['id'], 17)
        ordered, kw = self.repair_calls[0]
        self.assertEqual(ordered[-1].name, 'checksums.txt')
        self.assertEqual(kw['expected_snapshot']['id'], 17)
        self.assertEqual(self.producer_calls, 2)
        self.assertFalse(any('body=' in item for call in c.calls for item in call))

    def test_substituted_created_draft_never_adopted(self):
        c = Client(); c.substitute = True
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertFalse(self.repair_calls)
        self.assertFalse(any('PATCH' in call for call in c.calls))

    def test_lost_finalization_response_restores_exact_draft(self):
        c = Client(); c.lost_response = True
        with self.assertRaises(OSError): ap.publish(self.args, c)
        self.assertTrue(c.value['draft'])
        self.assertEqual(json.loads((self.work/'control'/'creation-journal.json').read_text())['phase'], 'returned_to_draft')

    def test_post_publication_drift_is_detected_and_redrafted(self):
        c = Client(); c.post_drift = True
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertTrue(c.value['draft'])

    def test_recovery_failure_remains_explicit(self):
        c = Client(); c.lost_response = True; c.recovery_failure = True
        with self.assertRaises(OSError): ap.publish(self.args, c)
        record = json.loads((self.work/'control'/'creation-journal.json').read_text())
        self.assertEqual(record['phase'], 'recovery_required')

    def test_drift_before_finalization_never_goes_public(self):
        self.after_repair = lambda c: c.value.update(id=18)
        c = Client()
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertFalse(any('draft=false' in call for call in c.calls))

    def test_existing_journal_stops_before_any_api_write(self):
        (self.work/'control'/'automatic-journal.json').write_text('{}')
        c = Client()
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertEqual(c.calls, [])

    def test_receipt_tamper_stops_before_writes(self):
        self.proofpath.write_text('{}')
        c = Client()
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertEqual(c.calls, [])

    def test_append_transaction_baseline_must_remain_exact(self):
        c = Client({'id': 8, 'tag_name': Client.tag, 'draft': False,
                    'body': 'existing notes', 'assets': []})
        self.plan.update(operation='append', before=ap.snapshot(c.release()))
        self.save_plan()
        self.before_repair = lambda client: client.value['assets'].append(
            {'id': 91, 'name': 'payload.tar.gz', 'size': 9, 'digest': 'sha256:'+'b'*64})
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertEqual(c.value['assets'][0]['id'], 91)
        self.assertEqual(c.calls, [])

    def test_append_cannot_replace_existing_bytes(self):
        c = Client({'id': 8, 'tag_name': Client.tag, 'draft': False,
            'body': '', 'assets': [{'id': 91, 'name':'payload.tar.gz',
            'size': 9, 'digest':'sha256:'+'b'*64}]})
        self.plan.update(operation='append', before=ap.snapshot(c.release()))
        self.save_plan()
        with self.assertRaises(ValueError): ap.publish(self.args, c)
        self.assertFalse(self.repair_calls)


class EventGates(unittest.TestCase):
    def test_readonly_and_candidate_modes_never_publish(self):
        for mode in ('build-only','validate-automatic','validate-promotion','promote-existing'):
            self.assertFalse(ap.operation_allowed('publish','workflow_dispatch',
                'refs/heads/master','master',mode,False))
        self.assertTrue(ap.operation_allowed('prepare','workflow_dispatch',
                'refs/heads/master','master','validate-automatic',False))
        self.assertFalse(ap.operation_allowed('prepare','workflow_dispatch',
                'refs/heads/master','master','build-only',False))
        self.assertFalse(ap.operation_allowed('publish','workflow_dispatch',
                'refs/heads/master','master','build',True))

    def test_normal_automatic_and_manual_build_events(self):
        for event, ref in [('schedule','refs/heads/master'),
                           ('push','refs/tags/v2.99.0'),
                           ('workflow_dispatch','refs/heads/master')]:
            self.assertTrue(ap.operation_allowed('publish',event,ref,'master','build',False))
        self.assertFalse(ap.operation_allowed('publish','pull_request','refs/pull/2/merge','master','build',False))
        self.assertFalse(ap.operation_allowed('publish','schedule','refs/heads/other','master','build',False))


class CurrentProducer(unittest.TestCase):
    def test_current_attempt_and_shared_gates_remain_authenticated(self):
        from test_publication_lineage import producer_fixture
        from matrix_contract import verify_current_jobs
        value,run,jobs=producer_fixture()
        run.update(status='in_progress',conclusion=None)
        jobs.append({'id':999,'name':'publication_prepare','run_id':10,'run_attempt':2,
                     'head_sha':'a'*40,'status':'in_progress','conclusion':None})
        self.assertEqual(verify_current_jobs(value,run,jobs,'a'*40,10,2),['amd64'])
        for case in ('run','attempt','head','cancel','pr','unknown-pending','shared','job-attempt','unfinished-compile'):
            r=copy.deepcopy(run);j=copy.deepcopy(jobs)
            if case=='run':r['id']=11
            if case=='attempt':r['run_attempt']=3
            if case=='head':r['head_sha']='b'*40
            if case=='cancel':r.update(status='completed',conclusion='cancelled')
            if case=='pr':r['event']='pull_request'
            if case=='unknown-pending':j[-1]['name']='unexpected'
            if case=='shared':j[0]['conclusion']='failure'
            if case=='job-attempt':j[-1]['run_attempt']=3
            if case=='unfinished-compile':j[3].update(status='in_progress',conclusion=None)
            with self.subTest(case=case),self.assertRaises(ValueError):verify_current_jobs(value,r,j,'a'*40,10,2)

    def test_provider_fetches_only_exact_attempt_and_all_pages(self):
        from test_publication_lineage import producer_fixture
        value,run,jobs=producer_fixture();run.update(status='in_progress',conclusion=None)
        calls=[]
        def api(path):
            calls.append(path)
            if path.endswith('/attempts/2'):return run
            if path.endswith('page=1'):return {'total_count':len(jobs),'jobs':jobs[:3]}
            if path.endswith('page=2'):return {'total_count':len(jobs),'jobs':jobs[3:]}
            raise AssertionError(path)
        with patch.dict(os.environ,GITHUB_RUN_ID='10',GITHUB_RUN_ATTEMPT='2'),patch.object(ap,'github_json',side_effect=api):
            ap.current_producer(value,'a'*40)
        self.assertEqual(len(calls),3)
        self.assertTrue(all('/runs/10/attempts/2' in path for path in calls))


class RealAppendTransactions(unittest.TestCase):
    setUp=AutomaticTransaction.setUp
    save_plan=AutomaticTransaction.save_plan
    producer=AutomaticTransaction.producer
    repair=AutomaticTransaction.repair
    def client(self,failure=None):
        from test_release_repair import FakeGitHub
        c=FakeGitHub(failure);c.repo=Client.repo;c.tag=Client.tag
        c.assets={1:('payload.tar.gz',b'archive'),2:('checksums.txt',b'old sums'),
                  3:('payload.tar.gz.sha256',b'archive checksum'),
                  4:('missing.tar.gz.backup-012345abcdef',b'old missing branch')}
        release=c.release
        c.release=lambda:dict(release(),id=8,draft=False,tag_name=Client.tag)
        self.plan.update(operation='append',before=ap.snapshot(c.release()),
                         preserved={'payload.tar.gz':facts(self.files[0])})
        self.save_plan()
        return c

    def test_real_transaction_preserves_existing_ids_and_recovery_assets(self):
        from release_asset_repair import repair
        c=self.client();before=copy.deepcopy(c.assets)
        with patch.object(ap,'repair',repair):self.assertEqual(ap.publish(self.args,c),'published')
        for i in (1,3,4):self.assertEqual(c.assets[i],before[i])
        self.assertFalse(any(call[0]=='rename' and call[1] in (1,3,4) for call in c.calls))
        self.assertEqual(c.body,'old release notes')

    def test_real_transaction_uncertain_switch_rolls_back(self):
        from release_asset_repair import repair
        c=self.client('switch');before=copy.deepcopy(c.assets)
        with patch.object(ap,'repair',repair),self.assertRaises(RuntimeError):ap.publish(self.args,c)
        for i in before:self.assertEqual(c.assets[i],before[i])
        self.assertEqual(json.loads((self.work/'control'/'automatic-journal.json').read_text())['phase'],'rolled_back')

    def test_real_transaction_rejects_entry_race_without_touching_new_asset(self):
        from release_asset_repair import repair
        c=self.client();before=copy.deepcopy(c.assets)
        new=self.work/'release'/'added.tar.gz';new.write_bytes(b'new successful branch')
        self.files.insert(0,new);self.proof['files'][new.name]=facts(new)
        self.proofpath.write_text(json.dumps(self.proof))
        self.plan['receipt_sha256']=facts(self.proofpath)['sha256'];self.save_plan()
        os.environ['EXPECTED_VALIDATION_RECEIPT_SHA256']=facts(self.proofpath)['sha256']
        def raced(client,*args,**kwargs):
            client.assets[99]=('added.tar.gz',b'human bytes')
            return repair(client,*args,**kwargs)
        with patch.object(ap,'repair',raced),self.assertRaises(ValueError):ap.publish(self.args,c)
        for i in before:self.assertEqual(c.assets[i],before[i])
        self.assertEqual(c.assets[99],('added.tar.gz',b'human bytes'))
        self.assertEqual(c.calls,[])

    def test_real_transaction_rejects_changed_existing_archive(self):
        from release_asset_repair import repair
        c=self.client()
        def raced(client,*args,**kwargs):
            client.assets[1]=('payload.tar.gz',b'concurrent replacement')
            return repair(client,*args,**kwargs)
        with patch.object(ap,'repair',raced),self.assertRaises(ValueError):ap.publish(self.args,c)
        self.assertEqual(c.assets[1],('payload.tar.gz',b'concurrent replacement'))
        self.assertEqual(c.calls,[])


class PrepareControlFlow(unittest.TestCase):
    def setUp(self):
        from test_discovered_inputs import fixture
        import resolved_contract
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.contract=fixture();self.contract_raw=resolved_contract.canonical(self.contract)
        self.contract_sha=hashlib.sha256(self.contract_raw).hexdigest()
        self.policyroot=self.root/'policy';(self.policyroot/'config').mkdir(parents=True)
        (self.policyroot/'config'/'repair-locks.json').write_text('{"schema":1,"recipes":[],"additions":{}}')
        (self.policyroot/'scripts').mkdir();(self.policyroot/'Dockerfile').write_text('synthetic')
        self.payload_checks=[]
        def validate(directory,*args):
            directory=Path(directory);self.payload_checks.append(directory)
            return sorted(directory.iterdir())
        import publication_contract as pc
        for target,fn in [(ap,'check_identity'),(ap,'current_producer')]:
            p=patch.object(target,fn,return_value='upstream-matrix' if fn=='check_identity' else None)
            p.start();self.addCleanup(p.stop)
        for target in (ap,pc):
            p=patch.object(target,'validate_payloads',side_effect=validate);p.start();self.addCleanup(p.stop)
        p=patch.object(pc,'policy_fingerprint',return_value='f'*64);p.start();self.addCleanup(p.stop)
        p=patch('resolved_contract.activate_release',return_value=None);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,GITHUB_RUN_ID='11',GITHUB_RUN_ATTEMPT='2',GITHUB_SHA='b'*40)
        p.start();self.addCleanup(p.stop)
        self.args=types.SimpleNamespace(version=Client.tag,repository=Client.repo,
                    work=self.root/'work',input=self.source)

    def payload(self,directory,successful,run_id,commit):
        from test_publication_lineage import producer_fixture
        manifest,run,jobs=producer_fixture(successful,run_id,2,commit,self.contract_sha)
        directory.mkdir(exist_ok=True);sums=[]
        for row in manifest['artifacts']:
            p=directory/row['file'];p.write_bytes(f"archive {row['architecture']} from {run_id}/2".encode())
            line=hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n'
            (directory/(p.name+'.sha256')).write_text(line);sums.append(line)
        (directory/'checksums.txt').write_text(''.join(sums))
        (directory/'build-manifest.json').write_text(json.dumps(manifest))
        (directory/'build-inputs.env').write_text('VERSION='+Client.tag+'\n')
        (directory/'resolved-source.json').write_bytes(self.contract_raw)
        return manifest,run,jobs

    def old_release(self,successful=('amd64',)):
        import publication_contract as pc
        directory=self.root/'previous';manifest,run,jobs=self.payload(directory,successful,10,'a'*40)
        proof=pc.make_proof(sorted(directory.iterdir()),'upstream-matrix',Client.tag,Client.tag,
                            Client.repo,10,'a'*40,self.policyroot)
        # A deliberately different old policy is allowed only by full current no-op revalidation.
        proof['policy_fingerprint']='e'*64 if len(successful)==2 else 'f'*64
        proofraw=json.dumps(proof).encode()
        objects={p.name:p.read_bytes() for p in directory.iterdir()};objects[ap.PROOF]=proofraw
        client=Client({'id':8,'tag_name':Client.tag,'draft':False,'body':'kept notes',
            'assets':[{'id':i+1,'name':name,'size':len(body),'digest':'sha256:'+hashlib.sha256(body).hexdigest()}
                      for i,(name,body) in enumerate(objects.items())]})
        def download(name,dest):(Path(dest)/name).write_bytes(objects[name])
        client.download=download
        def api(*args):
            if args == ('--version',):return 'gh version 2.92.0 (2026-05-13)'
            if args[:2] == ('run','view'):return 'VERIFIED_RELEASE_RECEIPT_SHA256='+hashlib.sha256(proofraw).hexdigest()
            path=args[-1]
            if '/jobs?' in path:
                if '--paginate' in args:return json.dumps([{'jobs':jobs}])
                return json.dumps({'jobs':[{'id':99,'name':'publication_prepare','conclusion':'success',
                    'run_id':10,'run_attempt':2,'head_sha':'a'*40}]})
            if path.endswith('/logs'):return 'VERIFIED_RELEASE_RECEIPT_SHA256='+hashlib.sha256(proofraw).hexdigest()
            if path.endswith('/attempts/2'):return json.dumps(run)
            raise AssertionError(args)
        client.run=api
        return client,objects,proof

    def perform(self,client):
        import publication_contract as pc
        real=pc.existing_release_state
        with patch.object(ap,'GitHub',return_value=client),patch.object(ap,'optional_release',side_effect=lambda c:c.release()), \
             patch.object(ap,'existing_release_state',side_effect=lambda c,*args,**kwargs:real(c,*args,root=self.policyroot,**kwargs)):
            ap.prepare(self.args)

    def test_create_plan_binds_current_contract_and_producer(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        self.perform(Client())
        plan=json.loads((self.args.work/'control'/'automatic-plan.json').read_text())
        manifest=json.loads((self.args.work/'release'/'build-manifest.json').read_text())
        self.assertEqual(plan['operation'],'create')
        self.assertIsNone(plan['before'])
        self.assertEqual(manifest['generations'][manifest['current_generation']]['producer_run_id'],11)

    def test_append_retains_exact_old_bytes_and_producer(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        client,objects,_=self.old_release()
        self.perform(client)
        manifest=json.loads((self.args.work/'release'/'build-manifest.json').read_text())
        selected={r['architecture']:r for r in manifest['artifacts']}
        self.assertEqual(selected['amd64']['build_repository_commit'],'a'*40)
        self.assertEqual(selected['arm64']['build_repository_commit'],'b'*40)
        self.assertEqual((self.args.work/'release'/selected['amd64']['file']).read_bytes(),objects[selected['amd64']['file']])

    def test_complete_old_policy_noop_revalidates_every_payload(self):
        self.payload(self.source,('amd64','arm64'),11,'b'*40)
        client,objects,proof=self.old_release(('amd64','arm64'))
        self.perform(client)
        self.assertEqual(json.loads((self.args.work/'control'/'automatic-plan.json').read_text())['operation'],'unchanged')
        self.assertTrue(any(p.name=='payloads' for p in self.payload_checks))
        self.assertEqual((self.args.work/'control'/ap.PROOF).read_bytes(),objects[ap.PROOF])

    def test_bad_old_receipt_is_rejected_before_plan(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        client,objects,_=self.old_release();objects[ap.PROOF]=b'{}'
        with self.assertRaises(ValueError):self.perform(client)
        self.assertFalse((self.args.work/'control'/'automatic-plan.json').exists())

    def test_partial_contract_change_requires_explicit_repair(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        client,_,proof=self.old_release()
        # Isolate the append policy after successful old-receipt authentication.
        proof['files']['resolved-source.json']['sha256']='c'*64
        client.download=lambda name,dest:(Path(dest)/name).write_text(json.dumps(proof))
        with patch.object(ap,'GitHub',return_value=client),patch.object(ap,'optional_release',return_value=client.release()), \
             patch.object(ap,'existing_release_state',return_value='verified'):
            with self.assertRaisesRegex(ValueError,'explicit repair'):ap.prepare(self.args)

    def test_changed_noop_readback_rejects(self):
        self.payload(self.source,('amd64','arm64'),11,'b'*40)
        client,objects,_=self.old_release(('amd64','arm64'))
        archive=next(name for name in objects if name.endswith('.tar.gz'));objects[archive]+=b'tampered'
        with self.assertRaises(ValueError):self.perform(client)
        self.assertFalse((self.args.work/'control'/'automatic-plan.json').exists())


if __name__ == '__main__':
    unittest.main()
