import copy
import hashlib
import json
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from release_asset_repair import GitHub, repair


class DraftTransportTests(unittest.TestCase):
    def client(self, pages, exact=None, status=404):
        class Transport(GitHub):
            def __init__(self):
                super().__init__('owner/repo', 'v2.3.2')
                self.calls=[]
            def run(self, *args):
                self.calls.append(args)
                endpoint=args[1]
                if '/releases/tags/' in endpoint:
                    raise subprocess.CalledProcessError(1,args,stderr=f'error (HTTP {status})')
                if '/releases?' in endpoint:
                    return json.dumps(pages[int(endpoint.rsplit('=',1)[1])-1])
                if endpoint.endswith('/releases/17'):
                    return json.dumps(exact or pages[-1][-1])
                raise AssertionError(args)
        return Transport()

    def draft(self):
        return dict(id=17,tag_name='v2.3.2',draft=True,body='',assets=[])

    def test_only_explicit_writer_lookup_discovers_and_binds_draft(self):
        from automatic_publication import optional_release
        c=self.client([[self.draft()]])
        with self.assertRaises(subprocess.CalledProcessError):c.release()
        self.assertIsNone(c.release_id)
        self.assertEqual(optional_release(c)['id'],17)
        self.assertEqual(c.release_id,17)
        self.assertTrue(c.calls[-1][1].endswith('/releases/17'))

    def test_draft_discovery_reads_all_pages(self):
        from automatic_publication import optional_release
        first=[dict(id=i+100,tag_name=f'other-{i}',draft=False,assets=[]) for i in range(100)]
        c=self.client([first,[self.draft()]])
        self.assertEqual(optional_release(c)['id'],17)
        self.assertEqual(sum('/releases?' in call[1] for call in c.calls),2)

    def test_no_draft_and_non_404_are_distinct(self):
        from automatic_publication import optional_release
        self.assertIsNone(optional_release(self.client([[]])))
        c=self.client([[]],status=403)
        with self.assertRaises(subprocess.CalledProcessError):optional_release(c)
        self.assertEqual(len(c.calls),1)

    def test_ambiguous_public_or_changed_drafts_are_rejected(self):
        from automatic_publication import optional_release
        for rows,exact in (([self.draft(),dict(self.draft(),id=18)],None),
                           ([dict(self.draft(),draft=False)],None),
                           ([self.draft()],dict(self.draft(),id=18)),
                           ([self.draft()],dict(self.draft(),tag_name='other'))):
            with self.subTest(rows=rows,exact=exact),self.assertRaises(ValueError):
                optional_release(self.client([rows],exact))

    def test_binding_cannot_be_silently_replaced(self):
        c=GitHub('owner/repo','v2.3.2')
        for release_id in (0,True,'17'):
            with self.assertRaises(ValueError):c.bind_release(release_id)
        c.bind_release(17)
        with self.assertRaises(ValueError):c.bind_release(18)

    def test_missing_bound_release_never_becomes_a_create_request(self):
        from automatic_publication import optional_release
        c=GitHub('owner/repo','v2.3.2');c.bind_release(17)
        error=subprocess.CalledProcessError(1,['gh','api'],stderr='Not Found (HTTP 404)')
        with patch.object(c,'run',side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError):optional_release(c)
        run.assert_called_once_with('api','repos/owner/repo/releases/17')

    def test_bound_upload_uses_id_even_when_tag_would_resolve_elsewhere(self):
        c=GitHub('owner/repo','v2.3.2');c.bind_release(17)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'archive &.tar.gz';path.write_bytes(b'\x00\xffbytes')
            with patch.object(c,'run',return_value='{}') as run:c.upload(path)
            call=run.call_args.args
            self.assertEqual(call[:4],('api','--method','POST',
                'https://uploads.github.com/repos/owner/repo/releases/17/assets?name=archive%20%26.tar.gz'))
            self.assertIn('Content-Length: 7',call)
            self.assertEqual(call[-2:],('--input',str(path)))
            self.assertNotIn('release',call)

    def test_bound_download_uses_asset_id_from_bound_release(self):
        c=GitHub('owner/repo','v2.3.2');c.bind_release(17)
        asset={'id':42,'name':'archive.tar.gz'}
        with patch.object(c,'run',return_value=json.dumps(dict(self.draft(),assets=[asset]))) as run, \
             patch.object(c,'download_asset') as download:
            c.download(asset['name'],Path('/unused'))
        run.assert_called_once_with('api','repos/owner/repo/releases/17')
        download.assert_called_once_with(asset,Path('/unused'))

class FakeGitHub(GitHub):
    repo='owner/repo';tag='v2.3.2'
    def __init__(self, failure=None):
        self.release_id=None
        self.assets={1:('package.tar.gz',b'old archive'),2:('checksums.txt',b'old checksum')}
        self.body='old release notes';self.failure=failure;self.failed=False;self.calls=[]
    def release(self):return {'body':self.body,'assets':[{'id':i,'name':n,'size':len(b),'digest':'sha256:'+hashlib.sha256(b).hexdigest()} for i,(n,b) in self.assets.items()]}
    def upload(self,path):self.assets[max(self.assets)+1]=(path.name,path.read_bytes());self.calls.append(('upload',path.name))
    def download(self,name,directory):
        data=next(b for n,b in self.assets.values() if n==name)
        (directory/name).write_bytes(data if self.failure!='bad_readback' or '.staged-' not in name else b'bad')
    def download_asset(self,asset,directory):
        (directory/asset['name']).write_bytes(self.assets[asset['id']][1])
    def rename(self,asset_id,name):
        self.calls.append(('rename',asset_id,name))
        if self.failure=='switch' and asset_id>2 and '.staged-' not in name and not self.failed:
            self.failed=True
            # Simulate successful server mutation followed by lost response.
            self.assets[asset_id]=(name,self.assets[asset_id][1]);raise RuntimeError('uncertain PATCH result')
        if name in [n for i,(n,b) in self.assets.items() if i!=asset_id]:raise RuntimeError('name collision')
        self.assets[asset_id]=(name,self.assets[asset_id][1])
    def notes(self,body):
        if self.failure=='notes' and body!='old release notes' and not self.failed:
            self.failed=True;raise RuntimeError('notes update failed')
        self.body=body

class RepairTests(unittest.TestCase):
    def fixture(self, unchanged=True):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);root=Path(t.name)
        client=FakeGitHub();files=[]
        for name,body in client.assets.values():
            path=root/name;path.write_bytes(body if unchanged else b'new bytes');files.append(path)
        return client,files,root/'journal.json'

    def test_all_conflicts_stop_before_any_upload(self):
        client,files,journal=self.fixture(False);before=copy.deepcopy(client.assets)
        new=journal.parent/'new.tar.gz';new.write_bytes(b'new');files.insert(0,new)
        client.assets[9]=('retired.tar.gz',b'old retired')
        with self.assertRaisesRegex(ValueError,'specific deletion confirmation'):
            repair(client,files,journal,retire=['retired.tar.gz'])
        state=json.loads(journal.read_text())
        self.assertEqual(state['phase'],'confirmation_required')
        self.assertEqual({a['id'] for a in state['conflicts']},{1,2,9})
        self.assertTrue(all(a['digest'].startswith('sha256:') for a in state['conflicts']))
        self.assertEqual(client.calls,[])
        for key in before:self.assertEqual(client.assets[key],before[key])

    def test_identical_files_are_noop_including_legacy_without_server_digest(self):
        for legacy in (False,True):
            client,files,journal=self.fixture();release=client.release
            if legacy:
                def missing_digest():
                    result=release()
                    for asset in result['assets']:asset.pop('digest')
                    return result
                client.release=missing_digest
            self.assertEqual(repair(client,files,journal)['phase'],'complete')
            self.assertEqual(client.calls,[])
            self.assertEqual(client.body,'old release notes')

    def test_legacy_readback_is_pinned_to_asset_id_during_name_reuse(self):
        client,files,journal=self.fixture();files=files[:1]
        original=b'oldAA';replacement=b'newBB';name=files[0].name
        client.assets={1:(name,original)};files[0].write_bytes(replacement)
        before=copy.deepcopy(client.assets)
        release=client.release;download=client.download;download_asset=client.download_asset
        pinned_ids=[]
        def missing_digest():
            result=release()
            for asset in result['assets']:asset.pop('digest')
            return result
        def while_name_is_reused(read):
            # An ABA race restores the original inventory before either recheck.
            client.assets[1]=('temporarily-hidden.tar.gz',original)
            client.assets[2]=(name,replacement)
            try:return read()
            finally:
                client.assets.pop(2);client.assets[1]=(name,original)
        def by_id(asset,directory):
            pinned_ids.append(asset['id'])
            return while_name_is_reused(lambda:download_asset(asset,directory))
        client.release=missing_digest
        client.download=lambda asset_name,directory:while_name_is_reused(lambda:download(asset_name,directory))
        client.download_asset=by_id
        with self.assertRaisesRegex(ValueError,'specific deletion confirmation'):
            repair(client,files,journal)
        state=json.loads(journal.read_text())
        self.assertEqual(state['phase'],'confirmation_required')
        self.assertEqual(state['conflicts'],[{
            'id':1,'name':name,'size':len(original),
            'digest':'sha256:'+hashlib.sha256(original).hexdigest(),
            'reason':'different_content',
            'replacement':{'bytes':len(replacement),'sha256':hashlib.sha256(replacement).hexdigest()}}])
        self.assertEqual(pinned_ids,[1])
        self.assertEqual(client.calls,[])
        self.assertEqual(client.assets,before)

    def test_missing_files_upload_under_final_names(self):
        client,files,journal=self.fixture();before=copy.deepcopy(client.assets)
        new=journal.parent/'new.tar.gz';new.write_bytes(b'new');files.append(new)
        self.assertEqual(repair(client,files,journal)['phase'],'complete')
        self.assertEqual(client.calls,[('upload','new.tar.gz')])
        for key in before:self.assertEqual(client.assets[key],before[key])
        self.assertEqual(client.assets[3],('new.tar.gz',b'new'))

    def test_uncertain_upload_can_be_retried_without_replacing_any_asset(self):
        client,files,journal=self.fixture();before=copy.deepcopy(client.assets)
        for name in ('new.tar.gz','new.tar.gz.sha256'):
            path=journal.parent/name;path.write_bytes(name.encode());files.append(path)
        upload=client.upload
        def uncertain(path):
            upload(path);raise RuntimeError('lost response')
        client.upload=uncertain
        with self.assertRaises(RuntimeError):repair(client,files,journal)
        self.assertEqual(json.loads(journal.read_text())['phase'],'incomplete')
        client.upload=upload
        self.assertEqual(repair(client,files,journal)['phase'],'complete')
        self.assertEqual(client.calls,[('upload','new.tar.gz'),('upload','new.tar.gz.sha256')])
        for key in before:self.assertEqual(client.assets[key],before[key])

    def test_bad_readback_stops_without_mutating_old_assets(self):
        client,files,journal=self.fixture();before=copy.deepcopy(client.assets)
        new=journal.parent/'new.tar.gz';new.write_bytes(b'new');files.append(new)
        client.download=lambda name,directory:(directory/name).write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'download verification'):repair(client,files,journal)
        for key in before:self.assertEqual(client.assets[key],before[key])
        self.assertFalse(any(call[0]=='rename' for call in client.calls))

    def test_concurrent_change_and_unknown_names_fail_closed(self):
        for race in (False,True):
            client,files,journal=self.fixture();release=client.release;count=[0]
            def changed():
                count[0]+=1
                if not race or count[0]==2:client.assets[99]=('human.tar.gz',b'human')
                return release()
            client.release=changed
            with self.assertRaises(ValueError):repair(client,files,journal)
            self.assertEqual(client.calls,[])

    def test_retirement_cannot_overlap_accepted_files(self):
        client,files,journal=self.fixture()
        with self.assertRaises(ValueError):repair(client,files,journal,retire=['package.tar.gz'])
        self.assertEqual(client.calls,[])

if __name__=='__main__':unittest.main()
