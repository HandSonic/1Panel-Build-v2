// Select an exact official LTS release using data-only npm engine declarations.
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const semver = require(process.env.PROBE_NPM_DIR + '/node_modules/semver');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const requirements = [];
for (const pkg of input.packages) {
  const engines = pkg.engines;
  if (engines == null) continue;
  let rows;
  if (Array.isArray(engines)) {
    rows = engines.map(value => {
      if (typeof value !== 'string') throw new Error('Unsupported engine declaration');
      const match = /^(node|npm)\s+(.+)$/.exec(value);
      if (!match) throw new Error('Unsupported legacy engine declaration');
      return [match[1], match[2]];
    });
  } else if (typeof engines === 'object') rows = Object.entries(engines);
  else throw new Error('Unsupported engine schema');
  for (const [name, range] of rows) {
    if (!['node', 'npm'].includes(name)) continue;
    if (typeof range !== 'string' || semver.validRange(range) == null) throw new Error('Invalid engine range');
    requirements.push([name, range]);
  }
}
const candidates = input.releases.filter(row => row.lts && semver.valid(row.version) &&
  semver.prerelease(row.version) == null && semver.valid(row.npm) &&
  requirements.every(([name, range]) => semver.satisfies(name === 'node' ? row.version : row.npm, range)));
if (!candidates.length) throw new Error('No official LTS Node/npm pair satisfies every declared engine');
// Prefer the oldest supported LTS major and its latest patch, reducing needless churn.
const major = Math.min(...candidates.map(row => semver.major(row.version)));
const selected = candidates.filter(row => semver.major(row.version) === major).sort((a,b) => semver.rcompare(a.version,b.version))[0];
console.log(JSON.stringify({node: selected.version.replace(/^v/, ''), npm: selected.npm}));
