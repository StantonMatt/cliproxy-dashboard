"""Synthetic model registry caching and poll/overview integration."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import api_keys
import providers
import server
import signin


class ModelsTests(unittest.TestCase):
    def setUp(self):
        for module, name in ((api_keys, 'management_request'), (server, 'api')):
            guard = patch.object(module, name, side_effect=AssertionError('Real management transport is forbidden'))
            transport = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(transport.assert_not_called)
        self.now = 0
        self.cache = providers.ModelsCache(clock=lambda: self.now)
        self.rows = [dict(name='a + account.json', type='kimi', auth_index='a', status='active')]
        self.call = Mock(return_value={'models': [
            dict(id='z-model', display_name='Zed', type='private-type', owned_by='private-owner'),
            dict(id='a-model', unexpected='private') ]})

    def view(self):
        gateway = dict(inventory=self.rows, account_models=self.cache.view(self.rows))
        return providers.groups(gateway, [])[0]['accounts'][0]['models']

    def test_public_projection_sorted_ids_and_unknown_until_first_fetch(self):
        self.assertIsNone(self.view())
        self.cache.fetch(self.rows[0], self.call)
        self.call.assert_called_once_with('/auth-files/models?name=a+%2B+account.json')
        self.assertEqual(self.view(), dict(count=2, items=[
            dict(id='a-model'), dict(id='z-model', display_name='Zed')]))
        self.assertNotIn('private', json.dumps(self.view()))
        changed = self.cache.view(self.rows)
        changed[self.rows[0]['name']]['items'].clear()
        self.assertEqual(self.view()['count'], 2)

    def test_ten_minute_cache_by_file_and_empty_success(self):
        self.cache.fetch(self.rows[0], self.call)
        self.now = 599
        self.cache.fetch(self.rows[0], self.call)
        self.call.assert_called_once()
        self.cache.fetch(dict(name='other.json'), self.call)
        self.assertEqual(self.call.call_count, 2)
        self.now = 600
        self.call.return_value = {'models': []}
        self.cache.fetch(self.rows[0], self.call)
        self.assertEqual(self.view(), dict(count=0, items=[]))

    def test_transport_and_malformed_errors_keep_last_good(self):
        self.cache.fetch(self.rows[0], self.call)
        expected = self.view()
        for response in (None, {}, {'models': None}, {'models': [{'id': 1}]}):
            self.now += 600
            self.call.side_effect = OSError('private error') if response is None else None
            self.call.return_value = response
            self.cache.fetch(self.rows[0], self.call)
            self.assertEqual(self.view(), expected)
        self.call.side_effect = OSError()
        self.cache.fetch(dict(name='unknown.json'), self.call)
        self.assertIsNone(self.cache.view([dict(name='unknown.json')])['unknown.json'])

    def test_pending_fetch_never_blocks_cached_view(self):
        entered, release = threading.Event(), threading.Event()
        def pending(*args):
            entered.set()
            release.wait(2)
            return {'models': []}
        thread = threading.Thread(target=self.cache.fetch, args=(self.rows[0], pending))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNone(self.view())
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(2)

    def test_poll_refreshes_models_in_background_and_snapshot_only_reads_cache(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.models_cache = self.cache
        state.api_keys.overview = Mock(return_value=[])
        state.access = Mock()
        state.access.reconcile.return_value = []
        threads = []
        done = threading.Event()
        fetch = self.cache._fetch
        def complete(*args):
            try:
                fetch(*args)
            finally:
                done.set()
        def call(path, *args, **kwargs):
            if path == '/config':
                return signin.ManagementResponse({'routing': {}}, '8.0.47')
            if path.startswith('/auth-files/models?'):
                threads.append(threading.get_ident())
                return self.call(path)
            raise AssertionError('Unexpected route')
        with patch.object(self.cache, '_fetch', complete), \
                patch.object(server, 'api', call), \
                patch.object(server.routing_policy, 'inventory_snapshot', return_value=self.rows), \
                patch.object(server, 'claude_routing_projection', return_value=[]), \
                patch.object(server, 'move_available', return_value=False):
            state._fetch_locked()
            self.assertTrue(done.wait(1))
        self.assertTrue(state.data['online'])
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        with patch.object(server, 'api', side_effect=AssertionError('Overview must not fetch')), \
                patch.object(server, 'billing_records', return_value={}), \
                patch.object(server, 'preference_status', return_value={}), \
                patch.object(server.routing_policy, 'routing_linked', return_value=False):
            snapshot = state.snapshot()
        self.assertEqual(providers.groups(snapshot, [])[0]['accounts'][0]['models'], self.view())

    def test_slow_models_do_not_block_inventory_publication_or_disconnect(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        state.models_cache = self.cache
        state.api_keys.overview = Mock(return_value=[])
        state.access = Mock()
        state.access.reconcile.return_value = []
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def call(path, *args, **kwargs):
            if path == '/config':
                return signin.ManagementResponse({'routing': {}}, '8.0.47')
            if path.startswith('/auth-files/models?'):
                entered.set()
                release.wait(3)
                return {'models': []}
            raise AssertionError('Unexpected route')
        fetch = self.cache._fetch
        def complete(*args):
            try:
                fetch(*args)
            finally:
                finished.set()
        with patch.object(self.cache, '_fetch', complete), \
                patch.object(server, 'api', call), \
                patch.object(server.routing_policy, 'inventory_snapshot', return_value=self.rows), \
                patch.object(server, 'claude_routing_projection', return_value=[]), \
                patch.object(server, 'move_available', return_value=False):
            poll = threading.Thread(target=state._fetch)
            poll.start()
            try:
                self.assertTrue(entered.wait(1))
                poll.join(1)
                self.assertFalse(poll.is_alive(), 'Essential poll results must publish while models are pending')
                self.assertTrue(state.data['online'])
                self.assertEqual(state.data['inventory'][0]['name'], self.rows[0]['name'])
                self.assertIsNone(self.view())
                # Exercise the disconnect lock and identity checks with an isolated config fixture.
                group = api_keys.validate('claude-api-key', {'api_key': 'synthetic-fixture-key'})
                row = next(api_keys.entries('claude-api-key', [group]))
                manager = api_keys.APIKeyManager(state.lifecycle_lock, Mock(), Mock())
                with patch.object(manager, '_read', return_value=api_keys.ConfigResponse([group], 'etag')), \
                        patch.object(manager, '_write'):
                    from request_deadline import RequestDeadline
                    result = manager.remove(row['key'], confirm_text='-key', deadline=RequestDeadline(seconds=.5))
                self.assertTrue(result['disconnected'])
                self.assertFalse(release.is_set(), 'Disconnect must finish before the models response')
            finally:
                release.set()
                poll.join(2)
                self.assertTrue(finished.wait(1))

    def test_refresh_is_bounded_and_single_inflight_per_file(self):
        release = threading.Event()
        entered = threading.Barrier(5)
        threads = []
        fetch = self.cache._fetch
        def complete(*args):
            threads.append(threading.current_thread())
            fetch(*args)
        def pending(*args):
            entered.wait(2)
            release.wait(3)
            return {'models': []}
        rows = [dict(name=f'{i}.json') for i in range(8)]
        with patch.object(self.cache, '_fetch', complete):
            try:
                self.cache.refresh(rows, pending)
                entered.wait(2)
                self.assertEqual(self.cache.inflight, 4)
                self.now = 601
                self.cache.refresh(rows, pending)
                self.assertEqual(len(threads), 4, 'No duplicate fetches even after TTL passes')
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
        self.assertEqual(self.cache.inflight, 0)

    def test_slow_native_models_do_not_block_publication_or_disconnect(self):
        state = server.GatewayState(Mock(), Mock(), Mock())
        self.addCleanup(state.signins.close)
        cache = providers.ModelsCache(clock=lambda: self.now, load=api_keys.model_definitions)
        state.api_key_models_cache = cache
        state.access = Mock()
        state.access.reconcile.return_value = []
        group = api_keys.validate('claude-api-key', {'api_key': 'synthetic-fixture-key'})
        group['keys'][0]['auth_index'] = 'fixture-index'
        row = next(api_keys.entries('claude-api-key', [group]))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        model_threads = []
        def native_call(path, *args, **kwargs):
            if path == api_keys.PREFIX:
                return api_keys.ConfigResponse({'api-keys': {'claude': [group]}}, version='8.0.47')
            if path == '/model-definitions/claude':
                model_threads.append(threading.get_ident())
                entered.set()
                release.wait(3)
                return api_keys.ConfigResponse({'models': [{'id': 'fixture-model', 'private': 'discard'}]})
            raise AssertionError('Unexpected route')
        state.api_keys = api_keys.APIKeyManager(state.lifecycle_lock, Mock(), native_call)
        fetch = cache._fetch
        def complete(*args):
            try:
                fetch(*args)
            finally:
                finished.set()
        with patch.object(cache, '_fetch', complete), \
                patch.object(server, 'api', return_value=signin.ManagementResponse({'routing': {}}, '8.0.47')), \
                patch.object(server.routing_policy, 'inventory_snapshot', return_value=[]), \
                patch.object(server, 'claude_routing_projection', return_value=[]), \
                patch.object(server, 'move_available', return_value=False):
            poll = threading.Thread(target=state._fetch)
            poll.start()
            try:
                self.assertTrue(entered.wait(1))
                poll.join(1)
                self.assertFalse(poll.is_alive(), 'Inventory must publish while channel models are pending')
                self.assertTrue(state.data['online'])
                self.assertEqual(state.data['api_key_accounts'][0]['auth_index'], 'fixture-index')
                self.assertIsNone(state.data['api_key_accounts'][0]['models'])
                self.assertNotEqual(model_threads[0], poll.ident)
                with patch.object(state.api_keys, '_read', return_value=api_keys.ConfigResponse([group], 'etag')), \
                        patch.object(state.api_keys, '_write'):
                    from request_deadline import RequestDeadline
                    result = state.api_keys.remove(row['key'], auth_index='fixture-index', confirm_text='-key',
                                                   deadline=RequestDeadline(seconds=.5))
                self.assertTrue(result['disconnected'])
                self.assertFalse(release.is_set(), 'Disconnect must finish before channel models return')
                self.assertIsNone(state.api_keys.overview(models=cache.view([dict(name='claude')]))[0]['models'])
            finally:
                release.set()
                poll.join(2)
                self.assertTrue(finished.wait(1))
            state._fetch()
            self.assertEqual(state.data['api_key_accounts'][0]['models'],
                             dict(count=1, items=[dict(id='fixture-model')]))
            self.assertEqual(len(model_threads), 1, 'Channel reads share the ten-minute cache')


if __name__ == '__main__':
    unittest.main()
