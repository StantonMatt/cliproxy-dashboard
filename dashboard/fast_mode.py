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
    """Apply the plugin policy to the recorded, normalized request tier."""
    if requested not in TIERS or requested == 'unknown':
        return 'unknown'
    if allowed is False:
        return 'normal'
    if requested == 'ultrafast' and supports_ultrafast is not True:
        return 'priority'
    return requested


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
                        or (setting.get('changed_at') is not None and epoch(setting['changed_at']) is None)):
                    raise ValueError('Fast mode state needs repair')
                history = setting.get('history', [])
                if (not isinstance(history, list) or any(
                        not isinstance(change, dict) or type(change.get('allow_fast')) is not bool
                        or epoch(change.get('changed_at')) is None for change in history)):
                    raise ValueError('Fast mode state needs repair')
            self.state = value
        except FileNotFoundError:
            self.state = {'accounts': {}}

    def setting(self, email):
        self.load()
        saved = self.state['accounts'].get(email, {})
        return {'allowed': saved.get('allow_fast', True), 'changed_at': saved.get('changed_at')}

    def history(self, email):
        self.load()
        saved = self.state['accounts'].get(email, {})
        # Preserve the one known change when upgrading an existing state file.
        if 'history' not in saved and saved.get('changed_at') is not None:
            return [{'changed_at': saved['changed_at'], 'allow_fast': saved.get('allow_fast', True)}]
        return saved.get('history', [])

    def allowed_at(self, account_id, timestamp):
        with self.lock:
            when = epoch(timestamp)
            matches = [a for a in self.accounts if a['id'] == account_id]
            if when is None or len(matches) != 1:
                return True
            try:
                changes = [c for c in self.history(matches[0]['email']) if epoch(c['changed_at']) <= when]
                return max(reversed(changes), key=lambda c: epoch(c['changed_at']))['allow_fast'] if changes else True
            except (OSError, ValueError, TypeError):
                return None

    def policy(self):
        accounts, files = {}, {}
        for account in self.accounts:
            value = {'allow_fast': self.setting(account['email'])['allowed'],
                     'supports_ultrafast': account.get('supports_ultrafast') is True}
            accounts[account['id']] = value
            files[account['name']] = value
        return {'version': 1, 'updated_at': stamp(time.time()), 'accounts': accounts, 'files': files}

    def request_view(self, account_id, timestamp, requested):
        with self.lock:
            allowed = self.allowed_at(account_id, timestamp)
            matches = [a for a in self.accounts if a['id'] == account_id]
            supports = len(matches) == 1 and matches[0].get('supports_ultrafast') is True
            return {'fast_mode_allowed': allowed,
                    'forwarded_service_tier': forwarded_tier(requested, allowed, supports)}

    def reconcile(self, accounts):
        with self.lock:
            if accounts:
                self.accounts = copy.deepcopy(accounts)
            if not self.accounts:
                return
            if self.restore_pending:
                write_json(self.path, self.state, 0o600)
                self.restore_pending = False
            wanted = self.policy()
            try:
                actual = json.loads(self.policy_path.read_text())
                if (isinstance(actual, dict) and all(actual.get(k) == wanted[k]
                        for k in ('version', 'accounts', 'files'))
                        and epoch(actual.get('updated_at')) is not None
                        and self.policy_path.stat().st_mode & 0o777 == 0o640):
                    return
            except (OSError, ValueError):
                pass
            write_json(self.policy_path, wanted, 0o640)

    def views(self, accounts, db):
        with self.lock:
            try:
                self.reconcile(accounts)
                live = enforcement(self.status_path, self.policy_path)
                settings = {a['id']: self.setting(a['email']) for a in accounts}
            except (OSError, ValueError, TypeError):
                live = 'unknown'
                settings = {a['id']: {'allowed': True, 'changed_at': None} for a in accounts}
                if self.state is not None:
                    settings = {a['id']: self.setting(a['email']) for a in accounts}
            return {a['id']: dict(settings[a['id']], enforcement=live)
                    for a in accounts}

    def set_allowed(self, account_id, allowed, accounts):
        if type(allowed) is not bool:
            raise ValueError('Allowed must be true or false')
        with self.lock:
            matches = [a for a in accounts if a['id'] == account_id]
            if len(matches) != 1:
                raise ValueError('Connected account unavailable')
            self.reconcile(accounts)
            account = matches[0]
            previous = copy.deepcopy(self.state)
            previous_policy = self.policy()
            saved = self.setting(account['email'])
            changed_at = stamp(time.time()) if saved['allowed'] != allowed else saved['changed_at']
            history = list(self.history(account['email']))
            if saved['allowed'] != allowed:
                history.append({'changed_at': changed_at, 'allow_fast': allowed})
            self.state['accounts'][account['email']] = {
                'allow_fast': allowed, 'changed_at': changed_at, 'history': history[-HISTORY_LIMIT:]}
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
            return dict(self.setting(account['email']), enforcement=enforcement(self.status_path, self.policy_path))
