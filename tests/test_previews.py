import http.client
import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import signal
from urllib.parse import parse_qs, urlsplit
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
        if self.path == '/stream':
            self.send_response(200);self.end_headers()
            self.wfile.write(b'first\n');self.wfile.flush()
            self.server.stream_release.wait(5)
            self.wfile.write(b'last\n')
            return
        if self.path.startswith('/redirect?'):
            self.send_response(302)
            self.send_header('Location', parse_qs(urlsplit(self.path).query)['to'][0])
            self.send_header('Set-Cookie', 'session=fixture; Domain=localhost; Path=/; HttpOnly; Secure; SameSite=Lax')
            self.send_header('Set-Cookie', 'other=fixture; Domain=example.test; Path=/')
            self.end_headers()
            return
        body = json.dumps({'path': self.path, 'host': self.headers['Host'], 'origin': self.headers.get('Origin'),
                           'read_only': self.headers.get('X-Router-Read-Only'),
                           'transport': self.headers.get('X-Router-Transport'),
                           'forwarded': self.headers.get('X-Forwarded-For'),
                           'proto': self.headers.get('X-Forwarded-Proto')}).encode()
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

    def test_cli_links_use_tls_hostname_with_legacy_names_preserved(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'previews.json'
            config.write_text(json.dumps({'bind':'100.64.0.10','dns':'old.tail-example.ts.net',
                                          'tls_hostname':'router.tail-example.ts.net','address':'router',
                                          'base_port':8400,'previews':[]}))
            for args,expected in ((['add','demo','3000'],'https://router.tail-example.ts.net:8401/'),
                                  (['list'],'https://router.tail-example.ts.net:8400/'),
                                  (['add','demo','3000','--permanent'],'https://router.tail-example.ts.net:8401/'),
                                  (['add','demo','3000'],'https://router.tail-example.ts.net:8401/'),
                                  (['list'],'(permanent)')):
                output=io.StringIO()
                with patch.object(preview,'CONFIG',config),patch('sys.argv',['router-preview']+args),redirect_stdout(output):
                    preview.main()
                self.assertIn(expected,output.getvalue())
                self.assertNotIn('http://',output.getvalue())
            self.assertEqual(json.loads(config.read_text())['address'],'router')
            self.assertIs(json.loads(config.read_text())['previews'][0]['permanent'],True)
            with patch('sys.argv',['router-preview','add','demo','3000','--permanent','--hours','4']), \
                    patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit) as error:
                preview.main()
            self.assertEqual(error.exception.code,2)

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

    def test_permanent_registration_preserves_shape_slot_and_duration(self):
        c = {'base_port': 8400, 'previews': []}
        timed = preview.add(c, 'demo', 3000, host='demo.localhost')
        self.assertEqual(set(timed), {'name','target','host','listen','expires_at'})
        permanent = preview.add(c, 'demo', 3000, host='demo.localhost', permanent=True)
        self.assertEqual(permanent, {**timed, 'permanent':True, 'expires_at':None})
        self.assertEqual(preview.add(c, 'demo', 3000, host='demo.localhost'), permanent)
        self.assertEqual(preview.add(c, 'demo', 3000, host='demo.localhost', permanent=True), permanent)
        for hours in (0,4,25):
            with self.assertRaisesRegex(ValueError, 'Remove'):
                preview.add(c, 'demo', 3000, hours=hours, host='demo.localhost')
        for changes in ({'port':3001,'host':'demo.localhost'}, {'port':3000,'host':'other.localhost'}):
            with self.assertRaisesRegex(ValueError, 'Remove the existing preview before changing its target'):
                preview.add(c, 'demo', **changes, permanent=True)
        with self.assertRaisesRegex(ValueError, '--permanent or --hours'):
            preview.add(c, 'demo', 3000, hours=4, permanent=True)
        self.assertEqual(c['previews'], [permanent])
        for i in range(2,9):
            self.assertEqual(preview.add(c, 'project-'+str(i), 3000+i, permanent=True)['listen'],8400+i)
        with self.assertRaisesRegex(ValueError, 'eight'):
            preview.add(c, 'ninth', 3009, permanent=True)

    def test_proxy_http_websocket_access_controls_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'previews.json'
            cert, key = Path(directory) / 'preview.crt', Path(directory) / 'preview.key'
            def generate_cert():
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                                '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                               check=True, capture_output=True, timeout=10)
            generate_cert()
            context = ssl._create_unverified_context()
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            upstream.stream_release = threading.Event()
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(upstream.server_close)
            self.addCleanup(upstream.shutdown)
            self.addCleanup(upstream.stream_release.set)
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
                                            'CREDENTIALS_DIRECTORY': directory,
                                            'ROUTER_PREVIEW_DASHBOARD_PORT': str(upstream.server_port)},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for _ in range(50):
                    try:
                        with socket.create_connection(('127.0.0.1',base),timeout=.1):break
                    except OSError:time.sleep(.05)
                def request(port, method='GET', headers=None, path='/nested?query=one', secure=True, source=None, full=False):
                    con = (http.client.HTTPSConnection('127.0.0.1',port,timeout=2,context=context,source_address=source)
                           if secure else http.client.HTTPConnection('127.0.0.1',port,timeout=2,source_address=source))
                    con.request(method, path, headers=headers or {})
                    res = con.getresponse(); result=(res.status,res.read(),res.getheaders()) if full else (res.status,res.read());con.close();return result
                self.assertEqual(request(base)[0], 200)
                code, body = request(base+1, headers={'Origin':f'https://127.0.0.1:{base+1}', 'X-Forwarded-For':'100.64.0.12', 'X-Forwarded-Proto':'forged'})
                self.assertEqual(code, 200)
                data=json.loads(body)
                self.assertEqual(data['path'],'/nested?query=one')
                self.assertEqual(data['host'],f'localhost:{upstream.server_port}')
                self.assertEqual(data['origin'],f'http://localhost:{upstream.server_port}')
                self.assertIsNone(data['forwarded'])
                self.assertEqual(data['proto'],'https')
                self.assertEqual(request(base+1,headers={'Host':'evil.test'})[0],403)
                self.assertEqual(request(base+1,'POST',{'Origin':'https://evil.test'})[0],403)
                # Named access must be explicitly configured and keep the same origin/port boundary.
                friendly=f'router:{base+9}'
                named={'Host':friendly,'Origin':f'https://{friendly}'}
                self.assertEqual(request(base+9,headers=named,path='/api/overview')[0],403)
                c['aliases']=['router','router.tail-example.ts.net']
                c['address']='router'
                path.write_text(json.dumps(c))
                code,body=request(base+9,headers=named,path='/api/overview')
                self.assertEqual(code,200)
                self.assertIsNone(json.loads(body)['read_only'])
                self.assertEqual(json.loads(body)['transport'],'tailscale')
                self.assertEqual(request(base+9,headers={'Host':f'router.tail-example.ts.net:{base+9}'},path='/')[0],200)
                self.assertEqual(request(base+9,headers={**named,'Origin':f'https://127.0.0.1:{base+9}'},path='/')[0],403)
                self.assertEqual(request(base+9,headers={'Host':f'router:{base+1}'},path='/')[0],403)
                self.assertEqual(request(base+9,headers={'Host':f'router.evil.test:{base+9}'},path='/')[0],403)
                self.assertIn(f'https://localhost:{base+9}/'.encode(),request(base)[1])
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
                for endpoint in ('/', '/app.js', '/boards.js', '/boards.css', '/charts.js', '/widgets.js', '/charts.css',
                                 '/api/series?metric=requests&bucket=hour', '/api/breakdown?by=model',
                                 '/api/heatmap?mode=calendar', '/api/retry?since=2026-01-01T00%3A00%3A00Z',
                                 '/api/overview?since=2026-01-01T00%3A00%3A00Z', '/manrope-0.ttf', '/ibm-plex-serif-0.ttf'):
                    code, body=request(base+9, headers={'X-Router-Read-Only':'1','X-Router-Transport':'forged'}, path=endpoint)
                    self.assertEqual(code,200)
                    self.assertIsNone(json.loads(body)['read_only'])
                    self.assertEqual(json.loads(body)['transport'],'tailscale')
                    self.assertEqual(json.loads(body)['host'],'127.0.0.1:8320')
                for endpoint in ('/api/claude-warnings/dismiss','/api/refresh','/api/routing','/api/claude-routing','/api/routing-link','/api/routing-all','/api/claude/accounts/routing','/api/resets/policy','/api/resets/consume','/api/claude/resets/policy','/api/claude/resets/consume','/api/fast-mode','/api/accounts/signin/start','/api/accounts/signin/cancel','/api/accounts/routing'):
                    self.assertEqual(request(base+9,'POST',path=endpoint)[0],200)
                    self.assertEqual(request(base+9,'POST',{'Origin':'https://evil.test'},path=endpoint)[0],403)
                    self.assertEqual(request(base+9,'POST',{'Sec-Fetch-Site':'cross-site'},path=endpoint)[0],403)
                self.assertEqual(request(base+9,'POST',path='/ibm-plex-serif-0.ttf')[0],403)
                for endpoint in ('/api/notifications/dismiss', '/v0/management/config','/management.html','/../server.py','/%61pi/resets/consume','/ibm-plex-serif-1.ttf','/api/accounts/signin','/api/accounts/signin/start/extra',
                                 '/api/accounts/signin/start/','/api/accounts/signin/status','/api/accounts/routing/extra',
                                 '/%61pi/accounts/signin/start'):
                    for method in ('GET','POST'):
                        self.assertEqual(request(base+9,method,path=endpoint)[0],403)
                # Every port redirects HTTP without contacting an upstream; path/query are exact.
                for port in range(base,base+10):
                    code, body, headers=request(port,secure=False,full=True)
                    self.assertEqual(code,308)
                    self.assertEqual(dict(headers)['location'],f'https://localhost:{port}/nested?query=one')
                for secure in (True,False):
                    self.assertEqual(request(base+1,headers={'Host':'evil.test'},secure=secure)[0],403)
                    self.assertEqual(request(base+1,headers={'X-Forwarded-For':'127.0.0.1'},
                                             source=('127.0.0.2',0),secure=secure)[0],403)
                    self.assertEqual(request(base+1,headers={'Origin':'https://evil.test'},secure=secure)[0],403)
                    self.assertEqual(request(base+1,secure=secure,path='//evil.test/path')[0],400)
                from urllib.parse import quote
                for local in (f'http://localhost:{upstream.server_port}',f'http://127.0.0.1:{upstream.server_port}',
                              f'https://localhost:{upstream.server_port}',f'https://127.0.0.1:{upstream.server_port}'):
                    result=request(base+1,path='/redirect?to='+quote(local+'/login?next=one#part'),full=True)
                    self.assertEqual(dict(result[2])['location'],f'https://localhost:{base+1}/login?next=one#part')
                    cookies=[v for k,v in result[2] if k.lower()=='set-cookie']
                    self.assertEqual(cookies[0],'session=fixture; Path=/; HttpOnly; Secure; SameSite=Lax')
                    self.assertIn('Domain=example.test',cookies[1])
                for location in ('/relative?next=one','https://external.test/login',f'http://localhost:{upstream.server_port+1}/other'):
                    result=request(base+1,path='/redirect?to='+quote(location),full=True)
                    self.assertEqual(dict(result[2])['location'],location)
                # Incomplete headers hit the parser deadline instead of holding the slot open.
                with socket.create_connection(('127.0.0.1',base+1),timeout=25) as slow:
                    slow.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n')
                    started=time.monotonic()
                    while slow.recv(4096):
                        pass
                    self.assertLess(time.monotonic()-started,20)
                c['previews'][0]['host']='demo.localhost'
                path.write_text(json.dumps(c))
                for origin in ('http://demo.localhost',f'http://demo.localhost:{upstream.server_port}'):
                    result=request(base+1,path='/redirect?to='+quote(origin+'/named'),full=True)
                    self.assertEqual(dict(result[2])['location'],f'https://localhost:{base+1}/named')
                c['previews'][0]['host']=f'localhost:{upstream.server_port}'
                path.write_text(json.dumps(c))
                # Reload changes the peer cert without restarting; an invalid pair retains the old cert.
                def peer_cert():
                    with context.wrap_socket(socket.create_connection(('127.0.0.1',base+1)),server_hostname='localhost') as client:
                        return client.getpeercert(binary_form=True)
                original=peer_cert();generate_cert();process.send_signal(signal.SIGHUP)
                deadline=time.monotonic()+3
                while peer_cert()==original and time.monotonic()<deadline:time.sleep(.02)
                renewed=peer_cert();self.assertNotEqual(original,renewed)
                cert.write_text('invalid');process.send_signal(signal.SIGHUP)
                self.assertEqual(request(base+1)[0],200)
                self.assertEqual(peer_cert(),renewed)
                preview.add(c,'standing',upstream.server_port,permanent=True)
                for offset,entry in enumerate(({'expires_at':None},{},{'expires_at':None,'permanent':'true'}),4):
                    c['previews'].append({'name':f'inactive-{offset}','listen':base+offset,
                                          'target':upstream.server_port,'host':f'localhost:{upstream.server_port}',**entry})
                path.write_text(json.dumps(c))
                self.assertEqual(request(base+2)[0],200)
                self.assertIn(b'<small>Permanent</small>',request(base)[1])
                self.assertNotIn(b'inactive-',request(base)[1])
                for offset in (4,5,6):self.assertEqual(request(base+offset)[0],404)
                for headers,source in (({'Host':'evil.test'},None),({'Origin':'https://evil.test'},None),
                                       ({'Sec-Fetch-Site':'cross-site'},None),({},('127.0.0.2',0))):
                    self.assertEqual(request(base+2,headers=headers,source=source)[0],403)
                stream=http.client.HTTPSConnection('127.0.0.1',base+2,timeout=3,context=context)
                self.addCleanup(stream.close)
                stream.request('GET','/stream');reply=stream.getresponse()
                self.assertEqual(reply.status,200)
                self.assertEqual(reply.read(6),b'first\n')
                # Minimal WebSocket handshake plus opaque frame round-trip.
                listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(3)
                def websocket():
                    conn,_=listener.accept();conn.settimeout(3)
                    with conn:
                        header=b''
                        while b'\r\n\r\n' not in header:header+=conn.recv(4096)
                        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                        for _ in range(2):
                            frame=conn.recv(32);conn.sendall(frame)
                        try:conn.recv(32)
                        except TimeoutError:pass
                ws_thread=threading.Thread(target=websocket,daemon=True);ws_thread.start()
                preview.add(c,'standing-tunnel',listener.getsockname()[1],permanent=True)
                c['previews'][0]['expires_at']=time.time()+.25
                path.write_text(json.dumps(c))
                with context.wrap_socket(socket.create_connection(('127.0.0.1',base+3),timeout=3),server_hostname='localhost') as ws:
                    ws.sendall(f'GET /socket HTTP/1.1\r\nHost: 127.0.0.1:{base+3}\r\nOrigin: https://127.0.0.1:{base+3}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'.encode())
                    self.assertIn(b'101 Switching',ws.recv(4096))
                    frame=b'\x81\x02ok';ws.sendall(frame);self.assertEqual(ws.recv(32),frame)
                    deadline=time.monotonic()+2
                    while request(base+1)[0]==200 and time.monotonic()<deadline:time.sleep(.02)
                    self.assertEqual(request(base+1)[0],404)
                    self.assertEqual(request(base+2)[0],200)
                    upstream.stream_release.set();self.assertEqual(reply.read(),b'last\n');stream.close()
                    ws.sendall(frame);self.assertEqual(ws.recv(32),frame)
                    c['previews']=[p for p in c['previews'] if p['name']!='standing-tunnel']
                    started=time.monotonic();path.write_text(json.dumps(c))
                    self.assertEqual(ws.recv(32),b'')  # Removing a preview closes an existing tunnel.
                    self.assertLess(time.monotonic()-started,1.1)
                listener.close();ws_thread.join(3)
                self.assertFalse(ws_thread.is_alive())
                self.assertEqual(request(base+3)[0],404)
                self.assertEqual(request(base+2)[0],200)
                c['previews']=[]
                preview.add(c,'expired',upstream.server_port)
                c['previews'][0]['expires_at']=time.time()-1
                path.write_text(json.dumps(c))
                self.assertEqual(request(base+1)[0],404)
            finally:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                process.stderr.close()
