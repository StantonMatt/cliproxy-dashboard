#!/usr/bin/env python3
"""Opt-in synthetic dashboard. Stdlib only: never imports the production server.

Serves the selected checkout's real static files and an in-memory API. There is
no auth, usage database, gateway transport, subprocess, or persistent state.
"""
import argparse
import copy
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

EPOCH = 1793620800  # 2026-11-02 12:00 UTC; browser uses the same clock origin.


def stamp(seconds=EPOCH):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def layout():
    specs = [
        ('chart', 4, {'metric': 'requests', 'chart': 'stacked', 'group': 'provider'}),
        ('chart', 4, {'metric': 'tokens', 'chart': 'area', 'group': 'none'}),
        ('chart', 4, {'metric': 'success_rate', 'chart': 'line', 'group': 'none'}),
        ('chart', 6, {'metric': 'requests', 'chart': 'grouped', 'group': 'account'}),
        ('heatmap', 6, {'chart': 'weekhour', 'metric': 'requests'}),
        ('timeline', 8, {'window': '7d', 'forecast': True}),
        ('pulse', 4, {'split': 'provider'}),
        ('health', 4, {}),
        ('account-card', 4, {'provider': 'codex', 'account': 'codex-0.json'}),
        ('account-card', 4, {'provider': 'claude', 'account': 'claude-0.json'}),
        ('account-card', 4, {'provider': 'codex', 'account': 'codex-1.json'}),
        ('account-card', 4, {'provider': 'claude', 'account': 'claude-1.json'}),
        ('breakdown', 4, {'metric': 'requests', 'by': 'model', 'chart': 'ranked'}),
        ('stat', 4, {'metric': 'requests', 'spark': True}),
        ('retry', 4, {}),
    ]
    return dict(version=2, revision=0, providers=['codex', 'claude'], inventory=['codex', 'claude'],
                accounts={p: [f'{p}-{i}.json' for i in range(2)] for p in ('codex', 'claude')},
                boards=[dict(id='overview', name='Overview', builtin=True, heading='', range='today',
                             showRange=False, widgets=[dict(id=f'perf-{i:02}', type=t, span=s,
                                 title='', options=o) for i, (t, s, o) in enumerate(specs)])])


def overview(saved, tick=0):
    now = EPOCH + tick
    groups, inventory, histories = [], [], {}
    for p, name in [('codex', 'Codex'), ('claude', 'Claude')]:
        accounts, history = [], []
        for i in range(2):
            key, identity = f'{p}-{i}.json', f'{p}-{i}'
            windows = [dict(kind=k, label=l, seconds=s, used=24+i*17+j*7,
                            used_pct=24+i*17+j*7, remaining=76-i*17-j*7,
                            resets_at=EPOCH+offset, measured_at=stamp())
                       for j, (k, l, s, offset) in enumerate([
                           ('five_hour', '5-hour limit', 18000, 11000-i*1500),
                           ('seven_day', 'Weekly', 604800, 340000-i*80000)])]
            counts = dict(provider=p, auth_index=identity, account_key=key, email=f'{p}.{i}@example.test',
                          requests=450+i*123+tick, failures=3+i, total_tokens=1240000+i*350000,
                          input_tokens=970000, output_tokens=270000, cached_tokens=450000,
                          hours=[12+(h*7+i*3)%35 for h in range(24)], last_request_at=stamp(now-8))
            a = dict(key=key, id=identity, auth_index=identity, provider=p, email=counts['email'],
                     label=counts['email'], plan='Pro' if p == 'codex' else 'Max', plan_type='pro',
                     active=True, disabled=False, status='ok' if p == 'claude' else 'active',
                     auth_state='authenticated', access_state='enabled', routing_enabled=True,
                     runtime_available=True, quota_checked_at=stamp(now), usage_checked_at=stamp(now),
                     usage_source='gateway', watch_observed=True, windows=windows,
                     availability=dict(state='ready'), requests=counts['requests'], failures=counts['failures'],
                     today=dict(counts, success_rate=.994), billing={}, subscription={},
                     fast_mode=dict(allowed=True, level='fast', enforcement='active', supported=True),
                     resets=dict(available=2, eligible=False, credits=[]))
            accounts.append(a)
            history.append(counts)
            inventory.append(dict(type=p, name=key, auth_index=identity, email=a['email'], status='active'))
        groups.append(dict(id=p, name=name, accent=p, features=[], routable=True, accounts=accounts))
        histories[p] = dict(available=True, today_accounts=history, accounts=history,
                            requests=sum(a['requests'] for a in history), failures=7,
                            total_tokens=2830000, hours=[], models=[])
    retry = dict(source='synthetic', available=True, recovered_requests=17, recovered_upstream_429s=21,
                 evidence_since=stamp(EPOCH-86400), evidence_through=stamp(now), last_scanned_at=stamp(now))
    recent = [dict(id=i+1, timestamp=stamp(now-i*43), provider='codex' if i%2 else 'claude',
                   model='gpt-5.4' if i%2 else 'claude-sonnet-4-6', auth_index=f'codex-{i%2}',
                   failed=False, total_tokens=8400+i*321, input_tokens=6200, output_tokens=2200,
                   latency_ms=900+i*17) for i in range(20)]
    usage = dict(available=True, since=stamp(EPOCH-86400*30), first_recorded=stamp(EPOCH-86400*30),
                 last_collected_at=stamp(now), accounts=[a for v in histories.values() for a in v['accounts']],
                 providers=histories, recent=recent, hours=[], models=[], retry_recovery=retry)
    gateway = dict(online=True, checked_at=stamp(now), gateway_version='8.0.47', refreshing=False,
                   accounts=groups[0]['accounts'], claude_accounts=groups[1]['accounts'], inventory=inventory,
                   pool=dict(ready=2, total=2), routing=dict(strategy='round-robin'),
                   preference=dict(enabled=True, policy='round-robin', status='active'),
                   claude_preference=dict(enabled=True, policy='round-robin', status='active'),
                   routing_linked=False)
    return dict(generated_at=stamp(now), machine='synthetic-perf', read_only=False, access_transport='local',
                gateway=gateway, providers=groups, watch_accounts=[], usage=usage, layout=copy.deepcopy(saved),
                provider_status={}, add_providers=[], notifications=dict(available=True, items=[]),
                health={k: dict(status='healthy', last_success_at=stamp(now))
                        for k in ('gateway', 'codex', 'backup', 'usage_collection')})


def data(path, query, saved, tick):
    get = lambda k, default='': query.get(k, [default])[0]
    if path == '/api/overview':
        return overview(saved, tick)
    if path == '/api/retry':
        return overview(saved, tick)['usage']['retry_recovery']
    metric, group, bucket = get('metric', 'requests'), get('group', 'none'), get('bucket', 'hour')
    if path == '/api/series':
        n, step = (60, 60) if bucket == 'minute' else (24, 3600)
        end = EPOCH if bucket == 'minute' else EPOCH-EPOCH%86400+86400
        buckets = [stamp(end-(n-i)*step) for i in range(n)]
        names = ['Codex', 'Claude'] if group == 'provider' else ['codex.0@example.test', 'claude.0@example.test', 'codex.1@example.test', 'claude.1@example.test'] if group == 'account' else ['Total']
        rows = []
        for j, name in enumerate(names):
            values = [(98+(i%4)*.4 if metric == 'success_rate' else 840+i*19 if metric == 'avg_duration'
                       else (13+(i*7+j*11)%39+tick)*(1400 if 'tokens' in metric else 1)) for i in range(n)]
            provider = 'codex' if j%2 == 0 else 'claude'
            rows.append(dict(key='total' if group == 'none' else f'{group}:{provider}:{j}', label=name,
                             provider=provider, auth_index=f'{provider}-{j//2}', email=name,
                             account_key=f'{provider}-{j//2}.json', values=values, total=sum(values)))
        result = dict(metric=metric, group=group, bucket=bucket, tz=get('tz', 'UTC'),
                      since=buckets[0], until=stamp(end), unit='percent' if metric.endswith('rate') else 'tokens' if 'tokens' in metric else 'count',
                      first_recorded=stamp(EPOCH-86400*30), archived_before=None, buckets=buckets,
                      current=n-1 if bucket == 'minute' else 12, series=rows, previous=None)
        if get('compare') == '1':
            result['previous'] = dict(since=stamp(end-n*step*2), until=buckets[0],
                buckets=[stamp(end-(2*n-i)*step) for i in range(n)], series=copy.deepcopy(rows))
        return result
    if path == '/api/heatmap':
        cells = [[(d*13+h*7)%48 for h in range(24)] for d in range(7)]
        return dict(mode='weekhour', metric=metric, tz=get('tz', 'UTC'), cells=cells, max=47,
                    first_recorded=stamp(EPOCH-86400*30))
    if path == '/api/breakdown':
        if get('by') == 'failure_reason':
            rows = [dict(key='failure_reason:'+kind, label=label, provider=None, value=n, share=n/14*100)
                    for kind, label, n in [('rate_limit', 'Rate limited', 8), ('timeout', 'Timed out', 4),
                                           ('server_error', 'Server error', 2)]]
            return dict(metric=metric, by='failure_reason', unit='count', rows=rows, total=14,
                        other=None, archived_before=None)
        rows = [dict(key=f'model:{i}', label=name, provider='codex' if i%2 else 'claude',
                     value=500-i*65, share=(500-i*65)/1850*100) for i, name in enumerate(
                         ['gpt-5.4', 'claude-sonnet-4-6', 'gpt-5.4-mini', 'claude-opus-4-6', 'gpt-5.3'])]
        return dict(metric=metric, by=get('by', 'model'), unit='count', rows=rows, total=1850,
                    other=None, archived_before=None)
    if path == '/api/requests':
        return dict(rows=overview(saved, tick)['usage']['recent'], next=None, archived_before=None)
    return None


def make_server(checkout, port):
    if port != 0 and not 8390 <= port <= 8399:
        raise ValueError('Synthetic server ports must be 8390–8399 (or 0 for an isolated test)')
    root = (Path(checkout) / 'dashboard/static').resolve(strict=True)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value, mime='application/json'):
            body = json.dumps(value, allow_nan=False).encode() if mime == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self, api=False):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                self.send(421, dict(error='Loopback host required'))
                return False
            if api and (self.headers.get('X-Router-Request') != '1'
                        or self.headers.get('Origin', 'http://'+host) != 'http://'+host
                        or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send(403, dict(error='Same-origin request required'))
                return False
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            if not self.allowed(url.path.startswith('/api/')):
                return
            if url.path.startswith('/api/'):
                with lock:
                    result = data(url.path, parse_qs(url.query), self.server.layout, self.server.tick)
                    if url.path == '/api/overview':
                        self.server.tick += 1
                return self.send(404 if result is None else 200, result or dict(error='Unknown synthetic endpoint'))
            file = (root / ('index.html' if url.path == '/' else url.path.lstrip('/'))).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                return self.send(404, dict(error='Not found'))
            return self.send(200, file.read_bytes(), mimetypes.guess_type(file)[0] or 'application/octet-stream')

        def do_POST(self):
            if not self.allowed(True):
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 262144:
                    raise ValueError()
                self.connection.settimeout(3)
                body = json.loads(self.rfile.read(size) or b'{}')
                path = urlsplit(self.path).path
                with lock:
                    if path == '/__perf/reset':
                        self.server.layout, self.server.tick = layout(), 0
                        if body.get('arrangement') == 'resize-non-chart':
                            widgets = self.server.layout['boards'][0]['widgets']
                            # Health has no chart host; place it before chart neighbours.
                            widgets.insert(0, widgets.pop(7))
                    elif path == '/api/layout':
                        if body.get('base_revision') != self.server.layout['revision']:
                            return self.send(409, dict(error='conflict', layout=self.server.layout))
                        self.server.layout.update(boards=body['boards'], accounts=body['accounts'],
                                                  revision=self.server.layout['revision']+1)
                    elif path == '/api/refresh':
                        self.server.tick += 1
                    else:
                        return self.send(403, dict(error='Synthetic fixture forbids mutations'))
                    return self.send(200, dict(layout=self.server.layout, ok=True))
            except (ValueError, KeyError, TypeError, TimeoutError):
                return self.send(400, dict(error='Invalid synthetic request'))

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.layout, server.tick = layout(), 0
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fake-data', action='store_true', required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if not 8390 <= args.port <= 8399:
        parser.error('Use an isolated port in 8390–8399')
    server = make_server(args.checkout, args.port)
    print(json.dumps(dict(synthetic=True, port=server.server_port)), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
