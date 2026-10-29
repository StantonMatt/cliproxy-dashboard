import http.client
import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preview', ROOT / 'scripts/router-preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        body = json.dumps({'path': self.path, 'host': self.headers['Host'], 'origin': self.headers.get('Origin'),
                           'read_only': self.headers.get('X-Router-Read-Only'),
                           'transport': self.headers.get('X-Router-Transport'),
                           'forwarded': self.headers.get('X-Forwarded-For')}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()


class PreviewTests(unittest.TestCase):
    def test_claude_timeout_chain_and_per_route_proxy_deadlines(self):
        script = r'''
            const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
            const source = fs.readFileSync(process.argv[1], 'utf8');
            const definition = source.match(/function upstreamTimeout\(req, p\) \{[\s\S]*?\n\}/)[0];
            const constants = source.match(/const ACCOUNT_[^;]+;/g).join('\n');
            const timeout = vm.runInNewContext(constants + '\n' + definition + '\nupstreamTimeout');
            assert(source.includes('timeout:upstreamTimeout(req,p)'));
            for (const url of ['/api/claude/resets/consume', '/api/claude/resets/policy']) {
                assert.equal(timeout({url}, {dashboard:true}), 65000);
                assert.equal(timeout({url:url+'?query=1'}, {dashboard:true}), 65000);
                assert.equal(timeout({url}, {dashboard:false}), 30000);
            }
            for (const url of ['/api/accounts/signin/start', '/api/accounts/signin/cancel', '/api/accounts/routing', '/api/routing', '/api/claude-routing', '/api/routing-link', '/api/routing-all', '/api/claude/accounts/routing']) {
                assert.equal(timeout({url}, {dashboard:true}), 50000);
                assert.equal(timeout({url:url+'?query=1'}, {dashboard:true}), 50000);
                assert.equal(timeout({url}, {dashboard:false}), 30000);
            }
            for (const url of ['/api/overview', '/api/resets/consume', '/api/claude/resets/consume/extra'])
                assert.equal(timeout({url}, {dashboard:true}), 30000);
            const app = fs.readFileSync(process.argv[2], 'utf8');
            const browserConstants = app.match(/const ACCOUNT_[^;]+;/g).join('\n');
            const accountBrowser = vm.runInNewContext(browserConstants + '\nACCOUNT_BROWSER_TIMEOUT_MS');
            assert.equal(accountBrowser, 55000);
            assert.equal(vm.runInNewContext(browserConstants + '\nACCOUNT_MUTATION_DEADLINE_MS'),
                vm.runInNewContext(constants + '\nACCOUNT_MUTATION_DEADLINE_MS'));
            assert(source.includes('setTimeout(()=>upstream.destroy(),upstreamTimeout(req,p))'));
            assert(source.includes('clearTimeout(accountDeadline)'));
            const post = app.match(/async function postOutcome\([\s\S]*?\n\}/)[0];
            const browser = Number(post.match(/AbortSignal.timeout\((\d+)\)/)[1]);
            console.log(JSON.stringify([timeout({url:'/api/claude/resets/consume'}, {dashboard:true})/1000, browser/1000]));
        '''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'scripts/router-preview.mjs'),
                                 str(ROOT / 'dashboard/static/app.js')],
                                capture_output=True, text=True, check=True, timeout=5)
        proxy, browser = json.loads(result.stdout)
        def constant(path, name):
            tree = ast.parse((ROOT / path).read_text())
            return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))
        lock = constant('scripts/claude-redeemer.py', 'LOCK_WAIT_TIMEOUT')
        provider = constant('scripts/collect-claude-usage.py', 'PROVIDER_TIMEOUT')
        dashboard = constant('dashboard/server.py', 'CLAUDE_MUTATION_TIMEOUT')
        self.assertLess(lock + 3 * provider, dashboard)
        self.assertLess(dashboard, proxy)
        self.assertLess(proxy, browser)
        source = (ROOT / 'dashboard/server.py').read_text()
        self.assertIn('timeout=timeout', source)
        self.assertIn('deadline.lock(self.lifecycle_lock)', source)
        self.assertNotIn('operation_budget', source)
        self.assertEqual(constant('dashboard/request_deadline.py', 'ACCOUNT_MUTATION_SECONDS'), 45)
        source = (ROOT / 'dashboard/static/app.js').read_text()
        account_request = source.split('async function accountRequest(', 1)[1].split('\n}', 1)[0]
        self.assertIn('AbortSignal.timeout(ACCOUNT_BROWSER_TIMEOUT_MS)', account_request)
        self.assertLess(45, 50)
        self.assertLess(50, 55)

    def test_dashboard_font_allowlist_without_listener(self):
        script = r'''
            const fs = require('node:fs');
            const vm = require('node:vm');
            const source = fs.readFileSync(process.argv[1], 'utf8');
            const definition = source.match(/function dashboardRequestAllowed\(req\) \{[\s\S]*?\n\}/)[0];
            const allowed = vm.runInNewContext(definition + '\ndashboardRequestAllowed');
            const requests = [
                {method:'GET', url:'/ibm-plex-serif-0.ttf'},
                {method:'GET', url:'/ibm-plex-serif-0.ttf?v=1'},
                {method:'POST', url:'/ibm-plex-serif-0.ttf'},
                {method:'GET', url:'/ibm-plex-serif-1.ttf'},
                {method:'GET', url:'/ibm-plex-serif-0.ttf/extra'},
            ];
            console.log(JSON.stringify(requests.map(allowed)));
        '''
        result = subprocess.run(['node', '-e', script, str(ROOT / 'scripts/router-preview.mjs')],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), [True, True, False, False, False])

    def test_registration_restricts_targets_duration_names_and_preserves_slots(self):
        c = {'base_port': 8400, 'previews': []}
        for port in (22, 80, 8317, 8320, 8400, 5432):
            with self.assertRaises(ValueError): preview.add(c, 'demo', port)
        for host in ('evil.test', 'localhost\r\nInjected: true', '100.64.0.10:8317'):
            with self.assertRaises(ValueError): preview.add(c, 'demo', 3000, host=host)
        with self.assertRaises(ValueError): preview.add(c, 'demo', 3000, hours=25)
        with self.assertRaises(ValueError): preview.add(c, 'bad/name', 3000)
        self.assertEqual(preview.add(c, 'demo', 3000)['listen'], 8401)
        self.assertEqual(preview.add(c, 'demo', 3000)['listen'], 8401)
        with self.assertRaises(ValueError): preview.add(c, 'demo', 3001)
        self.assertEqual(preview.add(c, 'second', 3001)['listen'], 8402)
        for i in range(3,9):
            self.assertEqual(preview.add(c, 'project-'+str(i), 3000+i)['listen'],8400+i)
        with self.assertRaisesRegex(ValueError,'eight'):
            preview.add(c,'ninth',3009)

    def test_proxy_http_websocket_access_controls_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'previews.json'
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(upstream.server_close)
            self.addCleanup(upstream.shutdown)
            # Find a contiguous test range without assuming production ports are free.
            for base in range(18500, 19500, 10):
                sockets = []
                try:
                    for port in range(base, base+10):
                        s = socket.socket();s.bind(('127.0.0.1',port));sockets.append(s)
                    break
                except OSError:
                    s.close()
                finally:
                    for s in sockets:s.close()
            c = {'bind': '127.0.0.1', 'hostname': 'localhost', 'dns': 'localhost', 'base_port': base,
                 'devices': [], 'previews': []}
            preview.add(c, 'demo', upstream.server_port)
            path.write_text(json.dumps(c))
            process = subprocess.Popen(['node', str(ROOT / 'scripts/router-preview.mjs')],
                                       env={**os.environ, 'ROUTER_PREVIEW_CONFIG': str(path),
                                            'ROUTER_PREVIEW_DASHBOARD_PORT': str(upstream.server_port)},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for _ in range(50):
                    try:
                        with socket.create_connection(('127.0.0.1',base),timeout=.1):break
                    except OSError:time.sleep(.05)
                def request(port, method='GET', headers=None, path='/nested?query=one'):
                    con = http.client.HTTPConnection('127.0.0.1',port,timeout=2)
                    con.request(method, path, headers=headers or {})
                    res = con.getresponse(); result=(res.status,res.read());con.close();return result
                self.assertEqual(request(base)[0], 200)
                code, body = request(base+1, headers={'Origin':f'http://127.0.0.1:{base+1}', 'X-Forwarded-For':'100.64.0.12'})
                self.assertEqual(code, 200)
                data=json.loads(body)
                self.assertEqual(data['path'],'/nested?query=one')
                self.assertEqual(data['host'],f'localhost:{upstream.server_port}')
                self.assertEqual(data['origin'],f'http://localhost:{upstream.server_port}')
                self.assertIsNone(data['forwarded'])
                self.assertEqual(request(base+1,headers={'Host':'evil.test'})[0],403)
                self.assertEqual(request(base+1,'POST',{'Origin':'https://evil.test'})[0],403)
                # Named access must be explicitly configured and keep the same origin/port boundary.
                friendly=f'router:{base+9}'
                named={'Host':friendly,'Origin':f'http://{friendly}'}
                self.assertEqual(request(base+9,headers=named,path='/api/overview')[0],403)
                c['aliases']=['router','router.tail-example.ts.net']
                c['address']='router'
                path.write_text(json.dumps(c))
                code,body=request(base+9,headers=named,path='/api/overview')
                self.assertEqual(code,200)
                self.assertIsNone(json.loads(body)['read_only'])
                self.assertEqual(json.loads(body)['transport'],'tailscale')
                self.assertEqual(request(base+9,headers={'Host':f'router.tail-example.ts.net:{base+9}'},path='/')[0],200)
                self.assertEqual(request(base+9,headers={**named,'Origin':f'http://127.0.0.1:{base+9}'},path='/')[0],403)
                self.assertEqual(request(base+9,headers={'Host':f'router:{base+1}'},path='/')[0],403)
                self.assertEqual(request(base+9,headers={'Host':f'router.evil.test:{base+9}'},path='/')[0],403)
                self.assertIn(f'http://router:{base+9}/'.encode(),request(base)[1])
                del c['aliases']
                path.write_text(json.dumps(c))
                self.assertEqual(request(base+9,headers=named,path='/')[0],403)
                self.assertEqual(request(base+1,headers={'Sec-Fetch-Site':'cross-site'})[0],403)
                navigation={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate',
                            'Sec-Fetch-Dest':'document','Sec-Fetch-User':'?1'}
                self.assertEqual(request(base+1,headers=navigation)[0],200)
                self.assertEqual(request(base+1,'POST',navigation)[0],403)
                self.assertEqual(request(base+1,headers={**navigation,'Sec-Fetch-Dest':'iframe'})[0],403)
                self.assertEqual(request(base+2)[0],404)
                # Approved devices can use all dashboard controls; raw gateway APIs stay separate.
                for endpoint in ('/', '/app.js', '/api/overview?since=2026-01-01T00%3A00%3A00Z', '/manrope-0.ttf', '/ibm-plex-serif-0.ttf'):
                    code, body=request(base+9, headers={'X-Router-Read-Only':'1','X-Router-Transport':'forged'}, path=endpoint)
                    self.assertEqual(code,200)
                    self.assertIsNone(json.loads(body)['read_only'])
                    self.assertEqual(json.loads(body)['transport'],'tailscale')
                    self.assertEqual(json.loads(body)['host'],'127.0.0.1:8320')
                for endpoint in ('/api/refresh','/api/routing','/api/claude-routing','/api/routing-link','/api/routing-all','/api/claude/accounts/routing','/api/resets/policy','/api/resets/consume','/api/claude/resets/policy','/api/claude/resets/consume','/api/fast-mode','/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/routing'):
                    self.assertEqual(request(base+9,'POST',path=endpoint)[0],200)
                    self.assertEqual(request(base+9,'POST',{'Origin':'https://evil.test'},path=endpoint)[0],403)
                    self.assertEqual(request(base+9,'POST',{'Sec-Fetch-Site':'cross-site'},path=endpoint)[0],403)
                self.assertEqual(request(base+9,'POST',path='/ibm-plex-serif-0.ttf')[0],403)
                for endpoint in ('/api/notifications/dismiss', '/v0/management/config','/management.html','/../server.py','/%61pi/resets/consume','/ibm-plex-serif-1.ttf','/api/accounts/signin','/api/accounts/signin/start/extra',
                                 '/api/accounts/signin/start/','/api/accounts/signin/status','/api/accounts/routing/extra',
                                 '/%61pi/accounts/signin/start'):
                    for method in ('GET','POST'):
                        self.assertEqual(request(base+9,method,path=endpoint)[0],403)
                # A distinct, non-allowlisted loopback source is rejected even with spoofed headers.
                con=http.client.HTTPConnection('127.0.0.1',base+1,source_address=('127.0.0.2',0),timeout=2)
                con.request('GET','/',headers={'X-Forwarded-For':'127.0.0.1'})
                self.assertEqual(con.getresponse().status,403);con.close()
                # Minimal WebSocket handshake plus opaque frame round-trip.
                listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(3)
                def websocket():
                    conn,_=listener.accept();conn.settimeout(3)
                    with conn:
                        header=b''
                        while b'\r\n\r\n' not in header:header+=conn.recv(4096)
                        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                        frame=conn.recv(32);conn.sendall(frame)
                        try:conn.recv(32)
                        except TimeoutError:pass
                ws_thread=threading.Thread(target=websocket,daemon=True);ws_thread.start()
                c['previews'][0]['target']=listener.getsockname()[1]
                path.write_text(json.dumps(c))
                with socket.create_connection(('127.0.0.1',base+1),timeout=3) as ws:
                    ws.sendall(f'GET /socket HTTP/1.1\r\nHost: 127.0.0.1:{base+1}\r\nOrigin: http://127.0.0.1:{base+1}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'.encode())
                    self.assertIn(b'101 Switching',ws.recv(4096))
                    frame=b'\x81\x02ok';ws.sendall(frame);self.assertEqual(ws.recv(32),frame)
                    c['previews']=[];path.write_text(json.dumps(c))
                    self.assertEqual(ws.recv(32),b'')  # Removing a preview closes an existing tunnel.
                listener.close();ws_thread.join(3)
                preview.add(c,'expired',upstream.server_port)
                c['previews'][0]['expires_at']=time.time()-1
                path.write_text(json.dumps(c))
                self.assertEqual(request(base+1)[0],404)
            finally:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                process.stderr.close()
