import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import embedded_configuration as ec
import configure_release
from semantic_configuration import production

class VendorConfigurationTests(unittest.TestCase):
    commit='1'*40
    source=b'base:\n  mode: dev\n  version: old\nlog:\n  level: debug\n'

    def setUp(self):
        ec.fetch_vendor_source.cache_clear()

    def response(self,data=None,url=None):
        stream=io.BytesIO(self.source if data is None else data)
        stream.geturl=lambda:url or f'https://raw.githubusercontent.com/1Panel-dev/1Panel/{self.commit}/core/cmd/server/conf/app.yaml'
        return stream

    def fetch(self,**kwargs):
        args=dict(commit=self.commit,component='core',source_path='core/cmd/server/conf/app.yaml',source_bytes=len(self.source),source_sha256=hashlib.sha256(self.source).hexdigest())
        args.update(kwargs)
        return ec.fetch_vendor_source(**args)

    def test_exact_source_and_bounded_cached_read(self):
        with patch.object(ec,'open_vendor_source',return_value=self.response()) as opener:
            self.assertEqual(self.fetch(),self.source)
            self.assertEqual(self.fetch(),self.source)
            opener.assert_called_once_with(f'https://raw.githubusercontent.com/1Panel-dev/1Panel/{self.commit}/core/cmd/server/conf/app.yaml')

    def test_invalid_identity_never_opens_network(self):
        with patch.object(ec,'open_vendor_source') as opener:
            for overrides in [dict(commit='main'),dict(commit='A'*40),dict(component='other'),dict(source_path='../app.yaml'),dict(source_path='https://example.com/a'),dict(source_bytes=0),dict(source_bytes=True),dict(source_bytes=65537),dict(source_sha256='bad')]:
                with self.subTest(overrides=overrides),self.assertRaises(ValueError):self.fetch(**overrides)
            opener.assert_not_called()

    def test_corruption_truncation_oversize_redirect_and_network_fail_closed(self):
        for data,url in [(self.source[:-1],None),(self.source+b'x',None),(b'x'*len(self.source),None),(self.source,'https://example.com/source')]:
            with patch.object(ec,'open_vendor_source',return_value=self.response(data,url)),self.assertRaises(ValueError):self.fetch()
        with patch.object(ec,'open_vendor_source',side_effect=OSError('unavailable')),self.assertRaises(ValueError):self.fetch()
        with self.assertRaises(ValueError):ec.NoVendorRedirect().redirect_request(None,None,None,None,None,None)

    def test_hash_only_profile_normalization_and_binary_checks(self):
        normalized=production(self.source,'v2.1.9','core','stable')
        profile={'source_acquisition':'immutable_vendor_https','source_path':'core/cmd/server/conf/app.yaml','source_bytes':len(self.source),'source_sha256':hashlib.sha256(self.source).hexdigest(),'normalized_sha256':hashlib.sha256(normalized).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir()
            registry={'v2.1.9':{'source_commit':self.commit,'mode':'stable','components':{'core':profile}}}
            path=root/'config/embedded-configs.json'
            path.write_text(json.dumps(registry))
            with patch.object(ec,'open_vendor_source',return_value=self.response()):
                self.assertEqual(ec.expected_bytes('v2.1.9','core',root),(self.source,normalized,self.commit))
                ec.validate_binary(b'ELF'+normalized,'v2.1.9','core',self.commit,root)
                for binary in [self.source,normalized+self.source]:
                    with self.assertRaises(ValueError):ec.validate_binary(binary,'v2.1.9','core',self.commit,root)
                with self.assertRaises(ValueError):ec.validate_binary(normalized,'v2.1.9','core','2'*40,root)
                profile['normalized_sha256']='0'*64;path.write_text(json.dumps(registry))
                with self.assertRaises(ValueError):ec.expected_bytes('v2.1.9','core',root)
                profile['source_file']='anything';path.write_text(json.dumps(registry))
                with self.assertRaises(ValueError):ec.expected_bytes('v2.1.9','core',root)

    def test_real_hash_only_profiles_have_no_local_payload(self):
        registry=json.loads((ROOT/'config/embedded-configs.json').read_text())
        for version,entry in registry.items():
            for component,profile in entry['components'].items():
                if profile.get('source_acquisition')!='immutable_vendor_https':continue
                self.assertNotIn('source_file',profile)
                self.assertEqual(profile['source_path'],f'{component}/cmd/server/conf/app.yaml')
                self.assertFalse((ROOT/f'config/embedded-configs/{version}/{component}.source.yaml').exists())

if __name__=='__main__':unittest.main()
