from codex_fixture import CODEX_PAIR, CODEX_INVENTORY
"""Fail-closed boundaries shared by local dashboard preview fixtures."""


def forbidden_transport(*args, **kwargs):
    raise AssertionError('Production transport forbidden in synthetic fixture')


def isolate(app, directory):
    # These previews exercise the two-account regression scenarios explicitly.
    # Their roster must not depend on the production Codex configuration.
    from codex_fixture import CODEX_PAIR
    import operations
    app.urlopen = forbidden_transport
    app.claude_request = forbidden_transport
    app.routing_policy.urlopen = forbidden_transport
    app.routing_policy.STATE = directory / 'routing'
    # Sign-in writes credentials; never let a fixture reach the real auth dir.
    (directory / 'auth').mkdir(mode=0o700, parents=True, exist_ok=True)
    app.signin.credential_replace.AUTH_DIR = str(directory / 'auth')
    # Moves write requests into /run and read the helper's result; fixtures own
    # both paths, and nothing creates them, so the move stays unavailable.
    app.MOVE_REQUEST_DIR = directory / 'move/request'
    app.MOVE_RESULT = directory / 'move/result.json'
    app.MOVE_HELPER = directory / 'move/move-conversations.py'


def handler(base, allowed):
    class IsolatedHandler(base):
        def reject_mutation(self):
            return self.send(404, b'{"error":"Not available in the synthetic fixture"}')

        def do_POST(self):
            if self.path not in allowed:
                return self.reject_mutation()
            return super().do_POST()

        do_PATCH = do_PUT = do_DELETE = reject_mutation

    return IsolatedHandler


def claude_routing(app, directory):
    """Synthetic Claude controls shared by both previews; no production transport."""
    import copy
    import time
    policy = app.routing_policy
    app.PREFERENCE = directory / 'routing/status.json'
    rows = [dict(type='claude', name=f'claude-{i}.json', auth_index=f'claude-{i}', email=email,
                 note='Claude '+chr(65+i), priority=100 if i == 0 else 0,
                 disabled=False, status='active') for i, email in enumerate(policy.identities('claude', inventory=CODEX_INVENTORY))]
    fixture = {'scenario': 'normal', 'rows': rows}
    def request(path, body=None, method=None, timeout=None):
        if fixture['scenario'] == 'unconfirmed':
            raise OSError('Synthetic unavailable gateway')
        if path == '/config' and body is None:
            return {'routing': {'strategy': 'round-robin'}}
        if path == '/auth-files' and body is None:
            return {'files': copy.deepcopy(rows)}
        if path == '/auth-files/fields' and method == 'PATCH':
            assert set(body) == {'name', 'priority'} and body['priority'] in (0, 100)
            matches = [r for r in rows if r['name'] == body['name']]
            assert len(matches) == 1
            matches[0]['priority'] = body['priority']
            return {'status': 'ok'}
        raise AssertionError('Unmocked routing transport forbidden')
    policy.request = request
    original_status = app.preference_status
    def status(accounts=None, provider='codex'):
        if provider == 'codex':
            return original_status(accounts)
        accounts = [dict(r, priority_checked_at=app.utcnow()) for r in rows]
        value = original_status(accounts, provider=provider)
        if fixture['scenario'] == 'drift' and value:
            value.update(drift=True, actual_priorities={r['email']: 0 if i == 0 else 100
                                                      for i, r in enumerate(rows)})
        return value
    app.preference_status = status
    def watch():
        now = time.time()
        return [dict(id='claude' if i == 0 else 'claude-b', label='Claude '+chr(65+i),
            email=email, provider='claude', plan='Max (20x)' if i == 0 else 'Pro',
            status='ok', checked_at=policy.stamp(now-1000 if fixture['scenario'] == 'held' else now),
            windows=[dict(kind=kind, seconds=seconds, label=label, used=used, remaining=100-used,
                          resets_at=now+offset) for kind, seconds, label, used, offset in (
                ('five_hour', 18000, '5-hour limit', 20, 10000),
                ('seven_day', 604800, 'Weekly · all models', 75-i*40, 200000+i*100000))], resets=None)
            for i, email in enumerate(policy.identities('claude', inventory=CODEX_INVENTORY))]
    app.watch_accounts = watch
    return fixture
