#!/usr/bin/env python3
"""Exact installed gateway + dummy file-backed Codex accounts + loopback TLS mock.
No outbound upstream network: each account's proxy handles CONNECT locally.
All subprocesses and threads are owned here and stopped in finally.
"""
import argparse, tempfile, importlib.util
from unittest.mock import patch
import base64, hashlib, http.server, json, os, pathlib, shutil, socket, ssl, struct, subprocess, sys, threading, time, urllib.request
from websockets.sync.client import connect
import yaml
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('install_fast_mode', ROOT / 'scripts/install-fast-mode-plugin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RUN = EVIDENCE = None
records = []; lock = threading.Lock(); fail_next = set(); force_account_a_failure=False
MODEL = 'gpt-6-luna'

def save_json(path, obj):
    if path.name == 'policy.json':
        obj = {'version': 1, 'updated_at': '2026-10-02T23:59:00Z', **obj}
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        stream.write(json.dumps(obj, indent=2)+'\n'); stream.flush(); os.fsync(stream.fileno())
    temp.replace(path)

def free_port():
    while True:
        with socket.socket() as s:
            s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
        if port != 8317: return port

def recv_exact(sock, n):
    out=b''
    while len(out)<n:
        b=sock.recv(n-len(out))
        if not b: raise EOFError()
        out+=b
    return out

def recv_frame(sock):
    h=recv_exact(sock,2); op=h[0]&15; n=h[1]&127
    if n==126: n=struct.unpack('!H',recv_exact(sock,2))[0]
    elif n==127: n=struct.unpack('!Q',recv_exact(sock,8))[0]
    mask=recv_exact(sock,4) if h[1]&128 else b''
    raw=recv_exact(sock,n)
    if mask: raw=bytes(x^mask[i%4] for i,x in enumerate(raw))
    return op,raw

def send_frame(sock,raw,op=1):
    n=len(raw); prefix=bytes([128|op])
    prefix+=bytes([n]) if n<126 else bytes([126])+struct.pack('!H',n)
    sock.sendall(prefix+raw)

def completed(i):
    return {'type':'response.completed','response':{'id':f'resp_{i}','object':'response','model':MODEL,'status':'completed','output':[], 'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}

class Mock(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass
    def do_CONNECT(self):
        # The requested external host is never dialed or resolved.
        if self.path!='chatgpt.com:443':
            self.send_error(403); return
        self.send_response(200); self.end_headers(); self.wfile.flush()
        self.connection=server_tls.wrap_socket(self.connection,server_side=True)
        self.rfile=self.connection.makefile('rb'); self.wfile=self.connection.makefile('wb')
        self.connection.settimeout(15)
        self.close_connection=False
        while not self.close_connection: self.handle_one_request()
    def record(self,body,transport):
        # Only known dummy account labels; do not log Authorization.
        credential=self.headers.get('Authorization','')
        account='A' if credential in ['Bearer spike-dummy-A','Bearer spike-dummy-A-refreshed'] else 'B' if credential=='Bearer spike-dummy-B' else 'unknown'
        record={'account':account,'transport':transport,'tier':body.get('service_tier','<absent>'),'body':body}
        with lock:
            records.append(record)
            with (EVIDENCE/'outbound.jsonl').open('a') as f: f.write(json.dumps(record)+'\n')
        return account
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        account=self.record(body,'http')
        with lock:
            failure=account in fail_next or (force_account_a_failure and account=='A')
            if account in fail_next: fail_next.remove(account)
        if failure:
            raw=b'{"error":{"message":"spike forced account failure"}}'
            self.send_response(503); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        raw=('data: '+json.dumps(completed(len(records)))+'\n\n').encode()
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.headers.get('Upgrade','').lower()!='websocket': self.send_error(404); return
        key=self.headers['Sec-WebSocket-Key']
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); self.wfile.flush()
        self.close_connection=True
        try:
            while True:
                op,raw=recv_frame(self.connection)
                if op==8: send_frame(self.connection,raw,8); break
                if op==9: send_frame(self.connection,raw,10); continue
                if op!=1: continue
                body=json.loads(raw); self.record(body,'websocket')
                event=completed(len(records)); response_id=event['response']['id']
                send_frame(self.connection,json.dumps({'type':'response.created','response':{'id':response_id,'status':'in_progress','output':[]}}).encode())
                item={'id':'msg_'+response_id,'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'spike','annotations':[]}]}
                send_frame(self.connection,json.dumps({'type':'response.output_item.done','response_id':response_id,'output_index':0,'item':item}).encode())
                event['response']['output']=[item]
                send_frame(self.connection,json.dumps(event).encode())
        except (EOFError,OSError): pass

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=pathlib.Path, required=True)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    parser.add_argument('--model', default=MODEL, help='Model available in the selected binary catalog')
    parser.add_argument('--response-steering', choices=('true', 'false'), default='true',
                        help='true retains the expected duplex policy failure; false enforces every turn')
    parser.add_argument('--scratch', type=pathlib.Path,
                        default=pathlib.Path('~/.cache/agent-scratch/personal-codex-router').expanduser())
    args=parser.parse_args()
    MODEL=args.model
    assert args.library.is_file(), 'build or supply the plugin first'
    args.scratch.mkdir(parents=True,exist_ok=True)
    RUN=pathlib.Path(tempfile.mkdtemp(prefix='fast-mode-isolated-', dir=args.scratch))
    EVIDENCE=RUN/'evidence'; EVIDENCE.mkdir()
    artifacts=RUN/'plugins/linux/amd64'; artifacts.mkdir(parents=True)
    shutil.copyfile(args.library, artifacts/'fast-mode-policy-v0.1.2.so')
    status=RUN/'status.json'
    cert=RUN/'mock-cert.pem'; key=RUN/'mock-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-keyout',str(key),'-out',str(cert),'-subj','/CN=chatgpt.com','-addext','subjectAltName=DNS:chatgpt.com'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    server_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); server_tls.load_cert_chain(cert,key)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Mock); server.daemon_threads=True
    assert server.server_port != 8317
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    port=free_port(); assert port!=8317
    auth=RUN/'auth'; auth.mkdir(exist_ok=True)
    for name in ['A','B']:
        save_json(auth/f'account-{name}.json',{'type':'codex','email':f'spike-{name.lower()}@example.invalid','plan_type':'plus','access_token':f'spike-dummy-{name}', 'prefix':name, 'websockets':True, 'proxy_url':f'http://127.0.0.1:{server.server_port}'})
    policy=RUN/'policy.json'
    save_json(policy,{'files':{'account-A.json':{'allow_fast':True,'supports_ultrafast':False},'account-B.json':{'allow_fast':False,'supports_ultrafast':True}}})
    cfg={'host':'127.0.0.1','port':port,'auth-dir':str(auth),'api-keys':['spike-client-dummy'],'remote-management':{'secret-key':'spike-management-dummy','disable-control-panel':True,'disable-auto-update-panel':True}, 'routing':{'strategy':'fill-first','session-affinity':False},'request-retry':0,'max-retry-credentials':2,'disable-cooling':True, 'codex':{'response-steering':True},'plugins':{'enabled':False,'dir':str(RUN/'plugins'),'configs':{'fast-mode-policy':{'enabled':True,'priority':100,'policy_path':str(policy),'status_path':str(status),'store':{'version':'0.1.2'},'map_unsupported_ultrafast':False,'audit':True}}}}
    cfg.pop('codex')
    cfg['upstream']={'codex':{'response-steering':args.response_steering=='true'}}
    config_path=RUN/'config.yaml'; config_path.write_text(yaml.safe_dump(cfg))
    gateway_log=(EVIDENCE/'gateway.log').open('w')
    proc=None
    try:
        proc=subprocess.Popen([str(args.binary),'--local-model','-config',str(config_path)],cwd=RUN,stdout=gateway_log,stderr=subprocess.STDOUT,env={'PATH':os.environ['PATH'],'SSL_CERT_FILE':str(cert),'LANG':'C.UTF-8','HTTP_PROXY':f'http://127.0.0.1:{server.server_port}','HTTPS_PROXY':f'http://127.0.0.1:{server.server_port}','NO_PROXY':'127.0.0.1,localhost'})
    except BaseException:
        server.shutdown(); server.server_close(); thread.join(timeout=2); gateway_log.close(); raise
    (EVIDENCE/'outbound.jsonl').write_text('')
    summary={'gateway_pid':proc.pid,'gateway_port':port,'mock_port':server.server_port,'matrix':[]}
    print(f'Started isolated gateway PID {proc.pid}, port {port}; mock port {server.server_port}; scratch {RUN}',flush=True)
    def request(model,tier=None):
        body={'model':model,'input':'spike','stream':False,'instructions':'spike'}
        if tier is not None: body['service_tier']=tier
        req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/responses',data=json.dumps(body).encode(),headers={'Authorization':'Bearer spike-client-dummy','Content-Type':'application/json'})
        try:
            with HTTP.open(req,timeout=15) as response: result=json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from error
        return result
    def websocket_turns():
        start=len(records)
        with connect(f'ws://127.0.0.1:{port}/v1/responses',additional_headers={'Authorization':'Bearer spike-client-dummy'},open_timeout=10,proxy=None) as ws:
            for turn in range(2):
                ws.send(json.dumps({'type':'response.create','model':'B/'+MODEL,'input':[],'service_tier':'priority','instructions':'spike'}))
                while True:
                    message=json.loads(ws.recv(timeout=15))
                    if message.get('type')=='response.completed': break
                    if message.get('type')=='error': raise RuntimeError(message)
        return records[start:]
    def run_case(stage,account,tier,expected):
        start=len(records); request(account+'/'+MODEL,tier); observed=records[start:]
        assert len(observed)==1,observed
        actual=observed[0]['tier']; assert actual==expected,(stage,account,tier,actual,expected)
        summary['matrix'].append({'stage':stage,'account':account,'input':tier if tier is not None else '<absent>','outbound':actual})
    def hot_reload():
        original=config_path.read_bytes(); candidate=yaml.safe_dump(cfg).encode()
        snapshot=([], {}, 'success')
        def journal(*args):
            cursor=next(arg.split('=',1)[1] for arg in args if arg.startswith('--after-cursor='))
            return (EVIDENCE/'gateway.log').read_text()[int(cursor):]
        # Observe this owned subprocess/log and use its unprivileged status owner.
        # Confirmation uses only the owned process's status and new log lines.
        with patch.object(installer, 'main_pid', side_effect=lambda: proc.pid if proc.poll() is None else 0), \
                patch.object(installer, 'journal_cursor', side_effect=lambda: str(len((EVIDENCE/'gateway.log').read_text()))), \
                patch.object(installer, 'command', side_effect=journal), \
                patch.object(installer, 'runtime_snapshot', return_value=snapshot), \
                patch.object(installer, 'status_owner', return_value=(os.getuid(), os.getgid())):
            return installer.verified_reload(config_path,original,candidate,proc.pid,snapshot,timeout=15)

    try:
        deadline=time.monotonic()+25
        while True:
            if proc.poll() is not None: raise RuntimeError('gateway exited; inspect evidence/gateway.log')
            try:
                response=HTTP.open(urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',headers={'Authorization':'Bearer spike-client-dummy'}),timeout=1)
                models=json.load(response)
                if any(m['id']=='A/'+MODEL for m in models.get('data',[])): break
                if time.monotonic()>deadline: raise RuntimeError('expected model absent: '+str([m['id'] for m in models.get('data',[])]))
                time.sleep(.1)
            except OSError:
                if time.monotonic()>deadline: raise
                time.sleep(.1)
        run_case('plugin-disabled-baseline','A','fast','priority')
        # Enabling through config hot reload, same PID, from initially disabled plugin host.
        # Keep the gateway's startup bcrypt normalization rather than rewriting its secret.
        cfg=yaml.safe_load(config_path.read_bytes())
        cfg['plugins']['enabled']=True; hot_reload()
        live=json.loads(status.read_text())
        assert live['plugin']=='fast-mode-policy' and live['version']=='0.1.2' and live['pid']==proc.pid,live
        assert live['policy_path']==str(policy) and not live['map_unsupported_ultrafast'],live
        assert status.stat().st_mode & 0o777 == 0o640
        for account in ['A','B']:
            for tier in ['fast','priority','ultrafast','default',None]:
                expected='<absent>' if account=='B' or tier in ['default',None] else 'priority' if tier=='fast' else tier
                run_case('initial',account,tier,expected)
        summary['hot_enable_same_pid']=proc.poll() is None
        # Hot policy toggle requires no gateway config change/reload.
        save_json(policy,{'files':{'account-A.json':{'allow_fast':False},'account-B.json':{'allow_fast':True,'supports_ultrafast':True}}})
        for account in ['A','B']:
            for tier in ['fast','priority','ultrafast','default',None]:
                expected='<absent>' if account=='A' or tier in ['default',None] else 'priority' if tier=='fast' else tier
                run_case('policy-toggle',account,tier,expected)
        cfg['plugins']['configs']['fast-mode-policy']['map_unsupported_ultrafast']=True
        hot_reload()
        mapped=json.loads(status.read_text())
        assert mapped['pid']==proc.pid and mapped['map_unsupported_ultrafast'] and mapped['loaded_at']!=live['loaded_at'],mapped
        summary['status_init_and_reconfigure']=True
        for allow in [False,True]:
            for supports in [False,True]:
                save_json(policy,{'files':{'account-A.json':{'allow_fast':allow,'supports_ultrafast':supports}}})
                for value in ['fast','priority','ultrafast','default',None]:
                    expected=('<absent>' if value in ['default',None] or (not allow and value in ['fast','priority','ultrafast'])
                              else 'priority' if value=='fast' or (value=='ultrafast' and not supports) else value)
                    run_case(f'contract-allow={allow}-supports={supports}','A',value,expected)
        policy.write_text('invalid JSON'); run_case('invalid-policy','A','priority','priority')
        policy.unlink(); run_case('missing-policy','B','priority','priority')
        expected_indexes={name:hashlib.sha256(('codex:'+str(auth/f'account-{name}.json')).encode()).hexdigest()[:16] for name in ['A','B']}
        indexed_policy={'accounts':{expected_indexes['A']:{'allow_fast':False},expected_indexes['B']:{'allow_fast':True}},'files':{'account-A.json':{'allow_fast':True},'account-B.json':{'allow_fast':False}}}
        save_json(policy,indexed_policy)
        run_case('index-over-filename','A','priority','<absent>'); run_case('index-over-filename','B','priority','priority')
        # File-backed identity survives a refresh-style token replacement.
        auth_a=auth/'account-A.json'; data=json.loads(auth_a.read_text()); data['access_token']='spike-dummy-A-refreshed'
        save_json(auth_a,data); time.sleep(1.2)
        run_case('token-file-update','A','priority','<absent>')
        summary['auth_indexes']=expected_indexes
        # Exercise genuine account failover with unprefixed model selection.
        save_json(policy,{'files':{'account-A.json':{'allow_fast':False},'account-B.json':{'allow_fast':True}}})
        force_account_a_failure=True
        failover=[]
        for _ in range(6):
            start=len(records); request(MODEL,'priority'); observed=records[start:]
            if len(observed)>1:
                failover=observed; break
        force_account_a_failure=False
        assert [(r['account'],r['tier']) for r in failover]==[('A','<absent>'),('B','priority')],failover
        summary['failover']=[{'account':r['account'],'tier':r['tier']} for r in failover]
        # Load a separate file copy under a new path without restarting the gateway.
        # This proves replacement mechanics, not a changed plugin implementation.
        update_dir=RUN/'plugin-update'; update_dir.mkdir(exist_ok=True)
        shutil.copyfile(args.library,update_dir/'fast-mode-policy-v0.1.2.so')
        cfg['plugins']['dir']=str(update_dir); hot_reload()
        run_case('library-path-update','A','priority','<absent>')
        assert 'pluginhost: plugin hot reloaded' in (EVIDENCE/'gateway.log').read_text()
        assert json.loads(status.read_text())['pid']==proc.pid
        summary['library_path_update_same_pid']=proc.poll() is None
        # Establish a real downstream and upstream websocket, then send a follow-up
        # with fast disabled. Duplex mode bypasses the after-auth hook on that follow-up.
        cfg['plugins']['configs']['fast-mode-policy']['map_unsupported_ultrafast']=False
        hot_reload()
        save_json(policy,{'files':{'account-B.json':{'allow_fast':False}}})
        observed=websocket_turns(); summary['websocket']=observed
        if args.response_steering=='true':
            # EXPECTED POLICY FAILURE, pinned on 8.0.30 and 8.0.43. Fail if
            # behavior changes so an upstream fix gets a deliberate reassessment.
            assert [r['tier'] for r in observed]==['<absent>','priority'],observed
            summary['duplex_expected_policy_failure']=True
        else:
            assert [r['tier'] for r in observed]==['<absent>','<absent>'],observed
            summary['serial_policy_enforced']=True
        gateway_log.flush(); logs=(EVIDENCE/'gateway.log').read_text()
        summary['websocket_warning_count']=logs.count('follow-up websocket turns bypass the fast-mode policy')
        assert summary['websocket_warning_count']==1,summary
        summary['same_pid_after_all_toggles']=proc.poll() is None
        # Disabling is deliberately unconfirmed by the installer; fake-instance
        # requests below independently exercise the eventual runtime behavior.
        save_json(policy,indexed_policy)
        for scope in ['individual', 'global']:
            if scope=='individual': cfg['plugins']['configs']['fast-mode-policy']['enabled']=False
            else: cfg['plugins']['enabled']=False
            assert hot_reload() == installer.DEACTIVATION_UNCONFIRMED
            assert json.loads(status.read_text())['disabled'], 'disable must mark enforcement not live'
            time.sleep(.3)  # Host application follows the early config-reload log.
            run_case('installer-disable-'+scope,'A','priority','priority')
            cfg['plugins']['enabled']=True
            cfg['plugins']['configs']['fast-mode-policy']['enabled']=True
            hot_reload()
            assert status.exists() and not json.loads(status.read_text()).get('disabled'), 're-enable must publish a live marker'
            run_case('installer-reenable-'+scope,'A','priority','<absent>')
        summary['installer_disable_unconfirmed']=True
        save_json(policy,indexed_policy)
        proc.terminate(); proc.wait(timeout=10)
        assert not status.exists(), 'shutdown must remove owned status'
        summary['status_shutdown_removed']=True
        proc=subprocess.Popen([str(args.binary),'--local-model','-config',str(config_path)],cwd=RUN,stdout=gateway_log,stderr=subprocess.STDOUT,env={'PATH':os.environ['PATH'],'SSL_CERT_FILE':str(cert),'LANG':'C.UTF-8','HTTP_PROXY':f'http://127.0.0.1:{server.server_port}','HTTPS_PROXY':f'http://127.0.0.1:{server.server_port}','NO_PROXY':'127.0.0.1,localhost'})
        print(f'Restarted isolated gateway PID {proc.pid}, port {port}',flush=True)
        time.sleep(1.2)
        run_case('gateway-restart','A','priority','<absent>'); run_case('gateway-restart','B','priority','priority')
        assert json.loads(status.read_text())['pid']==proc.pid
        summary['restart_indexes_unchanged']=True
        summary['status_restart_current_pid']=True
        save_json(EVIDENCE/'results.json',summary)
        print(f'PASS: {len(summary["matrix"])} HTTP cases; failover, live policy/config reload, status lifecycle and websocket warning. Evidence: {EVIDENCE}')
    finally:
        if proc.poll() is None: proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        gateway_log.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
        save_json(EVIDENCE/'process-cleanup.json',{'gateway_pid':proc.pid,'gateway_exit':proc.returncode,'mock_server_stopped':not thread.is_alive()})
