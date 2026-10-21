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

    def test_hash_only_contract_normalization(self):
        normalized=production(self.source,'v2.99.0','core','stable')
        vendor=FakeVendor();vendor.files['core/cmd/server/conf/app.yaml']=self.source
        vendor.commit=lambda *args:self.commit
        value=fixture(vendor=vendor)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);install_fixture(root,value)
            with patch.object(ec,'open_vendor_source',side_effect=lambda *args:self.response()):
                self.assertEqual(ec.expected_bytes('v2.99.0','core',root),(self.source,normalized,self.commit))
                profile=value['configuration']['core']
                profile['normalized_sha256']='0'*64;install_fixture(root,value)
                with self.assertRaises(ValueError):ec.expected_bytes('v2.99.0','core',root)
                profile['normalized_sha256']=hashlib.sha256(normalized).hexdigest()
                profile['source_file']='anything';install_fixture(root,value)
                with self.assertRaises(ValueError):ec.expected_bytes('v2.99.0','core',root)


if __name__=='__main__':unittest.main()
