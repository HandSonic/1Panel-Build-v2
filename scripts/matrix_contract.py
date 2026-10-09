#!/usr/bin/env python3
"""Bind partial architecture output to terminal jobs in one producer attempt."""
import re
from resolved_contract import ARCHES

REPOSITORY = 'HandSonic/1Panel-Build-v2'
FIELDS = {'schema_version', 'version', 'artifacts', 'requested_architectures',
          'producer_run_id', 'producer_run_attempt', 'producer_head_sha', 'outcomes'}
OUTCOME_FIELDS = {'architecture', 'status', 'stage', 'reason', 'job_id', 'job_url'}


def positive(value):
    return type(value) is int and value > 0


def validate(value, version=None):
    if not isinstance(value, dict) or set(value) != FIELDS or value['schema_version'] != 2:
        raise ValueError('Unsupported matrix manifest')
    if version is not None and value['version'] != version:
        raise ValueError('Matrix version mismatch')
    if not isinstance(value['producer_head_sha'],str) or not re.fullmatch('[0-9a-f]{40}',value['producer_head_sha']):
        raise ValueError('Invalid producer API head commit')
    requested = value['requested_architectures']
    if not isinstance(requested, list) or not requested or len(set(requested)) != len(requested) or any(a not in ARCHES for a in requested):
        raise ValueError('Invalid requested architectures')
    if not positive(value['producer_run_id']) or not positive(value['producer_run_attempt']):
        raise ValueError('Invalid producer attempt identity')
    outcomes = value['outcomes']
    if not isinstance(outcomes, list) or len(outcomes) != len(requested):
        raise ValueError('Every requested branch needs a terminal outcome')
    seen, ids, successful = set(), set(), []
    for row in outcomes:
        if not isinstance(row, dict) or set(row) != OUTCOME_FIELDS:
            raise ValueError('Invalid branch outcome fields')
        arch = row['architecture']
        if arch not in requested or arch in seen or not positive(row['job_id']) or row['job_id'] in ids:
            raise ValueError('Duplicate or unrequested branch identity')
        seen.add(arch); ids.add(row['job_id'])
        expected_url = f'https://github.com/{REPOSITORY}/actions/runs/{value["producer_run_id"]}/job/{row["job_id"]}'
        if row['job_url'] != expected_url or row['status'] not in ('success', 'failure') or row['stage'] != 'compile':
            raise ValueError('Invalid branch status or job binding')
        if not isinstance(row['reason'], str) or len(row['reason']) > 512 or any(ord(c) < 32 for c in row['reason']):
            raise ValueError('Invalid branch failure reason')
        if row['status'] == 'success':
            if row['reason']:
                raise ValueError('Successful branch has a failure reason')
            successful.append(arch)
        elif not row['reason']:
            raise ValueError('Failed branch must explain its outcome')
    artifacts = value['artifacts']
    if not isinstance(artifacts, list) or [r.get('architecture') for r in artifacts] != successful:
        raise ValueError('Accepted artifacts must exactly match successful outcomes')
    if [r['architecture'] for r in outcomes] != requested:
        raise ValueError('Outcome order differs from requested architecture order')
    return successful


def collect(jobs, requested, run_id, attempt):
    """Use only the API's exact-attempt job endpoint, never an all-attempt merge."""
    result = []
    for arch in requested:
        matches = [job for job in jobs if job.get('name') == f'compile ({arch})']
        if len(matches) != 1:
            raise ValueError('Missing or duplicate architecture job: ' + arch)
        job = matches[0]
        if job.get('run_id') != run_id or job.get('run_attempt') != attempt or job.get('status') != 'completed':
            raise ValueError('Nonterminal or different-attempt architecture job')
        conclusion = job.get('conclusion')
        if conclusion not in ('success', 'failure', 'timed_out'):
            raise ValueError('Cancelled, skipped or unsupported architecture conclusion')
        result.append({'architecture': arch, 'status': 'success' if conclusion == 'success' else 'failure',
                       'stage': 'compile', 'reason': '' if conclusion == 'success' else 'GitHub job ' + conclusion,
                       'job_id': job['id'], 'job_url': job['html_url']})
    return result


def verify_execution(run, expected_commit, execution_commit=None):
    head=run.get('head_sha')
    if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{40}',head) or not re.fullmatch('[0-9a-f]{40}',expected_commit):
        raise ValueError('Invalid immutable producer execution identity')
    if run.get('event')!='pull_request':
        if run.get('event') not in ('workflow_dispatch','push','schedule') or head!=expected_commit:
            raise ValueError('Producer API head differs from executed build commit')
        return
    if not isinstance(execution_commit,dict) or execution_commit.get('sha')!=expected_commit:
        raise ValueError('PR build requires authenticated synthetic merge commit')
    parents=[p.get('sha') for p in execution_commit.get('parents',[])]
    matches=[p for p in run.get('pull_requests',[]) if p.get('head',{}).get('sha')==head]
    if len(matches)!=1 or parents!=[matches[0].get('base',{}).get('sha'),head] or not re.fullmatch('[0-9a-f]{40}',execution_commit.get('tree',{}).get('sha','')):
        raise ValueError('PR merge parents do not match the recorded base/head')


def verify_jobs(value, run, jobs, expected_commit, execution_commit=None):
    successful = validate(value)
    for job in jobs:
        if job.get('run_id') != value['producer_run_id'] or job.get('run_attempt') != value['producer_run_attempt'] or job.get('head_sha') != value['producer_head_sha']:
            raise ValueError('Producer job belongs to another run, attempt or commit')
    if run.get('id') != value['producer_run_id'] or run.get('run_attempt') != value['producer_run_attempt'] or run.get('head_sha') != value['producer_head_sha']:
        raise ValueError('Producer run identity mismatch')
    verify_execution(run,expected_commit,execution_commit)
    if run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure') or run.get('path', '').split('@')[0] != '.github/workflows/build.yml':
        raise ValueError('Producer is not a terminal permitted build')
    for name in ('tests', 'prepare', 'build'):
        matches = [j for j in jobs if j.get('name') == name]
        if len(matches) != 1 or matches[0].get('conclusion') != 'success' or matches[0].get('status') != 'completed':
            raise ValueError('Shared producer gate failed: ' + name)
    if collect(jobs, value['requested_architectures'], value['producer_run_id'], value['producer_run_attempt']) != value['outcomes']:
        raise ValueError('Declared outcomes differ from exact API job identities')
    declared = {r['job_id'] for r in value['outcomes'] if r['status'] == 'failure'}
    actual = {j['id'] for j in jobs if j.get('conclusion') not in ('success', 'skipped')}
    if actual != declared or (run['conclusion'] == 'success') != (not declared):
        raise ValueError('Overall producer failure is not explained by declared branches')
    return successful
