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

    def prepare(self,remote,event='schedule',records=None):
        with patch.dict(os.environ,GITHUB_EVENT_NAME=event),\
             patch.object(automatic,'GitHub',return_value=remote),\
             patch('public_packages.verify_existing',return_value=(remote.release(),records)) as strict:
            automatic.prepare(self.args)
        plan=json.loads((self.args.work/'control'/'automatic-plan.json').read_text())
        return plan,strict

    def publish(self,remote):
        proof=self.args.work/'control'/automatic.PROOF
        with patch.dict(os.environ,GITHUB_EVENT_NAME='schedule',
             EXPECTED_VALIDATION_RECEIPT_SHA256=automatic_tests.facts(proof)['sha256']):
            return automatic.publish(self.args,remote)

    def test_schedule_preparation_uses_only_existing_metadata(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,_=self.remote()
        # Older release assets can predate GitHub's digest field. Daily gap
        # checks still need no historical package readback or strict verifier.
        for asset in remote.releases[8]['assets']:asset.pop('digest')
        with patch.object(remote,'download',side_effect=AssertionError('Existing package download is forbidden')),\
             patch.object(remote,'download_asset',side_effect=AssertionError('Existing asset download is forbidden')):
            plan,strict=self.prepare(remote)
        strict.assert_not_called()
        self.assertTrue(plan['metadata_only'])
        self.assertEqual(plan['operation'],'append')
        self.assertEqual(set(plan['preserved']),{asset['name'] for asset in remote.releases[8]['assets']})

    def test_explicit_workflow_dispatch_keeps_strict_public_package_verification(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,records=self.remote()
        plan,strict=self.prepare(remote,event='workflow_dispatch',records=records)
        strict.assert_called_once_with(remote,automatic_tests.Client.tag,automatic.ROOT)
        self.assertFalse(plan.get('metadata_only',False))

    def test_schedule_append_transfers_only_new_successful_architecture(self):
        self.payload(self.source,('arm64',),11,'b'*40)
        remote,_=self.remote();before=copy.deepcopy(remote.releases[8]['assets'])
        plan,strict=self.prepare(remote)
        self.assertEqual(plan['operation'],'append');strict.assert_not_called()
        self.assertEqual(self.publish(remote),'published')
        new_names={f'1panel-{remote.tag}-linux-arm64.tar.gz'+suffix for suffix in ('','.sha256')}
        self.assertEqual({name for _,name in remote.uploads},new_names)
        self.assertTrue(all(release_id==8 for release_id,_ in remote.uploads))
        after={asset['id']:asset for asset in remote.releases[8]['assets']}
        for asset in before:self.assertEqual(after[asset['id']],asset)
        new_ids={asset['id'] for asset in after.values() if asset['name'] in new_names}
        self.assertEqual(set(remote.downloads),new_ids)
        self.assertFalse(set(remote.downloads)&{asset['id'] for asset in before})

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

    def test_partial_pair_matching_current_candidate_only_uploads_the_missing_member(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        for index,suffix in enumerate(('','.sha256')):
            with self.subTest(existing_suffix=suffix):
                self.args.work=self.root/f'work-{index}'
                remote,existing=self.partial(suffix,matching=True)
                plan,strict=self.prepare(remote)
                self.assertEqual(plan['operation'],'append');strict.assert_not_called()
                self.assertEqual(set(plan['preserved']),{existing['name']})
                self.assertEqual(self.publish(remote),'published')
                missing=f'1panel-{remote.tag}-linux-amd64.tar.gz'+('' if suffix else '.sha256')
                self.assertEqual(remote.uploads,[(8,missing)])
                self.assertNotIn(existing['id'],remote.downloads)
                self.assertEqual(next(row for row in remote.releases[8]['assets'] if row['id']==existing['id']),existing)

    def test_partial_pair_candidate_mismatch_rejects_before_any_transfer(self):
        self.payload(self.source,('amd64',),11,'b'*40)
        for index,field in enumerate(('digest','size')):
            with self.subTest(field=field):
                self.args.work=self.root/f'work-{index}'
                remote,existing=self.partial('',matching=True)
                if field=='digest':remote.releases[8]['assets'][0]['digest']='sha256:'+'c'*64
                else:remote.releases[8]['assets'][0]['size']+=1
                plan,strict=self.prepare(remote)
                self.assertEqual(plan['operation'],'append');strict.assert_not_called()
                with self.assertRaises(ValueError):self.publish(remote)
                self.assertEqual(remote.uploads,[])
                self.assertEqual(remote.downloads,[])


if __name__=='__main__':unittest.main()
