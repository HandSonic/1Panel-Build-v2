#!/usr/bin/env python3
"""Keep publication-only and documentation PRs out of the build matrix."""
import json
import os
from pathlib import Path
import re
import subprocess


PUBLICATION_ONLY = {
    'scripts/automatic_publication.py',
    'scripts/candidate_publication.py',
    'scripts/manual_publication.py',
    'scripts/release_asset_repair.py',
    'scripts/public_packages.py',
}


def build_required(paths):
    if not paths:
        return True
    for path in paths:
        if path in PUBLICATION_ONLY or path in ('README.md', 'README_zh.md'):
            continue
        if re.fullmatch(r'docs[^/]*\.md', path) or (path.startswith('docs/') and path.endswith('.md')):
            continue
        if re.fullmatch(r'tests/test_[^/]+\.py', path):
            continue
        return True
    return False


def event_requires_build(event_name, event, diff=None, merge_sha=None):
    if event_name != 'pull_request':
        return True
    try:
        pull = event['pull_request']
        base, head = pull['base']['sha'], pull['head']['sha']
        if any(not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha) for sha in (base, head, merge_sha)):
            return True
        if diff is None:
            def diff(base, head):
                return subprocess.run(['git', 'diff', '--name-only', '--no-renames', '-z', base, head],
                                      check=True, capture_output=True).stdout
        # Checkout contains GitHub's merge result and its base parent at depth 2.
        # Comparing the branch head would also count unrelated base-only edits.
        raw = diff(base, merge_sha)
        paths = [path for path in raw.decode('utf-8').split('\0') if path]
        return build_required(paths)
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError):
        # An unavailable comparison must never silently suppress integration.
        return True


def main():
    name = os.environ.get('GITHUB_EVENT_NAME', '')
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()) if name == 'pull_request' else {}
        required = event_requires_build(name, event, merge_sha=os.environ.get('GITHUB_SHA'))
    except (KeyError, OSError, ValueError):
        required = True
    print(str(required).lower())


if __name__ == '__main__':
    main()
