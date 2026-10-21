import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from release_asset_repair import repair

class FakeGitHub:
    repo='owner/repo';tag='v2.3.2'
    def __init__(self):
        self.assets={1:('package.tar.gz',b'old archive'),2:('checksums.txt',b'old checksum')}
        self.body='old release notes';self.calls=[];self.downloads=[]
    def release(self):return {'body':self.body,'assets':[{'id':i,'name':n,'size':len(b),'digest':'sha256:'+hashlib.sha256(b).hexdigest()} for i,(n,b) in self.assets.items()]}
    def bind_release(self,release_id):self.release_id=release_id
    def upload(self,path):self.assets[max(self.assets)+1]=(path.name,path.read_bytes());self.calls.append(('upload',path.name))
    def download(self,name,directory):
        self.downloads.append(('name',name))
        data=next(b for n,b in self.assets.values() if n==name)
        (directory/name).write_bytes(data)
    def download_asset(self,asset,directory):
        self.downloads.append(('id',asset['id']))
        (directory/asset['name']).write_bytes(self.assets[asset['id']][1])

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
        with self.assertRaisesRegex(ValueError,'specific deletion confirmation'):
            repair(client,files,journal)
        state=json.loads(journal.read_text())
        self.assertEqual(state['phase'],'confirmation_required')
        self.assertEqual({a['id'] for a in state['conflicts']},{1,2})
        self.assertTrue(all(a['digest'].startswith('sha256:') for a in state['conflicts']))
        self.assertEqual(client.calls,[])
        for key in before:self.assertEqual(client.assets[key],before[key])

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

    def test_uploaded_server_digest_or_size_mismatch_stops_without_mutating_old_assets(self):
        for field,value in (('digest','sha256:'+'f'*64),('size',99)):
            with self.subTest(field=field):
                client,files,journal=self.fixture();before=copy.deepcopy(client.assets)
                new=journal.parent/'new.tar.gz';new.write_bytes(b'new');files.append(new)
                release=client.release
                def mismatched_metadata():
                    result=release()
                    for asset in result['assets']:
                        if asset['name']==new.name:asset[field]=value
                    return result
                client.release=mismatched_metadata
                with self.assertRaisesRegex(ValueError,'server digest/size mismatch'):
                    repair(client,files,journal)
                for key in before:self.assertEqual(client.assets[key],before[key])
                self.assertEqual(client.downloads,[])
                self.assertEqual(json.loads(journal.read_text())['phase'],'incomplete')

if __name__=='__main__':unittest.main()
