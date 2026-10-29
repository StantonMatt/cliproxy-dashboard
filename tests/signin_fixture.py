"""Entirely synthetic gateway used by sign-in tests and the local visual fixture."""
import base64
import copy
import json
import math
import os
import re
import shutil
import tempfile
import time
import weakref
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from urllib.request import Request, urlopen

import account_access
import credential_replace
import signin

NAME = 'codex-0a1b2c3d-one@example.com-pro.json'
# internal/auth/codex/token.go:96-105: restored from disk by every non-creation save.
# converge() checks for a saved, synced and runtime-verified sign-in.
CONFIRMED = {'disk': True, 'listing': True, 'runtime': True, 'repatched': False}
OUTER_KEYS = ('id_token', 'access_token', 'refresh_token', 'account_id', 'last_refresh',
              'email', 'expired', 'plan_type')


def jwt(email='one@example.com', workspace='workspace-a', plan='pro', **changes):
    claims = {'iss': 'https://auth.openai.com', 'aud': [signin.CLIENT_ID], 'exp': 2000000000,
              'email': email, 'sub': 'user-a', 'jti': 'new-signin',
              signin.AUTH: {'chatgpt_account_id': workspace, 'chatgpt_plan_type': plan,
                            'chatgpt_user_id': 'chatgpt-user-a', 'user_id': 'user-a'}}
    claims.update(changes)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'synthetic.' + payload + '.synthetic'


def auth_file_row(saved, name, auth_index):
    """v8.0.7 internal/api/handlers/management/auth_files.go:655-785,907-934.

    Match buildAuthFileEntryLocked: routing metadata, selected ID-token claims
    only, and no prefix or raw credential fields.
    """
    disabled = saved.get('disabled') is True
    provider = saved.get('type')
    provider = provider.strip().lower() if isinstance(provider, str) and provider.strip() else 'unknown'
    row = {'id': name, 'type': provider, 'provider': provider, 'name': name,
           'auth_index': auth_index, 'label': '', 'status': 'disabled' if disabled else 'active',
           'status_message': '', 'disabled': disabled, 'unavailable': False,
           'runtime_only': False, 'source': 'file', 'size': 0, 'success': 0, 'failed': 0,
           'recent_requests': [], 'quota': {'signals': {}}}
    if saved.get('email'):
        row['email'] = saved['email']
    value = saved.get('priority')
    if type(value) in (int, float) and math.isfinite(value):
        row['priority'] = int(value)
    elif isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+', value.strip()):
        row['priority'] = int(value.strip())
    for field in ('note', 'project_id'):
        if isinstance(saved.get(field), str) and saved[field].strip():
            row[field] = saved[field].strip()
    if saved.get('quota_probe') is not None:
        row['quota_probe'] = copy.deepcopy(saved['quota_probe'])
    value = saved.get('weight')
    if value is not None:
        if isinstance(value, str):
            value = int(value.strip()) if value.strip() else 1
        if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value or value > 1000000:
            raise ValueError('Invalid synthetic weight')
        row['weight'] = max(0, int(value))
    value = saved.get('websockets')
    if type(value) is bool:
        row['websockets'] = value
    elif isinstance(value, str):
        if value.strip() in ('1', 't', 'T', 'TRUE', 'true', 'True'):
            row['websockets'] = True
        elif value.strip() in ('0', 'f', 'F', 'FALSE', 'false', 'False'):
            row['websockets'] = False
    value = saved.get('request_retry', saved.get('request-retry'))
    if isinstance(value, str) and re.fullmatch(r'[+-]?[0-9]+', value.strip()):
        value = int(value.strip())
    if type(value) in (int, float) and math.isfinite(value) and int(value) >= 0:
        row['request_retry'] = int(value)
    if saved.get('id_token'):
        auth = signin.decode(saved['id_token'])[signin.AUTH]
        claims = {key: auth[key].strip() for key in ('chatgpt_account_id',) if auth.get(key)}
        if auth.get('chatgpt_plan_type'):
            claims['plan_type'] = auth['chatgpt_plan_type'].strip()
        claims.update({key: auth[key] for key in ('chatgpt_subscription_active_start',
            'chatgpt_subscription_active_until') if auth.get(key) is not None})
        if claims:
            row['id_token'] = claims
    return row


class DiskCredential(dict):
    """The credential file as a dict; mutations write through to disk."""
    def __init__(self, path):
        self.path = path
        with open(path) as file:
            super().__init__(json.load(file))

    def write(self):
        with open(self.path, 'w') as file:
            json.dump(dict(self), file)

    def __deepcopy__(self, memo):
        return copy.deepcopy(dict(self), memo)

    def __copy__(self):
        return dict(self)


for _method in ('__setitem__', '__delitem__', 'update', 'pop', 'setdefault', 'clear', 'popitem'):
    def _write_through(self, *args, _method=_method, **kwargs):
        result = getattr(dict, _method)(self, *args, **kwargs)
        self.write()
        return result
    setattr(DiskCredential, _method, _write_through)


class FakeGateway:
    """CLIProxyAPI v8.0.30 Codex persistence over a v8.0.7-shaped management API.

    v8.0.30: the credential file in a private temp auth dir is the source of
    truth. A fields PATCH without creation intent restores the outer token keys
    from disk in memory too (codex_persist.go:106-125, token.go:182-220), so it
    can never install tokens by itself. /api-call echoes the in-memory token to
    loopback URLs, and responses carry X-CPA-VERSION. stale_patches leaves
    memory tokens unchanged for that many PATCHes, to model a lagging runtime.

    v8.0.7, revision 97f244b8ddb9cbf564b6e6faab0159102cca8617:

    auth_files_fields.go:307-399 patches a live clone, not the disk credential;
    conductor_selection.go:1515-1536 returns clones from List/GetByID.
    :603-669,794-812 sync touched attributes, including plan_type and disabled.
    conductor_lifecycle.go:211-214,224-240,265-290 preserves the auth index,
    installs synchronously, and logs nonfatal persistence failures. filestore.go:
    125-152,417-425 saves full runtime metadata at the existing path. The list's
    last_refresh is not recomputed by this PATCH; a disabled auth's LastError
    also survives. fields_hook runs after the clone to expose overlapping writers.
    codex_executor_auth.go:42-66 defines the token/plan/RFC3339 refresh fields.
    """
    def __init__(self, parent=None):
        # parent: a caller-owned scratch dir, removed with it even if this process is killed.
        self.auth_dir = tempfile.mkdtemp(prefix='signin-auth-', dir=parent)
        self.remove = weakref.finalize(self, shutil.rmtree, self.auth_dir, True)
        # The writer only touches its fixed auth dir; point it at this fixture's until close().
        self.auth_patch = patch.object(credential_replace, 'AUTH_DIR', self.auth_dir)
        self.auth_patch.start()
        self.path = os.path.join(self.auth_dir, NAME)
        self.saved = {'type': 'codex', 'email': 'one@example.com', 'account_id': 'workspace-a',
            'id_token': jwt(jti='old-signin'), 'access_token': 'FAKE_OLD_ACCESS',
            'refresh_token': 'FAKE_OLD_REFRESH', 'expired': '2026-01-01T00:00:00Z',
            'last_refresh': '2025-12-31T00:00:00Z', 'plan_type': 'pro',
            'prefix': 'acct-a', 'priority': 23, 'disabled': True, 'note': 'keep this',
            'unknown_metadata': {'nested': ['preserve', 123]}}
        self.runtime = copy.deepcopy(self.saved)
        self.row = auth_file_row(self.runtime, NAME, 'account-a')
        self.row['path'] = self.path
        self.row['last_refresh'] = self.saved['last_refresh']
        self.row['status_message'] = 'refresh_token_invalidated'
        self.other_rows = []
        self.tokens = {'id_token': jwt(), 'access_token': 'FAKE_NEW_ACCESS',
                       'refresh_token': 'FAKE_NEW_REFRESH', 'expires_in': 3600}
        self.polls = [(200, {'authorization_code': 'FAKE_CODE', 'code_verifier': 'FAKE_VERIFIER'})]
        self.code = {'device_auth_id': 'FAKE_DEVICE_SECRET', 'user_code': 'ABCD-EFGH', 'interval': '5',
                     'verification_uri_complete': signin.VERIFY + '?user_code=ABCD-EFGH'}
        self.calls, self.field_patches, self.patches = [], [], []
        self.proxy = ''
        self.version = '8.0.30'
        self.stale_patches = 0
        self.echoes = []
        self.persist_fields = True
        self.fields_hook = None
        self.download_hook = None
        self.list_hook = None

    def close(self):
        if self.auth_patch:
            self.auth_patch.stop()
            self.auth_patch = None
        self.remove()

    @property
    def saved(self):
        return DiskCredential(self.path)

    @saved.setter
    def saved(self, value):
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as file:
            json.dump(value, file)

    def echo(self, body):
        assert body['proxy_url'] == 'direct' and body['header'] == {'Authorization': 'Bearer $TOKEN$'}
        assert body['auth_index'] == self.row['auth_index'] and body['method'] == 'GET'
        assert urlsplit(body['url']).hostname == '127.0.0.1'
        token = self.runtime.get('access_token', '')
        with urlopen(Request(body['url'], headers={'Authorization': 'Bearer ' + token}), timeout=5) as response:
            self.echoes.append(token)
            return {'status_code': response.status, 'header': {}, 'body': ''}

    def call(self, path, body=None, method=None, timeout=None):
        self.calls.append((path, copy.deepcopy(body), method))
        url = urlsplit(path)
        if path == '/config':
            return signin.ManagementResponse({'proxy-url': self.proxy, 'routing': {}}, self.version)
        if path == '/auth-files':
            if self.list_hook:
                self.list_hook()
            return {'files': copy.deepcopy([self.row] + self.other_rows)}
        if url.path == '/auth-files/download':
            assert parse_qs(url.query) == {'name': [self.row['name']]}
            if self.download_hook:
                self.download_hook()
            return copy.deepcopy(self.saved)
        if path == '/auth-files/fields':
            assert method == 'PATCH' and body['name'] == self.row['name']
            updated = copy.deepcopy(self.runtime)
            updated.update({key: copy.deepcopy(value) for key, value in body.items() if key != 'name'})
            if self.fields_hook:
                self.fields_hook()
            self.field_patches.append(copy.deepcopy(body))
            disk = self.saved
            for key in OUTER_KEYS:
                if key in disk:
                    updated[key] = disk[key]
                else:
                    updated.pop(key, None)
            persisted = copy.deepcopy(updated)
            if self.stale_patches:
                self.stale_patches -= 1
                updated.update({key: self.runtime[key] for key in OUTER_KEYS if key in self.runtime})
            self.runtime = updated
            projected = auth_file_row(updated, self.row['name'], self.row['auth_index'])
            if 'id_token' in body:
                self.row['id_token'] = projected['id_token']
            if 'priority' in body:
                self.row['priority'] = projected['priority']
            if 'disabled' in body:
                self.row.update(disabled=projected['disabled'], status=projected['status'], status_message='')
            elif not self.row['disabled'] and any(key in body for key in ('access_token', 'refresh_token', 'id_token')):
                self.row.update(status='active', status_message='', unavailable=False)
            self.runtime_plan = updated.get('plan_type') or projected['id_token']['plan_type']
            if self.persist_fields:
                self.saved = persisted
            return {'status': 'ok'}
        if path == '/auth-files/status':
            assert method == 'PATCH'
            matches = [r for r in [self.row] + self.other_rows
                       if body['name'] == r['name'] and body['auth_index'] == r['auth_index']]
            assert len(matches) == 1
            self.patches.append(copy.deepcopy(body))
            matches[0]['disabled'] = body['disabled']
            if matches[0] is self.row:
                self.runtime['disabled'] = body['disabled']
                self.saved['disabled'] = body['disabled']
            return {'status': 'ok'}
        if path == '/api-call':
            if body['url'].startswith('http://127.0.0.1:'):
                return self.echo(body)
            if body['url'] == 'https://chatgpt.com/backend-api/wham/usage':
                return {'status_code': 200, 'body': {'rate_limit': {'primary_window': {
                    'used_percent': 20, 'limit_window_seconds': 604800, 'reset_at': time.time() + 400000}}}}
            assert set(body) == {'method', 'url', 'header', 'data'}
            assert body['method'] == method == 'POST'
            assert set(body['header']) == {'Content-Type', 'Accept'}
            if body['url'] == signin.USERCODE:
                assert json.loads(body['data']) == {'client_id': signin.CLIENT_ID}
                return {'status_code': 200, 'body': json.dumps(self.code)}
            if body['url'] == signin.DEVICE_TOKEN:
                assert json.loads(body['data']) == {'device_auth_id': 'FAKE_DEVICE_SECRET', 'user_code': 'ABCD-EFGH'}
                status, result = self.polls.pop(0)
                return {'status_code': status, 'body': json.dumps(result)}
            if body['url'] == signin.TOKEN:
                assert body['header']['Content-Type'] == 'application/x-www-form-urlencoded'
                assert parse_qs(body['data']) == {'grant_type': ['authorization_code'], 'client_id': [signin.CLIENT_ID],
                    'code': ['FAKE_CODE'], 'code_verifier': ['FAKE_VERIFIER'], 'redirect_uri': [signin.REDIRECT]}
                return {'status_code': 200, 'body': json.dumps(self.tokens)}
        raise AssertionError('Unexpected fake gateway call')

    @property
    def identity(self):
        return account_access.identity(self.row)
