// Data-only validation for the single, pinned missing-lock resolution step.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const npa = require(process.env.LOCK_NPM_DIR + '/node_modules/npm-package-arg');
const semver = require(process.env.LOCK_NPM_DIR + '/node_modules/semver');

export function checkEngines(engines, name) {
  if (engines == null) return;
  let requirements;
  if (Array.isArray(engines)) {
    requirements = engines.map(value => {
      if (typeof value !== 'string') throw new Error('Invalid legacy engine: ' + name);
      const match = value.match(/^([A-Za-z][A-Za-z0-9_-]*)\s+(.+)$/);
      if (!match) throw new Error('Invalid legacy engine: ' + name);
      return [match[1], match[2]];
    });
  } else if (typeof engines === 'object') requirements = Object.entries(engines);
  else throw new Error('Invalid engine schema: ' + name);
  for (const [engine, range] of requirements) {
    if (!['node', 'npm'].includes(engine)) continue;
    if (typeof range !== 'string' || semver.validRange(range) === null) throw new Error('Invalid engine range: ' + name);
    if (!semver.satisfies(engine === 'node' ? '22.22.1' : '10.9.4', range)) {
      throw new Error('Incompatible ' + engine + ' engine for automatic lock resolver: ' + name);
    }
  }
}

export function checkManifest(manifest) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) throw new Error('Invalid manifest');
  for (const key of ['workspaces', 'bundleDependencies', 'bundledDependencies', 'devEngines']) {
    if (manifest[key] !== undefined) throw new Error('Unsupported source field: ' + key);
  }
  if (manifest.packageManager !== undefined && manifest.packageManager !== 'npm@10.9.4') throw new Error('Unsupported packageManager');
  const direct = {...manifest.dependencies, ...manifest.devDependencies, ...manifest.optionalDependencies};
  function registrySpec(name, value) {
    if (typeof value !== 'string') throw new Error('Non-string dependency spec');
    const spec = npa.resolve(name, value);
    if (!spec.registry || (spec.type === 'alias' && !spec.subSpec?.registry)) throw new Error('Non-registry dependency source: ' + name);
  }
  for (const key of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    if (manifest[key] !== undefined && (!manifest[key] || typeof manifest[key] !== 'object' || Array.isArray(manifest[key]))) throw new Error('Invalid dependency map');
    for (const [name, value] of Object.entries(manifest[key] || {})) registrySpec(name, value);
  }
  function overrides(value, parent) {
    if (typeof value === 'string') {
      if (value.startsWith('$')) {
        const name = value.slice(1);
        if (!Object.hasOwn(direct, name)) throw new Error('Unknown override reference');
        registrySpec(name, direct[name]);
      } else registrySpec(parent, value);
      return;
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid overrides');
    for (const [key, child] of Object.entries(value)) {
      if (key === '.') {
        if (!parent) throw new Error('Invalid root override');
        overrides(child, parent);
      } else {
        const spec = npa(key);
        if (!spec.registry || !spec.name) throw new Error('Non-registry override selector');
        overrides(child, spec.name);
      }
    }
  }
  if (manifest.overrides !== undefined) overrides(manifest.overrides, null);
  checkEngines(manifest.engines, 'root manifest');
}

export function checkLock(manifest, lock) {
  checkManifest(manifest);
  if (lock.lockfileVersion !== 3 || !lock.packages?.['']) throw new Error('Unsupported lock schema');
  for (const key of ['name', 'version']) {
    if (lock[key] !== manifest[key] || lock.packages[''][key] !== manifest[key]) throw new Error('Root identity changed: ' + key);
  }
  const canonical = value => JSON.stringify(Object.entries(value || {}).sort(([a], [b]) => a.localeCompare(b)));
  for (const key of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    if (canonical(lock.packages[''][key]) !== canonical(manifest[key])) throw new Error('Root manifest changed: ' + key);
  }
  checkEngines(lock.packages[''].engines, 'root lock');
  const name = '(?:@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+';
  const path = new RegExp('^node_modules/' + name + '(?:/node_modules/' + name + ')*$');
  function tarball(pkg, name) {
    if (typeof pkg.resolved !== 'string' || !/^https:\/\/registry\.npmjs\.org\/[^?#\s]+\/-\/[^/?#\s]+\.tgz$/.test(pkg.resolved)) throw new Error('Non-registry dependency: ' + name);
    if (typeof pkg.integrity !== 'string' || !/^sha512-[A-Za-z0-9+/]{86}==$/.test(pkg.integrity) || Buffer.from(pkg.integrity.slice(7), 'base64').length !== 64) throw new Error('Missing SHA-512 integrity: ' + name);
  }
  for (const [name, pkg] of Object.entries(lock.packages)) {
    if (!name) continue;
    if (!pkg || typeof pkg !== 'object' || Array.isArray(pkg) || !path.test(name) || name.split('/').includes('..') || pkg.link) throw new Error('Unexpected dependency path: ' + name);
    if (pkg.inBundle !== true) tarball(pkg, name);
    else {
      let child = name;
      while (true) {
        const separator = child.lastIndexOf('/node_modules/');
        if (separator < 0) throw new Error('Bundled dependency has no pinned container');
        const parent = child.slice(0, separator), direct = child.slice(separator + 14);
        const owner = lock.packages[parent];
        if (!owner || typeof owner !== 'object' || owner.link) throw new Error('Bundled dependency parent is absent or linked');
        if (owner.inBundle === true) { child = parent; continue; }
        const declared = owner.bundleDependencies ?? owner.bundledDependencies;
        if (!Array.isArray(declared) || !declared.includes(direct)) throw new Error('Containing archive does not declare bundled child');
        tarball(owner, parent);
        break;
      }
      if (Object.hasOwn(pkg, 'resolved') || Object.hasOwn(pkg, 'integrity')) tarball(pkg, name);
    }
    checkEngines(pkg.engines, name);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  assert.equal(process.version, 'v22.22.1', 'Pinned resolver Node required');
  assert.equal(process.permission?.has('child'), false);
  assert.equal(process.permission?.has('worker'), false);
  assert.equal(process.permission?.has('addons'), false);
  assert.throws(() => require('node:child_process').spawnSync('/bin/true'), {code: 'ERR_ACCESS_DENIED'});
  const manifest = JSON.parse(fs.readFileSync('package.json'));
  if (process.argv[2] === 'preflight') checkManifest(manifest);
  else if (process.argv[2] === 'validate') checkLock(manifest, JSON.parse(fs.readFileSync('package-lock.json')));
  else throw new Error('Unsupported validation operation');
}
