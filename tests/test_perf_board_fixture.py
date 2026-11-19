"""Harness self-tests: synthetic server isolation, deterministic API and writes."""
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/perf/fake-dashboard.py'
spec = importlib.util.spec_from_file_location('perf_fixture', SCRIPT)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class BoardFixtureTests(unittest.TestCase):
    def setUp(self):
        self.server = fixture.make_server(ROOT, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, headers=None):
        con = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            con.request('GET' if body is None else 'POST', path,
                        body=None if body is None else json.dumps(body),
                        headers=headers if headers is not None else {'X-Router-Request': '1'})
            response = con.getresponse()
            return response.status, response.read()
        finally:
            con.close()

    def test_explicit_opt_in_and_port_allowlist(self):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--checkout', str(ROOT),
                                 '--port', '8398'], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--fake-data', result.stderr)
        for port in (8317, 8320, 8400, 8409, 80):
            with self.assertRaises(ValueError):
                fixture.make_server(ROOT, port)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_fixture_has_required_board_and_deterministic_data(self):
        saved = fixture.layout()
        types = [w['type'] for w in saved['boards'][0]['widgets']]
        self.assertEqual(len(types), 15)
        self.assertEqual(types.count('chart'), 4)
        self.assertEqual(types.count('account-card'), 4)
        for required in ('heatmap', 'timeline', 'pulse', 'health'):
            self.assertIn(required, types)
        for endpoint in ('overview', 'series', 'breakdown', 'heatmap', 'retry', 'requests'):
            a = fixture.data('/api/'+endpoint, {}, saved, 0)
            self.assertEqual(a, fixture.data('/api/'+endpoint, {}, saved, 0))
            self.assertIsNotNone(a)
        before = fixture.overview(saved, 0)
        after = fixture.overview(saved, 1)
        self.assertNotEqual(before['usage']['accounts'][0]['requests'], after['usage']['accounts'][0]['requests'])

    def test_host_origin_and_api_header_checks(self):
        self.assertEqual(self.request('/', headers={'Host': 'example.test'})[0], 421)
        self.assertEqual(self.request('/api/overview', headers={})[0], 403)
        self.assertEqual(self.request('/api/overview', headers={
            'X-Router-Request': '1', 'Origin': 'http://example.test'})[0], 403)
        self.assertEqual(self.request('/api/overview', headers={
            'X-Router-Request': '1', 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('/api/overview')[0], 200)

    def test_static_containment_and_forbidden_mutations(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/../../AGENTS.md')[0], 404)
        self.assertEqual(self.request('/api/accounts/signin/start', {})[0], 403)
        self.assertEqual(self.request('/api/unknown')[0], 404)

    def test_layout_persistence_conflict_and_reset_are_memory_only(self):
        saved = copy.deepcopy(self.server.layout)
        widgets = saved['boards'][0]['widgets']
        widgets[0], widgets[1] = widgets[1], widgets[0]
        body = dict(version=2, base_revision=0, boards=saved['boards'], accounts=saved['accounts'])
        status, raw = self.request('/api/layout', body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['layout']['revision'], 1)
        self.assertEqual(self.request('/api/layout', body)[0], 409)
        self.assertEqual(self.request('/__perf/reset', {})[0], 200)
        self.assertEqual(self.server.layout, fixture.layout())
        self.assertEqual(self.server.tick, 0)

    def test_resize_non_chart_arrangement_is_resettable(self):
        self.assertEqual(self.request('/__perf/reset', {'arrangement': 'resize-non-chart'})[0], 200)
        widgets = self.server.layout['boards'][0]['widgets']
        self.assertEqual([w['type'] for w in widgets[:3]], ['health', 'chart', 'chart'])
        self.assertEqual(widgets[0]['id'], 'perf-07')
        self.assertEqual(len({w['id'] for w in widgets}), 15)
        self.assertEqual(self.request('/__perf/reset', {})[0], 200)
        self.assertEqual(self.server.layout, fixture.layout())

    def test_no_production_import_file_access_or_outgoing_transport(self):
        # Fresh process audit starts BEFORE importing the fixture. Bind/listen is
        # allowed, but any outgoing connection or production file access fails.
        code = r'''
import sys, importlib.util
from pathlib import Path
def audit(event, args):
    if event == 'socket.connect':
        raise AssertionError('Outgoing transport forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        name = str(args[0])
        if any(p in name for p in ('/etc/cliproxyapi', '/etc/personal-codex-router',
                                  '/var/lib/', '/auth/', 'usage.sqlite', 'management-key')):
            raise AssertionError('Production or auth file access forbidden')
sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location('isolated_perf', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert 'server' not in sys.modules
server = module.make_server(Path(sys.argv[2]), 0)
for tick in (0, 1):
    for endpoint in ('overview','series','breakdown','heatmap','retry','requests'):
        assert module.data('/api/'+endpoint, {}, server.layout, tick) is not None
server.server_close()
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(SCRIPT), str(ROOT)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
