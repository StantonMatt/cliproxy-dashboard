#!/usr/bin/env python3
"""Verify release-specific auth, catalog, transport and payload behavior in isolation.

Run through heavy. Reuses the upgrade fixture's synthetic OAuth and non-forwarding
TLS proxy; never reads production config/auth or calls the live usage queue.
"""
import argparse
import gzip
import json
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
from unittest.mock import patch

import yaml
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus

import gateway_upgrade_isolated as base


class CompressedMetadataHandler(base.Handler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        account = self.headers.get('Authorization', '').removeprefix('Bearer synthetic-')
        assert account in ('codex-A', 'codex-B')
        with self.server.lock:
            self.server.records.append({'account': account, 'body': body,
                                        'content_encoding': self.headers.get('Content-Encoding', '')})
        response = {'id': 'resp_delta', 'object': 'response', 'status': 'completed',
                    'model': body['model'], 'output': [],
                    'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}
        events = [{'type': name, 'synthetic': 'x' * 1200} for name in
                  ('response.metadata', 'codex.response.metadata', 'codex.rate_limits',
                   'responsesapi.synthetic', 'response.subscription_usage')]
        events += [{'type': 'response.completed', 'response': response}]
        raw = gzip.compress(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run(lab):
    foreign = {'Authorization': 'Bearer foreign-synthetic-token', 'X-Api-Key': base.CLIENT_KEY}
    models = lab.request('/v1/models', headers=foreign)
    lab.check('valid X-Api-Key accepted with foreign bearer', bool(models['data']))
    lab.check('embedded catalog advertises gpt-6.1-terra',
              any(m['id'] == 'gpt-6.1-terra' for m in models['data']))
    try:
        lab.request('/v1/models', headers={**foreign, 'X-Api-Key': 'wrong-synthetic-key'})
    except urllib.error.HTTPError as error:
        lab.check('wrong X-Api-Key with foreign bearer rejected', error.code == 401)
    else:
        lab.check('wrong X-Api-Key with foreign bearer rejected', False)

    def raw(path, body=None, headers=None):
        req = urllib.request.Request(f'http://127.0.0.1:{lab.port}' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + base.CLIENT_KEY,
                     'Content-Type': 'application/json', **(headers or {})})
        with base.HTTP.open(req, timeout=30) as response:
            data, encoding = response.read(), response.headers.get('Content-Encoding', '')
        return gzip.decompress(data) if encoding == 'gzip' else data, encoding

    data, encoding = raw('/v1/models', headers={'Accept-Encoding': 'gzip'})
    lab.check('default downstream gzip negotiates valid model JSON',
              encoding == 'gzip' and bool(json.loads(data)['data']))
    data, encoding = raw('/v1/models', headers={'Accept-Encoding': 'identity'})
    lab.check('identity client receives uncompressed valid model JSON',
              not encoding and bool(json.loads(data)['data']))

    for codex in (False, True):
        headers = {'Accept-Encoding': 'gzip'}
        if codex:
            headers['Originator'] = 'codex_cli_rs'
        # Exceed the default request-compression threshold with compressible data,
        # so the observed plain request proves the default is off.
        data, encoding = raw('/v1/responses', {'model': 'gpt-6.1-terra', 'input': 'synthetic ' * 512,
            'instructions': 'synthetic', 'stream': True}, headers)
        events = [json.loads(line[6:]) for line in data.decode().splitlines()
                  if line.startswith('data: ') and line != 'data: [DONE]']
        types = {e['type'] for e in events}
        label = 'Codex' if codex else 'standard Responses'
        lab.check(label + ' compressed upstream SSE completes', 'response.completed' in types)
        lab.check(label + ' private metadata filtered',
                  not types.intersection({'codex.rate_limits', 'responsesapi.synthetic',
                                           'response.subscription_usage'}))
        lab.check(label + ' moderation metadata policy',
                  ('response.metadata' in types) is codex)
        lab.check(label + ' Codex metadata policy',
                  ('codex.response.metadata' in types) is codex)
        lab.check(label + ' downstream gzip negotiated', encoding == 'gzip')
    lab.check('upstream request compression defaults off',
              len(lab.mock.records) == 2 and all(not r['content_encoding'] for r in lab.mock.records))
    if lab.version == '8.0.79':
        run_879(lab, raw)


def run_879(lab, raw):
    # The selected deployment disables v8.0.55's default-on bbolt store.
    lab.check_no_kv()
    inflight = '/v8/management/observability/requests/in-flight'
    snapshot = lab.request(inflight, key=base.MANAGEMENT_KEY)
    lab.check('authenticated standalone in-flight snapshot is empty after completion',
              snapshot == {'in_flight': 0, 'by_provider': {}})
    for key in ('', 'wrong-synthetic-management'):
        try:
            lab.request(inflight, key=key)
        except urllib.error.HTTPError as error:
            lab.check('in-flight endpoint rejects ' + ('missing' if not key else 'wrong') + ' key', error.code == 401)
        else:
            lab.check('in-flight endpoint requires management authentication', False)

    # v8.0.53/59 budget normalization; v8.0.79 removes the schema keyword
    # while retaining an ordinary property with the same name.
    tools = [{'type': 'function', 'name': 'synthetic_tool', 'parameters': {
        'type': 'object', 'properties': {'tags': {'type': 'array', 'items': {'type': 'string'},
                                               'uniqueItems': True},
                                          'uniqueItems': {'type': 'boolean'}},
        'required': ['tags', 'uniqueItems'], 'additionalProperties': False}, 'strict': True}]
    for budget in (1, 32):
        data, _ = raw('/v1/responses', {'model': 'gpt-6.1-terra', 'input': 'synthetic',
            'instructions': 'synthetic', 'stream': True, 'max_output_tokens': budget, 'tools': tools})
        body = lab.mock.records[-1]['body']
        lab.check(f'Codex output budget {budget} normalized without dropping field',
                  body.get('max_output_tokens') == max(16, budget))
        schema = body['tools'][0]['parameters']
        lab.check(f'Codex schema keyword stripped and literal property retained budget {budget}',
                  'uniqueItems' not in schema['properties']['tags']
                  and schema['properties']['uniqueItems'] == {'type': 'boolean'}
                  and schema['required'] == ['tags', 'uniqueItems']
                  and schema['additionalProperties'] is False)
        lab.check(f'Codex normalized request completes budget {budget}', b'response.completed' in data)

    # v8.0.68 adds an always-registered, API-key authenticated tunnel.
    # A second synthetic API key proves realms are separate, including UUIDs.
    cfg = yaml.safe_load(lab.config.read_text())
    other_key = 'isolated-upgrade-other-client'
    cfg['access']['api-keys'].append(other_key)
    lab.request('/v8/management/config', cfg, base.MANAGEMENT_KEY, 'PATCH')
    base.wait_for(lambda: other_key in lab.management('/config')['api-keys'])
    url = f'ws://127.0.0.1:{lab.port}/v1/tunnel'
    def tunnel(query, key):
        return connect(url + query, additional_headers={'Authorization': 'Bearer ' + key},
                       open_timeout=5, close_timeout=2, proxy=None)
    for key in ('', 'wrong-synthetic-client'):
        try:
            with tunnel('?role=client&uuid=synthetic-client&name=Client', key):
                lab.check('tunnel requires valid API key', False)
        except InvalidStatus as error:
            lab.check('tunnel rejects ' + ('missing' if not key else 'wrong') + ' key',
                      error.response.status_code == 401)
    with tunnel('?role=server&uuid=synthetic-server&name=Server&type=synthetic', base.CLIENT_KEY):
        with tunnel('?role=client&uuid=synthetic-client&name=Client', base.CLIENT_KEY) as first:
            servers = json.loads(first.recv(timeout=5))
            lab.check('authenticated tunnel client discovers same-key server',
                      servers['type'] == 'server_list' and len(servers['servers']) == 1
                      and servers['servers'][0]['uuid'] == 'synthetic-server')
            with tunnel('?role=client&uuid=synthetic-client&name=Client', other_key) as other:
                servers = json.loads(other.recv(timeout=5))
                lab.check('different-key tunnel realm has independent UUIDs and no foreign servers',
                          servers['type'] == 'server_list' and servers['servers'] == [])

    # v8.0.78's explicit instruction protocol persists a caller/session-scoped
    # baseline. Ordinary callers remain unaffected and the marker is private.
    session_headers = {'X-Session-Id': 'synthetic-instruction-session'}
    request = {'model': 'gpt-6.1-terra', 'input': 'synthetic', 'stream': True,
               'instructions': 'synthetic baseline instructions',
               'client_metadata': {'cpa_session_instructions': 'start'}}
    raw('/v1/responses', request, session_headers)
    body = lab.mock.records[-1]['body']
    lab.check('session instruction start moves baseline into developer input',
              body['input'][0]['role'] == 'developer'
              and body['input'][0]['content'][0]['text'] == request['instructions']
              and not body.get('instructions'))
    lab.check('reserved session instruction marker never reaches upstream',
              'cpa_session_instructions' not in body.get('client_metadata', {}))
    reuse = {**request, 'instructions': '', 'client_metadata': {'cpa_session_instructions': 'reuse'}}
    raw('/v1/responses', reuse, session_headers)
    lab.check('session instruction reuse restores the stored baseline',
              lab.mock.records[-1]['body']['input'][0]['content'][0]['text'] == request['instructions'])
    before = len(lab.mock.records)
    try:
        raw('/v1/responses', reuse, {**session_headers, 'Authorization': 'Bearer ' + other_key})
    except urllib.error.HTTPError as error:
        lab.check('instruction baseline is isolated by authenticated caller', error.code == 409)
    else:
        lab.check('instruction baseline is isolated by authenticated caller', False)
    lab.check('missing caller instruction baseline makes no upstream attempt', len(lab.mock.records) == before)

    # The management cache must not keep an old key usable after a hot rotation.
    rotated_key = 'isolated-upgrade-rotated-management'
    cfg = yaml.safe_load(lab.config.read_text())
    cfg['management']['secret-key'] = rotated_key
    lab.request('/v8/management/config', cfg, base.MANAGEMENT_KEY, 'PATCH')
    base.wait_for(lambda: lab.request(inflight, key=rotated_key))
    lab.check('management cache accepts rotated synthetic key',
              lab.request(inflight, key=rotated_key)['in_flight'] == 0)
    try:
        lab.request(inflight, key=base.MANAGEMENT_KEY)
    except urllib.error.HTTPError as error:
        lab.check('management cache rejects previous key after rotation', error.code == 401)
    else:
        lab.check('management cache rejects previous key after rotation', False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gateway-delta-', dir=args.scratch) as directory:
        lab = base.Lab(Path(directory), args.binary.resolve())
        try:
            with patch.object(base, 'Handler', CompressedMetadataHandler):
                lab.start()
                run(lab)
        finally:
            lab.close()
            args.output.write_text(json.dumps(lab.evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
