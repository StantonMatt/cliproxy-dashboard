#!/usr/bin/env python3
"""Scratch-only dashboard and HTTP gateway for inventory render acceptance."""
import argparse
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'dashboard'))
import server as app
import fast_mode
from fixture_safety import isolate
from test_single_codex_account import inventory, observed


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18324)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--xai',action='store_true')
    args=parser.parse_args()
    args.state_dir.mkdir(parents=True,exist_ok=True)
    isolate(app,args.state_dir)
    app.DB=args.state_dir/'usage.sqlite3'
    app.BILLING=args.state_dir/'billing.json'
    app.PREFERENCE=args.state_dir/'routing/status.json'
    rows=inventory(xai=args.xai)
    rows[0]['id_token']={'plan_type':'pro','chatgpt_account_id':'fixture-workspace'}
    app.routing_policy.reconcile=lambda *a,**kw: {'status':'active'}
    app.health=lambda _: {'gateway':{'status':'healthy'},'codex':{'status':'healthy'},'accounts':[]}
    fast_mode.enforcement=lambda *a:'active'
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value):
            raw=json.dumps(value).encode();self.send_response(200)
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            if self.path=='/auth-files':return self.reply({'files':copy.deepcopy(rows)})
            if self.path=='/config':return self.reply({'routing':{'strategy':'round-robin','session-affinity':True}})
            self.send_error(404)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/api-call':
                return self.reply({'status_code':200,'body':{'plan_type':'pro','rate_limit':{'primary_window':{
                    'used_percent':22,'limit_window_seconds':604800,'reset_at':time.time()+300000},'secondary_window':{
                    'used_percent':12,'limit_window_seconds':18000,'reset_at':time.time()+12000}}}})
            self.send_error(404)
    gateway=ThreadingHTTPServer(('127.0.0.1',0),Gateway)
    thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start()
    def api(path,body=None,method=None,timeout=5):
        request=Request(f'http://127.0.0.1:{gateway.server_port}'+path,
                        data=json.dumps(body).encode() if body is not None else None,method=method)
        with urlopen(request,timeout=timeout) as response:return json.load(response)
    app.api=api
    resets=app.ResetManager(api,app.account_metadata,args.state_dir/'resets.json')
    state=app.GatewayState(resets,app.NoticeStore(args.state_dir/'notices.json'),
                           fast_mode.FastModeManager(args.state_dir/'fast.json',args.state_dir/'policy.json',args.state_dir/'status.json'))
    state._fetch()
    if not state.data['online']:raise RuntimeError('Fixture gateway failed')
    state.refresh=lambda:False
    snapshot=state.snapshot
    def current_snapshot():
        state.data['checked_at']=app.utcnow()
        for a in state.data['accounts']:
            a['quota_checked_at']=app.utcnow()
            a['quota_attempted_at']=app.utcnow()
        return snapshot()
    state.snapshot=current_snapshot
    def watch():
        data=observed(rows)
        now=time.time()
        for i,a in enumerate(data['watch_accounts']):
            a.update(checked_at=app.utcnow(),label=a['email'],plan='Max (20x)' if i==0 else 'Pro',resets=None)
            for w in a['windows']:
                w.update(used=100-w['remaining'],resets_at=now+(12000 if w['kind']=='five_hour' else 200000)+i*50000,
                         seconds=18000 if w['kind']=='five_hour' else 604800,label='5-hour limit' if w['kind']=='five_hour' else 'Weekly · all models')
        return data['watch_accounts']
    app.watch_accounts=watch
    spec=importlib.util.spec_from_file_location('fixture_collector',ROOT/'scripts/collect-usage.py')
    collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)
    con=collector.connect(app.DB)
    for i,r in enumerate(rows):
        for j in range(3):
            stamp=dt.datetime.now(dt.timezone.utc).isoformat()
            con.execute('INSERT INTO usage (collected_at,timestamp,provider,model,auth_index,failed,input_tokens,output_tokens,cached_tokens,total_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (stamp,stamp,r['type'],'fixture-model',r['auth_index'],int(j==2 and i==2),100,50,20,150))
    con.commit();con.close()
    class Handler(app.Handler):
        def allowed(self,api_request=False):return True
        def do_POST(self):return self.send(404,b'{"error":"Fixture is read-only"}')
    http=app.ThreadingHTTPServer(('127.0.0.1',args.port),Handler);http.state=state
    print(f'Fixture dashboard 127.0.0.1:{args.port}; fixture gateway 127.0.0.1:{gateway.server_port}',flush=True)
    try:http.serve_forever()
    finally:
        http.server_close();gateway.shutdown();gateway.server_close();state.signins.close()


if __name__=='__main__':main()
