import hashlib
import io
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
from test_discovered_inputs import FakeVendor, fixture, install_fixture

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
        response=self.response();read=response.read;sizes=[]
        def bounded_read(size):
            sizes.append(size)
            return read(size)
        response.read=bounded_read
        with patch.object(ec,'open_vendor_source',return_value=response) as opener:
            self.assertEqual(self.fetch(),self.source)
            self.assertEqual(self.fetch(),self.source)
            opener.assert_called_once_with(f'https://raw.githubusercontent.com/1Panel-dev/1Panel/{self.commit}/core/cmd/server/conf/app.yaml')
            self.assertEqual(sizes,[len(self.source)+1])

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

    def test_hash_only_contract_normalization_and_binary_checks(self):
        normalized=production(self.source,'v2.99.0','core','stable')
        vendor=FakeVendor();vendor.files['core/cmd/server/conf/app.yaml']=self.source
        vendor.commit=lambda *args:self.commit
        value=fixture(vendor=vendor)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);install_fixture(root,value)
            with patch.object(ec,'open_vendor_source',side_effect=lambda *args:self.response()):
                self.assertEqual(ec.expected_bytes('v2.99.0','core',root),(self.source,normalized,self.commit))
                ec.validate_binary(b'ELF'+normalized,'v2.99.0','core',self.commit,root)
                for binary in [self.source,normalized+self.source,b'ELF without configuration']:
                    with self.assertRaises(ValueError):ec.validate_binary(binary,'v2.99.0','core',self.commit,root)
                with self.assertRaises(ValueError):ec.validate_binary(normalized,'v2.99.0','core','2'*40,root)
                profile=value['configuration']['core']
                profile['normalized_sha256']='0'*64;install_fixture(root,value)
                with self.assertRaises(ValueError):ec.expected_bytes('v2.99.0','core',root)
                profile['normalized_sha256']=hashlib.sha256(normalized).hexdigest()
                profile['source_file']='anything';install_fixture(root,value)
                with self.assertRaises(ValueError):ec.expected_bytes('v2.99.0','core',root)

    def test_contract_has_hashes_only_and_legacy_payloads_are_never_read(self):
        value=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);install_fixture(root,value)
            for name in ('sources.json','embedded-configs.json','frontend-lock-repairs.json'):
                (root/'config'/name).write_text('poisoned legacy registry')
            for component,profile in value['configuration'].items():
                self.assertEqual(set(profile),{'path','source_bytes','source_sha256','normalized_sha256'})
                self.assertEqual(profile['path'],f'{component}/cmd/server/conf/app.yaml')
                self.assertFalse((root/'config/embedded-configs').exists())
            original_read=Path.read_text
            def guard(path,*args,**kwargs):
                if path.name in ('sources.json','embedded-configs.json','frontend-lock-repairs.json'):
                    raise AssertionError('Legacy registry read')
                return original_read(path,*args,**kwargs)
            vendor=FakeVendor()
            def response(url):
                prefix='https://raw.githubusercontent.com/1Panel-dev/1Panel/'+'a'*40+'/'
                self.assertTrue(url.startswith(prefix))
                return self.response(vendor.files[url[len(prefix):]],url)
            with patch.object(Path,'read_text',guard),patch.object(ec,'open_vendor_source',side_effect=response):
                for component in ('core','agent'):
                    self.assertEqual(ec.expected_bytes('v2.99.0',component,root)[0],vendor.files[f'{component}/cmd/server/conf/app.yaml'])

if __name__=='__main__':unittest.main()
