import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('preference', Path(__file__).resolve().parents[1] / 'scripts/prefer-earliest-reset.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
from signin_fixture import auth_file_row
NOW = 1790160000
from codex_fixture import CODEX_PAIR, use_codex_pair
A, B = CODEX_PAIR


def setUpModule():
    use_codex_pair(app)


def fixture():
    return {'gateway': {'online': True, 'checked_at': app.stamp(NOW), 'accounts': [
        {'email': email, 'active': True, 'quota_checked_at': app.stamp(NOW), 'quota_error': None,
         'windows': [{'seconds': 604800, 'remaining': left, 'resets_at': NOW + offset}]}
        for email, left, offset in [(A, 96, 500000), (B, 43, 300000)]]}}


class PreferenceTests(unittest.TestCase):
    def test_earliest_reset_wins_over_larger_balance(self):
        self.assertEqual(app.choose(fixture(), NOW)['preferred_email'], B)

    def test_reset_rollover_changes_preference(self):
        data = fixture()
        data['gateway']['accounts'][1]['windows'][0]['resets_at'] = NOW + 604800
        self.assertEqual(app.choose(data, NOW)['preferred_email'], A)

    def test_exhausted_or_unavailable_account_is_skipped(self):
        for mode in ['weekly', 'short', 'unavailable']:
            data = fixture(); b = data['gateway']['accounts'][1]
            if mode == 'weekly': b['windows'][0]['remaining'] = 0
            elif mode == 'short': b['windows'].append({'seconds': 18000, 'remaining': 0})
            else: b['active'] = False
            self.assertEqual(app.choose(data, NOW)['preferred_email'], A)

    def test_uncertain_or_expired_observations_never_change_preference(self):
        for mode in ['stale', 'missing', 'error', 'expired', 'invalid']:
            data = fixture(); b = data['gateway']['accounts'][1]
            if mode == 'stale': b['quota_checked_at'] = app.stamp(NOW - 181)
            elif mode == 'missing': b['windows'] = []
            elif mode == 'error': b['quota_error'] = 'Unavailable'
            elif mode == 'expired': b['windows'][0]['resets_at'] = NOW - 1
            else: b['windows'][0]['remaining'] = None
            with self.assertRaises(app.Hold): app.choose(data, NOW)

    def test_resets_within_fifteen_minutes_are_tied_and_keep_current_winner(self):
        for delta in (0,180,900,901):
            data=fixture()
            data['gateway']['accounts'][0]['windows'][0]['resets_at']=NOW+300000-delta
            with self.subTest(delta=delta):
                self.assertEqual(app.choose(data,NOW,current=B)['preferred_email'],B if delta<=900 else A)
                self.assertEqual(app.choose(data,NOW)['preferred_email'],None if delta<=900 else A)
        data=fixture()
        data['gateway']['accounts'][1]['windows'][0]['remaining']=0
        self.assertEqual(app.choose(data,NOW,current=B)['preferred_email'],A)

    def test_partial_update_recovers_and_rollback_preserves_other_fields(self):
        rows = {e: {'email': e, 'name': str(i), 'type': 'codex', 'priority': None,
                    'prefix': 'unchanged', 'access_token': 'NEVER_PERSIST_THIS'} for i, e in enumerate(app.EMAILS)}
        writes = []
        def call(path, body=None, method=None):
            if method == 'PATCH':
                self.assertEqual(set(body), {'name', 'priority'})
                writes.append(body.copy())
                next(r for r in rows.values() if r['name'] == body['name'])['priority'] = body['priority']
                return {'status': 'ok'}
            return {'files': [auth_file_row(row, row['name'], row['email']) for row in rows.values()]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory); plan = app.choose(fixture(), NOW)
            app.apply_plan(plan, call, root)
            self.assertEqual(rows[B]['priority'], 100)
            self.assertEqual(writes[0]['name'], rows[B]['name'])
            self.assertEqual(app.apply_plan(plan, call, root), [])
            plan['priorities'] = {A: 100, B: 0}
            rows[A]['priority'] = 100  # Simulate interruption between promotion and demotion.
            app.apply_plan(plan, call, root)
            self.assertEqual(rows[B]['priority'], 0)
            self.assertNotIn('NEVER_PERSIST_THIS', (root / 'baseline.json').read_text())
            app.rollback(call, root)
            self.assertTrue(all(r['priority'] is None and r['prefix'] == 'unchanged' for r in rows.values()))

    def test_manual_priority_is_not_overwritten(self):
        def call(*args):
            return {'files': [{'type': 'codex', 'email': e, 'name': e, 'priority': 5} for e in app.EMAILS]}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with self.assertRaises(app.Hold): app.apply_plan(app.choose(fixture(), NOW), call, Path(directory))


if __name__ == '__main__':
    unittest.main()
