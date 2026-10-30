#!/usr/bin/env python3
"""Verify 8.0.44-47 auth, catalog, compression and metadata behavior in isolation.

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
