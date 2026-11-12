"""Private account settings and the derived gateway fast-mode policy."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

STATE = Path('/var/lib/personal-codex-router/fast-mode-controls.json')
POLICY = STATE.with_name('fast-mode-policy.json')
STATUS = Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json')
TIERS = ('normal', 'priority', 'ultrafast', 'unknown')
LEVELS = ('normal', 'fast', 'ultrafast')
POLICY_VERSION = 2
# Only plugin libraries whose status reports this schema enforce per-model ultrafast upgrades.
LEVEL_SCHEMA = 2
HISTORY_LIMIT = 50


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def write_json(path, value, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), mode)
            json.dump(value, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class LevelUnavailable(Exception):
    """A valid level that cannot be selected now; code and message are public."""
    MESSAGES = {
        'catalog_pending': "Ultrafast isn't available yet: this account's model list hasn't loaded.",
        'no_models': "Ultrafast isn't available: no model on this account's plan offers it.",
        'plugin_not_enforced': "Ultrafast isn't available: the gateway's speed plugin isn't loaded.",
        'plugin_outdated': "Ultrafast isn't available: the gateway's speed plugin needs updating.",
    }

    def __init__(self, code):
        super().__init__(self.MESSAGES[code])
        self.code = code


def normalize_model(model):
    """Match the plugin: lowercase, strip a trailing "(…)" suffix and any "prefix/"."""
    if not isinstance(model, str):
        return ''
    model = model.strip().lower()
    if model.endswith(')') and '(' in model:
        model = model[:model.rindex('(')].strip()
    if '/' in model:
        model = model[model.rindex('/') + 1:]
    return model.strip()


def level_of(value):
    """A saved setting's or history entry's level; v1 entries carry only allow_fast."""
    if isinstance(value, dict) and value.get('level') in LEVELS:
        return value['level']
    return 'normal' if isinstance(value, dict) and value.get('allow_fast') is False else 'fast'


def plugin_schema(status_path=STATUS):
    """The policy schema a published plugin status declares: 1 for 0.1.x libraries, None if unreadable."""
    try:
        status = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict) or status.get('plugin') != 'fast-mode-policy':
        return None
    value = status.get('policy_schema', 1)
    return value if type(value) is int and value > 0 else 1


def enforcement(status_path=STATUS, policy_path=POLICY):
    try:
        status = json.loads(status_path.read_text())
        if (not isinstance(status, dict) or status.get('plugin') != 'fast-mode-policy'
                or status.get('disabled') is True or status.get('enabled') is False
                or status.get('policy_path') != str(policy_path)
                or type(status.get('pid')) is not int or status['pid'] <= 0):
            return 'unavailable'
        result = subprocess.run(['systemctl', 'show', 'cliproxyapi', '--property=MainPID', '--value'],
                                capture_output=True, text=True, timeout=3, check=True)
        pid = int(result.stdout.strip())
        if pid != status['pid']:
            return 'unavailable'
        proc = Path('/proc') / str(pid)
        try:
            if 'cli-proxy-api' in (proc / 'exe').resolve(strict=True).name:
                return 'active'
        except OSError:
            pass
        return 'active' if b'cli-proxy-api' in (proc / 'cmdline').read_bytes() else 'unavailable'
    except FileNotFoundError:
        return 'unavailable'
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 'unknown'


def forwarded_tier(requested, allowed=True, supports_ultrafast=False):
    """Apply the schema 1 plugin policy to the recorded, normalized request tier."""
    if requested not in TIERS or requested == 'unknown':
        return 'unknown'
    if allowed is False:
        return 'normal'
    if requested == 'ultrafast' and supports_ultrafast is not True:
        return 'priority'
    return requested


def forwarded_at_level(requested, level, model=None, ultrafast_models=()):
    """Apply the schema 2 plugin policy to a recorded request.

    Request rows do not record the client format. Codex clients use the
    Responses API, so rows are inferred as openai-response requests, whose
    translator forwards ultrafast. An unknown level (history read failure)
    falls back to the default policy: fast, with ultrafast mapped to priority.
    """
    if level not in LEVELS:
        return forwarded_tier(requested, None)
    if requested not in TIERS or requested == 'unknown':
        return 'unknown'
    if level == 'normal' or requested == 'normal':
        return 'normal'
    if level == 'fast':
        return 'priority'
    name = normalize_model(model)
    allowed = bool(name) and name in {normalize_model(m) for m in ultrafast_models or ()}
    return 'ultrafast' if allowed else 'priority'


# Shown when the overview cannot match an account to its settings.
UNKNOWN_VIEW = {'allowed': True, 'level': 'fast', 'changed_at': None, 'enforcement': 'unknown',
                'levels': ['normal', 'fast'], 'ultrafast_available': False,
                'ultrafast_unavailable_reason': 'catalog_pending', 'ultrafast_models': [],
                'plugin_policy_schema': None, 'plugin_supports_levels': False,
                'catalog': {'status': 'pending', 'fetched_at': None, 'attempted_at': None}}


def fast_account(account_id, email, name, catalog=None, supports_ultrafast=False):
    """The account shape the manager reconciles; catalog is a CodexCatalog.view()."""
    catalog = catalog or {}
    names = [m for m in catalog.get('ultrafast_models') or [] if isinstance(m, dict) and isinstance(m.get('id'), str)]
    return {'id': account_id, 'email': email, 'name': name,
            # Used only to interpret schema 1 request history, never to build policy.
            'supports_ultrafast': supports_ultrafast is True,
            'ultrafast_models': [m['id'] for m in names], 'ultrafast_names': names,
            'catalog_known': catalog.get('known') is True,
            'catalog': {k: catalog.get(k) for k in ('status', 'fetched_at', 'attempted_at')}}


def valid_models(value):
    return isinstance(value, list) and all(isinstance(m, str) for m in value)


class FastModeManager:
    def __init__(self, path=None, policy_path=None, status_path=None):
        self.path = Path(path) if path is not None else STATE
        self.policy_path = Path(policy_path) if policy_path is not None else POLICY
        self.status_path = Path(status_path) if status_path is not None else STATUS
        self.lock = threading.RLock()
        self.accounts = []
        self.state = None
        self.restore_pending = False

    def load(self):
        if self.state is not None:
            return
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or not isinstance(value.get('accounts'), dict):
                raise ValueError('Fast mode state needs repair')
            for setting in value['accounts'].values():
                if (not isinstance(setting, dict) or type(setting.get('allow_fast', True)) is not bool
                        or ('level' in setting and setting['level'] not in LEVELS)
                        or (setting.get('changed_at') is not None and epoch(setting['changed_at']) is None)):
                    raise ValueError('Fast mode state needs repair')
                history = setting.get('history', [])
                if (not isinstance(history, list) or any(
                        not isinstance(change, dict) or type(change.get('allow_fast')) is not bool
                        or ('level' in change and change['level'] not in LEVELS)
                        or ('ultrafast_models' in change and not valid_models(change['ultrafast_models']))
                        or epoch(change.get('changed_at')) is None for change in history)):
                    raise ValueError('Fast mode state needs repair')
            self.state = value
        except FileNotFoundError:
            self.state = {'accounts': {}}

    def setting(self, email):
        self.load()
        saved = self.state['accounts'].get(email, {})
        level = level_of(saved)
        return {'allowed': level != 'normal', 'level': level, 'changed_at': saved.get('changed_at')}

    def history(self, email):
        self.load()
        saved = self.state['accounts'].get(email, {})
        # Preserve the one known change when upgrading an existing state file.
        if 'history' not in saved and saved.get('changed_at') is not None:
            return [{'changed_at': saved['changed_at'], 'allow_fast': saved.get('allow_fast', True)}]
        return saved.get('history', [])

    def level_at(self, account_id, timestamp):
        """The level and ultrafast allowlist in effect at a request time; (None, []) if unreadable."""
        return self._level_at(account_id, timestamp)[:2]

    def _level_at(self, account_id, timestamp):
        """Also retain whether the effective history entry predates schema 2."""
        with self.lock:
            when = epoch(timestamp)
            matches = [a for a in self.accounts if a['id'] == account_id]
            if when is None or len(matches) != 1:
                return 'fast', [], True
            try:
                changes = [c for c in self.history(matches[0]['email']) if epoch(c['changed_at']) <= when]
                if not changes:
                    return 'fast', [], True
                change = max(reversed(changes), key=lambda c: epoch(c['changed_at']))
                return level_of(change), list(change.get('ultrafast_models') or []), 'level' not in change
            except (OSError, ValueError, TypeError):
                return None, [], True

    def allowed_at(self, account_id, timestamp):
        level, _ = self.level_at(account_id, timestamp)
        return None if level is None else level != 'normal'

    @staticmethod
    def models(account):
        return sorted({normalize_model(m) for m in account.get('ultrafast_models') or [] if normalize_model(m)})

    def policy(self):
        accounts, files = {}, {}
        for account in self.accounts:
            level = self.setting(account['email'])['level']
            models = self.models(account)
            # The v1 fields mirror each level for a 0.1.x library after a rollback:
            # it removes tiers at normal and maps ultrafast to priority unless
            # ultrafast is the selected level and the plan offers it.
            value = {'level': level, 'ultrafast_models': models, 'allow_fast': level != 'normal',
                     'supports_ultrafast': level == 'ultrafast' and bool(models)}
            accounts[account['id']] = value
            files[account['name']] = value
        return {'version': POLICY_VERSION, 'updated_at': stamp(time.time()), 'accounts': accounts, 'files': files}

    def request_view(self, account_id, timestamp, requested, model=None):
        with self.lock:
            level, models, legacy = self._level_at(account_id, timestamp)
            allowed = None if level is None else level != 'normal'
            matches = [a for a in self.accounts if a['id'] == account_id]
            supports = len(matches) == 1 and matches[0].get('supports_ultrafast') is True
            forwarded = (forwarded_tier(requested, allowed, supports) if legacy else
                         forwarded_at_level(requested, level, model, models))
            return {'fast_mode_allowed': allowed,
                    'fast_mode_level': level,
                    'forwarded_service_tier': forwarded}

    def record_catalog_changes(self):
        """Log an allowlist change while ultrafast is selected, so past requests keep their tier."""
        changed = False
        state = copy.deepcopy(self.state)
        for account in self.accounts:
            saved = state['accounts'].get(account['email'])
            if not saved or level_of(saved) != 'ultrafast' or account.get('catalog_known') is not True:
                continue
            history = list(self.history(account['email']))
            models = self.models(account)
            if history and sorted(history[-1].get('ultrafast_models') or []) == models:
                continue
            history.append({'changed_at': stamp(time.time()), 'level': 'ultrafast', 'allow_fast': True,
                            'ultrafast_models': models, 'reason': 'catalog'})
            saved['history'] = history[-HISTORY_LIMIT:]
            changed = True
        if changed:
            write_json(self.path, state, 0o600)
            self.state = state
        return changed

    def record_migration_history(self, policy):
        """End legacy on-account inference when its schema 2 policy is published."""
        state = copy.deepcopy(self.state)
        changed = False
        for account in self.accounts:
            saved = state['accounts'].get(account['email'])
            # Off settings behave identically in both schemas; their next level
            # change already records a boundary.
            if not saved or level_of(saved) != 'fast':
                continue
            history = list(self.history(account['email']))
            if history and 'level' in history[-1]:
                continue
            entry = policy['accounts'][account['id']]
            history.append({'changed_at': policy['updated_at'], 'level': entry['level'],
                            'allow_fast': entry['allow_fast'], 'ultrafast_models': entry['ultrafast_models']})
            saved['history'] = history[-HISTORY_LIMIT:]
            changed = True
        if changed:
            write_json(self.path, state, 0o600)
            self.state = state

    def reconcile(self, accounts):
        with self.lock:
            if accounts:
                self.accounts = copy.deepcopy(accounts)
            if not self.accounts:
                return
            self.load()
            if self.restore_pending:
                write_json(self.path, self.state, 0o600)
                self.restore_pending = False
            self.record_catalog_changes()
            wanted = self.policy()
            current = False
            try:
                actual = json.loads(self.policy_path.read_text())
                if (isinstance(actual, dict) and all(actual.get(k) == wanted[k]
                        for k in ('version', 'accounts', 'files'))
                        and epoch(actual.get('updated_at')) is not None
                        and self.policy_path.stat().st_mode & 0o777 == 0o640):
                    wanted = actual
                    current = True
            except (OSError, ValueError):
                pass
            if not current:
                write_json(self.policy_path, wanted, 0o640)
            self.record_migration_history(wanted)

    def capability(self, account, live, schema):
        """Which levels can be selected now, and why ultrafast cannot."""
        reason = None
        if account.get('catalog_known') is not True:
            reason = 'catalog_pending'
        elif not self.models(account):
            reason = 'no_models'
        elif live != 'active':
            reason = 'plugin_not_enforced'
        elif schema is None or schema < LEVEL_SCHEMA:
            reason = 'plugin_outdated'
        return reason

    def view(self, account, setting, live, schema):
        reason = self.capability(account, live, schema)
        names = {m.get('id'): m.get('name') for m in account.get('ultrafast_names') or [] if isinstance(m, dict)}
        catalog = account.get('catalog') or {}
        return dict(setting, enforcement=live,
                    levels=list(LEVELS) if reason is None else ['normal', 'fast'],
                    ultrafast_available=reason is None, ultrafast_unavailable_reason=reason,
                    ultrafast_models=[{'id': m, 'name': names.get(m) or m} for m in self.models(account)],
                    plugin_policy_schema=schema, plugin_supports_levels=schema is not None and schema >= LEVEL_SCHEMA,
                    catalog={'status': catalog.get('status', 'pending'), 'fetched_at': catalog.get('fetched_at'),
                             'attempted_at': catalog.get('attempted_at')})

    def views(self, accounts, db):
        with self.lock:
            try:
                self.reconcile(accounts)
                live = enforcement(self.status_path, self.policy_path)
                settings = {a['id']: self.setting(a['email']) for a in accounts}
            except (OSError, ValueError, TypeError):
                live = 'unknown'
                settings = {a['id']: {'allowed': True, 'level': 'fast', 'changed_at': None} for a in accounts}
                if self.state is not None:
                    settings = {a['id']: self.setting(a['email']) for a in accounts}
            schema = plugin_schema(self.status_path)
            return {a['id']: self.view(a, settings[a['id']], live, schema) for a in accounts}

    def set_allowed(self, account_id, allowed, accounts):
        """Schema 1 request: off is normal; on keeps a fast or ultrafast level, else selects fast."""
        if type(allowed) is not bool:
            raise ValueError('Allowed must be true or false')
        with self.lock:
            matches = [a for a in accounts if a['id'] == account_id]
            if len(matches) != 1:
                raise ValueError('Connected account unavailable')
            self.load()
            current = self.setting(matches[0]['email'])['level']
            level = 'normal' if not allowed else current if current != 'normal' else 'fast'
            return self.set_level(account_id, level, accounts)

    def set_level(self, account_id, level, accounts):
        if level not in LEVELS:
            raise ValueError('Level must be normal, fast or ultrafast')
        with self.lock:
            matches = [a for a in accounts if a['id'] == account_id]
            if len(matches) != 1:
                raise ValueError('Connected account unavailable')
            self.reconcile(accounts)
            account = matches[0]
            saved = self.setting(account['email'])
            if level == 'ultrafast' and saved['level'] != 'ultrafast':
                reason = self.capability(account, enforcement(self.status_path, self.policy_path),
                                         plugin_schema(self.status_path))
                if reason is not None:
                    raise LevelUnavailable(reason)
            previous = copy.deepcopy(self.state)
            previous_policy = self.policy()
            changed = saved['level'] != level
            changed_at = stamp(time.time()) if changed else saved['changed_at']
            history = list(self.history(account['email']))
            if changed:
                history.append({'changed_at': changed_at, 'level': level, 'allow_fast': level != 'normal',
                                'ultrafast_models': self.models(account)})
            self.state['accounts'][account['email']] = {
                'level': level, 'allow_fast': level != 'normal', 'changed_at': changed_at,
                'history': history[-HISTORY_LIMIT:]}
            try:
                write_json(self.path, self.state, 0o600)
                write_json(self.policy_path, self.policy(), 0o640)
                actual = json.loads(self.policy_path.read_text())
                if any(actual.get(k) != self.policy()[k] for k in ('version', 'accounts', 'files')):
                    raise OSError('Fast mode policy was not confirmed')
            except Exception:
                self.state = previous
                # Restore the authoritative state first; overview reconciliation
                # also repairs the derived policy if a disk failure persists.
                self.restore_pending = True
                try:
                    write_json(self.path, previous, 0o600)
                    self.restore_pending = False
                except OSError:
                    pass
                try:
                    write_json(self.policy_path, previous_policy, 0o640)
                except OSError:
                    pass
                raise
            live = enforcement(self.status_path, self.policy_path)
            return self.view(account, self.setting(account['email']), live, plugin_schema(self.status_path))
