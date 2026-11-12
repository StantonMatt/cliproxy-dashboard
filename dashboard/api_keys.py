"""API-key accounts: private config CAS writes and explicit public projections.

Only this module handles raw API keys. Never publish management/config responses,
exception strings, or backup paths. Config writes replace one v8 provider list.
"""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from account_add import AccountError, confirmation_text, gateway_compat
from request_deadline import RequestDeadline

CONFIG_PATH = Path('/etc/cliproxyapi/config.yaml')
BACKUP_DIR = Path('/var/lib/personal-codex-router/disconnected')
MANAGEMENT_KEY = Path('/etc/cliproxyapi/dashboard-management-key')
NAMES = {'claude-api-key': 'Anthropic API key', 'codex-api-key': 'OpenAI API key',
         'gemini-api-key': 'Gemini API key', 'xai-api-key': 'xAI API key',
         'openai-compatibility': 'Other OpenAI-compatible API'}
CHANNELS = {p: p.removesuffix('-api-key') for p in NAMES}
# v8.0.47 discards Codex/xAI entries with an empty base URL.
DEFAULT_BASES = {'claude-api-key': 'https://api.anthropic.com',
                 'codex-api-key': 'https://api.openai.com/v1',
                 'gemini-api-key': 'https://generativelanguage.googleapis.com',
                 'xai-api-key': 'https://api.x.ai/v1'}
PREFIX = '/v8/management/config'


class InvalidField(AccountError):
    def __init__(self, field):
        self.field = field
        super().__init__('invalid_field', 'Check the account field.', 400)


class ConfigResponse:
    def __init__(self, value, etag=None, version=None):
        self.value, self.etag, self.version = value, etag, version


def management_request(path, body=None, method=None, timeout=18, headers=None):
    """Private transport; neither upstream bodies nor exception details escape."""
    target = path if path.startswith('/v8/management/') or path == PREFIX else '/v0/management' + path
    request = Request('http://127.0.0.1:8317' + target,
                      data=json.dumps(body).encode() if body is not None else None,
                      method=method, headers={'Authorization': 'Bearer ' + MANAGEMENT_KEY.read_text().strip(),
                                              'Content-Type': 'application/json', **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return ConfigResponse(json.load(response), response.headers.get('ETag'),
                                  response.headers.get('X-CPA-VERSION'))
    except HTTPError as error:
        error.close()
        raise


def supported(version):
    return gateway_compat(version)['supported']


def model_definitions(channel, call):
    """Load public channel models for the bounded background cache."""
    models = call('/model-definitions/' + channel).value.get('models')
    if not isinstance(models, list):
        raise ValueError()
    result = []
    for model in models:
        if isinstance(model, str):
            model = {'id': model}
        if not isinstance(model, dict):
            continue
        identifier = model.get('id', model.get('name'))
        if isinstance(identifier, str) and identifier:
            item = {'id': identifier}
            if isinstance(model.get('alias'), str) and model['alias'] != identifier:
                item['display_name'] = model['alias']
            result.append(item)
    return result


def catalog(version):
    if not supported(version):
        return []
    result = []
    for provider, name in NAMES.items():
        compat = provider == 'openai-compatibility'
        fields = ([dict(id='name', label='Name', required=True, secret=False)] if compat else [])
        fields += [dict(id='api_key', label='API key', required=True, secret=True),
                   dict(id='base_url', label='Base URL', required=compat, secret=False,
                        placeholder='https://api.example.com/v1' if compat else DEFAULT_BASES[provider])]
        if compat:
            fields.append(dict(id='models', label='Models', required=True, secret=False))
        result.append(dict(id=provider, name=name, kind='api_key', fields=fields))
    return result


def owns(key):
    return isinstance(key, str) and re.fullmatch(
        r'apikey-(?:claude-api-key|codex-api-key|gemini-api-key|xai-api-key|openai-compatibility)-[a-f0-9]{24}', key) is not None


def is_api_key_row(row):
    """Also exclude unexpected virtual API entries before OAuth routing/usage."""
    name = str(row.get('name') or row.get('id') or '')
    return (row.get('kind') == 'api_key' or row.get('type') in NAMES
            or row.get('account_type') in ('api_key', 'api-key', 'apikey')
            or str(row.get('label') or '').endswith('-apikey')
            or ':apikey' in name or name.startswith('openai-compatibility:')
            or row.get('runtime_only') is True and row.get('source') == 'config')


def stable_key(provider, base, secret, name='', *, prefix='', proxy_url='', headers=None):
    # Length-prefixed JSON avoids separator collisions. Name distinguishes two
    # existing compatible providers legitimately using the same URL/credential.
    identity = json.dumps([provider, base, secret, name, prefix, proxy_url, headers or {}],
                          sort_keys=True, separators=(',', ':'))
    return 'apikey-' + provider + '-' + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _text(value, field, maximum=200):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise InvalidField(field)
    return value


def validate(provider, fields):
    if not isinstance(provider, str) or provider not in NAMES:
        raise InvalidField('provider')
    compat = provider == 'openai-compatibility'
    allowed = {'api_key', 'base_url'} | ({'name', 'models'} if compat else set())
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise InvalidField('fields')
    secret = _text(fields.get('api_key'), 'api_key', 4096)
    # Otherwise the last-four label could reveal the entire credential.
    if len(secret) < 8:
        raise InvalidField('api_key')
    base = fields.get('base_url')
    if base in (None, '') and not compat:
        base = DEFAULT_BASES[provider]
    _text(base, 'base_url', 2048)
    try:
        url = urlsplit(base)
        if (url.scheme != 'https' or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or '\\' in base
                or url.port == 0 or secret in base):
            raise ValueError()
        url.hostname.encode('idna')
    except (ValueError, UnicodeError):
        raise InvalidField('base_url') from None
    group = {'base-url': base, 'keys': [{'api-key': secret}]}
    if compat:
        name = fields.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]{1,40}', name) or secret in name:
            raise InvalidField('name')
        group['name'] = name
        models = fields.get('models')
        if isinstance(models, str):
            models = list(dict.fromkeys(part.strip() for part in re.split(r'[,\n]', models) if part.strip()))
        if not isinstance(models, list) or not 1 <= len(models) <= 200:
            raise InvalidField('models')
        group['models'] = []
        seen = set()
        for model in models:
            if isinstance(model, str):
                model = {'id': model}
            if not isinstance(model, dict) or set(model) - {'id', 'name', 'alias'} or ('id' in model and 'name' in model):
                raise InvalidField('models')
            identifier = _text(model.get('id', model.get('name')), 'models')
            alias = _text(model['alias'], 'models') if 'alias' in model else identifier
            if secret in identifier or secret in alias or alias in seen:
                raise InvalidField('models')
            seen.add(alias)
            group['models'].append({'name': identifier, 'alias': alias})
    return group


def entries(provider, groups):
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get('keys'), list):
            raise AccountError('gateway_error', 'Gateway account configuration is unavailable.', 503)
        for key_index, item in enumerate(group['keys']):
            if not isinstance(item, dict):
                raise AccountError('gateway_error', 'Gateway account configuration is unavailable.', 503)
            secret = item.get('api-key')
            if not isinstance(secret, str) or not secret:
                continue
            base = group.get('base-url') or ''
            name = group.get('name', '') if provider == 'openai-compatibility' else ''
            yield dict(group=group, item=item, group_index=group_index, key_index=key_index,
                       key=stable_key(provider, base, secret, name,
                                      prefix=item.get('prefix', group.get('prefix', '')),
                                      proxy_url=item.get('proxy-url', group.get('proxy-url', '')),
                                      headers={**(group.get('headers') or {}), **(item.get('headers') or {})}),
                       provider=provider,
                       label=name if name else 'API key ••••' + secret[-4:] if len(secret) > 4 else 'API key ••••',
                       # Keep raw credentials inside this module, including malformed short keys.
                       confirm_text=confirmation_text({}, key_suffix=secret[-4:] if len(secret) > 4 else '',
                                                      custom_name=name if name and secret not in name else None),
                       auth_index=item.get('auth_index') or item.get('auth-index'))


def _without_indexes(value):
    groups = copy.deepcopy(value)
    # Only these two levels carry injected indexes. A custom HTTP header or
    # opaque model option named auth_index is configuration, not an injection.
    for group in groups:
        for record in [group, *group['keys']]:
            record.pop('auth_index', None)
            record.pop('auth-index', None)
    return groups


def backup_config(etag, config_path=None, backup_dir=None):
    """Durably save the exact local config whose hash was read through v8."""
    config_path = Path(config_path or CONFIG_PATH)
    directory = Path(backup_dir or BACKUP_DIR)
    repo = Path(__file__).resolve().parents[1]
    if (directory.is_symlink() or not directory.is_dir() or directory.resolve().is_relative_to(repo)
            or directory.resolve() == config_path.parent.resolve()
            or config_path.parent.resolve() in directory.resolve().parents):
        raise ValueError('Private backup directory unavailable')
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Private backup directory unavailable')
        fd = os.open(config_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if before.st_mode & 0o007:
                raise AccountError('config_unsafe', 'The gateway config must have no world permissions before saving accounts.', 503)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
                raise ValueError('Configuration unavailable')
            raw = source.read(4 * 1024 * 1024 + 1)
            after = os.fstat(source.fileno())
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (len(raw) > 4 * 1024 * 1024 or signature(before) != signature(after)
                or signature(after) != signature(os.stat(config_path, follow_symlinks=False))
                or etag != '"' + hashlib.sha256(raw).hexdigest() + '"'):
            raise AccountError('account_changed', 'Gateway configuration changed. Refresh and try again.', 409)
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        target = stamp + '-' + secrets.token_hex(6) + '-config.yaml'
        saved = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(saved, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


class APIKeyManager:
    def __init__(self, lifecycle_lock, changed, call=None, config_path=None, backup_dir=None):
        self.lock, self.changed = lifecycle_lock, changed
        self.call = call or management_request
        self.config_path, self.backup_dir = config_path, backup_dir

    def _read(self, call, provider):
        try:
            response = call(PREFIX + '/api-keys/' + CHANNELS[provider])
        except HTTPError as error:
            if error.code != 404:
                raise
            response = call(PREFIX)
            if not isinstance(response.value, dict):
                raise ValueError()
            families = response.value.get('api-keys', {})
            if not isinstance(families, dict):
                raise ValueError()
            response = ConfigResponse(families.get(CHANNELS[provider], []), response.etag, response.version)
        if (not supported(response.version) or not isinstance(response.value, list)
                or not isinstance(response.etag, str) or not re.fullmatch(r'"[a-f0-9]{64}"', response.etag)):
            raise AccountError('gateway_error', 'Safe API-key management is unavailable on this gateway.', 503)
        list(entries(provider, response.value))  # Validate list shape before mutation.
        return response

    def _write(self, call, provider, groups, etag):
        try:
            backup_config(etag, self.config_path, self.backup_dir)
        except (ValueError, OSError):
            raise AccountError('backup_unavailable', 'A private configuration backup could not be created.', 503) from None
        try:
            call(PREFIX + '/api-keys/' + CHANNELS[provider], _without_indexes(groups),
                 method='PUT', headers={'If-Match': etag})
        except HTTPError as error:
            if error.code != 500:
                raise
            raise AccountError('config_write_failed', "The gateway can't save its config.", 503) from None
        actual = self._read(call, provider).value
        if _without_indexes(actual) != _without_indexes(groups):
            raise AccountError('unconfirmed', 'The account change could not be confirmed. Refresh before retrying.', 503)

    def _mutate(self, provider, transform, deadline):
        call = deadline.wrap(self.call)
        with deadline.lock(self.lock):
            for attempt in range(2):
                response = self._read(call, provider)
                groups = copy.deepcopy(response.value)
                result = transform(groups)
                try:
                    self._write(call, provider, groups, response.etag)
                    break
                except HTTPError as error:
                    if error.code != 412:
                        raise
                    if attempt:
                        raise AccountError('account_changed', 'Gateway configuration changed. Refresh and try again.', 409) from None
                except AccountError as error:
                    if error.code != 'account_changed' or attempt:
                        raise
        try:
            self.changed()
        except Exception:
            pass
        return result

    def add(self, provider, fields, deadline=None):
        group = validate(provider, fields)
        public = next(entries(provider, [group]))

        def transform(groups):
            rows = list(entries(provider, groups))
            if any(r['item']['api-key'].strip() == group['keys'][0]['api-key'] for r in rows):
                raise AccountError('duplicate', 'This API key is already connected.', 409)
            if provider == 'openai-compatibility' and any(g.get('name', '').casefold() == group['name'] for g in groups):
                raise InvalidField('name')
            groups.append(copy.deepcopy(group))
            return {k: public[k] for k in ('key', 'provider', 'label')}
        return self._mutate(provider, transform, deadline or RequestDeadline())

    def remove(self, key, auth_index=None, confirm_text=None, deadline=None):
        """Disconnect seam: caller checks confirmed:true, then invokes this method."""
        if not owns(key):
            raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
        provider = next(p for p in NAMES if key.startswith('apikey-' + p + '-'))

        def transform(groups):
            matches = [r for r in entries(provider, groups) if r['key'] == key
                       and (auth_index is None or r['auth_index'] == auth_index)]
            if len(matches) != 1:
                raise AccountError('account_changed', 'The account changed. Refresh and try again.', 409)
            row = matches[0]
            expected = row['confirm_text']
            if (not isinstance(confirm_text, str) or not expected
                    or confirm_text.strip().casefold() != expected.strip().casefold()):
                raise AccountError('confirm_mismatch', 'Type the confirmation text to disconnect this account.')
            del groups[row['group_index']]['keys'][row['key_index']]
            if not groups[row['group_index']]['keys']:
                del groups[row['group_index']]
            ready = any(not (other['group'].get('disabled') or other['item'].get('disabled'))
                        for other in entries(provider, groups))
            return dict(disconnected=True, key=key, last_ready=not ready)
        return self._mutate(provider, transform, deadline or RequestDeadline())

    def overview(self, deadline=None, models=None):
        """Return only safe account fields; raw config lives within this call."""
        call = (deadline or RequestDeadline()).wrap(self.call)
        response = call(PREFIX)
        if not supported(response.version) or not isinstance(response.value, dict):
            return []
        families = response.value.get('api-keys', {})
        if not isinstance(families, dict):
            return []
        result = []
        for provider, channel in CHANNELS.items():
            groups = families.get(channel, [])
            if not isinstance(groups, list):
                continue
            for row in entries(provider, groups):
                group, item = row['group'], row['item']
                secret = item['api-key']
                raw_models = item.get('models', group.get('models'))
                if not raw_models and provider != 'openai-compatibility':
                    raw_models = ((models or {}).get(channel) or {}).get('items')
                public_models = []
                for model in raw_models or []:
                    if isinstance(model, str):
                        model = {'id': model}
                    if not isinstance(model, dict):
                        continue
                    identifier = model.get('id', model.get('name'))
                    if isinstance(identifier, str) and secret not in identifier:
                        public_model = {'id': identifier}
                        alias = model.get('alias', model.get('display_name'))
                        if isinstance(alias, str) and alias != identifier and secret not in alias:
                            public_model['display_name'] = alias
                        public_models.append(public_model)
                try:
                    host = urlsplit(group.get('base-url') or '').hostname
                except ValueError:
                    host = None
                if isinstance(host, str) and secret in host:
                    host = None
                label = row['label'] if secret not in row['label'] else 'API key ••••'
                disabled = bool(group.get('disabled') or item.get('disabled'))
                result.append(dict(key=row['key'], provider=provider, kind='api_key', label=label,
                                   confirm_text=row['confirm_text'],
                                   base_url=host, auth_index=row['auth_index'],
                                   models=dict(count=len(public_models), items=public_models) if public_models else None,
                                   disabled=disabled, active=not disabled,
                                   status='disabled' if disabled else 'active',
                                   usage_refresh=dict(supported=False, available_at=None)))
        return result


def groups(accounts, usage=None):
    """Group pre-sanitized accounts and join usage by the gateway's auth index."""
    history = {(provider, row['auth_index']): row
               for provider, summary in (usage or {}).get('providers', {}).items()
               for row in summary.get('today_accounts', []) if row.get('auth_index')}
    result = []
    for provider, name in NAMES.items():
        rows = []
        for original in accounts:
            if original.get('provider') != provider:
                continue
            row = copy.deepcopy(original)
            usage_provider = CHANNELS[provider]
            if provider == 'openai-compatibility':
                usage_name = row['label'].strip().lower()
                usage_provider = usage_name if usage_name == 'openai-compatibility' or usage_name.startswith('openai-compatible-') else 'openai-compatible-' + usage_name
            today = history.get((usage_provider, row.get('auth_index')), {})
            requests, failures = today.get('requests', 0), today.get('failures', 0)
            row.update(requests=requests, failures=failures,
                       today=dict(requests=requests, success_rate=(requests - failures) / requests if requests else None,
                                  hours=list(today.get('hours', [0] * 24)), last_request_at=today.get('last_request_at')),
                       availability=dict(state='disabled' if row['disabled'] else 'ready', scope='account'))
            rows.append(row)
        if rows:
            result.append(dict(id=provider, name=name, accent='neutral', features=[], routable=False, accounts=rows))
    return result
