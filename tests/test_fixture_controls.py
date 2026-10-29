"""Real HTTP controls against both isolated, synthetic dashboard previews."""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path.home() / '.cache/agent-scratch/personal-codex-router/fixture-control-tests'


class FixtureControlTests(unittest.TestCase):
    def test_fast_mode_and_routing_roundtrip_in_both_previews(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        for filename, account_id in (('fast_mode_fixture.py', 'on-fast'), ('signin_preview.py', 'account-b')):
            with self.subTest(fixture=filename), tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
                with socket.socket() as available:
                    available.bind(('127.0.0.1', 0))
                    port = available.getsockname()[1]
                process = subprocess.Popen([sys.executable, str(ROOT / 'tests' / filename), '--port', str(port),
                                            '--state-dir', directory], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                try:
                    def request(path, data=None):
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                        headers = {'Host': f'127.0.0.1:{port}', 'X-Router-Request': '1',
                                   'Content-Type': 'application/json'}
                        try:
                            connection.request('POST' if data is not None else 'GET', path,
                                               json.dumps(data) if data is not None else None, headers)
                            response = connection.getresponse()
                            return response.status, json.loads(response.read())
                        finally:
                            connection.close()
                    ends_at = time.monotonic() + 8
                    while True:
                        self.assertIsNone(process.poll(), 'Synthetic preview exited during startup')
                        try:
                            status, overview = request('/api/overview')
                            break
                        except (OSError, http.client.HTTPException):
                            if time.monotonic() >= ends_at:
                                self.fail('Synthetic preview did not start')
                            time.sleep(.05)
                    self.assertEqual(status, 200)
                    self.assertEqual(overview['gateway']['claude_preference']['saved_preferred_email'],
                                     'account-a@example.com')
                    for choice in ({'mode': 'round-robin'}, {'mode': 'earliest-weekly-reset'},
                                   {'mode': 'highest-remaining-quota'},
                                   {'mode': 'preferred-account', 'preferred_email': 'account-b@example.com'}):
                        status, result = request('/api/claude-routing', choice)
                        self.assertEqual(status, 200, result)
                        status, overview = request('/api/overview')
                        self.assertEqual(overview['gateway']['claude_preference']['saved_mode'], choice['mode'])
                        self.assertIs(overview['gateway']['claude_preference']['drift'], False)
                    account = next(account for account in overview['gateway']['accounts'] if account['id'] == account_id)
                    self.assertEqual(len(account['access_identity']), 64)
                    for allowed in (False, True):
                        status, result = request('/api/fast-mode', {'account_id': account_id, 'allowed': allowed})
                        self.assertEqual(status, 200, result)
                        self.assertEqual(result['allowed'], allowed)
                        status, overview = request('/api/overview')
                        account = next(account for account in overview['gateway']['accounts'] if account['id'] == account_id)
                        self.assertEqual(account['fast_mode']['allowed'], allowed)
                    for enabled in (False, True):
                        status, result = request('/api/accounts/routing', {
                            'account_id': account_id, 'identity': account['access_identity'],
                            'enabled': enabled, 'confirmed': False})
                        if not enabled and status == 409:
                            self.assertEqual(result, {'error': 'last_ready'})
                            status, result = request('/api/accounts/routing', {
                                'account_id': account_id, 'identity': account['access_identity'],
                                'enabled': False, 'confirmed': True})
                        self.assertEqual(status, 200, result)
                        self.assertEqual(result['enabled'], enabled)
                        ends_at = time.monotonic() + 3
                        while True:
                            status, overview = request('/api/overview')
                            account = next(account for account in overview['gateway']['accounts'] if account['id'] == account_id)
                            if account['disabled'] is not enabled:
                                break
                            if time.monotonic() >= ends_at:
                                self.fail('Routing overview did not reflect synthetic mutation')
                            time.sleep(.02)
                    for route in ('/api/claude/resets/policy', '/api/claude/resets/consume', '/api/unmocked'):
                        self.assertEqual(request(route, {})[0], 404)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                # A terminated preview removes its synthetic auth dir itself.
                self.assertEqual(list(Path(directory).glob('signin-auth-*')), [])


if __name__ == '__main__':
    unittest.main()
