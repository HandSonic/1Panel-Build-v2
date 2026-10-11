import copy
import tempfile
import unittest
from pathlib import Path
import manual_publication as manual
from release_asset_repair import repair
from test_release_repair import FakeGitHub

class LegacyArmRetirement(unittest.TestCase):
    def fixture(self, version='v2.0.8'):
        old=f'1panel-{version}-linux-arm.tar.gz'
        new=f'1panel-{version}-linux-armv7.tar.gz'
        proof={'outcomes':[{'architecture':'armv7','status':'success'}], 'files':{new:{},new+'.sha256':{}}}
        release={'assets':[{'name':old},{'name':old+'.sha256'}]}
        return old,new,proof,release

    def test_exact_pair_retires_for_any_valid_version(self):
        for version in ('v2.0.8','v2.9.99','v2.9.0-beta.1'):
            old,new,proof,release=self.fixture(version)
            self.assertEqual(manual.retired_upstream_assets(proof,version,release),[old,old+'.sha256'])

    def test_successful_verified_replacement_is_required(self):
        old,new,proof,release=self.fixture()
        for change in ('failed','missing-package','missing-sidecar','other-arch'):
            p=copy.deepcopy(proof)
            if change=='failed':p['outcomes'][0]['status']='failure'
            elif change=='other-arch':p['outcomes'][0]['architecture']='arm64'
            else:p['files'].pop(new if change=='missing-package' else new+'.sha256')
            self.assertNotIn(old,manual.retired_upstream_assets(p,'v2.0.8',release))

    def test_unknown_incomplete_duplicate_or_wrong_version_not_allowed(self):
        old,new,proof,release=self.fixture()
        for names in ([old],[old+'.sha256'],[old,old,old+'.sha256'],['1panel-v2.0.7-linux-arm.tar.gz','1panel-v2.0.7-linux-arm.tar.gz.sha256'],['unrelated.tar.gz','unrelated.tar.gz.sha256']):
            self.assertEqual(manual.retired_upstream_assets(proof,'v2.0.8',{'assets':[{'name':n} for n in names]}),[])

    def test_original_failure_retirement_preserved(self):
        old,new,proof,release=self.fixture();proof['outcomes'].append({'architecture':'s390x','status':'failure'})
        self.assertEqual(manual.retired_upstream_assets(proof,'v2.0.8',release),['1panel-v2.0.8-linux-s390x.tar.gz','1panel-v2.0.8-linux-s390x.tar.gz.sha256',old,old+'.sha256'])

    def test_pair_backup_retains_original_ids_and_bytes(self):
        self.run_transaction(None)

    def test_pair_is_restored_after_uncertain_switch(self):
        self.run_transaction('switch')

    def run_transaction(self,failure):
        old,new,proof,release=self.fixture()
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);client=FakeGitHub(failure)
            client.assets[9]=(old,b'old arm archive');client.assets[10]=(old+'.sha256',b'old arm checksum')
            files=[]
            for name in ('package.tar.gz','checksums.txt'):
                p=root/name;p.write_bytes(b'new bytes');files.append(p)
            retired=manual.retired_upstream_assets(proof,'v2.0.8',client.release())
            journal=root/'journal.json'
            if failure:
                with self.assertRaises(RuntimeError):repair(client,files,journal,retire=retired)
                self.assertEqual(client.assets[9],(old,b'old arm archive'))
                self.assertEqual(client.assets[10],(old+'.sha256',b'old arm checksum'))
            else:
                state=repair(client,files,journal,retire=retired)
                self.assertEqual(state['phase'],'complete')
                for asset_id,name,data in ((9,old,b'old arm archive'),(10,old+'.sha256',b'old arm checksum')):
                    self.assertTrue(client.assets[asset_id][0].startswith(name+'.backup-'))
                    self.assertEqual(client.assets[asset_id][1],data)
