import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import package_metadata as pm
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
    def test_normal_tar_metadata_roundtrip(self):
        path=self.archive();manifest,files=pm.read(path,'v2.99.0','amd64')
        self.assertEqual(files,{'resolved-source.json':self.raw});self.assertEqual(manifest,self.manifest)
    def test_bad_metadata_never_falls_back(self):
        for kwargs in ({'mode':0o755},{'raw':b'{}'},{'marker':False}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):pm.read(self.archive(**kwargs),'v2.99.0','amd64')
    def test_legacy_repack_preserves_payload_bytes_modes_build_identity_and_is_idempotent(self):
        manifest=dict(self.manifest)
        manifest.pop('publication_format')
        payloads={'1panel-core':(b'original binary bytes',0o755),'install.sh':(b'original installer bytes',0o751)}
        manifest['files']={name:{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,(body,mode) in payloads.items()}
        manifest['build_repository_commit']='a'*40
        archive=self.root/'1panel-v2.99.0-linux-amd64.tar.gz'
        with tarfile.open(archive,'w:gz') as tf:
            for name,(body,mode) in {**payloads,'manifest.json':(canonical(manifest),0o644)}.items():
                info=tarfile.TarInfo('1panel-v2.99.0-linux-amd64/'+name);info.size=len(body);info.mode=mode;tf.addfile(info,io.BytesIO(body))
        (self.root/'resolved-source.json').write_bytes(self.raw)
        row={'file':archive.name,'architecture':'amd64','resolved_contract_sha256':hashlib.sha256(self.raw).hexdigest(),'build_repository_commit':'a'*40}
        (self.root/'build-manifest.json').write_text(json.dumps({'artifacts':[row]}))
        self.assertTrue(pm.embed_legacy(self.root,'v2.99.0'))
        with tarfile.open(archive) as tf:
            for name,(body,mode) in payloads.items():
                member=tf.getmember('1panel-v2.99.0-linux-amd64/'+name)
                self.assertEqual(tf.extractfile(member).read(),body);self.assertEqual(member.mode,mode)
        migrated,_=pm.read(archive,'v2.99.0','amd64')
        self.assertEqual(migrated['build_repository_commit'],'a'*40)
        result=json.loads((self.root/'build-manifest.json').read_text())['artifacts'][0]
        self.assertEqual(result['build_repository_commit'],'a'*40)
        self.assertEqual(result['sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
        before=archive.read_bytes();self.assertFalse(pm.embed_legacy(self.root,'v2.99.0'));self.assertEqual(archive.read_bytes(),before)

    def test_legacy_repack_rejects_unbound_source_without_changing_archive(self):
        path=self.archive();before=path.read_bytes()
        (self.root/'resolved-source.json').write_bytes(self.raw)
        (self.root/'build-manifest.json').write_text(json.dumps({'artifacts':[{'file':path.name,'architecture':'amd64','resolved_contract_sha256':'0'*64}]}))
        with self.assertRaises(ValueError):pm.embed_legacy(self.root,'v2.99.0')
        self.assertEqual(path.read_bytes(),before)

    def test_legacy_repack_rejects_metadata_directory_collision(self):
        for name in ('build-metadata','./build-metadata'):
            with self.subTest(name=name):
                manifest=dict(self.manifest);manifest.pop('publication_format')
                body=b'original payload'
                manifest['files']={name:{'size':len(body),'sha256':hashlib.sha256(body).hexdigest()}}
                path=self.root/'1panel-v2.99.0-linux-amd64.tar.gz'
                with tarfile.open(path,'w:gz') as tf:
                    for rel,data in [(name,body),('manifest.json',canonical(manifest))]:
                        info=tarfile.TarInfo('1panel-v2.99.0-linux-amd64/'+rel);info.size=len(data);info.mode=0o644;tf.addfile(info,io.BytesIO(data))
                (self.root/'resolved-source.json').write_bytes(self.raw)
                (self.root/'build-manifest.json').write_text(json.dumps({'artifacts':[{'file':path.name,'architecture':'amd64','resolved_contract_sha256':hashlib.sha256(self.raw).hexdigest()}]}))
                before=path.read_bytes()
                with self.assertRaisesRegex(ValueError,'conflicts with metadata directory'):pm.embed_legacy(self.root,'v2.99.0')
                self.assertEqual(path.read_bytes(),before)
