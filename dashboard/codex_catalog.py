"""Cached Codex model catalogs: which models each Codex account can run at ultrafast.

The catalog is read through the gateway's /api-call with the account's own
credential ($TOKEN$ is substituted by the gateway; the dashboard never sees it).
Only public catalog fields are kept: model slugs, display names and tier ids.
A failed read keeps the last good list; the dashboard never guesses capability.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import threading
import time

from fast_mode import normalize_model, write_json

STATE = Path('/var/lib/personal-codex-router/codex-model-catalog.json')
CLIENT_VERSION = '0.166.0'
URL = 'https://chatgpt.com/backend-api/codex/models?client_version=' + CLIENT_VERSION
# Re-read every 6 hours, once per dashboard start, and whenever the plan changes.
MAX_AGE = 6 * 3600
# After a failed read, wait before trying again so a broken endpoint is not polled every 30 s.
RETRY_AFTER = 15 * 60
TIMEOUT = 10
SLUG_LIMIT = 120
MODEL_LIMIT = 200


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse(raw):
    """Reduce a catalog response to public model rows; raise ValueError when malformed."""
    body = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(body, dict) or not isinstance(body.get('models'), list):
        raise ValueError('Catalog has no model list')
    models = []
    for item in body['models'][:MODEL_LIMIT]:
        if not isinstance(item, dict):
            continue
        slug = normalize_model(_text(item.get('slug'), SLUG_LIMIT))
        if not slug:
            continue
        tiers = item.get('service_tiers') if isinstance(item.get('service_tiers'), list) else []
        ids = sorted({t['id'].strip().lower() for t in tiers
                      if isinstance(t, dict) and isinstance(t.get('id'), str) and t['id'].strip()})
        model = {'id': slug, 'service_tiers': ids[:10]}
        name = _text(item.get('display_name'), SLUG_LIMIT)
        if name:
            model['name'] = name
        models.append(model)
    models.sort(key=lambda m: m['id'])
    return models


def request_body(file, account_id):
    headers = {'Authorization': 'Bearer $TOKEN$', 'Accept': 'application/json',
               'originator': 'codex_cli_rs',
               'User-Agent': 'codex_cli_rs/%s (Ubuntu 26.4.0; x86_64) xterm-256color' % CLIENT_VERSION}
    if account_id:
        headers['ChatGPT-Account-Id'] = account_id
    return {'auth_index': file['auth_index'], 'method': 'GET', 'url': URL, 'header': headers}


class CodexCatalog:
    """Per-account catalog cache keyed by account email, persisted across restarts."""

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path is not None else STATE
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = None
        self.read_this_process = set()

    def _load(self):
        if self.entries is not None:
            return
        try:
            value = json.loads(self.path.read_text())
            accounts = value.get('accounts') if isinstance(value, dict) else None
            self.entries = {k: v for k, v in accounts.items()
                            if isinstance(k, str) and self._valid(v)} if isinstance(accounts, dict) else {}
        except (OSError, ValueError):
            self.entries = {}

    @staticmethod
    def _valid(entry):
        return (isinstance(entry, dict)
                and (entry.get('models') is None or (isinstance(entry['models'], list) and all(
                    isinstance(m, dict) and isinstance(m.get('id'), str)
                    and isinstance(m.get('service_tiers', []), list) for m in entry['models'])))
                and (entry.get('fetched_at') is None or epoch(entry['fetched_at']) is not None))

    def _save(self):
        write_json(self.path, {'version': 1, 'accounts': self.entries}, 0o640)

    def due(self, email, plan):
        with self.lock:
            self._load()
            entry = self.entries.get(email)
            now = self.clock()
            if entry is None:
                return True
            attempted = epoch(entry.get('attempted_at'))
            if entry.get('error') and attempted is not None and now - attempted < RETRY_AFTER:
                return False
            fetched = epoch(entry.get('fetched_at'))
            return (email not in self.read_this_process or fetched is None or now - fetched >= MAX_AGE
                    or (plan is not None and entry.get('plan_type') != plan))

    def refresh(self, file, account_id, plan, call):
        """Read one account's catalog if due. Never raises; keeps the last good list on failure."""
        email = file.get('email')
        if not isinstance(email, str) or not email or not isinstance(file.get('auth_index'), str):
            return False
        if not self.due(email, plan):
            return False
        now = self.clock()
        error = None
        models = None
        try:
            response = call('/api-call', request_body(file, account_id), timeout=TIMEOUT)
            status = response.get('status_code')
            if status != 200:
                error = 'http_%d' % status if type(status) is int else 'unavailable'
            else:
                models = parse(response.get('body'))
        except ValueError:
            error = 'invalid_catalog'
        except Exception:
            error = 'unavailable'
        with self.lock:
            self._load()
            previous = self.entries.get(email, {})
            entry = dict(previous, attempted_at=stamp(now), error=error)
            if error is None:
                entry.update(models=models, fetched_at=stamp(now), plan_type=plan)
            self.entries[email] = entry
            self.read_this_process.add(email)
            try:
                self._save()
            except OSError:
                pass  # The in-memory list still applies; the next read retries persistence.
        return error is None

    def view(self, email):
        """Public catalog view: ultrafast models, fetch timestamps and state."""
        with self.lock:
            self._load()
            entry = copy.deepcopy(self.entries.get(email))
        if not entry:
            return {'status': 'pending', 'fetched_at': None, 'attempted_at': None,
                    'ultrafast_models': [], 'known': False}
        models = entry.get('models')
        known = isinstance(models, list)
        return {'status': 'error' if entry.get('error') else 'ok' if known else 'pending',
                'fetched_at': entry.get('fetched_at'), 'attempted_at': entry.get('attempted_at'),
                'known': known,
                'ultrafast_models': [dict(id=m['id'], name=m.get('name') or m['id'])
                                     for m in models or [] if 'ultrafast' in m.get('service_tiers', [])]}

    def ultrafast_models(self, email):
        return [m['id'] for m in self.view(email)['ultrafast_models']]
