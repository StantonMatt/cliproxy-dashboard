"""Cross-process controller and completion barriers; synthetic transports only."""
import contextlib
import copy
import fcntl
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import routing_policy as routing
import server as app
import signin
from signin_fixture import CONFIRMED, FakeGateway, auth_file_row, jwt
from codex_fixture import use_codex_pair


def setUpModule():
    use_codex_pair()


def controller_process(connection, root, rollback):
    def call(path, body=None, method=None, timeout=None):
        connection.send(('call', (path, body, method)))
        return connection.recv()
    try:
        if rollback:
            routing.rollback(call, Path(root))
        else:
            routing.reconcile({}, {'mode': 'round-robin'}, call, Path(root))
        connection.send(('result', 'ok'))
    except routing.Hold:
        connection.send(('result', 'held'))
    finally:
        connection.close()


class CloneGateway(FakeGateway):
    def call(self, path, body=None, method=None, timeout=None):
        if path == '/config':
            return app.signin.ManagementResponse({'routing': {'strategy': 'round-robin'}}, self.version)
        if path == '/auth-files/fields' and body['name'] == 'second.json':
            self.other_rows[0]['priority'] = body['priority']
            return {}
        result = super().call(path, body, method, timeout=timeout)
        if path == '/auth-files/fields':
            self.row.update(auth_file_row(self.runtime, self.row['name'], self.row['auth_index']))
        return result


class MutationBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        isolation = patch.object(routing, 'STATE', self.root)
        isolation.start()
        self.addCleanup(isolation.stop)
        self.gateway = CloneGateway()
        self.addCleanup(self.gateway.close)
        for record in (self.gateway.row, self.gateway.runtime, self.gateway.saved):
            record.update(email=routing.EMAILS[0], priority=100)
        self.gateway.saved['id_token'] = self.gateway.runtime['id_token'] = jwt(email=routing.EMAILS[0])
        self.gateway.tokens['id_token'] = jwt(email=routing.EMAILS[0])
        self.gateway.other_rows = [dict(copy.deepcopy(self.gateway.row), name='second.json',
                                       auth_index='account-b', email=routing.EMAILS[1], priority=0)]
        self.state = app.GatewayState(Mock(), Mock(), Mock())
        self.manager = self.state.signins
        self.manager.call = self.gateway.call
        self.manager.changed = Mock()
        self.state.signin_changed = Mock()
        self.addCleanup(self.manager.close)
        transport = patch.object(app, 'api', side_effect=self.gateway.call)
        transport.start()
        self.addCleanup(transport.stop)
        self.release = threading.Event()

    @contextlib.contextmanager
    def imported(self):
        """Saved and confirmed without touching the fixture's disk or runtime."""
        with patch.object(self.manager, 'save', return_value=(self.gateway.row, {})), \
                patch.object(self.manager, 'converge', return_value=dict(CONFIRMED)):
            yield

    def worker(self, action):
        errors = []
        def run():
            try:
                action()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.release.set)
        return thread, errors

    def controller(self, rollback=False):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=controller_process, args=(child, str(self.root), rollback), daemon=True)
        process.start()
        child.close()
        result = []
        def dispatch():
            while True:
                kind, value = parent.recv()
                if kind == 'result':
                    result.append(value)
                    return
                parent.send(self.gateway.call(*value))
        thread, errors = self.worker(dispatch)
        def cleanup():
            self.release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            thread.join(5)
            parent.close()
            process.close()
        self.addCleanup(cleanup)
        return process, thread, result, errors

    def outcome(self):
        return self.manager.view('account-a', self.gateway.identity)['last_outcome']

    def start(self):
        with patch.object(signin.threading, 'Thread'):
            self.manager.start('account-a', self.gateway.identity, observed_required=True)
        return self.manager.sessions['account-a']

    def operation(self, kind):
        self.manager.sessions.clear()
        self.gateway.row['id_token']['plan_type'] = 'pro'
        for record in (self.gateway.saved, self.gateway.runtime):
            record.update(plan_type=self.gateway.row['id_token']['plan_type'],
                          id_token=jwt(email=routing.EMAILS[0], plan=self.gateway.row['id_token']['plan_type']))
        for record in (self.gateway.row, self.gateway.saved, self.gateway.runtime):
            record['disabled'] = kind != 'off'
            record['priority'] = 100
        self.gateway.patches.clear()
        self.gateway.field_patches.clear()
        if kind in ('paid', 'import'):
            session = self.start()
            session['enable'] = kind == 'paid'
            def complete():
                if kind == 'paid':
                    with self.imported():
                        self.manager.complete(session, self.gateway.tokens)
                else:
                    self.manager.complete(session, self.gateway.tokens)
            return complete
        return lambda: self.state.set_routing_enabled('account-a', self.gateway.identity, kind == 'on', True)

    def test_controller_clone_cannot_overwrite_dashboard_status(self):
        for kind in ('off', 'on', 'paid'):
            with self.subTest(kind=kind):
                self.release.clear()
                action = self.operation(kind)
                cloned, blocked = threading.Event(), threading.Event()
                def pause_clone():
                    cloned.set()
                    self.assertTrue(self.release.wait(5))
                self.gateway.fields_hook = pause_clone
                process, dispatcher, result, controller_errors = self.controller()
                self.assertTrue(cloned.wait(5))
                real_flock = fcntl.flock
                def observe_wait(*args):
                    try:
                        return real_flock(*args)
                    except BlockingIOError:
                        blocked.set()
                        raise
                with patch.object(routing.fcntl, 'flock', side_effect=observe_wait):
                    worker, errors = self.worker(action)
                    self.assertTrue(blocked.wait(3))
                    self.assertFalse(self.gateway.patches)
                    self.release.set()
                    worker.join(5)
                dispatcher.join(5)
                process.join(5)
                self.assertFalse(worker.is_alive() or dispatcher.is_alive() or process.is_alive())
                self.assertEqual(errors + controller_errors, [])
                self.assertEqual(result, ['ok'])
                self.assertEqual(self.gateway.runtime['disabled'], kind == 'off')
                self.assertEqual(self.gateway.row['disabled'], self.gateway.runtime['disabled'])
                self.gateway.fields_hook = None

    def test_dashboard_holds_flock_through_mutation_readback(self):
        for kind in ('off', 'on', 'paid', 'import'):
            for rollback in (False, True):
                with self.subTest(kind=kind, rollback=rollback):
                    self.release.clear()
                    action = self.operation(kind)
                    reading = threading.Event()
                    def pause_readback():
                        if self.gateway.patches or self.gateway.field_patches:
                            reading.set()
                            self.assertTrue(self.release.wait(5))
                    self.gateway.list_hook = pause_readback
                    worker, errors = self.worker(action)
                    self.assertTrue(reading.wait(3))
                    process, dispatcher, result, controller_errors = self.controller(rollback)
                    dispatcher.join(5)
                    process.join(5)
                    self.assertEqual(result, ['held'])
                    self.release.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive() or dispatcher.is_alive() or process.is_alive())
                    self.assertEqual(errors + controller_errors, [])
                    self.gateway.list_hook = None

    def test_lock_timeouts_do_not_mutate(self):
        for kind in ('off', 'on', 'paid', 'import'):
            with self.subTest(kind=kind):
                action = self.operation(kind)
                real_boundary = routing.controller_flock
                with (self.root / 'controller.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    with patch.object(routing, 'controller_flock', side_effect=lambda **kwargs: real_boundary(timeout=0)):
                        if kind in ('off', 'on'):
                            with self.assertRaises(routing.Hold):
                                action()
                        else:
                            # A saved sign-in never raises or rolls back; it reports what is unconfirmed.
                            action()
                self.assertFalse(self.gateway.field_patches or self.gateway.patches)
                if kind == 'import':
                    self.assertEqual(self.outcome()['kind'], 'saved_unconfirmed')
                    self.assertEqual(self.gateway.saved['access_token'], 'FAKE_NEW_ACCESS')
                elif kind == 'paid':
                    self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': False,
                                                      'enable_failed': True})

    def test_free_enable_keeps_flock_until_readback(self):
        for kind in ('on', 'paid'):
            with self.subTest(kind=kind):
                self.release.clear()
                action = self.operation(kind)
                reading = threading.Event()
                original = self.gateway.call
                def downgrade(path, body=None, method=None, timeout=None):
                    if path == '/auth-files/status' and not body['disabled']:
                        self.gateway.row['id_token']['plan_type'] = 'free'
                    return original(path, body, method)
                def pause_readback():
                    if len(self.gateway.patches) == 1:
                        reading.set()
                        self.assertTrue(self.release.wait(5))
                self.gateway.list_hook = pause_readback
                with patch.object(app, 'api', side_effect=downgrade), patch.object(self.manager, 'call', side_effect=downgrade):
                    worker, errors = self.worker(action)
                    self.assertTrue(reading.wait(3))
                    process, dispatcher, result, controller_errors = self.controller()
                    dispatcher.join(5)
                    process.join(5)
                    self.assertEqual(result, ['held'])
                    self.release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive() or dispatcher.is_alive() or process.is_alive())
                self.assertEqual(controller_errors, [])
                self.assertEqual(errors, [])
                self.assertEqual([entry['disabled'] for entry in self.gateway.patches], [False])
                self.assertFalse(self.gateway.row['disabled'])
                self.gateway.list_hook = None

    def test_cancel_during_paid_reread_is_rejected(self):
        session = self.start()
        reading = threading.Event()
        def pause_read():
            reading.set()
            self.assertTrue(self.release.wait(5))
        self.gateway.list_hook = pause_read
        with self.imported():
            worker, errors = self.worker(lambda: self.manager.complete(session, self.gateway.tokens))
            self.assertTrue(reading.wait(3))
            with patch.object(self.manager, 'resolve', return_value=self.gateway.row):
                with self.assertRaisesRegex(signin.AccessError, 'already_finishing'):
                    self.manager.cancel('account-a', self.gateway.identity, session['handle'])
            self.assertTrue(self.manager.active(session))
            self.release.set()
            worker.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(self.gateway.row['disabled'])

    def test_accepted_pending_cancel_prevents_completion_enable(self):
        session = self.start()
        waiting = threading.Event()
        def complete():
            waiting.set()
            self.assertTrue(self.release.wait(5))
            self.manager.complete(session, self.gateway.tokens)
        worker, errors = self.worker(complete)
        self.assertTrue(waiting.wait(3))
        self.manager.cancel('account-a', self.gateway.identity, session['handle'])
        self.release.set()
        worker.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(self.gateway.field_patches or self.gateway.patches)

    def test_paid_helper_rechecks_active_and_explicit_off_after_reread(self):
        for action in (lambda: self.manager.suppress_enable('account-a'), self.manager.close):
            with self.subTest(action=action):
                self.manager.closed = False
                session = self.start()
                self.gateway.list_hook = action
                with self.imported():
                    self.manager.complete(session, self.gateway.tokens)
                self.assertFalse(self.gateway.patches)
                if action == self.manager.close:
                    self.assertEqual(self.manager.outcomes, {})
                else:
                    self.assertEqual(self.outcome()['enabled'], False)
                self.gateway.list_hook = None
                self.manager.close()

    def test_claim_refresh_request_holds_controller_flock(self):
        def refresh(path, body=None, method=None, timeout=None):
            self.assertEqual(path, '/auth-files/refresh?name=' + self.gateway.row['name'])
            self.assertEqual(method, 'POST')
            with (self.root / 'controller.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {}
        self.state.access.maybe_refresh(self.gateway.row, 'free', refresh)

    def test_paid_enable_timeout_keeps_saved_signin_without_enabling(self):
        session = self.start()
        session['stop'] = Mock(is_set=lambda: False, wait=lambda _: False)
        original = self.manager.converge
        imported = []
        def converge_and_hold(*args):
            result = original(*args)
            lock = (self.root / 'controller.lock').open('a')
            self.addCleanup(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
            imported.append(lock)
            return result
        real_boundary = routing.controller_flock
        with patch.object(self.manager, 'converge', side_effect=converge_and_hold), patch.object(
                routing, 'controller_flock', side_effect=lambda **_: real_boundary(timeout=0)):
            self.manager.run(session)
        self.assertEqual(len(imported), 1)
        self.assertEqual(len(self.gateway.field_patches), 1)
        self.assertFalse(self.gateway.patches)
        self.assertTrue(self.gateway.row['disabled'])
        self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': False, 'enable_failed': True})
