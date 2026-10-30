"""Provider capabilities and secret-free, filename-keyed inventory projections."""
import usage_adapters

CATALOG = {
    'codex': dict(name='Codex', accent='codex', features=['fast_mode', 'reset_credits', 'billing', 'signin']),
    'claude': dict(name='Claude', accent='claude', features=['watch_usage', 'claude_resets', 'billing']),
    'xai': dict(name='Grok', accent='xai', features=[]),
    'antigravity': dict(name='Antigravity', accent='neutral', features=[]),
    'kimi': dict(name='Kimi (kimi.com)', accent='neutral', features=[]),
    'kimi-ai': dict(name='Kimi (kimi.ai)', accent='neutral', features=[]),
    'devin': dict(name='Devin', accent='neutral', features=[]),
    'meta': dict(name='Meta', accent='neutral', features=[]),
}
INVENTORY_FIELDS = ('name', 'type', 'email', 'label', 'plan', 'auth_index', 'status',
                    'disabled', 'unavailable', 'priority')


def inventory_view(files):
    return [{k: f[k] for k in INVENTORY_FIELDS if k in f}
            for f in sorted(files, key=lambda f: f.get('name') or '')]


def catalog(provider):
    return dict(id=provider, **CATALOG.get(provider, dict(name=provider, accent='neutral', features=[])))


def groups(gateway, watched, usage=None):
    inventory = gateway.get('inventory', [])
    result = []
    for provider in sorted({f.get('type') for f in inventory if f.get('type')},
                           key=lambda p: (0 if p == 'codex' else 1 if p == 'claude' else 2, p)):
        rows = [f for f in inventory if f.get('type') == provider]
        projected = gateway.get('accounts' if provider == 'codex' else 'claude_accounts', [])
        today_accounts = {r['auth_index']: r for r in
                          (usage or {}).get('providers', {}).get(provider, {}).get('today_accounts', [])}
        accounts = []
        for file in rows:
            email = file.get('email')
            public = {k: file[k] for k in ('email', 'plan', 'status', 'disabled') if k in file}
            if provider in ('codex', 'claude'):
                matches = [a for a in projected if a.get('key', a.get('name')) == file['name']]
                if len(matches) == 1:
                    public.update(matches[0])
            if provider == 'claude':
                public['watch_observed'] = False
                observed = usage_adapters.claude_observation(public, rows, watched)
                if observed:
                    public.update(observed)
                    # Legacy UI flag means usage is available, including gateway observations.
                    public['watch_observed'] = True
                public.pop('_gateway_usage', None)
            elif provider != 'codex':
                matches = [a for a in gateway.get('usage_accounts', [])
                           if a.get('provider') == provider and a.get('key') == file['name']
                           and a.get('auth_index') == file.get('auth_index')]
                if len(matches) == 1:
                    for field in ('windows', 'plan', 'usage_checked_at', 'usage_source'):
                        if field in matches[0]:
                            public[field] = matches[0][field]
                    # A failed latest read marks kept windows as last known before they age out.
                    public['usage_status'] = matches[0].get('status')
            if provider == 'codex':
                public['usage_checked_at'] = public.get('quota_checked_at')
                public['usage_source'] = 'gateway'
            if 'windows' in public:
                public['windows'] = [dict(w, used_pct=w.get('used_pct', w.get('used'))) for w in public['windows']]
            disabled = bool(file.get('disabled')) or file.get('status') == 'disabled'
            public.update(key=file['name'], provider=provider,
                          label=email or file.get('label') or file['name'],
                          disabled=disabled,
                          status=public.get('status') or ('disabled' if disabled else 'unknown'))
            public.setdefault('auth_index', file.get('auth_index'))
            if provider not in ('codex', 'claude'):
                public['routing_enabled'] = not disabled
            public.setdefault('availability', dict(state='disabled' if public['disabled'] else
                                                  'provider_unavailable' if file.get('unavailable') or file.get('status') == 'error' else
                                                  'ready' if file.get('status') == 'active' else 'unknown', scope='account'))
            public.setdefault('active', not public['disabled'] and public['availability']['state'] == 'ready')
            history = today_accounts.get(file.get('auth_index'), {})
            public['requests'] = history.get('requests', 0)
            public['failures'] = history.get('failures', 0)
            public['today'] = dict(requests=public['requests'],
                                   success_rate=(public['requests'] - public['failures']) / public['requests']
                                   if public['requests'] else None,
                                   hours=list(history.get('hours', [0] * 24)),
                                   last_request_at=history.get('last_request_at'))
            accounts.append(public)
        emails = [r.get('email') for r in rows]
        result.append(dict(**catalog(provider), routable=provider in CATALOG and len(rows) >= 2
                           and all(emails) and len({str(e).casefold() for e in emails}) == len(rows), accounts=accounts))
    return result
