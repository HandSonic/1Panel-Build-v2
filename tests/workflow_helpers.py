"""Read workflow jobs and stable step identities without depending on display text."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def github_jobs():
    return yaml.safe_load((ROOT / '.github/workflows/build.yml').read_text())['jobs']


def step_by_id(job, identity):
    matches = [step for step in job['steps'] if step.get('id') == identity]
    if len(matches) != 1:
        raise AssertionError(f'Expected one step with id {identity!r}, found {len(matches)}')
    return matches[0]
