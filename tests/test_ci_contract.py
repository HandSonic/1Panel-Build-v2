"""Static/shell execution contract checks; no Docker, network or remote writes."""
import os,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def github_block(name):
 s=(ROOT/'.github/workflows/build.yml').read_text().split('- name: '+name,1)[1].split('        run: |\n',1)[1]
 result=[]
 for line in s.splitlines():
  if line and not line.startswith('          '):break
  result.append(line[10:] if line.startswith('          ') else '')
 return '\n'.join(result)+'\n'
class CIContract(unittest.TestCase):
 def test_shell_syntax(self):
  for n in ['Resolve immutable build inputs','Prepare pinned source and frontend once','Verify shared source before use','Build and validate every requested architecture','Independently validate and aggregate all architecture shards','Stage a new draft candidate and verify downloaded bytes']:
   r=subprocess.run(['bash','-n'],input=github_block(n),text=True,capture_output=True);self.assertEqual(r.returncode,0,(n,r.stderr))
 def test_no_release_existence_build_skip_or_force(self):
  s=(ROOT/'.github/workflows/build.yml').read_text();self.assertNotIn('build=0',s);self.assertNotIn('git push',s);self.assertNotIn('--clobber',s)
  self.assertIn('--draft',s);self.assertNotIn('--draft=false',s)
 def test_cnb_trigger_inputs_and_runtime(self):
  s=(ROOT/'.cnb.yml').read_text();self.assertIn('CNB_IS_TAG',s);self.assertIn('CNB_BRANCH',s);self.assertIn('apk add --no-cache bash python3',s)
  self.assertNotIn('SKIP_BUILD',s);self.assertNotIn('git push',s);self.assertNotIn('overlying: true',s)
  self.assertIn('scripts/validate_artifacts.py',s);self.assertIn('image: cnbcool/attachments:latest',s)
 def test_unsupported_arch_stops_before_resolution(self):
  with tempfile.TemporaryDirectory() as d:
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=dict(os.environ,INPUT_VERSION='v2.3.2',INPUT_ARCHES='amd64 wrongarch',GITHUB_REF='refs/heads/main'),capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0);self.assertFalse((Path(d)/'build-inputs.env').exists())
 def test_tag_selected_not_latest_fallback(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'config').mkdir();(p/'config/sources.json').write_text('{"v2.3.2":{}}');(p/'scripts').mkdir();(p/'scripts/resolve_inputs.py').write_text('import sys; print("SOURCE_COMMIT="+sys.argv[1]+"\\nGO_VERSION=1.26.1")')
   (p/'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()')
   e=dict(os.environ,INPUT_VERSION='',INPUT_ARCHES='amd64',GITHUB_REF='refs/tags/v2.3.2',GITHUB_REF_NAME='v2.3.2',GITHUB_ENV=str(p/'env'),GITHUB_OUTPUT=str(p/'out'))
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=e,capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('SOURCE_COMMIT=v2.3.2',(p/'build-inputs.env').read_text())
if __name__=='__main__':unittest.main()
