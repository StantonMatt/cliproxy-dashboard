"""Keep two-account regression coverage independent of the configured roster."""
import unittest

import operations
import routing_policy

CODEX_PAIR = ('account-a@example.com', 'account-b@example.com')


def use_codex_pair(*aliases):
    for module in (routing_policy, operations, *aliases):
        previous = module.EMAILS
        module.EMAILS = CODEX_PAIR
        unittest.addModuleCleanup(setattr, module, 'EMAILS', previous)
