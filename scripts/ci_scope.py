#!/usr/bin/env python3
"""Keep publication-only PRs in unit CI; build all architectures for build changes."""
import os
from pathlib import Path
import re
import subprocess


PUBLICATION_ONLY = {
    'scripts/automatic_publication.py', 'scripts/release_asset_repair.py',
    'scripts/public_packages.py', 'scripts/manual_publication.py',
    'README.md', 'README_zh.md', 'docs-publication-runbook.md',
}


def requires_compile(event, paths):
    if event != 'pull_request' or not paths:
        return True
    return any(path.startswith('tests/go_sqlite_compat/') or
               (path not in PUBLICATION_ONLY and not path.startswith(('docs/', 'tests/')))
               for path in paths)


def decide(env, read_diff=subprocess.check_output):
    event = env.get('GITHUB_EVENT_NAME')
    if event != 'pull_request':
        return True
    base, head = env.get('PR_BASE_SHA', ''), env.get('PR_HEAD_SHA', '')
    if not all(re.fullmatch('[0-9a-f]{40}', sha) for sha in (base, head)):
        return True
    try:
        raw = read_diff(['git', 'diff', '--no-renames', '--name-only', '-z', base + '...' + head])
        paths = [name.decode('utf-8') for name in raw.split(b'\0') if name]
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return True
    return requires_compile(event, paths)


if __name__ == '__main__':
    value = str(decide(os.environ)).lower()
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write('build_required=' + value + '\n')
