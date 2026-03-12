import fs from 'node:fs';
import path from 'node:path';

const repoRoot = process.argv[2];

if (!repoRoot) {
    console.error('Usage: node patch_backend_xpack_compat.mjs <repo-root>');
    process.exit(1);
}

const stubRelativePath = path.join('agent', 'xpack', 'xglobal', 'community.go');
const stubPath = path.join(repoRoot, stubRelativePath);
const stubContent = `//go:build !xpack

package xglobal

const IsXpack = false
`;

if (!fs.existsSync(repoRoot)) {
    console.error(`Repository root not found: ${repoRoot}`);
    process.exit(1);
}

if (fs.existsSync(stubPath)) {
    console.log(`[xpack-compat] Reuse existing ${stubRelativePath}`);
    process.exit(0);
}

fs.mkdirSync(path.dirname(stubPath), { recursive: true });
fs.writeFileSync(stubPath, stubContent);
console.warn(`[xpack-compat] Created stub ${stubRelativePath}`);
