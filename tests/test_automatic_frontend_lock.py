"""Reimplemented, offline rejection checks after workspace recovery."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import os
import shutil
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import automatic_frontend_lock as afl


def contract_fixture(vendor=None):
    from test_discovered_inputs import FakeVendor, fixture as discovered_fixture
    vendor=vendor or FakeVendor()
    contract=discovered_fixture(vendor=vendor)
    raw_manifest=vendor.files['frontend/package.json']
    manifest=json.loads(raw_manifest)
    raw=json.dumps({'name':manifest.get('name'),'version':manifest.get('version'),
        'lockfileVersion':3,'packages':{'':manifest,'node_modules/example':{
            'version':'1.0.0','resolved':'https://registry.npmjs.org/example/-/example-1.0.0.tgz',
            'integrity':'sha512-'+'A'*86+'==','engines':['node >=20']}}}).encode()
    contract['source']['files'].pop('frontend/package-lock.json')
    contract['source']['absent']=['frontend/package-lock.json']
    contract['frontend_lock']={'kind':'resolved','sha256':afl.sha(raw),'bytes':len(raw),
        'manifest_sha256':afl.sha(raw_manifest),'manifest':raw_manifest.decode(),
        'generator':dict(afl.GENERATOR)}
    return contract,raw


def fixture():
    manifest = {'name': 'synthetic', 'version': '1.0.0', 'dependencies': {'example': '1.0.0'}}
    text = json.dumps(manifest)
    raw = json.dumps({'name':'synthetic','version':'1.0.0','lockfileVersion':3,
        'packages': {'': manifest, 'node_modules/example': {
            'version':'1.0.0', 'resolved':'https://registry.npmjs.org/example/-/example-1.0.0.tgz',
            'integrity':'sha512-'+'A'*86+'=='}}}).encode()
    contract = {'source': {'repository':'1Panel-dev/1Panel', 'commit':'a'*40,
        'absent':['frontend/package-lock.json'], 'files': {'frontend/package.json': {
            'sha256':afl.sha(text.encode()),'bytes':len(text.encode())}}},
        'frontend_lock': {'kind':'resolved','manifest':text,
            'manifest_sha256':afl.sha(text.encode()), 'sha256':afl.sha(raw),
            'bytes':len(raw),'generator':dict(afl.GENERATOR)}}
    return raw, contract


class ResolvedLocks(unittest.TestCase):
    def test_manifest_source_preflight_and_engine_rejections(self):
        node, npm = shutil.which('node'), shutil.which('npm')
        self.assertIsNotNone(node, 'Node is required for resolver validation tests')
        self.assertIsNotNone(npm, 'npm parser dependencies are required')
        module=(Path(__file__).resolve().parents[1]/'scripts/automatic_frontend_lock.mjs').as_uri()
        code = r'''
import assert from 'node:assert/strict';
const moduleURL=process.argv[1]; process.argv[1]='';
const {checkManifest, checkEngines, checkLock} = await import(moduleURL);
const base = {name:'synthetic',version:'1.0.0',dependencies:{example:'^1.0.0'}};
checkManifest(base);
checkManifest({...base,overrides:{example:'1.0.0',nested:{'.':'1.0.0'}}});
checkManifest({...base,packageManager:'npm@10.9.4'});
checkEngines(['node >= 6.0','npm >= 8'],'legacy');
for(const spec of ['git+https://example.com/a.git','https://example.com/a.tgz','file:../x','workspace:*','github:someone/repo']) {
  for(const key of ['dependencies','devDependencies','optionalDependencies','peerDependencies'])
    assert.throws(()=>checkManifest({...base,[key]:{example:spec}}));
  assert.throws(()=>checkManifest({...base,overrides:{example:spec}}));
}
for(const addition of [{workspaces:[]},{devEngines:{}},{bundleDependencies:[]},{packageManager:'npm@11.0.0'},{engines:{node:'>=99'}},{engines:['node >=99']},{engines:['malformed']}])
  assert.throws(()=>checkManifest({...base,...addition}));
assert.throws(()=>checkManifest({...base,overrides:{example:'$unknown'}}));
assert.throws(()=>checkManifest({...base,dependencies:[]}));
const root={...base};
const lock={name:base.name,version:base.version,lockfileVersion:3,packages:{'':root,'node_modules/example':{version:'1.0.0',resolved:'https://registry.npmjs.org/example/-/example-1.0.0.tgz',integrity:'sha512-'+'A'.repeat(86)+'=='}}};
checkLock(base,lock);
for(const mutation of [p=>p.resolved='https://evil.example/x.tgz',p=>delete p.integrity,p=>p.engines=['node >=99'],p=>p.link=true,p=>{delete p.resolved;delete p.integrity;p.inBundle=true;}]){
 const bad=structuredClone(lock);mutation(bad.packages['node_modules/example']);assert.throws(()=>checkLock(base,bad));
}
console.log('synthetic source/engine/origin cases passed');
'''
        env=dict(os.environ,LOCK_NPM_DIR=str(Path(npm).resolve().parents[1]))
        result=subprocess.run([node,'--input-type=module','-e',code,module],env=env,
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('cases passed',result.stdout)

    def test_exact_replay_is_offline(self):
        raw, contract = fixture()
        with patch('subprocess.run', side_effect=AssertionError('replay executed a process')):
            self.assertEqual(afl.validate_payload(raw, contract), raw)

    def test_descriptor_rejects_identity_and_generator_drift(self):
        raw, original = fixture()
        changes = [
            lambda c:c['frontend_lock'].update(bytes=True),
            lambda c:c['frontend_lock'].update(extra='unknown'),
            lambda c:c['frontend_lock']['generator'].update(node='22.23.3'),
            lambda c:c['frontend_lock']['generator'].update(lifecycle_scripts=True),
            lambda c:c['frontend_lock']['generator'].update(registry='https://example.com'),
            lambda c:c['source'].update(repository='unknown/repo'),
            lambda c:c['source'].update(commit='main'),
            lambda c:c['source'].update(absent=[]),
            lambda c:c['source']['files'].update({'frontend/package-lock.json': {'sha256':'b'*64,'bytes':1}}),
            lambda c:c['source']['files']['frontend/package.json'].update(bytes=1),
            lambda c:c['frontend_lock'].update(manifest_sha256='b'*64),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                contract=copy.deepcopy(original); change(contract)
                with self.assertRaises((ValueError,KeyError)): afl.validate_payload(raw,contract)

    def test_lock_bytes_root_and_origin_cannot_be_substituted(self):
        raw, original = fixture()
        with self.assertRaises(ValueError): afl.validate_payload(raw+b' ',original)
        for mutation in [lambda d:d.update(lockfileVersion=2),
                         lambda d:d['packages'][''].update(version='2.0.0'),
                         lambda d:d['packages']['']['dependencies'].update(example='2.0.0'),
                         lambda d:d['packages']['node_modules/example'].update(resolved='https://evil.example/x'),
                         lambda d:d['packages']['node_modules/example'].pop('integrity')]:
            lock=json.loads(raw); mutation(lock)
            modified=json.dumps(lock).encode(); contract=copy.deepcopy(original)
            contract['frontend_lock'].update(sha256=afl.sha(modified),bytes=len(modified))
            with self.assertRaises(ValueError): afl.validate_payload(modified,contract)

    def test_ambiguous_json_rejected(self):
        for value in [b'{"name":1,"name":2}', b'{"x":NaN}', b'[]', b'']:
            with self.assertRaises(ValueError): afl.data_object(value,65536,'fixture')

    def test_source_lock_and_symlink_are_never_overwritten(self):
        raw,contract=fixture()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'config').mkdir(); frontend=root/'frontend'; frontend.mkdir()
            (root/'config'/afl.SIDECAR).write_bytes(raw)
            (frontend/'package.json').write_text(contract['frontend_lock']['manifest'])
            target=frontend/'package-lock.json'; target.write_bytes(b'original')
            with self.assertRaises(ValueError): afl.apply(frontend,contract,root)
            self.assertEqual(target.read_bytes(),b'original')
            target.unlink(); target.symlink_to(root/'missing')
            with self.assertRaises(ValueError): afl.apply(frontend,contract,root)
            self.assertTrue(target.is_symlink())

    def test_successful_absent_lock_replay_preserves_exact_bytes(self):
        raw,contract=fixture()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name); (root/'config').mkdir(); frontend=root/'frontend'; frontend.mkdir()
            (root/'config'/afl.SIDECAR).write_bytes(raw)
            (frontend/'package.json').write_text(contract['frontend_lock']['manifest'])
            with patch('subprocess.run',side_effect=AssertionError('offline replay')):
                afl.apply(frontend,contract,root)
            self.assertEqual((frontend/'package-lock.json').read_bytes(),raw)

    def test_conflicting_sidecar_rejected_without_replacement(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/afl.SIDECAR; path.write_bytes(b'old')
            with self.assertRaises(ValueError): afl.write_payload(path,b'new')
            self.assertEqual(path.read_bytes(),b'old')


class GeneratorBoundary(unittest.TestCase):
    def execute(self, *, node_version='v22.22.1', npm_version='10.9.4', mutate=None):
        raw, contract=fixture(); manifest=contract['frontend_lock']['manifest'].encode()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);node=root/'bin/node';node.parent.mkdir();node.write_text('unused')
        npm=root/'npm/bin/npm-cli.js';npm.parent.mkdir(parents=True);npm.write_text('unused')
        (npm.parent.parent/'package.json').write_text(json.dumps({'name':'npm','version':npm_version}))
        calls=[];events=[]
        def run(command,**kwargs):
            calls.append((command,kwargs));work=Path(kwargs['cwd'])
            if command[-1]=='--version':event='version';output=node_version
            elif command[-1]=='preflight':event='preflight';output=''
            elif 'install' in command:
                event='install';output='';(work/'package-lock.json').write_bytes(raw)
            elif command[-1]=='validate':event='validate';output=''
            elif 'ci' in command:event='ci';output=''
            else:raise AssertionError(command)
            events.append(event)
            if mutate:mutate(event,work)
            return type('Result',(),{'stdout':output})()
        with patch.object(afl.shutil,'which',side_effect=lambda name:str(node if name=='node' else npm)), \
             patch.object(afl.subprocess,'run',side_effect=run), \
             patch('verify_bundled_archives.verify_bundled_archives',side_effect=lambda lock:events.append('bundle')), \
             patch.dict(os.environ,GH_TOKEN='must-not-inherit',NODE_OPTIONS='--unsafe',NPM_CONFIG_USERCONFIG='/unexpected'):
            result=afl.generate(manifest,'a'*40,afl.sha(manifest))
        return result,calls,events

    def test_pins_permissions_environment_and_gate_order(self):
        result,calls,events=self.execute()
        self.assertEqual(events,['version','preflight','install','validate','bundle','ci'])
        self.assertEqual(result[1]['generator'],afl.GENERATOR)
        for command,kwargs in calls:
            env=kwargs['env']
            self.assertNotIn('GH_TOKEN',env);self.assertNotIn('NODE_OPTIONS',env)
            self.assertEqual(env['NPM_CONFIG_REGISTRY'],'https://registry.npmjs.org')
            self.assertEqual(env['NPM_CONFIG_IGNORE_SCRIPTS'],'true')
            self.assertEqual(env['NPM_CONFIG_ENGINE_STRICT'],'true')
            self.assertNotEqual(env['NPM_CONFIG_USERCONFIG'],env['NPM_CONFIG_GLOBALCONFIG'])
            self.assertNotEqual(env['NPM_CONFIG_USERCONFIG'],'/unexpected')
            if command[-1]!='--version':
                self.assertIn('--permission',command)
                self.assertIn('--allow-fs-write='+str(kwargs['cwd']),command)
                self.assertFalse(any(x.startswith(('--allow-child-process','--allow-worker','--allow-addons')) for x in command))
        install=next(c for c,_ in calls if 'install' in c)
        self.assertIn('--package-lock-only',install);self.assertIn('--ignore-scripts',install)
        self.assertIn('--dry-run',next(c for c,_ in calls if 'ci' in c))

    def test_wrong_tools_and_preparation_mutations_fail(self):
        for values in ({'node_version':'v24.0.0'},{'npm_version':'11.0.0'},
                       {'mutate':lambda event,work:(work/'node_modules').mkdir() if event=='install' else None},
                       {'mutate':lambda event,work:(work/'package.json').write_text('{}') if event=='install' else None},
                       {'mutate':lambda event,work:(work/'package-lock.json').write_bytes(b'changed') if event=='ci' else None}):
            with self.subTest(case=tuple(values)),self.assertRaises(ValueError):self.execute(**values)

    def test_input_identity_failure_executes_no_subprocess(self):
        raw,contract=fixture();manifest=contract['frontend_lock']['manifest'].encode()
        with patch.object(afl.subprocess,'run',side_effect=AssertionError('must reject before subprocess')) as run:
            for commit,digest in [('main',afl.sha(manifest)),('a'*40,'b'*64)]:
                with self.subTest(commit=commit),self.assertRaises(ValueError):afl.generate(manifest,commit,digest)
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
