"""Board storage and optimistic concurrency use private temporary files only."""
import copy
import json
import threading
import unittest
import test_dashboard_layout as legacy

server = legacy.server


class LayoutV2Tests(unittest.TestCase):
    setUp = legacy.LayoutTests.setUp
    post = legacy.LayoutTests.post

    def body(self, revision=0):
        return dict(version=2, base_revision=revision, accounts=self.default['accounts'], boards=[
            dict(id='overview', name='Overview', builtin=True, heading='', range='today', showRange=False,
                 widgets=[dict(id='notes1', type='notes', span=4, title='', options={'text': 'Hello'})])])

    def test_optional_height_validation_and_round_trip(self):
        for height in ['auto', *range(3, 21)]:
            with self.subTest(height=height):
                body = self.body()
                body['boards'][0]['widgets'][0]['height'] = height
                self.assertEqual(server.validate_layout(body), body)
                stored = dict(version=2, revision=0, providers=[], accounts={}, boards=body['boards'])
                self.assertEqual(server.validate_layout(stored, stored=True), stored)
        body = self.body()
        body['boards'][0]['widgets'][0]['height'] = 7
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(server.layout_view(self.groups)['boards'], body['boards'])
        for height in [2, 21, 1.5, True, False, '3', '20', 'short', '', None, [], {}]:
            with self.subTest(invalid_height=height):
                invalid = self.body(1)
                invalid['boards'][0]['widgets'][0]['height'] = height
                with self.assertRaises(ValueError):
                    server.validate_layout(invalid)
        extra = self.body(1)
        extra['boards'][0]['widgets'][0]['unknown'] = 4
        with self.assertRaises(ValueError):
            server.validate_layout(extra)

    def test_split_widgets_and_long_account_keys_round_trip(self):
        body = self.body()
        widgets = body['boards'][0]['widgets']
        widgets[:] = [
            dict(id='header', type='accounts', span='auto', height='auto', title='',
                 options=dict(provider='claude', hideCards=True, hideRouting=False)),
            dict(id='card', type='account-card', span=3, height=6, title='',
                 options=dict(provider='claude', account='a' * 255, label='Saved name')),
        ]
        self.assertEqual(server.validate_layout(body), body)
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(server.layout_view(self.groups)['boards'], body['boards'])
        saved = json.loads(self.path.read_text())
        self.assertEqual(server.validate_layout(saved, stored=True), saved)
        self.assertEqual(server.layout_view(self.groups, saved=saved)['boards'], body['boards'])
        for key, length in [('account', 256), ('label', 81), ('provider', 81), ('other', 81)]:
            invalid = copy.deepcopy(body)
            invalid['base_revision'] = 1
            invalid['boards'][0]['widgets'][1]['options'][key] = 'x' * length
            with self.subTest(option=key):
                self.assertEqual(self.post(invalid)[0], 400)

    def test_in_memory_revision_logic_is_shared_and_does_not_mutate_stored_layout(self):
        saved = dict(self.default, version=2, revision=0, boards=None)
        before = copy.deepcopy(saved)
        updated = server.updated_layout(self.body(), saved, self.groups)
        self.assertEqual(saved, before)
        self.assertEqual(updated['revision'], 1)
        with self.assertRaises(server.LayoutConflict) as conflict:
            server.updated_layout(self.body(), updated, self.groups)
        self.assertEqual(conflict.exception.layout['revision'], 1)
        forced = server.updated_layout(dict(self.body(), force=True), updated, self.groups)
        self.assertEqual(forced['revision'], 2)
        legacy = server.updated_layout(self.default, forced, self.groups)
        self.assertEqual(legacy['boards'], forced['boards'])
        self.assertEqual(legacy['revision'], 3)

    def test_v1_file_migrates_on_read_without_writing_or_inventing_boards(self):
        self.path.write_text(json.dumps(self.default))
        before = self.path.read_bytes()
        view = server.layout_view(self.groups)
        self.assertEqual(view, dict(self.default, version=2, revision=0, boards=None, inventory=list(self.default['accounts'])))
        self.assertEqual(self.path.read_bytes(), before)

    def test_layout_view_exposes_saved_inventory_separately_from_current_accounts(self):
        saved = dict(version=2, revision=2, providers=['codex'], accounts={'codex': ['old'], 'removed': []}, boards=[])
        view = server.layout_view([{'id': 'codex', 'accounts': [{'key': 'new'}]}, {'id': 'xai', 'accounts': [{'key': 'x'}]}], saved=saved)
        self.assertEqual(view['inventory'], ['codex', 'removed'])
        self.assertEqual(view['accounts'], {'codex': ['new'], 'xai': ['x']})
        self.assertEqual(saved['accounts'], {'codex': ['old'], 'removed': []})

    def test_revision_conflict_force_and_v1_preservation(self):
        first = self.body()
        status, result = self.post(first)
        self.assertEqual(status, 200)
        self.assertEqual(result['layout']['revision'], 1)
        self.assertEqual(result['layout']['boards'], first['boards'])
        stored = self.path.read_bytes()
        stale = self.body()
        stale['boards'][0]['name'] = 'Stale'
        status, result = self.post(stale)
        self.assertEqual((status, result['error']), (409, 'conflict'))
        self.assertEqual(result['layout'], server.layout_view(self.groups))
        self.assertEqual(self.path.read_bytes(), stored)
        stale['force'] = True
        self.assertEqual(self.post(stale)[1]['layout']['revision'], 2)
        status, result = self.post(dict(providers=['xai'], accounts={'codex': ['codex-b.json']}))
        self.assertEqual(status, 200)
        self.assertEqual(result['layout']['revision'], 3)
        self.assertEqual(result['layout']['boards'], stale['boards'])
        self.assertEqual(result['layout']['accounts']['codex'], ['codex-b.json', 'codex-a.json'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        next_body = self.body(3)
        next_body['accounts']['codex'] = ['removed', 'codex-b.json']
        self.assertEqual(self.post(next_body)[1]['layout']['accounts']['codex'], ['codex-b.json', 'codex-a.json'])

    def test_two_simultaneous_writers_have_one_winner(self):
        results = []
        barrier = threading.Barrier(2)
        def write():
            barrier.wait(timeout=5)
            try:
                results.append(server.save_layout(self.path, self.body(), server.RequestDeadline())['revision'])
            except server.LayoutConflict:
                results.append('conflict')
        threads = [threading.Thread(target=write) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, [1, 'conflict'])

    def test_validation_bounds_and_unknown_types_round_trip(self):
        body = self.body()
        widget = body['boards'][0]['widgets'][0]
        widget.update(type='future-widget', span='auto', title='x' * 60,
                      options=dict(text='x' * 4000, boolean=True, number=3.5, empty=None, future='v'))
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(server.layout_view(self.groups)['boards'], body['boards'])
        for board_patch in ({'id': 'bad.id'}, {'name': ''}, {'name': 'x' * 25}, {'heading': 'x' * 61},
                            {'builtin': 1}, {'showRange': 0}, {'range': 'x' * 17}, {'widgets': [widget] * 41}):
            invalid = self.body(1)
            invalid['boards'][0].update(board_patch)
            with self.subTest(board_patch=board_patch):
                self.assertEqual(self.post(invalid)[0], 400)
        for widget_patch in ({'id': 'overview'}, {'id': 'x' * 65}, {'type': 'Upper'}, {'type': 'x' * 33},
                             {'span': True}, {'span': 0}, {'span': 13}, {'span': 1.5}, {'title': 'x' * 61},
                             {'options': []}, {'options': {'x': []}}, {'options': {'x': {}}},
                             {'options': {'x': 'x' * 81}}, {'options': {'text': 'x' * 4001}},
                             {'options': {'x' * 33: 1}}, {'options': {str(i): 0 for i in range(25)}},
                             {'options': {'x': float('nan')}}, {'options': {'x': float('inf')}}):
            invalid = self.body(1)
            invalid['boards'][0]['widgets'][0].update(widget_patch)
            with self.subTest(widget_patch=widget_patch):
                self.assertEqual(self.post(invalid)[0], 400)
        for root in ({'base_revision': True}, {'base_revision': -1}, {'base_revision': '1'},
                     {'force': 1}, {'boards': None}, {'boards': self.body()['boards'] * 13}):
            invalid = self.body(1)
            invalid.update(root)
            self.assertEqual(self.post(invalid)[0], 400)
        duplicate = self.body(1)
        duplicate['boards'][0]['widgets'].append(copy.deepcopy(duplicate['boards'][0]['widgets'][0]))
        self.assertEqual(self.post(duplicate)[0], 400)

    def test_body_cap_and_invalid_stored_v2(self):
        body = self.body()
        raw = json.dumps(body).encode()
        self.assertEqual(self.post(raw=raw + b' ' * (262144 - len(raw)))[0], 200)
        self.assertEqual(self.post(raw=raw + b' ' * (262145 - len(raw)))[0], 400)
        self.path.write_text(json.dumps(dict(version=2, revision=-1, accounts={}, providers=[], boards=[])))
        self.assertEqual(server.layout_view(self.groups)['revision'], 0)
        self.assertIsNone(server.layout_view(self.groups)['boards'])
