from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fixture_safety import handler, isolate


class FixtureSafetyTests(unittest.TestCase):
    def test_watch_snapshots_are_isolated_before_gateway_polling(self):
        dashboard = str(Path(__file__).resolve().parents[1] / 'dashboard')
        sys.path.insert(0, dashboard)
        self.addCleanup(sys.path.remove, dashboard)
        import server
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(routing_policy=SimpleNamespace(),
                                  signin=SimpleNamespace(credential_replace=SimpleNamespace()))
            isolate(app, Path(directory))
            self.assertEqual(app.CLAUDE_USAGE, Path(directory) / 'claude.json')
            with patch.object(server, 'CLAUDE_USAGE', app.CLAUDE_USAGE):
                self.assertEqual(server.watch_accounts(), [])

    def test_all_unmocked_mutations_are_rejected_before_production_handlers(self):
        base = type('Base', (), {'do_POST': Mock(side_effect=AssertionError('Production handler invoked')),
                                'send': Mock()})
        fixture = handler(base, {'/mocked'})()
        for route in ('/api/claude/resets/policy', '/api/claude/resets/consume', '/api/resets/consume',
                      '/api/routing', '/api/fast-mode', '/api/anything-new'):
            fixture.path = route
            for method in ('do_POST', 'do_PATCH', 'do_PUT', 'do_DELETE'):
                getattr(fixture, method)()
                self.assertEqual(base.send.call_args.args[0], 404)
        base.do_POST.assert_not_called()

    def test_production_transports_raise_in_each_dashboard_fixture(self):
        script = '''
import io, json, runpy, sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]).parent / 'dashboard')]
import server as app
from fixture_safety import forbidden_transport
class Server:
    def __init__(self, address, handler):
        self.handler = handler
    def serve_forever(self):
        for transport in (app.urlopen, app.claude_request, app.routing_policy.urlopen):
            assert transport is forbidden_transport
            try:
                transport('synthetic-request')
            except AssertionError as error:
                assert 'Production transport forbidden' in str(error)
            else:
                raise AssertionError('Transport did not fail closed')
        handler = self.handler.__new__(self.handler)
        handler.allowed = lambda *args: True
        sent = []
        handler.send = lambda status, *args: sent.append(status)
        for route in ('/api/claude/resets/policy', '/api/claude/resets/consume',
                      '/api/resets/consume', '/api/routing', '/api/anything-new'):
            handler.path = route
            handler.do_POST()
            assert sent[-1] == 404
        if Path(filename).name == 'signin_preview.py':
            body = json.dumps({'scenario':'free_signed_out'}).encode()
            handler.server = self
            handler.path = '/fixture'
            handler.rfile = io.BytesIO(body)
            handler.headers = {'Content-Length':str(len(body))}
            handler.do_POST()
            assert sent[-1] == 200
            account = self.state.snapshot()['accounts'][0]
            assert account['disabled'] is False
            assert account['plan_type'] == 'free'
            assert account['auth_state'] == 'sign_in_required'
            assert account['windows'] == []
            assert account['billing']['confirmed'] is True
            self.gateway.runtime['access_token'] = self.gateway.tokens['access_token']
            # Credential renewal invalidates scheduled quota polling in production.
            self.state.usage_cache.due_now(self.gateway.row)
            self.state._fetch_locked()
            account = self.state.snapshot()['accounts'][0]
            assert account['auth_state'] == 'authenticated'
            assert account['windows']
    def server_close(self):
        pass
filename, directory = sys.argv[2:]
with patch.object(app, 'ThreadingHTTPServer', Server):
    sys.argv = [filename, '--port', '18429', '--state-dir', directory]
    runpy.run_path(filename, run_name='__main__')
'''
        for filename in ('signin_preview.py', 'fast_mode_fixture.py'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).parent),
                                         str(Path(__file__).with_name(filename)), directory],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
