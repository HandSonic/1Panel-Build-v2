import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]

class CNBMatrixTests(unittest.TestCase):
 def config(self):
  return yaml.safe_load((ROOT/'.cnb.yml').read_text())

 def resolver_script(self):
  stages=self.config()['.build_pipeline'][0]['stages']
  return next(stage['script'] for stage in stages
              if set(stage.get('exports',{}))=={'BUILD_VERSION','BUILD_ARCHES'})

 def run_case(self,fail='',bad='',prepare=False,arches='amd64 arm64 armv7',stale=False):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
  (root/'bin').mkdir();(root/'scripts').mkdir()
  (root/'build-inputs.env').write_text('SOURCE_COMMIT=a\nINSTALLER_REF=b\nGO_VERSION=1.26.1\nNODE_VERSION=22.22.1\n')
  (root/'scripts/validate_artifacts.py').write_text('''import pathlib,sys

def architectures(value):
 a=value.replace(',', ' ').replace(';', ' ').split()
 if not a or any(x not in ('amd64','arm64','armv7') for x in a):raise ValueError('bad architecture')
 return a
if __name__=='__main__':
 root=pathlib.Path(sys.argv[1])
 for arch in architectures(sys.argv[3]):
  p=root/('1panel-'+sys.argv[2]+'-linux-'+arch+'.tar.gz')
  if p.read_bytes()!=b'verified':raise SystemExit(1)
''')
  (root/'scripts/package_release.py').write_text('''import json,pathlib,sys
root=pathlib.Path(sys.argv[2])/'dist'
(root/'build-manifest.json').write_text(json.dumps({'accepted':sys.argv[4:]}))
''')
  docker=root/'bin/docker'
  docker.write_text('#!'+sys.executable+'\n'+'''import os,pathlib,sys
args=sys.argv[1:]
if args[0]=='build':raise SystemExit(1 if os.environ.get('FAIL_PREPARE')=='1' else 0)
arch=next(x.split('=',1)[1] for x in args if x.startswith('TARGET_ARCHES='))
with open('attempts','a') as f:f.write(arch+'\\n')
if arch in os.environ.get('FAIL_ARCHES','').split():raise SystemExit(1)
root=pathlib.Path(args[args.index('-v')+1].split(':')[0])
name='1panel-'+os.environ['BUILD_VERSION']+'-linux-'+arch+'.tar.gz'
(root/name).write_bytes(b'bad' if arch==os.environ.get('BAD_ARCH') else b'verified')
(root/(name+'.sha256')).write_text('fixture')
''');docker.chmod(0o755)
  env=dict(os.environ,PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],BUILD_VERSION='v2.99.0',BUILD_ARCHES=arches,CNB_COMMIT='c'*40,CNB_BUILD_ID='build-new',CNB_PIPELINE_ID='pipeline-new',FAIL_ARCHES=fail,BAD_ARCH=bad,FAIL_PREPARE='1' if prepare else '0')
  if stale:(root/'cnb-verified-artifacts').write_text('build-old:pipeline-old:'+('c'*40))
  result=subprocess.run(['bash',str(ROOT/'scripts/build_cnb_matrix.sh')],cwd=root,env=env,capture_output=True,text=True)
  return root,result
 def test_one_failure_preserves_other_validated_branches_and_red_status(self):
  root,result=self.run_case(fail='arm64')
  self.assertEqual(result.returncode,1,result.stderr)
  self.assertEqual((root/'attempts').read_text().splitlines(),['amd64','arm64','armv7'])
  self.assertEqual(json.loads((root/'dist/build-manifest.json').read_text())['accepted'],['amd64','armv7'])
  self.assertTrue((root/'cnb-verified-artifacts').is_file())
  self.assertFalse((root/'dist/1panel-v2.99.0-linux-arm64.tar.gz').exists())
 def test_validation_failure_is_omitted(self):
  root,result=self.run_case(bad='amd64');self.assertEqual(result.returncode,1)
  self.assertEqual(json.loads((root/'dist/build-manifest.json').read_text())['accepted'],['arm64','armv7'])
 def test_shared_failure_and_all_branch_failure_never_make_a_manifest(self):
  for kwargs in ({'prepare':True},{'fail':'amd64 arm64 armv7'}):
   root,result=self.run_case(**kwargs);self.assertNotEqual(result.returncode,0)
   self.assertFalse((root/'dist/build-manifest.json').exists())
   self.assertFalse((root/'cnb-verified-artifacts').exists())
 def test_fail_stage_does_not_mask_pipeline_failure(self):
  config=self.config();pipeline=config['.build_pipeline'][0]
  self.assertFalse(pipeline.get('allowFailure',False))
  preservation=next(stage for stage in pipeline['stages'] if 'attachments' in stage.get('settings',{}))
  self.assertEqual(pipeline['failStages'],[preservation])
  self.assertIn('$CNB_BUILD_ID:$CNB_PIPELINE_ID:$CNB_COMMIT',pipeline['failStages'][0]['if'])
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'cnb-verified-artifacts';marker.write_text('stale')
   # Stop at setup failure and check that a preceding run cannot export its marker.
   result=subprocess.run(['bash','-c','apk() { return 23; }\n'+self.resolver_script()],
                         cwd=root,capture_output=True,text=True)
   self.assertEqual(result.returncode,23,result.stderr)
   self.assertFalse(marker.exists())

 def test_normalizes_separators_and_invalidates_stale_marker(self):
  for arches in ('amd64,arm64','amd64;arm64'):
   root,result=self.run_case(arches=arches);self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((root/'attempts').read_text().splitlines(),['amd64','arm64'])
  root,result=self.run_case(prepare=True,stale=True)
  self.assertNotEqual(result.returncode,0);self.assertFalse((root/'cnb-verified-artifacts').exists())

 def test_cnb_uses_the_same_exact_automatic_resolver_toolchain(self):
  setup='''
apk() { :; }
mkdir() { :; }
docker() { echo fixture-toolchain; }
node() { echo node >> "$FIXTURE_LOG"; echo "$FIXTURE_NODE"; }
npm() { echo npm >> "$FIXTURE_LOG"; echo "$FIXTURE_NPM"; }
python3() {
  if [[ "$1" == -m && "$2" == venv ]]; then
    echo venv >> "$FIXTURE_LOG"
    return 23
  fi
  echo unexpected-python >> "$FIXTURE_LOG"
  return 47
}
'''
  for node,npm,expected,calls in (
    ('v22.22.1','10.9.4',23,['node','npm','venv']),
    ('v22.0.0','10.9.4',1,['node']),
    ('v22.22.1','10.0.0',1,['node','npm'])):
   with self.subTest(node=node,npm=npm),tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    env=dict(os.environ,FIXTURE_LOG=str(root/'calls'),FIXTURE_NODE=node,FIXTURE_NPM=npm)
    result=subprocess.run(['bash','-c',setup+self.resolver_script()],cwd=root,env=env,capture_output=True,text=True)
    self.assertEqual(result.returncode,expected,result.stderr)
    self.assertEqual((root/'calls').read_text().splitlines(),calls)
