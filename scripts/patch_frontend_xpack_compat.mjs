import fs from 'node:fs';
import path from 'node:path';

const frontendRoot = process.argv[2];

if (!frontendRoot) {
    console.error('Usage: node patch_frontend_xpack_compat.mjs <frontend-root>');
    process.exit(1);
}

const srcRoot = path.join(frontendRoot, 'src');
const fallbackVueImport = '@/components/error-message/404.vue';
const filePattern = /\.(vue|ts|js)$/;
const importCallPattern = /import\(\s*(['"`])((?:@\/xpack|xpack)\/[^'"`]+\.vue)\1\s*\)/g;
const importFromPattern = /from\s+(['"`])((?:@\/xpack|xpack)\/[^'"`]+\.vue)\1/g;
const patchedFiles = [];

function walk(dir) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            walk(fullPath);
            continue;
        }
        if (!filePattern.test(entry.name)) {
            continue;
        }
        patchFile(fullPath);
    }
}

function resolveImportPath(importPath) {
    if (importPath.startsWith('@/')) {
        return path.join(srcRoot, importPath.slice(2));
    }
    if (importPath.startsWith('xpack/')) {
        return path.join(srcRoot, importPath);
    }
    return null;
}

function patchFile(filePath) {
    const original = fs.readFileSync(filePath, 'utf8');
    let changed = false;
    const replacements = [];

    const collectReplacement = (match, _quote, importPath) => {
        const resolved = resolveImportPath(importPath);
        if (!resolved || fs.existsSync(resolved)) {
            return match;
        }

        changed = true;
        replacements.push(importPath);
        return match.replace(importPath, fallbackVueImport);
    };

    const patched = original
        .replace(importCallPattern, collectReplacement)
        .replace(importFromPattern, collectReplacement);

    if (!changed) {
        return;
    }

    fs.writeFileSync(filePath, patched);
    patchedFiles.push(path.relative(frontendRoot, filePath));
    for (const importPath of replacements) {
        console.warn(`[xpack-compat] ${path.relative(frontendRoot, filePath)}: ${importPath} -> ${fallbackVueImport}`);
    }
}

if (!fs.existsSync(srcRoot)) {
    console.error(`Frontend src directory not found: ${srcRoot}`);
    process.exit(1);
}

walk(srcRoot);

if (patchedFiles.length === 0) {
    console.log('[xpack-compat] No missing xpack imports detected.');
} else {
    console.log(`[xpack-compat] Patched ${patchedFiles.length} file(s).`);
}
