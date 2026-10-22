#!/usr/bin/env python3
"""Bind partial architecture output to the effective jobs of one producer attempt."""
import re
import json
import os
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime, timedelta
from resolved_contract import ARCHES

REPOSITORY = 'HandSonic/1Panel-Build-v2'
FIELDS = {'schema_version', 'version', 'artifacts', 'requested_architectures',
          'producer_run_id', 'producer_run_attempt', 'producer_head_sha', 'outcomes'}
OUTCOME_FIELDS = {'architecture', 'status', 'stage', 'reason', 'job_id', 'job_url'}
WRITERS = {'candidate', 'automatic_publication', 'publication_repair'}
PENDING_STATUSES = {'queued', 'in_progress', 'waiting', 'pending', 'requested'}
BRANCH_RESULT = 'branch-result.json'
RESULT_FIELDS = {'schema_version', 'version', 'architecture', 'producer_run_id',
                 'producer_run_attempt', 'producer_head_sha', 'build_repository_commit',
                 'generation', 'status', 'reason'}


class PendingJobs(ValueError):
    """An incomplete API snapshot, rather than a failed or invalid producer."""


def job_pending(job):
    status, conclusion = job.get('status'), job.get('conclusion')
    if status == 'completed':
        return conclusion is None
    if status not in PENDING_STATUSES or conclusion is not None:
        raise ValueError(f'Invalid job state: {job.get("name")} job={job.get("id")} status={status} conclusion={conclusion}')
    return True


def retry_pending(check, *, attempts=13, delay=5):
    """Refresh read-only evidence; never retry invalid identities or writes."""
    for index in range(attempts):
        try:
            return check()
        except PendingJobs as error:
            if index + 1 == attempts:
                raise ValueError(f'GitHub job state did not settle after {attempts} snapshots: {error}') from error
            print(f'Waiting for GitHub job state ({index + 1}/{attempts}): {error}', file=sys.stderr, flush=True)
            time.sleep(delay)


def read_jobs(fetch_json, repository, run_id):
    """Include inherited executions when a failed-only rerun omits old jobs."""
    jobs = []
    for page in range(1, 11):
        result = fetch_json(f'repos/{repository}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}')
        if not isinstance(result, dict):
            raise ValueError('Invalid producer job inventory')
        rows, total = result.get('jobs'), result.get('total_count')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or type(total) is not int or total < 0:
            raise ValueError('Invalid producer job inventory')
        jobs.extend(rows)
        if len(jobs) == total:
            return jobs
        if not rows or len(jobs) > total:
            raise PendingJobs('Producer job inventory changed during pagination')
    raise ValueError('Excessive producer job inventory')


def positive(value):
    return type(value) is int and value > 0


def validate(value, version=None):
    if not isinstance(value, dict) or set(value) not in (FIELDS, FIELDS | {'outcome_source'}) or value['schema_version'] != 2:
        raise ValueError('Unsupported matrix manifest')
    if 'outcome_source' in value and value['outcome_source'] != 'package-step':
        raise ValueError('Unsupported architecture outcome source')
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
        elif value.get('outcome_source') == 'package-step' and row['reason'] not in ('Package step failure', 'Package step skipped'):
            raise ValueError('Invalid recorded package failure reason')
    artifacts = value['artifacts']
    if not isinstance(artifacts, list) or [r.get('architecture') for r in artifacts] != successful:
        raise ValueError('Accepted artifacts must exactly match successful outcomes')
    if [r['architecture'] for r in outcomes] != requested:
        raise ValueError('Outcome order differs from requested architecture order')
    return successful


def latest_executed_job(jobs):
    """Keep genuine reruns, resolving exact successful GitHub metadata copies.

    Partial reruns may assign an inherited job a new ID/attempt while retaining
    the original execution times, runner and steps. Only an exact copy created
    after that execution can refer back to a unique original successful job.
    """
    fields = ('name', 'run_id', 'head_sha', 'status', 'conclusion',
              'started_at', 'completed_at', 'runner_id', 'runner_name',
              'runner_group_name', 'labels', 'steps')
    def instant(value):
        if not isinstance(value, str):
            raise ValueError('Invalid producer job timestamp')
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as error:
            raise ValueError('Invalid producer job timestamp') from error
        if parsed.tzinfo is None:
            raise ValueError('Unzoned producer job timestamp')
        return parsed

    executed = []
    for job in sorted(jobs, key=lambda row: row['run_attempt']):
        # GitHub can create a skipped dependency node after the workflow has
        # ended, with completed_at preceding created_at. It remains a skipped
        # job in this attempt, never an inherited successful execution.
        if job.get('status') == 'completed' and job.get('conclusion') == 'skipped':
            executed.append(job)
            continue
        # Missing timing metadata never grants inherited-producer treatment.
        copied = (job.get('created_at') is not None and job.get('completed_at') is not None and
                  instant(job['created_at']) > instant(job['completed_at']))
        if not copied:
            executed.append(job)
            continue
        if (job.get('status') != 'completed' or job.get('conclusion') != 'success' or
            any(field not in job for field in fields) or not positive(job.get('runner_id')) or
            not isinstance(job.get('steps'), list) or not job['steps'] or
            instant(job['started_at']) > instant(job['completed_at'])):
            raise ValueError('Invalid inherited producer job metadata')
        originals = [old for old in executed if
                     all(field in old and old[field] == job[field] for field in fields) and
                     (old.get('runner_group_id') == job.get('runner_group_id') or
                      (old.get('runner_group_id') == 0 and 'runner_group_id' in job and
                       job['runner_group_id'] is None and old.get('runner_group_name') == 'GitHub Actions')) and
                     old.get('created_at') is not None and
                     instant(old['created_at']) <= instant(old['started_at'])]
        if len(originals) != 1:
            raise ValueError('Missing or ambiguous original producer job')
    if not executed:
        raise ValueError('Missing executed producer job')
    return executed[-1]


def effective_jobs(jobs, run_id, attempt, head_sha=None):
    """Keep the latest real execution through the selected producer attempt."""
    if not positive(attempt):
        raise ValueError('Invalid producer attempt')
    grouped, seen, ids = {}, set(), set()
    for job in jobs:
        job_attempt = job.get('run_attempt')
        if not positive(job_attempt):
            raise ValueError('Invalid producer job attempt')
        if job_attempt > attempt:
            continue
        if job.get('run_id') != run_id or (head_sha is not None and job.get('head_sha') != head_sha):
            raise ValueError('Producer job belongs to another run or commit')
        name = job.get('name')
        if not isinstance(name, str) or not name or not positive(job.get('id')):
            raise ValueError('Invalid producer job identity')
        key = (name, job_attempt)
        if key in seen or job['id'] in ids:
            raise ValueError('Duplicate job execution: ' + str(name))
        seen.add(key); ids.add(job['id'])
        grouped.setdefault(name, []).append(job)
    return [latest_executed_job(rows) for rows in grouped.values()]


def collect(jobs, requested, run_id, attempt, *, head_sha=None):
    jobs = effective_jobs(jobs, run_id, attempt, head_sha)
    names = {f'compile ({arch})' for arch in requested}
    extra = {j.get('name') for j in jobs if isinstance(j.get('name'), str) and
             j['name'].startswith('compile')} - names
    if extra:
        raise ValueError('Unrequested architecture jobs: ' + ', '.join(sorted(extra)))
    result = []
    for arch in requested:
        matches = [job for job in jobs if job.get('name') == f'compile ({arch})']
        if len(matches) != 1:
            raise PendingJobs('Missing or duplicate architecture job: ' + arch)
        job = matches[0]
        detail = f'{arch}: job={job.get("id")} attempt={job["run_attempt"]} status={job.get("status")} conclusion={job.get("conclusion")}'
        if job_pending(job):
            raise PendingJobs('Nonterminal architecture job: ' + detail)
        conclusion = job.get('conclusion')
        if conclusion not in ('success', 'failure', 'timed_out'):
            raise ValueError('Cancelled, skipped or unsupported architecture conclusion: ' + detail)
        result.append({'architecture': arch, 'status': 'success' if conclusion == 'success' else 'failure',
                       'stage': 'compile', 'reason': '' if conclusion == 'success' else 'GitHub job ' + conclusion,
                       'job_id': job['id'], 'job_url': job['html_url']})
    return result


def record(directory, version, arch, generation, step_result):
    """Persist the controlled package step's outcome, including failed retries."""
    if arch not in ARCHES or step_result not in ('success', 'failure', 'skipped'):
        raise ValueError('Invalid architecture package result')
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    head, commit = os.environ['PRODUCER_HEAD_SHA'], os.environ['BUILD_REPOSITORY_COMMIT']
    if (not positive(run_id) or not positive(attempt) or
        not re.fullmatch('[0-9a-f]{40}', head) or not re.fullmatch('[0-9a-f]{40}', commit) or
        not re.fullmatch(str(run_id) + r'-[1-9][0-9]*', generation)):
        raise ValueError('Invalid architecture result identity')
    directory = Path(directory)
    if directory.name != 'dist' or directory.is_symlink():
        raise ValueError('Architecture result destination must be a dist directory')
    success = step_result == 'success'
    if not success and directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    value = dict(schema_version=1, version=version, architecture=arch,
                 producer_run_id=run_id, producer_run_attempt=attempt,
                 producer_head_sha=head, build_repository_commit=commit,
                 generation=generation, status='success' if success else 'failure',
                 reason='' if success else 'Package step ' + step_result)
    (directory / BRANCH_RESULT).write_text(json.dumps(value, indent=2) + '\n')
    return value


def collect_recorded(jobs, results, requested, run_id, attempt, head, commit, generation, version):
    """Use artifacts for outcomes and REST only for immutable job identities."""
    selected = effective_jobs(jobs, run_id, attempt, head)
    expected = {f'compile ({arch})' for arch in requested}
    extra = {j.get('name') for j in selected if isinstance(j.get('name'), str) and
             j['name'].startswith('compile')} - expected
    if extra:
        raise ValueError('Unrequested architecture jobs: ' + ', '.join(sorted(extra)))
    if not any(j.get('name') == 'build' and j['run_attempt'] == attempt for j in selected):
        raise PendingJobs('Current aggregate job identity is not visible')
    outcomes = []
    for arch in requested:
        value = results[arch]
        if (not isinstance(value, dict) or set(value) != RESULT_FIELDS or type(value['schema_version']) is not int or value['schema_version'] != 1 or
            value['version'] != version or value['architecture'] != arch or
            type(value['producer_run_id']) is not int or value['producer_run_id'] != run_id or
            not positive(value['producer_run_attempt']) or value['producer_run_attempt'] > attempt or
            value['producer_head_sha'] != head or value['build_repository_commit'] != commit or
            value['generation'] != generation or value['status'] not in ('success', 'failure') or
            value['reason'] not in (('',) if value['status'] == 'success' else ('Package step failure', 'Package step skipped'))):
            raise ValueError('Invalid or stale architecture result: ' + arch)
        matches = [j for j in selected if j.get('name') == f'compile ({arch})']
        if not matches or matches[0]['run_attempt'] < value['producer_run_attempt']:
            raise PendingJobs('Recorded architecture job identity is not visible: ' + arch)
        job = matches[0]
        if job['run_attempt'] != value['producer_run_attempt']:
            raise ValueError('Architecture result belongs to an older execution: ' + arch)
        outcomes.append(dict(architecture=arch, status=value['status'], stage='compile',
                             reason=value['reason'], job_id=job['id'], job_url=job['html_url']))
    return outcomes


def merged_pr_checkout(job, head, commit, fetch_json, fetch_log):
    """Recover an absent run/PR link from the authenticated checkout and PR."""
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(positive(job.get('id')) and job.get('name') == 'build' and
            job.get('status') == 'completed' and job.get('conclusion') == 'success',
            'PR checkout requires a successful exact build job')
    steps = job.get('steps')
    require(isinstance(steps, list), 'Missing PR checkout steps')
    matches = [step for step in steps if isinstance(step, dict) and isinstance(step.get('name'), str) and
               re.fullmatch(r'Run actions/checkout@(?:v[1-9][0-9]*|[0-9a-f]{40})', step['name'])]
    require(len(matches) == 1, 'Missing or ambiguous PR checkout step')
    step = matches[0]
    require(step.get('status') == 'completed' and step.get('conclusion') == 'success' and
            positive(step.get('number')), 'PR checkout step did not succeed')

    def timestamp(value):
        require(isinstance(value, str) and re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z', value),
            'Invalid PR checkout timestamp')
        return datetime.fromisoformat(value.replace('Z', '+00:00'))

    start, end = timestamp(step.get('started_at')), timestamp(step.get('completed_at'))
    require(start <= end, 'Invalid PR checkout interval')
    end += timedelta(seconds=1)  # API step times omit the log's fractional second.
    log = fetch_log(job['id'])
    require(isinstance(log, str) and len(log.encode('utf-8')) <= 8 * 1024 * 1024,
            'PR checkout log exceeds parser budget')
    escape = r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))'
    def neutralize(match):
        left, right = log[match.start()-1:match.start()], log[match.end():match.end()+1]
        inside = bool(left and right and re.match(r'[A-Za-z0-9_=]', left) and re.match(r'[A-Za-z0-9_=]', right))
        return '' if re.fullmatch(r'\x1b\[[0-9;]*m', match[0]) and not inside else '[CONTROL]'
    log = re.sub(escape, neutralize, log)
    log = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '[CONTROL]', log)
    lines = []
    for line in log.splitlines():
        record = re.fullmatch(r'([0-9T:.-]+Z) (.*)', line)
        lines.append((record[1], record[2]) if record else (None, line))

    def one(pattern):
        found = [(index, stamp, re.fullmatch(pattern, body))
                 for index, (stamp, body) in enumerate(lines) if re.fullmatch(pattern, body)]
        require(len(found) == 1, 'Missing or ambiguous PR checkout log evidence')
        index, stamp, match = found[0]
        require(start <= timestamp(stamp) < end, 'PR checkout evidence is outside its authenticated step')
        return index, match

    opened, _ = one(r'##\[group\]' + re.escape(step['name']))
    synced, _ = one(r'Syncing repository: ' + re.escape(REPOSITORY))
    fetched, fetch = one(r'\[command\]/\S+/git -c protocol\.version=2 fetch --no-tags --prune '
                         r'--no-recurse-submodules --depth=1 origin \+([0-9a-f]{40}):refs/remotes/pull/([1-9][0-9]*)/merge')
    checked, checkout = one(r'\[command\]/\S+/git checkout --progress --force refs/remotes/pull/([1-9][0-9]*)/merge')
    described, description = one(r'HEAD is now at ([0-9a-f]{7,40}) Merge ([0-9a-f]{40}) into ([0-9a-f]{40})')
    queried, _ = one(r'\[command\]/\S+/git log -1 --format=%H')
    executed, execution = one(r'([0-9a-f]{40})')
    later = [index for index, (_, body) in enumerate(lines) if index > opened and
             re.match(r'##\[group\](?:Run |Post )', body)]
    require(not later or executed < min(later), 'PR checkout evidence crosses a later step boundary')
    require(opened < synced < fetched < checked < described < queried and executed == queried + 1 and
            fetch[1] == execution[1] == commit and commit.startswith(description[1]) and
            fetch[2] == checkout[1] and description[2] == head,
            'PR checkout does not bind the executed merge')
    number, base = int(fetch[2]), description[3]
    pr = fetch_json(f'repos/{REPOSITORY}/pulls/{number}')
    require(isinstance(pr, dict) and pr.get('number') == number and pr.get('state') == 'closed' and
            pr.get('merged') is True and all(isinstance(pr.get(side), dict) and
                isinstance(pr[side].get('repo'), dict) and pr[side]['repo'].get('full_name') == REPOSITORY
                for side in ('base', 'head')) and
            pr['base'].get('sha') == base and pr['head'].get('sha') == head,
            'Recovered PR does not bind the exact merged repository/base/head')
    return base


def verify_execution(run, expected_commit, execution_commit=None, *, checkout_job=None, fetch_json=None, fetch_log=None):
    head=run.get('head_sha')
    if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{40}',head) or not re.fullmatch('[0-9a-f]{40}',expected_commit):
        raise ValueError('Invalid immutable producer execution identity')
    if run.get('event')!='pull_request':
        if run.get('event') not in ('workflow_dispatch','push','schedule') or head!=expected_commit:
            raise ValueError('Producer API head differs from executed build commit')
        return
    if not isinstance(execution_commit,dict) or execution_commit.get('sha')!=expected_commit:
        raise ValueError('PR build requires authenticated synthetic merge commit')
    prs = run.get('pull_requests')
    if not isinstance(prs, list) or len(prs) > 1:
        raise ValueError('Ambiguous PR producer head')
    if prs:
        pr = prs[0]
        if (not isinstance(pr, dict) or not isinstance(pr.get('head'), dict) or
            not isinstance(pr.get('base'), dict) or pr['head'].get('sha') != head or
            not isinstance(pr['base'].get('sha'), str) or not re.fullmatch('[0-9a-f]{40}', pr['base']['sha'])):
            raise ValueError('Ambiguous PR producer head')
        base = pr['base']['sha']
    else:
        if checkout_job is None or fetch_json is None or fetch_log is None:
            raise ValueError('Missing authenticated merged PR checkout evidence')
        base = merged_pr_checkout(checkout_job, head, expected_commit, fetch_json, fetch_log)
    parents = execution_commit.get('parents')
    if (not isinstance(parents, list) or any(not isinstance(parent, dict) for parent in parents) or
        [parent.get('sha') for parent in parents] != [base, head] or
        not re.fullmatch('[0-9a-f]{40}', execution_commit.get('tree', {}).get('sha', ''))):
        raise ValueError('PR merge parents do not match the recorded base/head')


def verify_jobs(value, run, jobs, expected_commit, execution_commit=None, *, allow_publication_failures=False,
                fetch_json=None, fetch_log=None):
    successful = validate(value)
    jobs = effective_jobs(jobs, value['producer_run_id'], value['producer_run_attempt'], value['producer_head_sha'])
    if run.get('id') != value['producer_run_id'] or run.get('run_attempt') != value['producer_run_attempt'] or run.get('head_sha') != value['producer_head_sha']:
        raise ValueError('Producer run identity mismatch')
    if run.get('path', '').split('@')[0] != '.github/workflows/build.yml':
        raise ValueError('Producer is not a terminal permitted build')
    if job_pending(run):
        raise PendingJobs('Producer run not yet terminal')
    if run.get('conclusion') not in ('success', 'failure'):
        raise ValueError('Producer is not a terminal permitted build')
    for name in ('tests', 'prepare', 'build'):
        matches = [j for j in jobs if j.get('name') == name]
        if not matches or job_pending(matches[0]):
            raise PendingJobs('Shared producer gate not yet terminal: ' + name)
        if len(matches) != 1 or matches[0].get('conclusion') != 'success' or matches[0].get('status') != 'completed':
            raise ValueError('Shared producer gate failed: ' + name)
    verify_execution(run, expected_commit, execution_commit,
                     checkout_job=next(job for job in jobs if job.get('name') == 'build'),
                     fetch_json=fetch_json, fetch_log=fetch_log)
    native = collect(jobs, value['requested_architectures'], value['producer_run_id'], value['producer_run_attempt'])
    if value.get('outcome_source') == 'package-step':
        # Package failures are captured by the controlled build step. Every
        # surrounding native job must succeed, including artifact transport and
        # post-job cleanup, so infrastructure failures cannot masquerade as an
        # independently failed package branch.
        if any(row['status'] != 'success' for row in native):
            raise ValueError('Native architecture job failed outside the package outcome contract')
        identity = lambda row: (row['architecture'], row['stage'], row['job_id'], row['job_url'])
        if [identity(row) for row in native] != [identity(row) for row in value['outcomes']]:
            raise ValueError('Declared outcomes differ from effective API job identities')
        status = next((job for job in jobs if job.get('name') == 'matrix_status'), None)
        if status is None or status['run_attempt'] != value['producer_run_attempt'] or job_pending(status):
            raise PendingJobs('Final matrix status gate not yet terminal for the producer attempt')
        partial = any(row['status'] == 'failure' for row in value['outcomes'])
        if status.get('conclusion') != ('failure' if partial else 'success'):
            raise ValueError('Final matrix status gate differs from recorded package outcomes')
        declared = {status['id']} if partial else set()
    else:
        if native != value['outcomes']:
            raise ValueError('Declared outcomes differ from effective API job identities')
        declared = {r['job_id'] for r in value['outcomes'] if r['status'] == 'failure'}
    # A rerun can reach publication preparation before its new writer is
    # created. A failed writer from an older attempt is not this build's result.
    actual = {j['id'] for j in jobs if j.get('conclusion') not in ('success', 'skipped') and
              not (j.get('name') in WRITERS and j['run_attempt'] < value['producer_run_attempt'])}
    writers = {j['id'] for j in jobs if allow_publication_failures and
               j.get('name') in WRITERS and j['run_attempt'] == value['producer_run_attempt'] and
               j.get('status')=='completed' and j.get('conclusion') in ('failure','timed_out')}
    if actual != declared | writers or (run['conclusion'] == 'success') != (not actual):
        raise ValueError('Overall producer failure is not explained by declared branches')
    return successful


def verify_current_jobs(value, run, jobs, expected_commit, run_id, attempt):
    """Validate the current non-PR producer before its own publication jobs finish."""
    if run.get('event') not in ('push', 'schedule', 'workflow_dispatch'):
        raise ValueError('Automatic publication requires a trusted non-PR execution')
    if value.get('producer_run_id') != run_id or value.get('producer_run_attempt') != attempt:
        raise ValueError('Automatic publication must consume its own exact attempt')
    if run.get('id') != run_id or run.get('run_attempt') != attempt or run.get('head_sha') != value.get('producer_head_sha'):
        raise ValueError('Producer run identity mismatch')
    verify_execution(run, expected_commit)
    if run.get('path', '').split('@')[0] != '.github/workflows/build.yml':
        raise ValueError('Producer is not a permitted build workflow')
    if value.get('outcome_source') == 'package-step':
        # Current automatic publication uses the native DAG and its bound
        # preparation receipt. This API verifier must not synthesize the final
        # matrix gate while that producer is still running.
        return verify_jobs(value, run, jobs, expected_commit)
    if run.get('status') in PENDING_STATUSES - {'in_progress'} and run.get('conclusion') is None:
        raise PendingJobs('Current producer run not yet visible as in_progress')
    if run.get('status') not in ('in_progress', 'completed') or run.get('conclusion') not in (None, 'success', 'failure'):
        raise ValueError('Current workflow is cancelled or unavailable')
    if run.get('status')=='completed':
        return verify_jobs(value,run,jobs,expected_commit)
    jobs = effective_jobs(jobs, run_id, attempt, value.get('producer_head_sha'))
    pending = {'publication_prepare', 'automatic_publication', 'publication_repair', 'candidate'}
    checked = []
    for job in jobs:
        if job_pending(job):
            if job.get('name') not in pending:
                raise PendingJobs('Unfinished shared or architecture gate: ' + str(job.get('name')))
            continue
        checked.append(job)
    terminal = dict(run, status='completed', conclusion='failure' if any(r['status']=='failure' for r in value['outcomes']) else 'success')
    return verify_jobs(value, terminal, checked, expected_commit)


if __name__ == '__main__':
    if len(sys.argv) != 7 or sys.argv[1] != 'record':
        raise SystemExit('Usage: matrix_contract.py record DIST VERSION ARCH GENERATION RESULT')
    record(*sys.argv[2:])
