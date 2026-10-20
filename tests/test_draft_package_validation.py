"""Draft recovery must validate original bytes, without admitting public inputs."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import public_packages as packages
from resolved_contract import canonical
from test_discovered_inputs import fixture


class DraftPackageValidation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.version='v2.99.0';self.name=f'1panel-{self.version}-linux-amd64.tar.gz'
        raw=canonical(fixture());self.contract_sha=hashlib.sha256(raw).hexdigest()
        manifest={'schema_version':1,'publication_format':'package-only-v1','version':self.version,
            'architecture':'amd64','mode':'stable','resolved_contract_sha256':self.contract_sha,
            'files':{'build-metadata/resolved-source.json':{'size':len(raw),'sha256':self.contract_sha}}}
        archive=self.root/self.name
        with tarfile.open(archive,'w:gz') as tar:
            for name,body in [('manifest.json',json.dumps(manifest).encode()),
                              ('build-metadata/resolved-source.json',raw)]:
                member=tarfile.TarInfo(f'1panel-{self.version}-linux-amd64/'+name)
                member.size=len(body);member.mode=0o644;tar.addfile(member,io.BytesIO(body))
        self.original=archive.read_bytes();sha=hashlib.sha256(self.original).hexdigest()
        release={'id':17,'tag_name':self.version,'draft':True,'prerelease':False,'body':'',
            'assets':[{'id':91,'name':self.name,'size':len(self.original),'digest':'sha256:'+sha}]}
        class Client:
            repo='HandSonic/1Panel-Build-v2';tag=self.version
            def release(client):return copy.deepcopy(release)
            def download(client,name,directory):
                (Path(directory)/name).write_bytes(self.original)
        self.client=Client();self.release=release
        self.checkout=Path(__file__).resolve().parents[1]

    def test_missing_sidecar_requires_full_semantic_validator_on_original_archive(self):
        with patch('resolved_contract.apply_contract'),patch.object(packages.subprocess,'run') as validate:
            release,records=packages.verify_draft(self.client,self.version,self.checkout,self.contract_sha)
        self.assertTrue(release['draft'])
        self.assertEqual(records['amd64']['sha256'],hashlib.sha256(self.original).hexdigest())
        args=validate.call_args.args[0]
        self.assertIn('from validate_artifacts import validate_package',args[2])
        self.assertEqual(args[-2:],[self.version,'amd64'])
        self.assertEqual(Path(args[-3]).name,self.name)
        self.assertTrue(validate.call_args.kwargs['check'])

    def test_failed_semantic_validation_never_admits_existing_bytes(self):
        error=subprocess.CalledProcessError(1,['validator'])
        with patch('resolved_contract.apply_contract'),patch.object(packages.subprocess,'run',side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                packages.verify_draft(self.client,self.version,self.checkout,self.contract_sha)

    def test_different_current_contract_is_rejected_after_package_validation(self):
        with patch('resolved_contract.apply_contract'),patch.object(packages.subprocess,'run'):
            with self.assertRaisesRegex(ValueError,'differs from current validated inputs'):
                packages.verify_draft(self.client,self.version,self.checkout,'b'*64)

    def test_corrupt_archive_cannot_match_snapshot_by_name(self):
        self.original=b'changed after snapshot'
        with self.assertRaisesRegex(ValueError,'differs from GitHub snapshot'):
            packages.verify_draft(self.client,self.version,self.checkout,self.contract_sha)

    def test_draft_remains_invalid_for_default_public_source_reader(self):
        with self.assertRaisesRegex(ValueError,'Canonical public release identity mismatch'):
            packages.verify_existing(self.client,self.version,self.checkout)


if __name__=='__main__':unittest.main()
