"""Automatic package-only publication, immutable append and recovery behavior."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
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
        self.lost_response = False
        self.post_drift = False
        self.recovery_failure = False

    def release(self):
        return copy.deepcopy(self.value)

    def bind_release(self, release_id):
        self.release_id = release_id

    def run(self, *args):
        self.calls.append(args)
        if 'POST' in args:
            created = {'id': 17, 'tag_name': self.tag, 'draft': True,
                       'name':self.tag,'target_commitish':os.environ['GITHUB_SHA'],
                       'prerelease':False,'body': '', 'assets': [],
                       'author':{'id':41898282,'login':'github-actions[bot]','type':'Bot'}}
            self.value = copy.deepcopy(created)
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
        for name, body in [(f'1panel-{Client.tag}-linux-amd64.tar.gz', b'archive'),
                           (f'1panel-{Client.tag}-linux-amd64.tar.gz.sha256', b'archive checksum'),
                           ('checksums.txt', b'aggregate checksum'),
                           ('build-manifest.json', b'{"schema_version":2}')]:
            p = self.work / 'release' / name
            p.write_bytes(body)
            self.files.append(p)
        self.proof = {'files': {p.name: facts(p) for p in self.files}}
        self.proofpath = self.work / 'control' / ap.PROOF
        self.proofpath.write_text(json.dumps(self.proof))
        self.args = types.SimpleNamespace(version=Client.tag,
                repository=Client.repo, work=self.work)
        self.after_repair = None
        for target, replacement in [
            ('check_identity', lambda *a: {}),
            ('validate_payloads', lambda *a: self.files),
            ('make_proof', lambda *a, **kw: self.proof),
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

    def producer(self, *args):
        return None

    def repair(self, client, ordered, journal, **kwargs):
        client.value['assets'] = [dict(id=100+i, name=p.name,
             size=facts(p)['bytes'], digest='sha256:'+facts(p)['sha256'])
             for i, p in enumerate(ordered)]
        if self.after_repair:
            self.after_repair(client)
        return {'phase': 'complete'}

    def test_writer_retry_preserves_the_preparation_proof_attempt(self):
        self.proof['workflow_run_attempt']=1
        self.proofpath.write_text(json.dumps(self.proof))
        def original_proof(*args,**kwargs):
            self.assertEqual(kwargs['workflow_run_attempt'],1)
            return self.proof
        with patch.dict(os.environ,GITHUB_RUN_ATTEMPT='2',
                        EXPECTED_VALIDATION_RECEIPT_SHA256=facts(self.proofpath)['sha256']),\
             patch.object(ap,'make_proof',side_effect=original_proof):
            self.assertEqual(ap.publish(self.args,Client()),'published')

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

    def test_writer_ignores_obsolete_plan_and_noops_when_another_writer_finished(self):
        c = Client({'id': 8, 'tag_name': Client.tag, 'draft': False, 'prerelease': False,
            'body': '', 'assets': [dict(id=91+i,name=p.name,size=facts(p)['bytes'],
                digest='sha256:'+facts(p)['sha256']) for i,p in enumerate(self.files[:2])]})
        (self.work/'control'/'automatic-plan.json').write_text('{"operation":"create","before":null}')
        self.assertEqual(ap.publish(self.args,c),'unchanged')
        self.assertEqual(c.calls,[])


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
    def test_same_run_and_writer_only_retry_use_native_identity_without_status_api(self):
        from test_publication_lineage import producer_fixture
        value,_,_=producer_fixture()
        for current_attempt in ('2','3'):
            for event in ('push','schedule','workflow_dispatch'):
                with self.subTest(current_attempt=current_attempt,event=event),\
                     patch.dict(os.environ,GITHUB_RUN_ID='10',GITHUB_RUN_ATTEMPT=current_attempt,
                         GITHUB_SHA='a'*40,GITHUB_REPOSITORY=Client.repo,GITHUB_EVENT_NAME=event),\
                     patch('subprocess.run',side_effect=AssertionError('Same-run publication must not query job status')):
                    ap.current_producer(value,'a'*40)

    def test_forged_or_cross_run_producers_never_pass_the_same_run_gate(self):
        from test_publication_lineage import producer_fixture
        original,_,_=producer_fixture()
        for mutation in ('run','future-attempt','invalid-attempt','head','built-commit','payload-commit','repository','event','outcome'):
            value=copy.deepcopy(original);commit='a'*40
            env=dict(GITHUB_RUN_ID='10',GITHUB_RUN_ATTEMPT='3',GITHUB_SHA='a'*40,
                     GITHUB_REPOSITORY=Client.repo,GITHUB_EVENT_NAME='schedule')
            if mutation=='run':value['producer_run_id']=11
            elif mutation=='future-attempt':value['producer_run_attempt']=4
            elif mutation=='invalid-attempt':value['producer_run_attempt']=True
            elif mutation=='head':value['producer_head_sha']='b'*40
            elif mutation=='built-commit':commit='b'*40
            elif mutation=='payload-commit':value['artifacts'][0]['build_repository_commit']='b'*40
            elif mutation=='repository':env['GITHUB_REPOSITORY']='another/repository'
            elif mutation=='event':env['GITHUB_EVENT_NAME']='pull_request'
            elif mutation=='outcome':value['outcomes'][0]['status']='failure'
            with self.subTest(mutation=mutation),patch.dict(os.environ,env):
                with self.assertRaises(ValueError):ap.current_producer(value,commit)


class RealAppendTransactions(unittest.TestCase):
    setUp=AutomaticTransaction.setUp
    producer=AutomaticTransaction.producer
    repair=AutomaticTransaction.repair
    def client(self):
        from test_release_repair import FakeGitHub
        c=FakeGitHub();c.repo=Client.repo;c.tag=Client.tag
        c.assets={1:(f'1panel-{Client.tag}-linux-amd64.tar.gz',b'archive'),2:('checksums.txt',b'old sums'),
                  3:(f'1panel-{Client.tag}-linux-amd64.tar.gz.sha256',b'archive checksum'),
                  4:(f'1panel-{Client.tag}-linux-arm.tar.gz.backup-012345abcdef',b'old missing branch')}
        release=c.release
        c.release=lambda:dict(release(),id=8,draft=False,prerelease=False,tag_name=Client.tag)
        # Rebuilding an already-public architecture must not replace its old bytes.
        self.files[0].write_bytes(b'rebuilt amd64 archive')
        self.proof['files'][self.files[0].name]=facts(self.files[0])
        for suffix,body in [('',b'new arm64 archive'),('.sha256',b'new arm64 checksum')]:
            path=self.work/'release'/f'1panel-{Client.tag}-linux-arm64.tar.gz{suffix}'
            path.write_bytes(body);self.files.append(path);self.proof['files'][path.name]=facts(path)
        self.proofpath.write_text(json.dumps(self.proof))
        os.environ['EXPECTED_VALIDATION_RECEIPT_SHA256']=facts(self.proofpath)['sha256']
        return c

    def test_writer_only_retry_after_partial_public_append_uploads_only_the_gap(self):
        from release_asset_repair import repair
        c=self.client();original=copy.deepcopy(c.assets)
        control={path.name:path.read_bytes() for path in (self.work/'control').iterdir()}
        upload=c.upload;attempts=[]
        def fail_second(path):
            attempts.append(path.name)
            if len(attempts)==2:raise OSError('Second upload failed')
            upload(path)
        c.upload=fail_second
        with patch.object(ap,'repair',repair),self.assertRaises(OSError):ap.publish(self.args,c)
        partial=copy.deepcopy(c.assets)
        c.body='Human release notes updated while the missing architecture built'
        retry=self.work/'fresh-runner';(retry/'control').mkdir(parents=True)
        shutil.copytree(self.work/'release',retry/'release')
        for name,raw in control.items():(retry/'control'/name).write_bytes(raw)
        self.files=[retry/'release'/path.name for path in self.files]
        self.args.work=retry;c.upload=upload
        with patch.object(ap,'repair',repair),patch.dict(os.environ,GITHUB_RUN_ATTEMPT='2'):
            self.assertEqual(ap.publish(self.args,c),'published')
            calls=list(c.calls)
            self.assertEqual(ap.publish(self.args,c),'unchanged')
            self.assertEqual(c.calls,calls)
        for asset_id,value in partial.items():self.assertEqual(c.assets[asset_id],value)
        self.assertEqual(len(c.assets),len(original)+2)
        self.assertEqual([call[1] for call in c.calls if call[0]=='upload'],attempts)
        self.assertEqual(c.downloads,[])
        self.assertEqual(c.body,'Human release notes updated while the missing architecture built')

class PrepareControlFlow(unittest.TestCase):
    def setUp(self):
        from test_discovered_inputs import fixture
        import resolved_contract
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.contract=fixture();self.contract_raw=resolved_contract.canonical(self.contract)
        self.contract_sha=hashlib.sha256(self.contract_raw).hexdigest()
        def validate(directory,*args):
            return sorted(Path(directory).iterdir())
        import publication_contract as pc
        for target,fn in [(ap,'check_identity'),(ap,'current_producer')]:
            p=patch.object(target,fn,return_value='upstream-matrix' if fn=='check_identity' else None)
            p.start();self.addCleanup(p.stop)
        p=patch.object(ap,'validate_payloads',side_effect=validate);p.start();self.addCleanup(p.stop)
        p=patch.object(pc,'policy_fingerprint',return_value='f'*64);p.start();self.addCleanup(p.stop)
        p=patch('resolved_contract.activate_release',return_value=None);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,GITHUB_RUN_ID='11',GITHUB_RUN_ATTEMPT='2',GITHUB_SHA='b'*40,
                     GITHUB_EVENT_NAME='workflow_dispatch')
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

    def old_release(self, successful=('amd64',)):
        directory=self.root/'previous'
        self.payload(directory,successful,10,'a'*40)
        packages=[p for p in directory.iterdir() if p.name.endswith(('.tar.gz','.tar.gz.sha256'))]
        client=Client({'id':8,'tag_name':Client.tag,'draft':False,'prerelease':False,'body':'kept notes',
            'assets':[{'id':i+1,'name':p.name,'size':p.stat().st_size,
                       'digest':'sha256:'+facts(p)['sha256']} for i,p in enumerate(packages)]})
        return client

    def test_prepare_is_local_and_writes_only_the_authenticated_payload_receipt(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        with patch.object(ap,'GitHub',side_effect=AssertionError('Preparation must not discover Releases')), \
             patch('manual_publication.github_json',side_effect=AssertionError('Preparation must not read remote jobs')), \
             patch.object(ap,'current_producer',side_effect=AssertionError('Writer authenticates the producer')):
            ap.prepare(self.args)
        control=self.args.work/'control'
        self.assertEqual({path.name for path in control.iterdir()},{ap.PROOF})
        receipt=json.loads((control/ap.PROOF).read_text())
        for path in self.source.iterdir():
            copied=self.args.work/'release'/path.name
            self.assertEqual(copied.read_bytes(),path.read_bytes())
            self.assertEqual(receipt['files'][path.name],facts(copied))
        self.assertEqual(receipt['producer_run_id'],11)
        self.assertEqual(receipt['workflow_run_attempt'],2)


if __name__ == '__main__':
    unittest.main()
