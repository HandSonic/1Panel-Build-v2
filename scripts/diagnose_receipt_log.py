#!/usr/bin/env python3
"""Read-only, fixed-target receipt-log diagnostic; never emit response content."""
import json
import re
import subprocess

REPOSITORY = 'HandSonic/1Panel-Build-v2'
JOB_ID = 114212180857
RECEIPT = '6051eba7c22146de04c1bf6fceec15c94d9c9e426a0295b1afe1d5e4f4f24129'
ENDPOINT = f'repos/{REPOSITORY}/actions/jobs/{JOB_ID}/logs'


def summarize(result, include=False):
    stdout = result.stdout if isinstance(result.stdout, str) else ''
    stderr = result.stderr if isinstance(result.stderr, str) else ''
    status = 'unknown'
    body = stdout
    if include:
        # Only the leading HTTP header block can supply a status, never log text.
        match = re.match(r'\AHTTP/[0-9.]+ ([1-5][0-9]{2})[^\r\n]*\r?\n', stdout)
        if match:
            status = int(match[1])
            split = re.search(r'\r?\n\r?\n', stdout)
            body = stdout[split.end():] if split else ''
        else:
            body = ''
    category = 'unclassified'
    for name, pattern in (
        ('timeout', r'timed out|timeout|deadline exceeded'),
        ('connection', r'connection reset|connection refused|unexpected eof'),
        ('dns', r'no such host|name resolution'),
        ('tls', r'tls handshake|certificate verify'),
        ('http2', r'http/2|stream error|internal_error'),
        ('usage', r'unknown flag|accepts [0-9]+ arg|unknown command|invalid argument'),
        ('authentication', r'authentication required|not logged|GH_TOKEN'),
        ('redirect', r'redirect'),
        ('terminal-content', r'terminal escape sequences|binary data.*terminal'),
        ('output', r'broken pipe|write /dev|error writing'),
    ):
        if re.search(pattern, stderr, re.I):
            category = name
            break
    hashes = re.findall(r'VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})', body)
    return {'probe': 'include-status' if include else 'original-command',
            'parameter_shape': 'gh-api-fixed-repo-job-logs-get',
            'exit_code': result.returncode if type(result.returncode) is int else 'unknown',
            'http_status': status, 'error_category': category,
            'receipt_matches': result.returncode == 0 and hashes == [RECEIPT]}


def main():
    from publication_contract import sanitized_validation_log
    from release_asset_repair import GitHub
    client = GitHub(REPOSITORY, '')
    matched = False
    category = 'validation-failed'
    try:
        job = json.loads(client.run('api', f'repos/{REPOSITORY}/actions/jobs/{JOB_ID}'))
        if job.get('id') != JOB_ID or job.get('name') != 'publication_prepare' or job.get('conclusion') != 'success':
            raise ValueError('Unexpected fixed validation job')
        logs = sanitized_validation_log(client, JOB_ID, job['run_attempt'])
        matched = re.findall(r'VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})', logs) == [RECEIPT]
        category = 'none' if matched else 'receipt-mismatch'
    except (ValueError, KeyError, subprocess.CalledProcessError, OSError):
        pass
    print(json.dumps({'diagnostic_only': True, 'transport': 'gh-sanitized-job-log',
                      'receipt_matches': matched, 'error_category': category}, sort_keys=True))
    return 0 if matched else 1


if __name__ == '__main__':
    raise SystemExit(main())
