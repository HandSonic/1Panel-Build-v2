import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_receipt_log as diagnostic


class ReceiptLogDiagnostic(unittest.TestCase):
    def result(self, out='', err='', code=0):
        return subprocess.CompletedProcess(['gh'], code, out, err)

    def test_headers_credentials_and_raw_errors_never_emitted(self):
        secret = 'private-token-example'
        url = 'https://private.example/signed?token=' + secret
        for status in (200, 302, 401, 403, 404, 500):
            out = f'HTTP/2.0 {status} message {secret}\r\nLocation: {url}\r\nAuthorization: Bearer {secret}\r\n\r\nprivate body'
            summary = diagnostic.summarize(self.result(out, f'failed {url} {secret}', 1), True)
            text = json.dumps(summary)
            self.assertEqual(summary['http_status'], status)
            self.assertNotIn(secret, text)
            self.assertNotIn(url, text)
            self.assertNotIn('private body', text)
            self.assertFalse(summary['receipt_matches'])

    def test_terminal_content_rejection_is_classified_without_bypass(self):
        error = 'the response contains terminal escape sequences; secret https://private.example'
        result = diagnostic.summarize(self.result(err=error, code=1))
        self.assertEqual(result['error_category'], 'terminal-content')
        self.assertFalse(result['receipt_matches'])
        self.assertNotIn('secret', json.dumps(result))

    def test_only_header_status_and_single_successful_receipt(self):
        marker = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + diagnostic.RECEIPT
        self.assertTrue(diagnostic.summarize(self.result(marker))['receipt_matches'])
        self.assertTrue(diagnostic.summarize(self.result('HTTP/2.0 200 OK\n\n' + marker), True)['receipt_matches'])
        for text in (marker + '\n' + marker, marker.replace(diagnostic.RECEIPT, '0' * 64), ''):
            self.assertFalse(diagnostic.summarize(self.result(text))['receipt_matches'])
        self.assertFalse(diagnostic.summarize(self.result(marker, code=1))['receipt_matches'])
        self.assertEqual(diagnostic.summarize(self.result('log\nHTTP/2.0 403\n\n'), True)['http_status'], 'unknown')
        self.assertFalse(diagnostic.summarize(self.result('HTTP/2.0 200 OK\nX-Forged: ' + marker), True)['receipt_matches'])

    def test_safe_transport_fixed_job_and_redacted_failure(self):
        marker = 'VERIFIED_RELEASE_RECEIPT_SHA256=' + diagnostic.RECEIPT
        job = {'id': diagnostic.JOB_ID, 'name': 'publication_prepare', 'conclusion': 'success', 'run_attempt': 1}
        with patch('release_asset_repair.GitHub.run', return_value=json.dumps(job)) as run, patch('publication_contract.sanitized_validation_log', return_value=marker) as safe:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(diagnostic.main(), 0)
            safe.assert_called_once()
            self.assertEqual(safe.call_args.args[1:], (diagnostic.JOB_ID, 1))
            self.assertEqual(run.call_args.args, ('api', f'repos/{diagnostic.REPOSITORY}/actions/jobs/{diagnostic.JOB_ID}'))
            self.assertNotIn(marker, output.getvalue())
        with patch('release_asset_repair.GitHub.run', side_effect=OSError('secret')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(diagnostic.main(), 1)
            self.assertNotIn('secret', output.getvalue())

    def test_workflow_read_only_without_dispatch_or_artifact(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/diagnose-receipt-log.yml').read_text()
        self.assertIn('actions: read', text)
        self.assertIn('contents: read', text)
        for forbidden in ('write', 'workflow_dispatch', 'upload-artifact', 'download-artifact', 'publication.py'):
            self.assertNotIn(forbidden, text)
