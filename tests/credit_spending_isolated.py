#!/usr/bin/env python3
"""Real gateway, dummy Codex auth, loopback-only upstream. Deletes task state on exit."""
import argparse
import copy
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import fast_mode_isolated as harness
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'dashboard'))
import account_access as access
import routing_policy
from request_deadline import RequestDeadline


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,default=Path('/opt/cliproxyapi/8.0.47/cli-proxy-api'))
    parser.add_argument('--scratch',type=Path,default=Path('~/.cache/agent-scratch/personal-codex-router').expanduser())
    args=parser.parse_args()
    if not args.binary.is_file():raise RuntimeError('Required real gateway binary unavailable')
    args.scratch.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='credits-isolated-',dir=args.scratch) as tmp:
        root=Path(tmp);harness.RUN=root;harness.EVIDENCE=root
        cert=root/'cert.pem';key=root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(key),'-out',str(cert),
                        '-subj','/CN=chatgpt.com','-addext','subjectAltName=DNS:chatgpt.com'],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        harness.server_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);harness.server_tls.load_cert_chain(cert,key)
        mock=http.server.ThreadingHTTPServer(('127.0.0.1',0),harness.Mock);mock.daemon_threads=True
        thread=threading.Thread(target=mock.serve_forever);thread.start()
        port=harness.free_port();assert port!=8317 and mock.server_port!=8317
        auth=root/'auth';auth.mkdir()
        # One account ensures failure proves removal rather than ordinary rotation.
        harness.save_json(auth/'account-A.json',dict(type='codex',email='spike-a@example.invalid',plan_type='pro',
                access_token='spike-dummy-A',proxy_url=f'http://127.0.0.1:{mock.server_port}',
                id_token={'email':'spike-a@example.invalid','plan_type':'pro','chatgpt_account_id':'workspace-a'}))
        config=root/'config.json'
        config.write_text(json.dumps({'host':'127.0.0.1','port':port,'auth-dir':str(auth),'api-keys':['spike-client-dummy'],
            'remote-management':{'secret-key':'spike-management-dummy','disable-control-panel':True,'disable-auto-update-panel':True},
            'routing':{'strategy':'fill-first','session-affinity':False},'request-retry':0,'max-retry-credentials':0,'disable-cooling':True}))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def call(path,body=None,method=None,timeout=5):
            req=urllib.request.Request(f'http://127.0.0.1:{port}/v0/management'+path,
                data=None if body is None else json.dumps(body).encode(),method=method,
                headers={'Authorization':'Bearer spike-management-dummy','Content-Type':'application/json'})
            with opener.open(req,timeout=timeout) as response:return json.load(response)
        def request():
            req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/responses',
                data=json.dumps({'model':harness.MODEL,'input':'synthetic','instructions':'synthetic','stream':False}).encode(),
                headers={'Authorization':'Bearer spike-client-dummy','Content-Type':'application/json'})
            try:
                with opener.open(req,timeout=10) as response:response.read();return response.status
            except urllib.error.HTTPError as error:error.read();return error.code
        proc=None
        try:
            with (root/'gateway.log').open('w') as log:
                proc=subprocess.Popen([str(args.binary),'--local-model','-config',str(config)],cwd=root,stdout=log,stderr=subprocess.STDOUT,
                    env={'PATH':os.environ['PATH'],'SSL_CERT_FILE':str(cert),'LANG':'C.UTF-8',
                         'HTTP_PROXY':f'http://127.0.0.1:{mock.server_port}','HTTPS_PROXY':f'http://127.0.0.1:{mock.server_port}','NO_PROXY':'127.0.0.1,localhost'})
                end=time.monotonic()+15
                while True:
                    try:
                        rows=call('/auth-files')['files']
                        if len(rows)==1 and access.identity(rows[0]):break
                    except OSError:pass
                    if time.monotonic()>end:raise RuntimeError('Scratch gateway did not expose a bound dummy identity')
                    time.sleep(.1)
                row=rows[0]
                # The inventory can precede the model registry during startup.
                while True:
                    with opener.open(urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',headers={'Authorization':'Bearer spike-client-dummy'}),timeout=5) as response:
                        models=[m['id'] for m in json.load(response).get('data',[])]
                    if models:break
                    if time.monotonic()>end:raise RuntimeError('Scratch gateway model registry did not become ready')
                    time.sleep(.1)
                if harness.MODEL not in models:
                    harness.MODEL=next((m for m in models if m.startswith('gpt-') and 'codex' not in m), models[0] if models else '')
                status=request()
                assert status==200, f'Initial dummy request returned {status}; model {harness.MODEL}; upstream calls {len(harness.records)}'
                assert len(harness.records)==1
                guard=access.CreditSpending(root/'credits.json');guard.update(row['email'],spend_credits=False)
                now=time.time();a=dict(auth_state='authenticated',quota_error=None,quota_checked_at=routing_policy.stamp(now),
                    windows=[dict(slot='primary_window',seconds=604800,remaining=0,used=100,resets_at=now+600)])
                lifecycle=threading.RLock();routing_policy.STATE=root/'routing'
                with lifecycle:row=guard.reconcile(row,a,call,RequestDeadline())
                assert row['disabled'] is True and call('/auth-files')['files'][0]['disabled'] is True
                before=len(harness.records);paused_status=request()
                assert paused_status>=400 and len(harness.records)==before
                a['windows'][0].update(remaining=90,used=10);a['quota_checked_at']=routing_policy.stamp(time.time())
                with lifecycle:row=guard.reconcile(row,a,call,RequestDeadline())
                assert row['disabled'] is False and call('/auth-files')['files'][0]['disabled'] is False
                assert guard.view(row)['paused'] is None and request()==200
                assert len(harness.records)==before+1
                print(f'PASS real gateway: pause/readback, request refused ({paused_status}) with no upstream call, fresh resume/readback, request 200; ephemeral gateway {port}, mock {mock.server_port}',flush=True)
        finally:
            if proc is not None:
                if proc.poll() is None:proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
            mock.shutdown();mock.server_close();thread.join(2)
            assert not thread.is_alive()
    print('PASS cleanup: gateway and mock stopped; scratch removed',flush=True)


if __name__=='__main__':main()
