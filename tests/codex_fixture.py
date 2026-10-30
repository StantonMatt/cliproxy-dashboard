"""Synthetic gateway rosters; production account identities never enter fixtures."""
CODEX_PAIR = ('owner@example.test', 'alternate@example.test')
CODEX_INVENTORY = [dict(type=provider, email=email, name=f'{provider}-{i}.json', auth_index=f'{provider}-{i}')
                   for provider in ('codex', 'claude') for i, email in enumerate(CODEX_PAIR)]


def use_codex_pair(*aliases):
    """Supply a synthetic CLI transport when a test calls helpers without an API."""
    import unittest
    import routing_policy
    for module in (routing_policy, *aliases):
        original = module.inventory_snapshot
        def snapshot(call=None, original=original):
            return [dict(row) for row in CODEX_INVENTORY] if call is None else original(call)
        module.inventory_snapshot = snapshot
        unittest.addModuleCleanup(setattr, module, 'inventory_snapshot', original)
