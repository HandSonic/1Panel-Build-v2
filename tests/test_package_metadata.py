import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import package_metadata as pm
import public_packages as pp
from resolved_contract import canonical
from test_discovered_inputs import fixture

class PackageMetadata(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.value=fixture();self.raw=canonical(self.value)
        self.manifest={'schema_version':1,'publication_format':pm.FORMAT,'version':'v2.99.0',
            'architecture':'amd64','mode':'stable','resolved_contract_sha256':hashlib.sha256(self.raw).hexdigest(),
            'files':{'build-metadata/resolved-source.json':{'size':len(self.raw),'sha256':hashlib.sha256(self.raw).hexdigest()}}}
    def archive(self, *, marker=True, mode=0o644, raw=None):
        manifest=dict(self.manifest)
        if not marker:manifest.pop('publication_format')
        path=self.root/'1panel-v2.99.0-linux-amd64.tar.gz'
        with tarfile.open(path,'w:gz') as tf:
            for name,data,perm in [('manifest.json',json.dumps(manifest).encode(),0o644),('build-metadata/resolved-source.json',self.raw if raw is None else raw,mode)]:
                info=tarfile.TarInfo('1panel-v2.99.0-linux-amd64/'+name);info.size=len(data);info.mode=perm;tf.addfile(info,io.BytesIO(data))
        return path
    def test_normal_tar_metadata_roundtrip_and_public_file_filter(self):
        path=self.archive();manifest,files=pm.read(path,'v2.99.0','amd64')
        self.assertEqual(files,{'resolved-source.json':self.raw});self.assertEqual(manifest,self.manifest)
        pp.require_embedded([path],'v2.99.0')
        self.assertEqual(pp.package_files([path,Path(str(path)+'.sha256'),self.root/'checksums.txt']),[path,Path(str(path)+'.sha256')])
    def test_bad_metadata_never_falls_back(self):
        for kwargs in ({'mode':0o755},{'raw':b'{}'},{'marker':False}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):pm.read(self.archive(**kwargs),'v2.99.0','amd64')
    def test_packaging_adds_only_required_nonexecutable_inputs(self):
        config=self.root/'config';config.mkdir();(config/'resolved-source.json').write_bytes(self.raw)
        stage=self.root/'stage';stage.mkdir()
        pm.install(stage,config,dict(version='v2.99.0',mode='stable',resolved_contract_sha256=hashlib.sha256(self.raw).hexdigest()))
        path=stage/'build-metadata/resolved-source.json';self.assertEqual(path.read_bytes(),self.raw);self.assertFalse(path.stat().st_mode & 0o111)
    def test_public_inventory_preserves_old_controls_and_backups_not_unknown(self):
        names=['1panel-v2.99.0-linux-amd64.tar.gz','1panel-v2.99.0-linux-amd64.tar.gz.sha256','build-manifest.json','build-manifest.json.backup-123456789abc']
        release={'draft':False,'tag_name':'v2.99.0','assets':[{'name':n} for n in names]}
        self.assertEqual(set(pp.inventory(release,'v2.99.0')),{'amd64'})
        release['assets'].append({'name':'unrelated'})
        with self.assertRaises(ValueError):pp.inventory(release,'v2.99.0')
    def test_control_migration_retains_old_bytes_and_rolls_back_on_failure(self):
        from test_release_repair import FakeGitHub
        from release_asset_repair import repair
        for failure in (None,'switch'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);client=FakeGitHub(failure)
                archive=root/'package.tar.gz';archive.write_bytes(b'new package')
                sha=root/'package.tar.gz.sha256';sha.write_bytes(b'new checksum')
                if failure:
                    with self.assertRaises(RuntimeError):repair(client,[archive,sha],root/'journal.json',retire=['checksums.txt'])
                    self.assertEqual(client.assets[2],('checksums.txt',b'old checksum'))
                else:
                    repair(client,[archive,sha],root/'journal.json',retire=['checksums.txt'])
                    self.assertTrue(client.assets[2][0].startswith('checksums.txt.backup-'))
                    self.assertEqual(client.assets[2][1],b'old checksum')
                    self.assertNotIn('checksums.txt',[row[0] for row in client.assets.values()])
