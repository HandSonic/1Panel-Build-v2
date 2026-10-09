import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from discover_inputs import validate_lock_origins

class LockOriginTests(unittest.TestCase):
 def fixture(self):
  return {'packages':{'':{},'node_modules/parent':{'version':'1.0.0','resolved':'https://registry.npmjs.org/parent/-/parent-1.0.0.tgz','integrity':'sha512-'+'A'*86+'==','bundleDependencies':['@scope/child']},'node_modules/parent/node_modules/@scope/child':{'version':'1.2.3','inBundle':True}}}
 def test_direct_and_nested_bundle_share_exact_parent_archive(self):
  value=self.fixture();validate_lock_origins(value)
  value['packages']['node_modules/parent/node_modules/@scope/child/node_modules/nested']={'version':'2.0.0','inBundle':True}
  validate_lock_origins(value)
 def test_unbound_origin_or_bundle_metadata_reject(self):
  for case in ('parent','membership','integrity','origin','flag','link','path','unrelated','child-origin','wrong-parent'):
   value=self.fixture();packages=value['packages'];parent=packages['node_modules/parent'];child=packages['node_modules/parent/node_modules/@scope/child']
   if case=='parent':del packages['node_modules/parent']
   if case=='membership':parent['bundleDependencies']=[]
   if case=='integrity':parent['integrity']='sha512-bad'
   if case=='origin':parent['resolved']='https://example.org/parent.tgz'
   if case=='flag':del child['inBundle']
   if case=='link':parent['link']=True
   if case=='path':packages['node_modules/parent/node_modules/../outside']=child
   if case=='unrelated':packages['node_modules/unrelated']=child
   if case=='child-origin':child['resolved']='https://example.org/child.tgz'
   if case=='wrong-parent':parent['bundleDependencies']=['different']
   with self.subTest(case=case),self.assertRaises(ValueError):validate_lock_origins(value)
