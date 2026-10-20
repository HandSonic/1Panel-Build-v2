"""Automatic retries retain authenticated original draft bytes across builds."""
import copy
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import test_automatic_publication as fixtures
import test_draft_publication as drafts
import automatic_publication as automatic
from release_asset_repair import repair,verify_new_release


class AutomaticDraftReuse(unittest.TestCase):
    setUp=fixtures.AutomaticTransaction.setUp
    save_plan=fixtures.AutomaticTransaction.save_plan
    producer=fixtures.AutomaticTransaction.producer
    repair=fixtures.AutomaticTransaction.repair

    def existing(self,*,hidden=False):
        source=self.work/'release'/'resolved-source.json'
        source.write_bytes(b'current validated source contract')
        self.contract_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        release=drafts.draft('b'*40)
        self.name=self.files[0].name
        self.original=b'original independently validated archive, with older tar timestamps'
        release['assets']=[dict(id=91,name=self.name,size=len(self.original),
            digest='sha256:'+hashlib.sha256(self.original).hexdigest())]
        client=drafts.TransportGitHub(release);client.bytes[91]=self.original
        self.records={'amd64':dict(file=self.name,sha256=hashlib.sha256(self.original).hexdigest(),
            size=len(self.original),resolved_contract_sha256=self.contract_sha,
            publication_format='package-only-v1')}
        if not hidden:
            self.plan.update(operation='resume',before=automatic.snapshot(release),
                draft_identity=automatic.draft_identity(release),
                preserved={asset['name']:automatic.asset_identity(asset) for asset in release['assets']})
            self.save_plan()
        return client

    def publish(self,client,verify=None):
        verified=(verify if verify is not None else lambda c,*args:(c.release(),self.records))
        with patch.object(automatic,'optional_release',drafts.OPTIONAL_RELEASE), \
             patch.object(automatic,'repair',repair), \
             patch('public_packages.verify_draft',side_effect=verified):
            return automatic.publish(self.args,client)

    def test_changed_rebuild_bytes_preserve_original_id_and_derive_missing_sidecar(self):
        client=self.existing();before=copy.deepcopy(client.value['assets'][0])
        self.assertNotEqual(self.files[0].read_bytes(),self.original)
        self.assertEqual(self.publish(client),'published')
        self.assertEqual(client.value['assets'][0],before)
        self.assertEqual(client.bytes[91],self.original)
        sidecar=next(asset for asset in client.value['assets'] if asset['name']==self.name+'.sha256')
        self.assertEqual(client.bytes[sidecar['id']],
            (hashlib.sha256(self.original).hexdigest()+'  '+self.name+'\n').encode())
        self.assertFalse(client.value['draft'])
        self.assertEqual(client.value['target_commitish'],os.environ['GITHUB_SHA'])

    def test_writer_discovers_nonempty_draft_hidden_from_readonly_preparation(self):
        client=self.existing(hidden=True)
        self.assertEqual(self.publish(client),'published')
        self.assertFalse(any(call[:4]==('api','--method','POST',f'repos/{client.repo}/releases')
                             for call in client.calls))
        self.assertEqual(client.bytes[91],self.original)

    def test_union_retains_original_architecture_missing_from_current_successes(self):
        client=self.existing();before=copy.deepcopy(client.value['assets'][0])
        self.files=self.files[2:]
        for suffix,raw in [('',b'new arm64'),('.sha256',b'new arm64 checksum')]:
            path=self.work/'release'/f'1panel-{client.tag}-linux-arm64.tar.gz{suffix}'
            path.write_bytes(raw);self.files.append(path)
        self.proof={'files':{path.name:fixtures.facts(path) for path in self.files}}
        self.proofpath.write_text(json.dumps(self.proof))
        self.plan['receipt_sha256']=fixtures.facts(self.proofpath)['sha256'];self.save_plan()
        os.environ['EXPECTED_VALIDATION_RECEIPT_SHA256']=self.plan['receipt_sha256']
        self.assertEqual(self.publish(client),'published')
        self.assertEqual(client.value['assets'][0],before)
        self.assertEqual(len(client.value['assets']),4)

    def test_invalid_original_is_rejected_before_retarget_or_upload(self):
        client=self.existing()
        def rejected(*args):raise ValueError('original package semantic validation failed')
        with self.assertRaisesRegex(ValueError,'semantic validation failed'):self.publish(client,rejected)
        self.assertFalse(any(call[:3] in (('api','--method','POST'),('api','--method','PATCH'))
                             for call in client.calls))
        self.assertTrue(client.value['draft'])

    def test_original_reuse_keeps_bot_ownership_and_ancestry_gates(self):
        client=self.existing();client.value['author']={'id':5,'login':'someone','type':'User'}
        with self.assertRaises(ValueError):self.publish(client)
        client.value['author']=drafts.draft()['author'];client.ancestor=False
        with self.assertRaisesRegex(ValueError,'not an ancestor'):self.publish(client)
        self.assertFalse(any(call[:3] in (('api','--method','POST'),('api','--method','PATCH'))
                             for call in client.calls))

    def test_finalization_rejects_same_bytes_replacement_of_preserved_asset_id(self):
        client=self.existing();original=copy.deepcopy(client.value)
        preserved={asset['name']:asset for asset in original['assets']}
        for visibility in (True,False):
            release=copy.deepcopy(original);release['draft']=visibility;release['assets'][0]['id']=92
            with self.subTest(draft=visibility),self.assertRaisesRegex(ValueError,'Preserved release asset identity changed'):
                verify_new_release(release,original,[],visibility,preserved_assets=preserved)


if __name__=='__main__':unittest.main()
