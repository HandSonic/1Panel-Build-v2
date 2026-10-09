import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from embedded_configuration import expected_bytes,validate_binary
from test_discovered_inputs import fixture,synthetic_runtime,configuration_shapes


class EmbeddedConfigurationTests(unittest.TestCase):
 def test_source_shapes_and_channels_without_version_registry(self):
  for shape,vendor in configuration_shapes():
   for version,mode in [('v2.99.0','stable'),('v2.987.65-beta.4','beta'),('v2.456.78-dev.9','dev')]:
    value=fixture(version,mode,vendor)
    with self.subTest(shape=shape,mode=mode),tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp),value,vendor):
     for component in ('core','agent'):
      old,new,commit=expected_bytes(version,component)
      self.assertEqual(old,vendor.files[f'{component}/cmd/server/conf/app.yaml'])
      self.assertNotEqual(old,new)
      validate_binary(b'ELF fixture'+new,version,component,commit)
      for binary in (b'ELF fixture'+old,new+old,b'ELF without config'):
       with self.assertRaises(ValueError):validate_binary(binary,version,component,commit)
      if component=='core':self.assertIn(('  version: '+version).encode(),new)
      else:self.assertNotIn(b'  version:',new)
      self.assertIn(('  mode: '+mode).encode(),new)
      self.assertIn(b'  level: info',new)
      if shape=='flags':
       for flag in ('is_demo','is_offline','is_fxplay','is_enterprise'):
        self.assertIn(('  '+flag+': false').encode(),new)

 def test_optional_legacy_fields_and_trailing_newline_are_preserved(self):
  for shape,vendor in configuration_shapes():
   if shape not in ('legacy','minimal'):continue
   for newline in (True,False):
    for component in ('core','agent'):
     key=f'{component}/cmd/server/conf/app.yaml'
     vendor.files[key]=vendor.files[key].rstrip(b'\n')+(b'\n' if newline else b'')
    value=fixture(vendor=vendor)
    with self.subTest(shape=shape,newline=newline),tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp),value,vendor):
     for component in ('core','agent'):
      old,new,_=expected_bytes(value['version'],component)
      self.assertEqual(old.endswith(b'\n'),new.endswith(b'\n'))
      self.assertNotIn(b'is_enterprise:',new)
      self.assertNotIn(b'is_fxplay:',new)
      if shape=='legacy':
       self.assertIn(b'remote_url:',new)
       self.assertIn(b'  is_intl: false',new)

 def test_wrong_source_or_other_run_version_is_not_success(self):
  with tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp)):
   _,new,commit=expected_bytes('v2.99.0','core')
   with self.assertRaises(ValueError):validate_binary(new,'v2.99.0','core','0'*40)
   with self.assertRaises(ValueError):expected_bytes('v2.98.0','core')

if __name__=='__main__':unittest.main()
