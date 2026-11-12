#!/usr/bin/env python3
"""Fetch public official feeds; atomically publish a secret-free status snapshot."""
import argparse
import concurrent.futures
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str((ROOT.parent if (ROOT.parent / 'dashboard').is_dir() else ROOT) / 'dashboard'))
import provider_status as contract

FEEDS = {
    'openai': 'https://status.openai.com/proxy/status.openai.com',
    'anthropic': 'https://status.claude.com/api/v2/summary.json',
    'xai': 'https://status.x.ai/api/status',
    'google': 'https://status.cloud.google.com/incidents.json',
    'moonshot': 'https://status.moonshot.cn/api/v2/summary.json',
    'devin': 'https://www.devinstatus.com/api/v2/summary.json',
}
STATUS = {
    'none': 'operational', 'operational': 'operational',
    'minor': 'minor', 'degraded_performance': 'minor',
    'major': 'major', 'partial_outage': 'major',
    'critical': 'critical', 'major_outage': 'critical', 'full_outage': 'critical',
    'maintenance': 'maintenance', 'under_maintenance': 'maintenance',
}
XAI_STATUS = {
    'available': 'operational', 'info': 'minor', 'disruption': 'major',
    'outage': 'critical', 'maintenance': 'maintenance',
}
RANK = {'operational': 0, 'maintenance': 1, 'unknown': 2, 'minor': 3, 'major': 4, 'critical': 5}
DESCRIPTIONS = {
    'operational': 'All systems operational', 'minor': 'Degraded performance',
    'major': 'Partial service outage', 'critical': 'Major service outage',
    'maintenance': 'Scheduled maintenance in progress', 'unknown': 'Status unavailable',
}
ACTIVE = {'investigating', 'identified', 'monitoring', 'in_progress', 'verifying'}


def restrict_https():
    """Landlock ABI 4+: deny TCP binds and all outbound TCP except port 443.

    systemd has IP filters but no destination-port filter. DNS over UDP/Unix
    remains available; the HTTP client separately restricts destinations.
    Fail closed on older/disabled kernels rather than silently losing the rule.
    """
    if sys.platform != 'linux' or os.uname().machine not in ('x86_64', 'aarch64'):
        raise RuntimeError('Provider status collection requires Linux Landlock ABI 4+')
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(value):
        if value < 0:
            raise OSError(ctypes.get_errno(), 'Provider status network sandbox unavailable')
        return value

    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64), ('handled_access_net', ctypes.c_uint64)]

    class Port(ctypes.Structure):
        _fields_ = [('allowed_access', ctypes.c_uint64), ('port', ctypes.c_uint64)]

    # These syscall numbers are shared by Linux x86_64 and aarch64.
    abi = checked(libc.syscall(444, None, 0, 1))  # LANDLOCK_CREATE_RULESET_VERSION
    if abi < 4:
        raise RuntimeError('Provider status collection requires Linux Landlock ABI 4+')
    rules = Ruleset(0, 3)  # BIND_TCP | CONNECT_TCP
    fd = checked(libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0))
    try:
        port = Port(2, 443)  # CONNECT_TCP, host byte order
        checked(libc.syscall(445, fd, 2, ctypes.byref(port), 0))  # LANDLOCK_RULE_NET_PORT
        checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        checked(libc.syscall(446, fd, 0))
    finally:
        os.close(fd)


def indicator(value):
    return STATUS.get(value, 'unknown') if isinstance(value, str) else 'unknown'


def worst(values):
    return max(values, key=RANK.__getitem__, default='unknown')


def rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('Invalid feed list')
    return value


def result(state, incidents=(), description=None):
    title = next((title for _, title in sorted(incidents, key=lambda i: i[0], reverse=True)
                  if contract.text(title)), None)
    return dict(indicator=state, description=description or DESCRIPTIONS[state], incident=title)


def statuspage(payload, source):
    status = payload['status']
    overall = indicator(status['indicator'])
    components = rows(payload.get('components', []))
    # Claude consumer chat/Cowork incidents must not mark API/Code as down.
    selected = [c for c in components if source == 'anthropic' and
                ('api' in str(c.get('name', '')).casefold() or
                 'claude code' in str(c.get('name', '')).casefold())]
    state = worst(indicator(c.get('status')) for c in selected) if selected else overall
    ids = {c['id'] for c in selected}
    incidents = []
    for incident in rows(payload.get('incidents', [])) + rows(payload.get('scheduled_maintenances', [])):
        if incident.get('status') not in ACTIVE:
            continue
        affected = {c['id'] for c in rows(incident.get('components', []))}
        if ids and affected and not ids.intersection(affected):
            continue
        impact = 'maintenance' if incident['status'] in ('in_progress', 'verifying') else indicator(incident.get('impact'))
        incidents.append((RANK[impact], incident.get('name')))
    return result(state, incidents)


def openai(payload, now):
    summary = payload['summary']
    ids = set()
    groups = set()
    for item in rows(summary['structure']['items']):
        group = item.get('group', {})
        if group.get('name', '').casefold() in ('apis', 'api', 'codex'):
            groups.add(group['name'].casefold())
            ids.update(c['component_id'] for c in rows(group['components']) if not c.get('hidden'))
    # The compatibility API currently omits Codex. Do not silently accept that loss.
    if not ids or 'codex' not in groups or not groups.intersection({'api', 'apis'}):
        raise ValueError('Missing API/Codex groups')
    impacts = {c['component_id']: indicator(c['status']) for c in rows(summary['affected_components'])}
    state = worst(impacts.get(key, 'operational') for key in ids)
    incidents = []
    for incident in rows(summary['ongoing_incidents']):
        if incident.get('status') in ('resolved', 'completed', 'cancelled'):
            continue
        affected = [c for c in rows(incident['affected_components']) if c['component_id'] in ids]
        history = rows(incident['component_impacts'])
        current = [c for c in history if c['component_id'] in ids and
                   (not c.get('end_at') or (contract.epoch(c['end_at']) or 0) > now) and
                   (not c.get('start_at') or (contract.epoch(c['start_at']) or float('inf')) <= now)]
        # Only feeds without impact history need the affected-components fallback.
        relevant = current if history else affected
        if not relevant:
            continue
        impact = worst(indicator(c.get('status')) for c in relevant)
        state = worst([state, impact])
        incidents.append((RANK[impact], incident.get('name')))
    return result(state, incidents)


def google(payload):
    incidents = []
    states = []
    for incident in rows(payload):
        if incident.get('end'):
            continue
        products = rows(incident['affected_products'])
        names = [str(p.get('title', '')) + ' ' + str(p.get('current_title', '')) for p in products]
        if not any('gemini' in name.casefold() or 'vertex ai' in name.casefold() for name in names):
            continue
        state = {'SERVICE_INFORMATION': 'minor', 'SERVICE_DISRUPTION': 'major',
                 'SERVICE_OUTAGE': 'critical'}.get(incident.get('status_impact'), 'unknown')
        states.append(state)
        incidents.append((RANK[state], incident.get('external_desc')))
    state = worst(states) if states else 'operational'
    return result(state, incidents, 'No active Gemini/Vertex AI incidents reported' if state == 'operational' else None)


def xai(payload, now):
    updated = contract.epoch(payload['updated_at'])
    if updated is None or not 0 <= now - updated <= contract.MAX_AGE:
        raise ValueError('Invalid or stale xAI status timestamp')
    # Both Grok CLI OAuth and API-key accounts use the API. Consumer Grok
    # website/mobile/media/voice health must not override that scoped status.
    selected = [p for p in rows(payload['products']) if p.get('id') == 'api']
    if len(selected) != 1:
        raise ValueError('Missing or ambiguous xAI API product')
    product = selected[0]
    components = rows(product['components'])
    if not components:
        raise ValueError('Missing xAI API components')
    states = []
    for item in [product] + components:
        value = item.get('status')
        states.append(XAI_STATUS.get(value, 'unknown') if isinstance(value, str) else 'unknown')
    state = worst(states)
    # /api/status exposes no incidents; the separate incidents feed is not
    # collected because its nonempty schema has not been verified.
    return result(state, description='xAI API: ' + DESCRIPTIONS[state] if state != 'unknown' else None)


def normalize(source, payload, now):
    if source == 'openai':
        return openai(payload, now)
    if source == 'google':
        return google(payload)
    if source == 'xai':
        return xai(payload, now)
    return statuspage(payload, source)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Status feed redirect refused')


def fetch(url):
    # Fixed HTTPS destinations only; no environment proxies, redirects or credentials.
    if url not in FEEDS.values():
        raise ValueError('Unknown feed')
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'PersonalCodexRouter-Status/1.0'})
    with opener.open(request, timeout=8) as response:
        content = response.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError('Oversized feed')
    return json.loads(content)


def collect(load=fetch, clock=time.time):
    def one(source):
        try:
            payload = load(FEEDS[source])
            status = normalize(source, payload, clock())
        except Exception:
            status = result('unknown')
        return source, dict(contract.unavailable(source, contract.stamp(clock())), **status)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        sources = dict(pool.map(one, FEEDS))
    # No feed establishes Antigravity availability; its Google link is informational.
    providers = {provider: dict(sources[source]) if source in sources and provider != 'antigravity'
                 else contract.unavailable(source) for provider, source in contract.PROVIDERS.items()}
    return {'provider_status': providers}


def write_snapshot(path, data):
    # systemd creates the shared runtime directory with mode 0755.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.status-', delete=False) as handle:
            candidate = Path(handle.name)
            os.fchmod(handle.fileno(), 0o644)
            json.dump(data, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        candidate.replace(path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=contract.SNAPSHOT)
    args = parser.parse_args()
    restrict_https()
    write_snapshot(args.output, collect())


if __name__ == '__main__':
    main()
