import copy,hashlib,json,os,subprocess,sys,tempfile,unittest,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import discover_inputs as discovery
import resolved_contract as resolved

class FakeVendor:
 def __init__(self):
  package={'name':'synthetic','version':'1','dependencies':{'example':'1.0.0'}}
  self.files={'core/go.mod':b'module example/core\ngo 1.24.0\ntoolchain go1.24.9\n','agent/go.mod':b'module example/agent\ngo 1.25.0\n','frontend/package.json':json.dumps(package).encode(),'frontend/package-lock.json':json.dumps({'lockfileVersion':3,'packages':{'':package,'node_modules/example':{'version':'1.0.0','resolved':'https://registry.npmjs.org/example/-/example-1.0.0.tgz','integrity':'sha512-'+'A'*86+'==' }}}).encode(),'core/cmd/server/conf/app.yaml':b'base:\n  version: v0.0.0\n  mode: dev\n  is_enterprise: false\nlog:\n  level: debug\n','agent/cmd/server/conf/app.yaml':b'base:\n  mode: dev\nlog:\n  level: debug\n'}
 def source(self,repo,commit,path,optional=False):
  if path not in self.files and not optional:raise ValueError('Missing test source')
  return self.files.get(path)
 def commit(self,repo,ref):return 'a'*40
 def api(self,repo,path):return [{'tag_name':'v2.99.0','draft':False,'prerelease':False},{'tag_name':'v2.100.0-beta.1','draft':False,'prerelease':True},{'tag_name':'v2.999.0','draft':True,'prerelease':False}]
 def read(self,url,**kwargs):
  if url.startswith('https://go.dev/'):return json.dumps([{'version':v,'stable':True} for v in ['go1.24.9','go1.25.0','go1.25.8','go1.26.1']]).encode()
  raise ValueError(url)

def fixture():
 result,_,_=discovery.resolve_source(FakeVendor(),'v2.99.0')
 return {'schema':1,'kind':'1panel-resolved-build-inputs','version':'v2.99.0','mode':'stable','edition':'community','architectures':discovery.ARCHES,'source':result['source'],'configuration':result['configuration'],'frontend_lock':result['frontend_lock'],'toolchain':{'go':result['go'],'node':'22.22.1','npm':'10.9.4'},'installer':{'repository':discovery.INSTALLER,'commit':'b'*40,'original_version':'version','resources':{name:discovery.facts(b'synthetic') for name in discovery.INSTALLER_FILES}},'resources':{'geoip':dict(url='https://resource.fit2cloud.com/1panel/package/v2/geo/GeoIP.mmdb',**discovery.facts(b'geo'))}}

class SourceDiscoveryTests(unittest.TestCase):
 def test_unlisted_version_and_channels(self):
  v=FakeVendor();self.assertEqual(discovery.latest(v,'stable'),'v2.99.0');self.assertEqual(discovery.latest(v,'beta'),'v2.100.0-beta.1')
  value,_,_=discovery.resolve_source(v,'v2.99.0');self.assertEqual(value['source']['commit'],'a'*40);self.assertEqual(value['go'],'1.25.8')
  with self.assertRaises(ValueError):discovery.resolve_source(v,'v2.99.0-beta.1','stable')
 def test_npm_resolution_drift_rejected(self):
  for path in ['frontend/.npmrc','package.json','frontend/npm-shrinkwrap.json']:
   v=FakeVendor();v.files[path]=b'{}'
   with self.subTest(path=path),self.assertRaises(ValueError):discovery.resolve_source(v,'v2.99.0')
  v=FakeVendor();v.files['frontend/package.json']=b'{"name":"changed"}'
  with self.assertRaises(ValueError):discovery.resolve_source(v,'v2.99.0')
 def test_bad_tag_and_unavailable_go_rejected(self):
  with self.assertRaises(ValueError):discovery.identity('v2.99.0/../../main')
  v=FakeVendor();v.files['agent/go.mod']=b'module test\ngo 9.0.0\n'
  with self.assertRaises(ValueError):discovery.resolve_source(v,'v2.99.0')
 def test_missing_lock_needs_a_manifest_bound_candidate(self):
  v=FakeVendor();del v.files['frontend/package-lock.json']
  with self.assertRaisesRegex(ValueError,'candidate dependency resolution required'):discovery.resolve_source(v,'v2.99.0')

class ContractTransportTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);(self.root/'config').mkdir();self.path=self.root/'resolved-source.json';self.value=fixture()
  for name,value in [('sources.json',{}),('embedded-configs.json',{}),('frontend-lock-repairs.json',{'versions':{},'additions':{}})]: (self.root/'config'/name).write_text(json.dumps(value))
 def write(self):self.path.write_bytes(resolved.canonical(self.value));return resolved.digest(self.value)
 def test_unknown_contract_apply_is_repeatable(self):
  sha=self.write();resolved.apply_contract(self.path,sha,self.root,'v2.99.0');first={p.name:p.read_bytes() for p in (self.root/'config').iterdir()};resolved.apply_contract(self.path,sha,self.root,'v2.99.0');self.assertEqual(first,{p.name:p.read_bytes() for p in (self.root/'config').iterdir()});self.assertEqual(json.loads(first['sources.json'])['v2.99.0']['resolved_contract_sha256'],sha)
 def test_bad_contract_fails_before_writes(self):
  initial={p.name:p.read_bytes() for p in (self.root/'config').iterdir()}
  for kind in ['digest','source','edition','config','installer','architecture']:
   self.value=fixture()
   if kind=='source':self.value['source']['repository']='evil/other'
   if kind=='edition':self.value['edition']='enterprise'
   if kind=='config':self.value['configuration']['core']['path']='elsewhere'
   if kind=='installer':del self.value['installer']['resources']['install.sh']
   if kind=='architecture':self.value['architectures']=['amd64']
   sha=self.write()
   with self.subTest(kind=kind),self.assertRaises(ValueError):resolved.apply_contract(self.path,'0'*64 if kind=='digest' else sha,self.root,'v2.99.0')
   self.assertEqual(initial,{p.name:p.read_bytes() for p in (self.root/'config').iterdir()})
 def test_existing_history_cannot_be_overwritten(self):
  (self.root/'config/sources.json').write_text(json.dumps({'v2.99.0':{'source_commit':'c'*40}}));sha=self.write()
  with self.assertRaisesRegex(ValueError,'checked-in historical'):resolved.apply_contract(self.path,sha,self.root,'v2.99.0')
 def test_detached_release_contract_is_rejected(self):
  self.write();(self.root/'build-manifest.json').write_text(json.dumps({'version':'v2.99.0','artifacts':[{'resolved_contract_sha256':'0'*64}]}))
  with self.assertRaises(ValueError):resolved.activate_release(self.root,'v2.99.0',self.root)

class NodeSelectionTests(unittest.TestCase):
 def test_all_engines_including_legacy_arrays(self):
  root=Path(__file__).resolve().parents[1];env=dict(os.environ);env['PROBE_NPM_DIR']=str(Path(shutil.which('npm')).resolve().parents[1]);data={'packages':[{'engines':{'node':'>=22','npm':'>=10'}},{'engines':['node <24']}],'releases':[{'version':'v22.22.1','npm':'10.9.4','lts':'Jod'},{'version':'v24.1.0','npm':'11.0.0','lts':'Krypton'}]}
  command=['node',str(root/'scripts/select_node_toolchain.mjs')];r=subprocess.run(command,input=json.dumps(data),text=True,capture_output=True,env=env);self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout),{'node':'22.22.1','npm':'10.9.4'});data['packages'].append({'engines':['node >=30']});r=subprocess.run(command,input=json.dumps(data),text=True,capture_output=True,env=env);self.assertNotEqual(r.returncode,0)

class OfficialArchiveTests(unittest.TestCase):
 def archive_vendor(self, mutate=None):
  import io,tarfile
  data=io.BytesIO();version='v2.99.0';name='1panel-'+version+'-linux-amd64'
  with tarfile.open(fileobj=data,mode='w:gz') as tar:
   for path in discovery.INSTALLER_FILES+['GeoIP.mmdb','1panel-core']:
    body=b'official synthetic '+path.encode();member=tarfile.TarInfo(name+'/'+path);member.size=len(body);tar.addfile(member,io.BytesIO(body))
   if mutate=='unsafe':
    member=tarfile.TarInfo('../outside');member.size=1;tar.addfile(member,io.BytesIO(b'x'))
  compressed=data.getvalue();sha=hashlib.sha256(compressed).hexdigest()
  class Vendor:
   def read(self,url,**kwargs):return ((('0'*64) if mutate=='digest' else sha)+'  '+name+'.tar.gz\n').encode()
   def open(self,url):return io.BytesIO(compressed)
  return Vendor()
 def test_complete_compressed_hash_and_resources(self):
  resources,geo=discovery.official_archive(self.archive_vendor(),'v2.99.0','stable');self.assertEqual(set(resources),set(discovery.INSTALLER_FILES));self.assertEqual({k:geo[k] for k in ('sha256','bytes')},discovery.facts(b'official synthetic GeoIP.mmdb'));self.assertEqual(geo['archive']['member'],'1panel-v2.99.0-linux-amd64/GeoIP.mmdb')
 def test_bad_digest_and_unsafe_member_rejected(self):
  for mutation in ['digest','unsafe']:
   with self.subTest(mutation=mutation),self.assertRaises(ValueError):discovery.official_archive(self.archive_vendor(mutation),'v2.99.0','stable')

class OfficialNodeScheduleTests(unittest.TestCase):
 def test_selection_uses_live_schedule_and_bundled_npm_pair(self):
  class Vendor:
   def read(self,url,limit=0):
    if url=='https://nodejs.org/dist/index.json':return json.dumps([{'version':'v20.20.0','npm':'10.8.0','lts':'Iron'},{'version':'v22.22.1','npm':'10.9.4','lts':'Jod'}]).encode()
    if url=='https://raw.githubusercontent.com/nodejs/Release/main/schedule.json':return json.dumps({'v20':{'lts':'2023-10-01','end':'2025-01-01'},'v22':{'lts':'2024-10-01','end':'2099-01-01'}}).encode()
    raise AssertionError(url)
  value=discovery.select_node(Vendor(),{'engines':{'node':'>=20'}},{'packages':{}})
  self.assertEqual(value,{'node':'22.22.1','npm':'10.9.4'})


class TemporalResourceTests(unittest.TestCase):
 def vendor(self, history=True):
  resources={p:b'synthetic '+p.encode() for p in discovery.INSTALLER_FILES}
  resources['1pctl']=b'ORIGINAL_VERSION=v2.99.0\n'
  class Vendor:
   calls=[]
   def commit(self,*args):return 'a'*40
   def api(self,repo,path):
    self.calls.append(path)
    return [{'sha':'c'*40}] if history else []
   def source(self,repo,commit,path,optional=False):
    if commit != 'c'*40:return b'new installer'
    return b'ORIGINAL_VERSION=version\n' if path=='1pctl' else resources[path]
   def digest(self,*args):raise AssertionError('Mutable GeoIP must not be requested')
  return Vendor(),resources
 def test_unlisted_history_and_advanced_geoip(self):
  from unittest.mock import patch
  vendor,resources=self.vendor();geo=dict(discovery.facts(b'old geo'),archive={'url':'version-bound'})
  with patch.object(discovery,'official_archive',return_value=(resources,geo)):
   installer,actual=discovery.installer_inputs(vendor,'v2.99.0','stable')
  self.assertEqual(installer['commit'],'c'*40);self.assertEqual(actual,geo)
  self.assertEqual(vendor.calls[-1],'commits?sha='+'a'*40+'&per_page=100&page=1')
 def test_history_exhaustion_and_network_failure(self):
  from unittest.mock import patch
  vendor,resources=self.vendor(False)
  with patch.object(discovery,'official_archive',return_value=(resources,{})),self.assertRaisesRegex(ValueError,'does not match'):
   discovery.installer_inputs(vendor,'v2.99.0','stable')
  vendor.api=lambda *a: (_ for _ in ()).throw(ValueError('HTTP 403'))
  with patch.object(discovery,'official_archive',return_value=(resources,{})),self.assertRaisesRegex(ValueError,'HTTP 403'):
   discovery.installer_inputs(vendor,'v2.99.0','stable')
 def test_history_is_bounded(self):
  from unittest.mock import patch
  vendor,resources=self.vendor(False);calls=[]
  def pages(repo,path):
   calls.append(path);return [{'sha':format(i,'040x')} for i in range(100)]
  vendor.api=pages
  with patch.object(discovery,'official_archive',return_value=(resources,{})),self.assertRaises(ValueError):
   discovery.installer_inputs(vendor,'v2.99.0','stable')
  self.assertEqual(len(calls),3)
 def test_pinned_archive_stream_checks_full_size_hash_and_member(self):
  import io
  vendor=OfficialArchiveTests().archive_vendor();_,geo=discovery.official_archive(vendor,'v2.99.0','stable')
  vendor.read=lambda *a,**k: (_ for _ in ()).throw(AssertionError('No mutable checksum re-read'))
  output=io.BytesIO();_,actual=discovery.official_archive(vendor,'v2.99.0','stable',geo['archive'],output)
  self.assertEqual(actual,geo);self.assertEqual(output.getvalue(),b'official synthetic GeoIP.mmdb')
  for key,value in [('sha256','0'*64),('bytes',geo['archive']['bytes']+1),('member','../GeoIP.mmdb')]:
   bad=dict(geo['archive']);bad[key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):discovery.official_archive(vendor,'v2.99.0','stable',bad,io.BytesIO())

class RecipeManifestBindingTests(unittest.TestCase):
 setUp=ContractTransportTests.setUp
 write=ContractTransportTests.write
 def test_derived_recipe_requires_matching_original_manifest_and_absence(self):
  data=b'synthetic reviewed lock';sha=hashlib.sha256(data).hexdigest();recipe={'original_absent':True,'derived_sha256':sha,'reviewed_lock_file':'lock.json'}
  (self.root/'config/lock.json').write_bytes(data)
  (self.root/'config/frontend-lock-repairs.json').write_text(json.dumps({'versions':{'v2.1.0':recipe}}))
  self.value['source']['absent']=['frontend/package-lock.json'];del self.value['source']['files']['frontend/package-lock.json']
  manifest=self.value['source']['files']['frontend/package.json']['sha256']
  self.value['frontend_lock']={'kind':'derived','sha256':sha,'manifest_sha256':manifest,'recipe_sha256':resolved.digest(recipe)}
  for source in [{},{'source_files_absent':[],'source_files_sha256':{'frontend/package.json':manifest}},{'source_files_absent':['frontend/package-lock.json'],'source_files_sha256':{'frontend/package.json':'0'*64}}]:
   (self.root/'config/sources.json').write_text(json.dumps({'v2.1.0':source}));initial={p.name:p.read_bytes() for p in (self.root/'config').iterdir()}
   with self.assertRaisesRegex(ValueError,'verified repair recipe'):resolved.apply_contract(self.path,self.write(),self.root,'v2.99.0')
   self.assertEqual(initial,{p.name:p.read_bytes() for p in (self.root/'config').iterdir()})
  (self.root/'config/sources.json').write_text(json.dumps({'v2.1.0':{'source_files_absent':['frontend/package-lock.json'],'source_files_sha256':{'frontend/package.json':manifest}}}))
  resolved.apply_contract(self.path,self.write(),self.root,'v2.99.0')
 def test_archive_contract_identity_bounds_and_channel(self):
  _,geo=discovery.official_archive(OfficialArchiveTests().archive_vendor(),'v2.99.0','stable');self.value['resources']['geoip']=geo
  resolved.apply_contract(self.path,self.write(),self.root,'v2.99.0')
  for key,value in [('url',geo['archive']['url'].replace('stable','beta')),('member','other/GeoIP.mmdb'),('bytes',512*1024*1024+1)]:
   bad=copy.deepcopy(self.value);bad['resources']['geoip']['archive'][key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):resolved.source_contract(resolved.canonical(bad),resolved.digest(bad),'v2.99.0','stable')
