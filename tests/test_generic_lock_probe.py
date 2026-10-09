import os
import pathlib
import shutil
import subprocess
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GenericLockProbeTests(unittest.TestCase):
    def test_manifest_sources_reject_before_resolution(self):
        text = (ROOT / '.github/workflows/probe-repair-lock.yml').read_text()
        function = textwrap.dedent(text.split('// MANIFEST_PREFLIGHT_BEGIN\n')[1].split('          // MANIFEST_PREFLIGHT_END')[0])
        npm_root = str(pathlib.Path(shutil.which('npm')).resolve().parents[2])
        script = '''
const assert = require('node:assert/strict');
const npa = require(process.env.PROBE_NPM_ROOT + '/npm/node_modules/npm-package-arg');
''' + function + '''
checkManifest({dependencies:{example:'^1.0.0', alias:'npm:esbuild-wasm@latest'}, overrides:{example:{'.':'1.2.3'}}});
checkManifest({dependencies:{example:'1'}, overrides:{nested:'$example'}});
for (const spec of ['git+https://github.com/a/b.git#abc', 'https://example.com/a.tgz', 'file:../x', 'workspace:*', 'github:a/b']) {
  assert.throws(()=>checkManifest({dependencies:{example:spec}}));
  assert.throws(()=>checkManifest({overrides:{example:spec}}));
}
for (const manifest of [{workspaces:[]}, {devEngines:{}}, {packageManager:'npm@11.0.0'}, {bundleDependencies:[]}, {dependencies:[]}, {overrides:{x:'$missing'}}, {overrides:{'.':'1'}}]) assert.throws(()=>checkManifest(manifest));
'''
        result = subprocess.run(['node', '-'], input=script, text=True, capture_output=True,
                                env={**os.environ, 'PROBE_NPM_ROOT': npm_root})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(text.index('checkManifest(JSON.parse'), text.index('npm install --package-lock-only'))
        self.assertIn('NODE_OPTIONS="--permission --allow-fs-read=* --allow-fs-write=$PWD"', text)
        self.assertNotIn('--allow-child-process', text)
        self.assertNotIn('--allow-worker', text)
        self.assertNotIn('--allow-addons', text)
        self.assertIn("checkEngines(manifest.engines, 'root manifest')", text)
        self.assertIn("checkEngines(lock.packages[''].engines, 'root lock')", text)

    def test_probe_is_manifest_addressed_readonly_and_candidate_only(self):
        text = (ROOT / '.github/workflows/probe-repair-lock.yml').read_text()
        for value in ['contents: read', 'SOURCE_COMMIT: ${{ inputs.source_commit }}',
                      'MANIFEST_SHA256: ${{ inputs.manifest_sha256 }}',
                      '--max-filesize 65536', '--package-lock-only --ignore-scripts --engine-strict',
                      'npm ci --dry-run --ignore-scripts --engine-strict',
                      'test ! -e node_modules', 'node-version: 22.22.1',
                      "source_commit: process.env.SOURCE_COMMIT",
                      'manifest-lock-probe/empty-user.npmrc',
                      'manifest-lock-probe/empty-global.npmrc']:
            self.assertIn(value, text)
        self.assertEqual(text.count('"$MANIFEST_SHA256" | sha256sum --check --strict'), 2)
        for value in ['contents: write', 'pull_request_target', 'gh release',
                      'npm publish', 'publish_candidate', 'secrets.', 'v2.1.1', 'curl -L']:
            self.assertNotIn(value, text)
        # Untrusted dispatch values enter shell only through validated environment variables.
        for line in text.splitlines():
            if '${{ inputs.' in line:
                self.assertTrue(line.strip().startswith(('SOURCE_COMMIT:', 'MANIFEST_SHA256:')))

    def test_dispatch_identity_rejects_nonimmutable_or_injected_values(self):
        text = (ROOT / '.github/workflows/probe-repair-lock.yml').read_text()
        guards = '\n'.join(line.strip() for line in text.splitlines() if line.strip().startswith('[['))
        self.assertEqual(len(guards.splitlines()), 2)
        for commit, digest, valid in [
            ('a' * 40, 'b' * 64, True),
            ('master', 'b' * 64, False),
            ('A' * 40, 'b' * 64, False),
            ('a' * 40, 'B' * 64, False),
            ('a' * 40 + '/other', 'b' * 64, False),
            ('$(false)', 'b' * 64, False),
            ('a' * 40, 'b' * 64 + '\nextra', False),
        ]:
            with self.subTest(commit=commit, digest=digest):
                result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + guards],
                                        env={**os.environ, 'SOURCE_COMMIT': commit,
                                             'MANIFEST_SHA256': digest}, capture_output=True)
                self.assertEqual(result.returncode == 0, valid)


if __name__ == '__main__':
    unittest.main()
