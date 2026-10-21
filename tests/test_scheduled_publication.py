"""Daily publication reads existing metadata and transfers only missing files."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import automatic_publication as automatic
import test_automatic_publication as automatic_tests
import test_publication_recovery as recovery_tests


class ScheduledPublication(unittest.TestCase):
    setUp=automatic_tests.PrepareControlFlow.setUp
    payload=automatic_tests.PrepareControlFlow.payload
    old_release=automatic_tests.PrepareControlFlow.old_release

    def remote(self,successful=('amd64',)):
        previous,records=self.old_release(successful)
        release=previous.release()
        remote=recovery_tests.RemoteRelease(automatic_tests.Client.tag,release,[])
        remote.contents={asset['id']:(self.root/'previous'/asset['name']).read_bytes()
                         for asset in release['assets']}
        return remote,records

    def prepare(self,remote,event='schedule'):
        with patch.dict(os.environ,GITHUB_EVENT_NAME=event),\
             patch.object(automatic,'GitHub',return_value=remote):
            automatic.prepare(self.args)
        plan=json.loads((self.args.work/'control'/'automatic-plan.json').read_text())
        return plan

    def publish(self,remote,event='schedule'):
        proof=self.args.work/'control'/automatic.PROOF
        with patch.dict(os.environ,GITHUB_EVENT_NAME=event,
             EXPECTED_VALIDATION_RECEIPT_SHA256=automatic_tests.facts(proof)['sha256']):
            return automatic.publish(self.args,remote)

    def test_normal_build_preparation_uses_only_existing_metadata(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,_=self.remote()
        # Older release assets can predate GitHub's digest field. Daily gap
        # checks still need no historical package downloads or source controls.
        for asset in remote.releases[8]['assets']:asset.pop('digest')
        with patch.object(remote,'download',side_effect=AssertionError('Existing package download is forbidden')),\
             patch.object(remote,'download_asset',side_effect=AssertionError('Existing asset download is forbidden')):
            plan=self.prepare(remote,event='workflow_dispatch')
        self.assertEqual(plan['operation'],'append')
        self.assertEqual(set(plan['preserved']),{asset['name'] for asset in remote.releases[8]['assets']})

    def test_schedule_append_transfers_only_new_successful_architecture(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,_=self.remote();before=copy.deepcopy(remote.releases[8]['assets'])
        plan=self.prepare(remote)
        self.assertEqual(plan['operation'],'append')
        self.assertEqual(self.publish(remote),'published')
        new_names={f'1panel-{remote.tag}-linux-arm64.tar.gz'+suffix for suffix in ('','.sha256')}
        self.assertEqual({name for _,name in remote.uploads},new_names)
        self.assertTrue(all(release_id==8 for release_id,_ in remote.uploads))
        after={asset['id']:asset for asset in remote.releases[8]['assets']}
        for asset in before:self.assertEqual(after[asset['id']],asset)
        self.assertEqual(remote.downloads,[])

    def test_preserved_asset_id_or_digest_drift_rejects_without_download_or_write(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,_=self.remote();self.prepare(remote)
        baseline=copy.deepcopy(remote.releases[8])
        for field,value in (('id',999),('digest','sha256:'+'c'*64)):
            with self.subTest(field=field):
                remote.releases[8]=copy.deepcopy(baseline)
                remote.releases[8]['assets'][0][field]=value
                with self.assertRaises(ValueError):self.publish(remote)
                self.assertEqual(remote.uploads,[])
                self.assertEqual(remote.downloads,[])

    def partial(self,suffix,*,matching):
        remote,_=self.remote()
        name=f'1panel-{remote.tag}-linux-amd64.tar.gz'+suffix
        asset=next(row for row in remote.releases[8]['assets'] if row['name']==name)
        remote.releases[8]['assets']=[asset]
        if matching:
            path=self.source/name
            asset.update(size=path.stat().st_size,digest='sha256:'+automatic_tests.facts(path)['sha256'])
            remote.contents={asset['id']:path.read_bytes()}
        else:
            remote.contents={asset['id']:(self.root/'previous'/name).read_bytes()}
        return remote,copy.deepcopy(asset)

    def test_partial_pair_uses_old_archive_digest_or_matches_existing_checksum(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        for index,suffix in enumerate(('','.sha256')):
            with self.subTest(existing_suffix=suffix):
                self.args.work=self.root/f'work-{index}'
                remote,existing=self.partial(suffix,matching=bool(suffix))
                plan=self.prepare(remote,event='workflow_dispatch')
                self.assertEqual(plan['operation'],'append')
                self.assertEqual(set(plan['preserved']),{existing['name']})
                original={path.name:path.read_bytes() for path in (self.args.work/'release').iterdir()}
                proof=self.args.work/'control'/automatic.PROOF;receipt=proof.read_bytes()
                self.assertEqual(self.publish(remote,event='workflow_dispatch'),'published')
                missing=f'1panel-{remote.tag}-linux-amd64.tar.gz'+('' if suffix else '.sha256')
                self.assertEqual(remote.uploads,[(8,missing)])
                self.assertNotIn(existing['id'],remote.downloads)
                self.assertEqual(next(row for row in remote.releases[8]['assets'] if row['id']==existing['id']),existing)
                if not suffix:
                    uploaded=next(row for row in remote.releases[8]['assets'] if row['name']==missing)
                    expected=(existing['digest'][7:]+'  '+existing['name']+'\n').encode()
                    self.assertEqual(remote.contents[uploaded['id']],expected)
                    self.assertNotEqual(expected,original[missing])
                self.assertEqual({path.name:path.read_bytes() for path in (self.args.work/'release').iterdir()},original)
                self.assertEqual(proof.read_bytes(),receipt)

    def test_invalid_archive_digest_or_conflicting_orphan_checksum_rejects_before_transfer(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        for index,(suffix,invalid) in enumerate((('',None),('','sha256:short'),('.sha256',None))):
            with self.subTest(suffix=suffix,digest=invalid):
                self.args.work=self.root/f'work-{index}'
                remote,existing=self.partial(suffix,matching=False)
                if not suffix:remote.releases[8]['assets'][0]['digest']=invalid
                plan=self.prepare(remote)
                self.assertEqual(plan['operation'],'append')
                with self.assertRaises(ValueError):self.publish(remote)
                self.assertEqual(remote.uploads,[])
                self.assertEqual(remote.downloads,[])


if __name__=='__main__':unittest.main()
