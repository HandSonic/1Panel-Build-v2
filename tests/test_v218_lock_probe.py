import pathlib
import unittest
import subprocess
import textwrap

ROOT=pathlib.Path(__file__).resolve().parents[1]

class LockProbeTests(unittest.TestCase):
    def test_probe_is_fixed_input_readonly_and_never_publishes(self):
        text=(ROOT/'.github/workflows/probe-v218-lock.yml').read_text()
        for required in ['contents: read','node-version: 22.22.1','10.9.4',
                         'v218-lock-probe/empty-user.npmrc','v218-lock-probe/empty-global.npmrc',
                         '6ea07421acf1e396606a892f44d76af9d5e8a7b2/frontend/package.json',
                         'd5a914e32b8f3bd931c56b4ed48752484656bc7ae6bd14c36b54b1996683fffa',
                         '--package-lock-only --ignore-scripts --engine-strict',
                         'npm ci --dry-run --ignore-scripts --engine-strict',
                         'test ! -e node_modules', 'proposed_derived_repair_lock_not_original_source',
                         "url.hostname !== 'registry.npmjs.org'", 'pkg.integrity',
                         'retention-days: 14']:
            self.assertIn(required,text)
        for forbidden in ['contents: write','pull_request_target','gh release','npm publish','publish_candidate','secrets.']:
            self.assertNotIn(forbidden,text)

    def test_shared_manifest_probe_changes_only_fixed_identity(self):
        original=(ROOT/'.github/workflows/probe-v218-lock.yml').read_text()
        shared=(ROOT/'.github/workflows/probe-v217-lock.yml').read_text()
        expected=(original.replace('v218','v217').replace('v2.1.8','v2.1.7')
                  .replace('6ea07421acf1e396606a892f44d76af9d5e8a7b2','6e052b8ef265dedbe2a50316159ee2b47fefb9ed')
                  .replace('d5a914e32b8f3bd931c56b4ed48752484656bc7ae6bd14c36b54b1996683fffa','136f560604df9d40d22efd9ca3344c0098d5afbaf20af13573afe990843e07f5')
                  .replace('tests/test_v217_lock_probe.py','tests/test_v218_lock_probe.py'))
        self.assertEqual(shared,expected)

    def test_legacy_and_object_engines_reach_the_same_version_gate(self):
        text=(ROOT/'.github/workflows/probe-v218-lock.yml').read_text()
        function=textwrap.dedent(text.split('// ENGINE_CHECK_BEGIN\n',1)[1].split('          // ENGINE_CHECK_END',1)[0])
        script="""
const assert = require('node:assert/strict');
const calls = [];
const semver = {
  validRange: range => range === 'bad' ? null : range,
  satisfies: (version, range) => { calls.push([version, range]); return range !== '<22'; }
};
"""+function+"""
checkEngines(['node >= 6.0', 'npm >= 2'], 'legacy');
assert.deepEqual(calls, [['22.22.1','>= 6.0'], ['10.9.4','>= 2']]);
assert.throws(() => checkEngines(['node <22'], 'incompatible'), /Unsupported node/);
assert.throws(() => checkEngines({node:'<22'}, 'incompatible'), /Unsupported node/);
checkEngines(['node >= 0.2.0'], 'jsonparse');
for (const value of [['node bad'], [42], ['unparseable'], 'node >= 6', {node:42}, {node:'bad'}]) {
  assert.throws(() => checkEngines(value, 'malformed'));
}
checkEngines(undefined, 'absent');
checkEngines({yarn:'>=1'}, 'unused manager');
"""
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
