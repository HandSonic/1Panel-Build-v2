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
 def test_unsupported_arch_stops_before_resolution(self):
  with tempfile.TemporaryDirectory() as d:
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=dict(os.environ,PYTHONPATH=str(ROOT/'scripts'),INPUT_VERSION='v2.3.2',INPUT_ARCHES='amd64 wrongarch',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/main'),capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0);self.assertFalse((Path(d)/'build-inputs.env').exists())
 def test_tag_selected_not_latest_fallback(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'config').mkdir();(p/'config/sources.json').write_text('{"v2.3.2":{}}');(p/'scripts').mkdir();(p/'scripts/resolve_inputs.py').write_text('import sys; print("SOURCE_COMMIT="+sys.argv[1]+"\\nGO_VERSION=1.26.1")')
   (p/'scripts/validate_artifacts.py').write_text('def architectures(value): return value.split()')
   (p/'scripts/discover_inputs.py').write_text('import sys; assert \"--latest\" not in sys.argv; print(\"a\"*64)')
   (p/'scripts/resolved_contract.py').write_text('')
   e=dict(os.environ,INPUT_VERSION='',INPUT_ARCHES='amd64',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/tags/v2.3.2',GITHUB_REF_NAME='v2.3.2',GITHUB_ENV=str(p/'env'),GITHUB_OUTPUT=str(p/'out'))
   r=subprocess.run(['bash','-c',github_block('Resolve immutable build inputs')],cwd=d,env=e,capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('SOURCE_COMMIT=v2.3.2',(p/'build-inputs.env').read_text())
if __name__=='__main__':unittest.main()
