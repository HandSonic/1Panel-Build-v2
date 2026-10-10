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
    results = []
    for include in (False, True):
        args = ['gh', 'api', ENDPOINT] + (['--include'] if include else [])
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=60)
            results.append(summarize(result, include))
        except subprocess.TimeoutExpired:
            results.append({'probe': 'include-status' if include else 'original-command',
                            'exit_code': 'timeout', 'http_status': 'unknown',
                            'error_category': 'timeout', 'receipt_matches': False})
        except OSError:
            results.append({'probe': 'include-status' if include else 'original-command',
                            'exit_code': 'unavailable', 'http_status': 'unknown',
                            'error_category': 'process-start', 'receipt_matches': False})
    print(json.dumps({'diagnostic_only': True, 'results': results}, sort_keys=True))
    return 0 if all(row['receipt_matches'] for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
