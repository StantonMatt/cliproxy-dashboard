"""Status-first polling and fail-closed JSON boundaries, using synthetic transports."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import account_access as access
import routing_policy
import server as app
import signin
from signin_fixture import FakeGateway
from test_signin import ClockEvent


BAD_BODIES = ('', ' \n', '<html>synthetic-private-body</html>', '{', 'null', '[]', 'true', '1', None, [])


class ResponseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        isolation = patch.object(routing_policy, 'STATE', Path(directory.name))
        isolation.start()
        self.addCleanup(isolation.stop)

    def manager(self, target, response):
        gateway, clock = FakeGateway(), [1800000000.0]
        self.addCleanup(gateway.close)
        remaining = [response]

        def call(path, body=None, method=None, timeout=None):
            if path == '/api-call' and body['url'] == target and remaining:
                return remaining.pop()
            return gateway.call(path, body, method)

        manager = signin.SigninManager(call, threading.RLock(), Mock(), clock=lambda: clock[0])
        self.addCleanup(manager.close)
        with patch.object(signin.threading, 'Thread'):
            view = manager.start('account-a', gateway.identity)
        event = None
        if view['session']:
            session = manager.sessions['account-a']
            session['stop'] = event = ClockEvent(clock)
            manager.run(session)
        return gateway, manager.view('account-a', gateway.identity), event

    def test_pending_statuses_ignore_every_body_like_upstream(self):
        bodies = BAD_BODIES + ({}, {'error': 'access_denied'}, {'error': 'slow_down'},
                              {'error': {'code': 'expired_token'}})
        for status in (403, 404):
            for body in bodies:
                with self.subTest(status=status, body=body):
                    gateway, view, event = self.manager(signin.DEVICE_TOKEN,
                        {'status_code': status, 'body': body})
                    self.assertEqual(view['last_outcome']['kind'], 'signed_in')
                    self.assertEqual(event.waits, [5, 5])
                    self.assertEqual(len(gateway.field_patches), 1)
            with self.subTest(status=status, body='missing'):
                _, view, event = self.manager(signin.DEVICE_TOKEN, {'status_code': status})
                self.assertEqual(view['last_outcome']['kind'], 'signed_in')
                self.assertEqual(event.waits, [5, 5])

    def test_other_relay_bodies_fail_with_fixed_outcomes_and_no_writes(self):
        for target in (signin.USERCODE, signin.DEVICE_TOKEN, signin.TOKEN):
            for status in (200, 400, 401, 403, 404, 429, 500):
                if target == signin.DEVICE_TOKEN and status in (403, 404):
                    continue
                for body in BAD_BODIES:
                    with self.subTest(target=target, status=status, body=body):
                        gateway, view, _ = self.manager(target, {'status_code': status, 'body': body})
                        expected = 'start_failed' if target == signin.USERCODE else 'provider_error'
                        self.assertEqual(view['last_outcome']['kind'], expected)
                        self.assertNotIn('synthetic-private-body', json.dumps(view))
                        self.assertEqual(gateway.field_patches, [])
                        self.assertEqual(gateway.patches, [])

    def test_usage_status_precedes_body_and_empty_success_is_not_authentication(self):
        gateway = FakeGateway()
        self.addCleanup(gateway.close)
        gateway.row['status_message'] = ''
        for status in (200, 401, 403, 404, 429, 500):
            for body in BAD_BODIES:
                with self.subTest(status=status, body=body), patch.object(app, 'api',
                        return_value={'status_code': status, 'body': body}):
                    resets = Mock()
                    account = app.fetch_account(gateway.row, resets)
                    self.assertEqual(account['quota_error'], 'Quota refresh unavailable')
                    self.assertEqual(account['auth_state'], 'sign_in_required' if status == 401 else 'unknown')
                    self.assertEqual(account['windows'], [])
                    self.assertNotIn('synthetic-private-body', json.dumps(account))
                    resets.fetch.assert_called_once_with(gateway.row)

    def test_management_list_and_download_json_and_http_errors_fail_closed(self):
        for path in ('/auth-files', '/auth-files/download?name=synthetic.json'):
            for body in BAD_BODIES:
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                response = Mock()
                response.read.return_value = raw
                with self.subTest(path=path, body=body), patch.object(app, 'KEY', Mock(
                        read_text=Mock(return_value='synthetic'))), patch.object(app, 'urlopen',
                        return_value=contextlib.nullcontext(response)):
                    with self.assertRaisesRegex(ValueError, '^Invalid JSON response$'):
                        app.api(path)
            for status in (401, 403, 404, 500):
                error = HTTPError('http://synthetic.invalid', status, 'synthetic', {}, None)
                self.addCleanup(error.close)
                with self.subTest(path=path, status=status), patch.object(app, 'KEY', Mock(
                        read_text=Mock(return_value='synthetic'))), patch.object(app, 'urlopen', side_effect=error):
                    with self.assertRaises(HTTPError) as raised:
                        app.api(path)
                    self.assertEqual(raised.exception.code, status)

    def test_json_object_accepts_encoded_and_decoded_objects(self):
        for value in ({}, '{}', b'{}', {'synthetic': 1}, '{"synthetic":1}'):
            with self.subTest(value=value):
                self.assertIsInstance(access.json_object(value), dict)


if __name__ == '__main__':
    unittest.main()
