"""Public provider status contract and offline snapshot reader (no network I/O)."""
import datetime as dt
import json
from pathlib import Path
import time

SNAPSHOT = Path('/run/personal-codex-router-provider-status/status.json')
MAX_AGE = 20 * 60
MAX_BYTES = 128 * 1024
INDICATORS = ('operational', 'minor', 'major', 'critical', 'maintenance', 'unknown')
SOURCES = {
    'openai': ('OpenAI', 'https://status.openai.com/'),
    'anthropic': ('Anthropic', 'https://status.claude.com/'),
    'xai': ('xAI', 'https://status.x.ai/'),
    'google': ('Google', 'https://status.cloud.google.com/'),
    'moonshot': ('Moonshot', 'https://status.moonshot.cn/'),
    'devin': ('Cognition', 'https://www.devinstatus.com/'),
}
PROVIDERS = {
    'codex': 'openai', 'codex-api-key': 'openai',
    'claude': 'anthropic', 'claude-api-key': 'anthropic',
    'xai': 'xai', 'xai-api-key': 'xai',
    'gemini-api-key': 'google', 'antigravity': 'google',
    'kimi': 'moonshot', 'kimi-ai': 'moonshot', 'devin': 'devin',
}


def stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def epoch(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def unavailable(source, checked_at=None):
    name, url = SOURCES[source]
    return dict(source=name, indicator='unknown', description='Status unavailable',
                url=url, checked_at=checked_at)


def text(value, limit=500):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def read_snapshot(path=None, now=None):
    """Fail independently per provider; only local data and canonical URLs escape."""
    now = time.time() if now is None else now
    result = {provider: unavailable(source) for provider, source in PROVIDERS.items()}
    try:
        with (SNAPSHOT if path is None else Path(path)).open('rb') as handle:
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            return result
        rows = json.loads(content)['provider_status']
        if not isinstance(rows, dict):
            return result
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return result
    for provider, source in PROVIDERS.items():
        row = rows.get(provider)
        if not isinstance(row, dict):
            continue
        checked = row.get('checked_at')
        at = epoch(checked)
        if at is not None:
            result[provider]['checked_at'] = checked
        # Reject future timestamps too: clock skew must not keep a snapshot green.
        if (at is None or not 0 <= now - at <= MAX_AGE or
                row.get('indicator') not in INDICATORS or not text(row.get('description')) or
                (row.get('incident') is not None and not text(row['incident']))):
            continue
        result[provider].update(indicator=row['indicator'], description=row['description'])
        if row['indicator'] != 'unknown' and row.get('incident') is not None:
            result[provider]['incident'] = row['incident']
    return result
