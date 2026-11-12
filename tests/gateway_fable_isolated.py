"""Focused, credential-free Fable catalog registration check on 8.0.47.

Uses the existing upgrade mock; never contacts live management or inference.
Pass an already downloaded public general catalog containing Fable 5.2.
"""
import argparse
import json
from pathlib import Path
import tempfile
import urllib.error

import yaml

from gateway_upgrade_isolated import Lab, MANAGEMENT_KEY, wait_for


def models(lab):
    return {row['id'] for row in lab.request('/v1/models')['data']}


def check_catalog(lab, catalog):
    baseline = models(lab)
    lab.check('embedded catalog omits Fable 5.2', 'claude-fable-5-2' not in baseline)
    try:
        lab.request('/v1/messages', {
            'model': 'claude-fable-5-2', 'max_tokens': 8,
            'messages': [{'role': 'user', 'content': 'OK'}],
        })
    except urllib.error.HTTPError as error:
        body = json.load(error)
        lab.check('missing registration reproduces HTTP 400', error.code == 400
                  and 'unknown provider for model claude-fable-5-2' in str(body))
    else:
        raise AssertionError('Unregistered model unexpectedly routed')

    cfg = yaml.safe_load(lab.config.read_text())
    cfg['oauth']['model-alias'] = {'claude': [{
        'name': 'claude-fable-5-2', 'alias': 'claude-fable-5-2',
    }]}
    lab.request('/v8/management/config', cfg, MANAGEMENT_KEY, 'PATCH')
    lab.check('same-ID OAuth alias cannot register an absent model',
              'claude-fable-5-2' not in models(lab))
    cfg['oauth'].pop('model-alias')
    selected = lab.root / 'catalog.json'
    old_catalog = json.loads(catalog.read_text())
    old_catalog['claude'] = [row for row in old_catalog['claude']
                             if row['id'] != 'claude-fable-5-2']
    selected.write_text(json.dumps(old_catalog))
    cfg['models'] = {'catalog': str(selected)}
    lab.request('/v8/management/config', cfg, MANAGEMENT_KEY, 'PATCH')
    lab.request('/v8/management/routing/model-definitions/refresh', {},
                MANAGEMENT_KEY, 'POST')
    lab.check('pre-release catalog still omits Fable 5.2',
              'claude-fable-5-2' not in models(lab))
    # A new official publication becomes visible without another config edit.
    selected.write_bytes(catalog.read_bytes())
    refreshed = lab.request('/v8/management/routing/model-definitions/refresh', {},
                            MANAGEMENT_KEY, 'POST')
    lab.check('8.0.47 on-demand catalog refresh succeeds',
              refreshed['ok'] and refreshed['catalogs']['catalog']['refreshed']
              and 'claude' in refreshed['changed_providers'])
    wait_for(lambda: 'claude-fable-5-2' in models(lab))
    lab.check('catalog refresh preserves all embedded routes', baseline <= models(lab))

    for label in ('A', 'B'):
        for account in ('A', 'B'):
            lab.management('/auth-files/status', {
                'name': f'claude-{account}.json', 'disabled': account != label,
            }, 'PATCH')
        wait_for(lambda: all(row.get('disabled') == (row['name'] != f'claude-{label}.json')
                            for row in lab.management('/auth-files')['files']
                            if row['type'] == 'claude'))
        for model in ('claude-fable-5-2', 'claude-fable-5-1',
                      'claude-opus-5-5', 'claude-haiku-5-5'):
            attempts = lab.call('claude', model=model, extra={
                'thinking': {'type': 'disabled'}, 'max_tokens': 8,
            })
            lab.check(f'{label} {model} upstream ID reaches mock unchanged',
                      len(attempts) == 1 and attempts[0]['body']['model'] == model
                      and attempts[0]['account'] == f'claude-{label}')

    cfg['oauth']['excluded-models'] = {'claude': ['claude-fable-5-2']}
    lab.request('/v8/management/config', cfg, MANAGEMENT_KEY, 'PATCH')
    wait_for(lambda: 'claude-fable-5-2' not in models(lab))
    lab.check('emergency exclusion withdraws only the new route', baseline <= models(lab))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--binary', type=Path,
                        default=Path('/opt/cliproxyapi/8.0.47/cli-proxy-api'))
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    catalog = args.catalog.resolve()
    assert any(row['id'] == 'claude-fable-5-2'
               for row in json.loads(catalog.read_text())['claude'])
    with tempfile.TemporaryDirectory(dir=args.scratch, prefix='fable-catalog-') as directory:
        lab = Lab(Path(directory), args.binary.resolve())
        try:
            lab.start()
            check_catalog(lab, catalog)
        finally:
            lab.close()
            args.output.write_text(json.dumps(lab.evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
