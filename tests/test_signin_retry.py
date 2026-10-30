"""Sign-in save, completion and deadline boundaries; temp dirs and synthetic tokens only."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import credential_replace as store
import operations
import routing_policy
import server as app
import signin
from request_deadline import RequestDeadline
from signin_fixture import NAME, FakeGateway, jwt
from test_signin import ClockEvent

class SigninCompletionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        isolation = patch.object(routing_policy, 'STATE', self.root)
        isolation.start()
        self.addCleanup(isolation.stop)
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)
        self.clock = [1800000000.0]
        self.changed = Mock()
        self.manager = signin.SigninManager(self.gateway.call, threading.RLock(), self.changed,
                                            clock=lambda: self.clock[0])
        self.addCleanup(self.manager.close)

    def begin(self):
        with patch.object(signin.threading, 'Thread'):
            view = self.manager.start('account-a', self.gateway.identity)
        session = self.manager.sessions['account-a']
        session['stop'] = ClockEvent(self.clock)
        return view, session

    def run_flow(self):
        _, session = self.begin()
        self.manager.run(session)
        return self.view()

    def view(self):
        return self.manager.view('account-a', self.gateway.identity)

    def outcome(self):
        return self.view()['last_outcome']

    def unconfirmed(self):
        """Saved but never confirmed; the gateway no longer reports the old token's rejection."""
        self.gateway.stale_patches = 2
        self.assertEqual(self.run_flow()['last_outcome'], {'kind': 'saved_unconfirmed', 'at': self.clock[0]})
        self.gateway.row['status_message'] = ''
        self.saved_at = self.clock[0]
        self.clock[0] += 60

    def device_calls(self, since=0):
        return [c for c in self.gateway.calls[since:] if c[0] == '/api-call'
                and c[1]['url'].startswith('https://auth.openai.com')]

    def thread(self, action):
        results, errors = [], []
        def run():
            try:
                results.append(action())
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(worker.join, 5)
        return worker, results, errors

    def test_converge_checks_the_deadline_after_each_echo(self):
        self.unconfirmed()
        record = self.manager.resolve('account-a', self.gateway.identity)
        saved = self.gateway.saved
        expected = {key: saved[key] for key in store.TOKEN_FIELDS}
        for echoes in ([False, True], [True]):
            with self.subTest(echoes=echoes):
                now, results = [0.0], list(echoes)
                deadline = RequestDeadline(45, clock=lambda: now[0])
                def echo(*args, **kwargs):
                    now[0] = 100 if len(results) == 1 else now[0]
                    return results.pop(0)
                with patch.object(signin, 'runtime_echo', side_effect=echo), self.assertRaises(TimeoutError):
                    self.manager.converge(record, expected, deadline.wrap(self.gateway.call), deadline)
                self.assertEqual(results, [])

    def test_credential_lock_wait_does_not_hold_the_lifecycle_lock(self):
        _, session = self.begin()
        lock = open(os.path.join(self.gateway.auth_dir, NAME + '.lock'), 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = threading.Event()
        real = store.credential_lock
        def credential_lock(*args, **kwargs):
            waiting.set()
            return real(*args, **kwargs)
        lifecycle_held = []
        original = self.manager.converge
        def converge(*args, **kwargs):
            probe = threading.Thread(target=lambda: lifecycle_held.append(not self.manager.lifecycle.acquire(timeout=0.05)))
            probe.start()
            probe.join()
            return original(*args, **kwargs)
        with patch.object(store, 'credential_lock', side_effect=credential_lock), \
                patch.object(self.manager, 'converge', side_effect=converge):
            worker, _, errors = self.thread(lambda: self.manager.run(session))
            self.assertTrue(waiting.wait(5))
            time.sleep(0.1)
            # A routing change can run meanwhile and still suppresses the enable.
            self.assertTrue(self.manager.lifecycle.acquire(timeout=0.5))
            self.manager.suppress_enable('account-a')
            self.manager.lifecycle.release()
            fcntl.flock(lock, fcntl.LOCK_UN)
            worker.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(lifecycle_held, [True])
        self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': False})
        self.assertEqual(self.gateway.saved['access_token'], 'FAKE_NEW_ACCESS')
        self.assertFalse(self.gateway.patches)

    def test_close_while_waiting_for_the_credential_lock_writes_nothing(self):
        _, session = self.begin()
        lock = open(os.path.join(self.gateway.auth_dir, NAME + '.lock'), 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = threading.Event()
        real = store.credential_lock
        def credential_lock(*args, **kwargs):
            waiting.set()
            return real(*args, **kwargs)
        with patch.object(store, 'credential_lock', side_effect=credential_lock):
            worker, _, errors = self.thread(lambda: self.manager.run(session))
            self.assertTrue(waiting.wait(5))
            self.manager.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            worker.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.gateway.saved['access_token'], 'FAKE_OLD_ACCESS')
        self.assertFalse(self.gateway.field_patches or self.gateway.patches)
        self.assertEqual(self.manager.outcomes, {})


    def enable_then_fail(self, routing_change=False):
        real = signin.access.enable_after_signin
        def enable(record, call, can_enable=lambda: True):
            if routing_change:
                # An explicit routing change lands first and supersedes the sign-in's enable.
                self.manager.suppress_enable('account-a')
                self.gateway.call('/auth-files/status', {'name': NAME, 'auth_index': 'account-a', 'disabled': False},
                                  method='PATCH')
            else:
                real(record, call, can_enable)
            raise ValueError('synthetic readback failure')
        return patch.object(signin.access, 'enable_after_signin', side_effect=enable)

    def test_enable_error_with_routing_on_is_not_enable_failed(self):
        for routing_change in (False, True):
            with self.subTest(routing_change=routing_change):
                self.setUp()
                with self.enable_then_fail(routing_change):
                    view = self.run_flow()
                self.assertFalse(self.gateway.row['disabled'])
                self.assertEqual(view['last_outcome'], {'kind': 'signed_in', 'at': ANY, 'enabled': False})

    def test_enable_error_while_still_wanted_and_off_is_enable_failed(self):
        with patch.object(signin.access, 'enable_after_signin', side_effect=ValueError('synthetic')):
            view = self.run_flow()
        self.assertTrue(self.gateway.row['disabled'])
        self.assertEqual(view['last_outcome'],
                         {'kind': 'signed_in', 'at': ANY, 'enabled': False, 'enable_failed': True})
        now = self.clock[0]
        account = {'disabled': True, 'quota_attempted_at': routing_policy.stamp(now), 'signin': view}
        gateway = {'online': True, 'checked_at': routing_policy.stamp(now)}
        self.assertEqual(operations.account_health(account, gateway, now), 'enable_failed')
        account['signin']['last_outcome'].pop('enable_failed')
        self.assertEqual(operations.account_health(account, gateway, now), 'intentionally_disabled')

    def test_enable_timeout_before_the_status_patch_is_a_completion_failure(self):
        real = signin.access.enable_after_signin
        def enable(record, call, can_enable):
            def listing_timeout(path, *args, **kwargs):
                self.assertEqual(path, '/auth-files')
                raise TimeoutError('synthetic initial enable listing timeout')
            return real(record, listing_timeout, can_enable)
        with patch.object(signin.access, 'enable_after_signin', side_effect=enable):
            view, lines = self.journal(self.run_flow)
        self.assertEqual(view['last_outcome'],
                         {'kind': 'signed_in', 'at': ANY, 'enabled': False, 'enable_failed': True})
        self.assertTrue(lines[0]['checks']['timed_out'])
        self.assertTrue(lines[0]['checks']['enable_failed'])
        self.assertFalse(self.gateway.patches)

    def test_completion_enable_timeout_checks_routing_and_respects_a_deliberate_change(self):
        for routing_on, suppressed in ((True, False), (False, True)):
            with self.subTest(routing_on=routing_on, suppressed=suppressed):
                self.setUp()
                def enable(*args, **kwargs):
                    if routing_on:
                        self.gateway.row['disabled'] = self.gateway.saved['disabled'] = False
                    if suppressed:
                        self.manager.suppress_enable('account-a')
                    raise TimeoutError('synthetic enable timeout')
                with patch.object(self.manager, 'enable', side_effect=enable):
                    view, lines = self.journal(self.run_flow)
                self.assertEqual(view['last_outcome'], {'kind': 'signed_in', 'at': ANY, 'enabled': False})
                self.assertTrue(lines[0]['checks']['timed_out'])
                self.assertNotIn('enable_failed', lines[0]['checks'])

    def test_completion_readback_timeout_treats_unknown_routing_as_a_failure(self):
        for error in (TimeoutError('synthetic'), ValueError('synthetic')):
            for suppressed in (False, True):
                with self.subTest(error=type(error).__name__, suppressed=suppressed):
                    self.setUp()
                    def disabled(*args, **kwargs):
                        if suppressed:
                            self.manager.suppress_enable('account-a')
                        raise TimeoutError('synthetic disabled readback timeout')
                    with patch.object(self.manager, 'enable', side_effect=error), \
                            patch.object(self.manager, 'still_disabled', side_effect=disabled):
                        view, lines = self.journal(self.run_flow)
                    self.assertEqual(view['last_outcome'].get('enable_failed', False), not suppressed)
                    self.assertEqual(view['last_outcome']['kind'], 'signed_in')
                    self.assertTrue(lines[0]['checks']['timed_out'])

    def test_expired_completion_deadline_records_enable_failure_without_extra_gateway_calls(self):
        for error in (TimeoutError('synthetic'), ValueError('synthetic')):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                now = [0.0]
                deadline = RequestDeadline(45, clock=lambda: now[0])
                calls = []
                def enable(*args, **kwargs):
                    now[0] = 100
                    calls.append(len(self.gateway.calls))
                    raise error
                with patch.object(signin, 'RequestDeadline', return_value=deadline), \
                        patch.object(self.manager, 'enable', side_effect=enable):
                    view, lines = self.journal(self.run_flow)
                self.assertEqual(view['last_outcome'],
                                 {'kind': 'signed_in', 'at': ANY, 'enabled': False, 'enable_failed': True})
                self.assertEqual(len(self.gateway.calls), calls[0])
                self.assertTrue(lines[0]['checks']['timed_out'])

    def test_convergence_ignores_the_proxy_setting(self):
        _, session = self.begin()
        self.gateway.proxy = 'http://127.0.0.1:9'
        calls = len(self.gateway.calls)
        self.manager.run(session)
        self.assertEqual(self.outcome(), {'kind': 'signed_in', 'at': ANY, 'enabled': True})
        patched = next(i for i, c in enumerate(self.gateway.calls) if c[0] == '/auth-files/fields')
        self.assertFalse(any(c[0] == '/config' for c in self.gateway.calls[patched:]))
        self.assertGreater(patched, calls)

    def test_journal_failures_are_not_fatal(self):
        self.gateway.stale_patches = 2
        with patch('builtins.print', side_effect=OSError('journal unavailable')):
            self.assertEqual(self.run_flow()['last_outcome']['kind'], 'saved_unconfirmed')
            self.changed.assert_called_once_with('account-a')
        self.assertFalse(self.view()['retry_allowed'])

    def test_unwritable_auth_dir_fails_before_the_device_flow(self):
        os.chmod(self.gateway.auth_dir, 0o500)
        self.addCleanup(os.chmod, self.gateway.auth_dir, 0o700)
        if os.access(self.gateway.auth_dir, os.W_OK):
            self.skipTest('Running with privileges that ignore directory modes')
        with patch.object(signin.threading, 'Thread') as thread:
            view = self.manager.start('account-a', self.gateway.identity)
        self.assertEqual(view['last_outcome'], {'kind': 'save_unavailable', 'at': ANY})
        self.assertEqual(self.device_calls(), [])
        thread.assert_not_called()

    def test_auth_dir_check_uses_the_writer_directory(self):
        with patch.object(signin.os, 'access', return_value=False) as access:
            view = self.manager.start('account-a', self.gateway.identity)
        access.assert_called_once_with(self.gateway.auth_dir, os.W_OK)
        self.assertEqual(view['last_outcome']['kind'], 'save_unavailable')

    def test_every_save_blocker_fails_at_start_before_the_device_flow(self):
        def other_owner():
            real = store.os.fstat
            def fstat(fd):
                info = real(fd)
                if os.readlink('/proc/self/fd/%d' % fd).endswith(NAME):
                    fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                    return SimpleNamespace(**dict(fields, st_uid=info.st_uid + 1))
                return info
            return patch.object(store.os, 'fstat', side_effect=fstat)
        def fifo():
            os.unlink(self.gateway.path)
            os.mkfifo(self.gateway.path, 0o600)
        auth = lambda name: os.path.join(self.gateway.auth_dir, name)
        cases = {
            'name': lambda: self.gateway.row.update(name='existing-paid-name.json', path=auth('existing-paid-name.json')),
            'path': lambda: self.gateway.row.update(path=auth('codex-other.json')),
            'source': lambda: self.gateway.row.update(source='memory'),
            'runtime_only': lambda: self.gateway.row.update(runtime_only=True),
            'not_regular': fifo,
            'siwc': lambda: self.gateway.saved.update(siwc={}),
            'use_siwc': lambda: self.gateway.saved.update(use_siwc=False),
            'type': lambda: self.gateway.saved.update(type='claude'),
            'file_owner': other_owner,
            'dir_owner': lambda: patch.object(store.os, 'geteuid', return_value=os.geteuid() + 1),
        }
        for case, change in cases.items():
            with self.subTest(case=case):
                self.setUp()
                context = change() or contextlib.nullcontext()
                output = io.StringIO()
                with context, patch.object(signin.threading, 'Thread') as thread, contextlib.redirect_stdout(output):
                    view = self.manager.start('account-a', self.gateway.identity)
                self.assertEqual(view['last_outcome'], {'kind': 'save_unavailable', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertEqual(self.device_calls(), [])
                thread.assert_not_called()
                self.assertFalse(os.path.exists(self.gateway.path + '.lock'))
                self.assertEqual([n for n in os.listdir(self.gateway.auth_dir) if n != NAME], [])
                line, = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(line['kind'], 'save_unavailable')
                self.assertIn(line['checks']['reason'], ('invalid_name', 'path_mismatch', 'not_file', 'not_regular',
                                                         'siwc_present', 'not_codex', 'wrong_owner'))

    def test_save_requires_an_explicit_file_source_and_path(self):
        for change in ({'source': None}, {'path': ''}, {'path': None}, {'source': 'memory'}):
            with self.subTest(change=change):
                self.setUp()
                _, session = self.begin()
                for key, value in change.items():
                    if value is None:
                        self.gateway.row.pop(key)
                    else:
                        self.gateway.row[key] = value
                session['identity'] = self.gateway.identity
                self.manager.run(session)
                # A listing that is not a file credential will not change; no Try again.
                self.assertEqual(self.manager.outcomes['account-a']['kind'], 'save_unavailable')
                self.assertFalse(self.view()['retry_allowed'])
                self.assertEqual(self.gateway.saved['access_token'], 'FAKE_OLD_ACCESS')
                self.assertFalse(self.gateway.field_patches)

    def untrusted_locks(self):
        lock = lambda: self.gateway.path + '.lock'
        def symlink():
            os.symlink(os.path.join(self.gateway.auth_dir, 'elsewhere'), lock())
            return lambda: os.path.islink(lock()) and not os.path.lexists(os.path.join(self.gateway.auth_dir, 'elsewhere'))
        def directory():
            os.mkdir(lock())
            return lambda: os.path.isdir(lock()) and os.listdir(lock()) == []
        def fifo():
            os.mkfifo(lock(), 0o600)
            return lambda: stat.S_ISFIFO(os.lstat(lock()).st_mode)
        def other_owner():
            with open(lock(), 'w'):
                pass
            before = os.stat(lock())
            real = store.os.stat
            def fake(path, *args, **kwargs):
                info = real(path, *args, **kwargs)
                if str(path).endswith('.lock'):
                    fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                    return SimpleNamespace(**dict(fields, st_uid=info.st_uid + 1))
                return info
            unchanged = lambda: store.fingerprint(os.stat(lock())) == store.fingerprint(before)
            return unchanged, patch.object(store.os, 'stat', side_effect=fake)
        return {'symlink': symlink, 'directory': directory, 'fifo': fifo, 'other_owner': other_owner}

    def test_an_untrusted_lock_file_fails_at_start_before_the_device_flow(self):
        for case, make in self.untrusted_locks().items():
            with self.subTest(case=case):
                self.setUp()
                made = make()
                unchanged, context = made if isinstance(made, tuple) else (made, contextlib.nullcontext())
                output = io.StringIO()
                with context, patch.object(signin.threading, 'Thread') as thread, contextlib.redirect_stdout(output):
                    view = self.manager.start('account-a', self.gateway.identity)
                self.assertEqual(view['last_outcome'], {'kind': 'save_unavailable', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertEqual(self.device_calls(), [])
                thread.assert_not_called()
                self.assertTrue(unchanged())
                self.assertEqual(sorted(os.listdir(self.gateway.auth_dir)), sorted([NAME, NAME + '.lock']))
                line, = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(line['checks'], {'reason': 'lock_untrusted'})

    def test_a_trusted_existing_lock_file_passes_the_preflight(self):
        with open(self.gateway.path + '.lock', 'w'):
            pass
        os.chmod(self.gateway.path + '.lock', 0o600)
        self.assertEqual(self.run_flow()['last_outcome']['kind'], 'signed_in')

    def test_a_lock_that_turns_untrusted_after_start_offers_no_retry(self):
        for case, make in self.untrusted_locks().items():
            with self.subTest(case=case):
                self.setUp()
                _, session = self.begin()
                made = make()
                _, context = made if isinstance(made, tuple) else (made, contextlib.nullcontext())
                if case == 'other_owner':
                    # The writer checks the opened lock with fstat.
                    real = store.os.fstat
                    def fstat(fd):
                        info = real(fd)
                        if os.readlink('/proc/self/fd/%d' % fd).endswith('.lock'):
                            fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                            return SimpleNamespace(**dict(fields, st_uid=info.st_uid + 1))
                        return info
                    context = patch.object(store.os, 'fstat', side_effect=fstat)
                with context:
                    self.manager.run(session)
                view = self.view()
                self.assertEqual(view['last_outcome'], {'kind': 'save_unavailable', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertEqual(self.gateway.saved['access_token'], 'FAKE_OLD_ACCESS')
                self.assertFalse(self.gateway.field_patches)

    def refresh_after_write(self, label="rotated", **claims):
        """A gateway refresh rotates the new tokens on disk between the write and converge()."""
        real = store.replace
        def replace(*args, **kwargs):
            real(*args, **kwargs)
            self.clock[0] += 1
            self.gateway.saved.update(id_token=jwt(jti=label, **claims), access_token='FAKE_REFRESHED_ACCESS',
                                      refresh_token='FAKE_REFRESHED_REFRESH', last_refresh=store.stamp(self.clock[0]),
                                      expired=store.stamp(self.clock[0] + 3600))
        return patch.object(signin.credential_replace, 'replace', side_effect=replace)

    def test_rotated_disk_tokens_that_are_not_this_account_stay_unconfirmed(self):
        cases = {'identity': lambda: self.refresh_after_write(email='other@example.com'),
                 'older': lambda: self.refresh_after_write()}
        for case, refresh in cases.items():
            with self.subTest(case=case):
                self.setUp()
                if case == 'older':
                    real_stamp = store.stamp
                    stamp = patch.object(store, 'stamp', side_effect=lambda epoch: real_stamp(epoch - 3600))
                    # Only the refresh's own stamp is backdated: patch after the write's stamps are taken.
                    real = store.replace
                    def replace(*args, **kwargs):
                        real(*args, **kwargs)
                        with stamp:
                            self.gateway.saved.update(access_token='FAKE_ROTATED_ACCESS', refresh_token='FAKE_ROTATED_REFRESH',
                                                      id_token=jwt(jti='rotated'), last_refresh=store.stamp(self.clock[0]))
                    context = patch.object(signin.credential_replace, 'replace', side_effect=replace)
                else:
                    context = refresh()
                output = io.StringIO()
                with context, contextlib.redirect_stdout(output):
                    view = self.run_flow()
                self.assertEqual(view['last_outcome'], {'kind': 'saved_unconfirmed', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertFalse(self.gateway.field_patches)
                self.assertFalse(self.gateway.patches)
                self.assertEqual(self.gateway.runtime['access_token'], 'FAKE_OLD_ACCESS')
                line, = [json.loads(line) for line in output.getvalue().splitlines()]

    def test_lasting_save_refusals_offer_no_retry_and_transient_ones_do(self):
        source = Path(store.__file__).read_text()
        codes = set(__import__('re').findall(r"ReplaceError\('([a-z_]+)'\)", source)) | set(store.OPEN_ERRORS.values())
        transient = {'lock_timeout', 'changed', 'unreadable', 'invalid_json', 'invalid_tokens'}
        self.assertEqual(codes, set(store.LASTING) | transient)
        self.assertFalse(set(store.LASTING) & transient)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.setUp()
                _, session = self.begin()
                output = io.StringIO()
                with patch.object(signin.credential_replace, 'replace', side_effect=store.ReplaceError(code)), \
                        contextlib.redirect_stdout(output):
                    self.manager.run(session)
                view = self.view()
                kind = 'save_unavailable' if code in store.LASTING else 'save_failed'
                self.assertEqual(view['last_outcome'], {'kind': kind, 'at': ANY})
                self.assertEqual(view['retry_allowed'], code not in store.LASTING)
                self.assertFalse(self.gateway.field_patches)
                line, = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual((line['kind'], line['checks']), (kind, {'reason': code}))

    def test_an_unwritable_auth_dir_is_journalled_with_its_reason(self):
        output = io.StringIO()
        with patch.object(signin.os, 'access', return_value=False), contextlib.redirect_stdout(output):
            view = self.manager.start('account-a', self.gateway.identity)
        self.assertEqual(view['last_outcome'], {'kind': 'save_unavailable', 'at': ANY})
        line, = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual((line['kind'], line['checks']), ('save_unavailable', {'reason': 'auth_dir_unwritable'}))

    # Sweep fixes. A3: only a lasting preflight refusal hides the button at start.

    def journal(self, action):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = action()
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_a_transient_preflight_refusal_at_start_is_retryable(self):
        for code, kind in (('unreadable', 'save_failed'), ('invalid_json', 'save_failed'),
                           ('too_large', 'save_unavailable'), ('no_access', 'save_unavailable')):
            with self.subTest(code=code):
                self.setUp()
                with patch.object(signin.credential_replace, 'preflight', side_effect=store.ReplaceError(code)), \
                        patch.object(signin.threading, 'Thread') as thread:
                    view, lines = self.journal(lambda: self.manager.start('account-a', self.gateway.identity))
                self.assertEqual(view['last_outcome'], {'kind': kind, 'at': ANY})
                self.assertEqual(view['retry_allowed'], kind == 'save_failed')
                self.assertEqual(self.device_calls(), [])
                thread.assert_not_called()
                self.assertEqual([(l['kind'], l['checks']) for l in lines], [(kind, {'reason': code})])
                if kind == 'save_failed':
                    # The cause has passed: the next click starts the device flow.
                    with patch.object(signin.threading, 'Thread'):
                        self.assertIsNotNone(self.manager.start('account-a', self.gateway.identity)['session'])
                    self.assertEqual(len(self.device_calls()), 1)

    def test_a_disk_identity_that_does_not_match_the_listing_fails_before_the_device_flow(self):
        for change in ({'email': 'two@example.com', 'id_token': jwt(email='two@example.com', jti='old-signin')},
                       {'account_id': 'workspace-b', 'id_token': jwt(workspace='workspace-b', jti='old-signin')}):
            with self.subTest(change=sorted(change)):
                self.setUp()
                self.gateway.saved.update(change)
                with patch.object(signin.threading, 'Thread') as thread:
                    view, lines = self.journal(lambda: self.manager.start('account-a', self.gateway.identity))
                self.assertEqual(view['last_outcome'], {'kind': 'identity_unavailable', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertEqual(self.device_calls(), [])
                thread.assert_not_called()
                self.assertEqual(lines[0]['checks'], {'reason': 'identity_unavailable'})

    def test_start_failures_journal_a_fixed_reason(self):
        def fail(failing, response=None):
            original = self.gateway.call
            def call(path, body=None, method=None, timeout=None):
                if failing(path, body):
                    if response is None:
                        raise OSError('synthetic failure')
                    return response
                return original(path, body, method)
            self.manager.call = call
        cases = {
            'proxy_configured': lambda: setattr(self.gateway, 'proxy', 'http://127.0.0.1:9'),
            'config_failed': lambda: fail(lambda path, body: path == '/config'),
            'download_failed': lambda: fail(lambda path, body: path.startswith('/auth-files/download')),
            'usercode_failed': lambda: fail(lambda path, body: path == '/api-call' and body['url'] == signin.USERCODE,
                                            {'status_code': 500, 'body': '{}'}),
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                change()
                view, lines = self.journal(lambda: self.manager.start('account-a', self.gateway.identity))
                self.assertEqual(view['last_outcome']['kind'], 'start_failed')
                line, = lines
                self.assertEqual((line['kind'], line['checks']), ('start_failed', {'reason': reason}))
                self.assertIn('seconds', line)
        self.setUp()
        self.gateway.version = '8.0.31'
        view, lines = self.journal(lambda: self.manager.start('account-a', self.gateway.identity))
        self.assertEqual(view['last_outcome'], {'kind': 'gateway_untested', 'at': ANY})
        self.assertEqual(lines[0]['checks'], {'reason': 'gateway_untested', 'version': '8.0.31'})
        self.setUp()
        self.gateway.version = 'bad version\n'
        _, lines = self.journal(lambda: self.manager.start('account-a', self.gateway.identity))
        self.assertEqual(lines[0]['checks'], {'reason': 'gateway_untested', 'version': None})

    def test_first_attempt_and_ended_session_journal_lines(self):
        _, lines = self.journal(self.run_flow)
        line, = lines
        self.assertEqual((line['kind'], line['enabled']), ('signed_in', True))
        self.assertNotIn('retry', line)
        # The token exchange happened but the session ended: a line, nothing recorded or written.
        self.setUp()
        _, session = self.begin()
        self.manager.sessions.pop('account-a')
        before = dict(self.gateway.saved)
        _, lines = self.journal(lambda: self.manager.complete(session, self.gateway.tokens))
        line, = lines
        self.assertEqual((line['kind'], line['recorded'], line['checks']),
                         ('save_failed', False, {'reason': 'session_ended'}))
        self.assertIn('seconds', line)
        self.assertEqual(self.gateway.saved, before)
        # OpenAI's device response is unusable.
        self.setUp()
        _, session = self.begin()
        self.gateway.polls = [(500, {})]
        _, lines = self.journal(lambda: self.manager.run(session))
        self.assertEqual([(l['kind'], l['checks']) for l in lines],
                         [('provider_error', {'reason': 'device_response_invalid'})])

    # D16: completion after the save has a bounded deadline and never rolls back.

    def test_a_hung_gateway_after_the_save_ends_saved_unconfirmed_within_the_deadline(self):
        _, session = self.begin()
        saved, original = threading.Event(), self.gateway.call
        real_replace = store.replace
        def replace(*args, **kwargs):
            real_replace(*args, **kwargs)
            saved.set()
        def call(path, body=None, method=None, timeout=None):
            if saved.is_set():
                time.sleep(timeout)  # Hangs until the per-call timeout, as urlopen would.
                raise TimeoutError('synthetic hang')
            return original(path, body, method)
        self.manager.call = call
        started = time.monotonic()
        with patch.object(signin, 'COMPLETE_SECONDS', 0.6), patch.object(signin.credential_replace, 'replace',
                                                                         side_effect=replace):
            self.manager.run(session)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.outcome(), {'kind': 'saved_unconfirmed', 'at': ANY})
        self.assertEqual(self.gateway.saved['access_token'], 'FAKE_NEW_ACCESS')  # Never rolled back.
        self.assertTrue(self.manager.lifecycle.acquire(timeout=1))
        self.manager.lifecycle.release()
        # The lifecycle lock held elsewhere for too long: the file and informational outcome stay saved.
        self.setUp()
        _, session = self.begin()
        acquired, release = threading.Event(), threading.Event()
        def hold():
            with self.manager.lifecycle:
                acquired.set()
                release.wait(5)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(acquired.wait(5))
        try:
            with patch.object(signin, 'COMPLETE_SECONDS', 0.3):
                _, lines = self.journal(lambda: self.manager.run(session))
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(lines[0]['checks'], {'timed_out': True})
        self.assertEqual(self.outcome(), {'kind': 'saved_unconfirmed', 'at': ANY})
        self.gateway.row['status_message'] = ''
        before = len(self.gateway.calls)
        self.clock[0] += 60
        self.manager.prune([self.gateway.row])
        self.assertEqual(self.outcome(), {'kind': 'saved_unconfirmed', 'at': ANY})
        self.assertFalse(self.view()['retry_allowed'])
        self.assertEqual(len(self.gateway.calls), before)
        self.assertFalse(self.gateway.patches)
        self.assertEqual(self.gateway.saved['access_token'], 'FAKE_NEW_ACCESS')

    def test_rotated_or_replaced_tokens_after_write_are_never_confirmed_or_enabled(self):
        # A newer timestamp and matching identity cannot prove who replaced the tokens.
        for label in ('gateway-refresh', 'independent-signin'):
            with self.subTest(label=label):
                self.setUp()
                with self.refresh_after_write(label=label):
                    view = self.run_flow()
                self.assertEqual(view['last_outcome'], {'kind': 'saved_unconfirmed', 'at': ANY})
                self.assertFalse(view['retry_allowed'])
                self.assertTrue(self.gateway.saved['disabled'])
                self.assertFalse(self.gateway.patches)
                self.assertFalse(self.gateway.field_patches)

    def test_rotation_during_verification_does_not_confirm_or_enable(self):
        rotated = []
        def rotate():
            if not rotated:
                rotated.append(True)
                self.gateway.saved.update(id_token=jwt(jti='rotated'), access_token='FAKE_ROTATED_ACCESS',
                                          refresh_token='FAKE_ROTATED_REFRESH',
                                          last_refresh=store.stamp(self.clock[0] + 1))
        self.gateway.fields_hook = rotate
        view = self.run_flow()
        self.assertEqual(view['last_outcome'], {'kind': 'saved_unconfirmed', 'at': ANY})
        self.assertFalse(view['retry_allowed'])
        self.assertEqual(self.gateway.saved['refresh_token'], 'FAKE_ROTATED_REFRESH')
        self.assertEqual(self.gateway.runtime['access_token'], 'FAKE_ROTATED_ACCESS')
        self.assertTrue(self.gateway.saved['disabled'])
        self.assertFalse(self.gateway.patches)
        self.assertEqual(len(self.gateway.field_patches), 2)

    def test_rotation_that_keeps_refresh_token_stays_unconfirmed(self):
        real = store.replace
        def replace(*args, **kwargs):
            real(*args, **kwargs)
            self.gateway.saved.update(id_token=jwt(jti='rotated'), access_token='FAKE_ROTATED_ACCESS',
                                      last_refresh=store.stamp(self.clock[0] + 1))
        with patch.object(signin.credential_replace, 'replace', side_effect=replace):
            view = self.run_flow()
        self.assertEqual(view['last_outcome'], {'kind': 'saved_unconfirmed', 'at': ANY})
        self.assertEqual(self.gateway.saved['refresh_token'], 'FAKE_NEW_REFRESH')
        self.assertFalse(self.gateway.patches)

    def test_later_authenticated_poll_keeps_unconfirmed_outcome_and_routing_off(self):
        self.unconfirmed()
        original = dict(self.manager.outcomes['account-a'])
        self.gateway.row.update(status_message='', disabled=True)
        self.gateway.runtime.update(self.gateway.saved)  # The gateway watcher reloads it.
        self.manager.prune([self.gateway.row])
        self.assertEqual(self.view()['last_outcome']['kind'], 'saved_unconfirmed')
        self.assertEqual(self.manager.outcomes['account-a'], original)
        self.assertFalse(self.view()['retry_allowed'])
        self.assertFalse(self.gateway.patches)
        since = len(self.gateway.calls)
        with self.assertRaisesRegex(signin.AccessError, 'signin_not_required'):
            self.manager.start('account-a', self.gateway.identity)
        self.assertFalse(self.device_calls(since))

    def test_later_required_poll_starts_a_fresh_device_flow(self):
        self.unconfirmed()
        self.gateway.row['status_message'] = 'refresh_token_expired'
        since = len(self.gateway.calls)
        with patch.object(signin.threading, 'Thread'):
            view = self.manager.start('account-a', self.gateway.identity)
        self.assertIsNotNone(view['session'])
        self.assertIsNone(view['last_outcome'])
        self.assertEqual(len(self.device_calls(since)), 1)
        self.assertEqual(self.device_calls(since)[0][1]['url'], signin.USERCODE)
        self.assertFalse(self.gateway.patches)

class ManagementVersionTests(unittest.TestCase):
    def test_api_copies_the_gateway_build_header(self):
        class Response(io.BytesIO):
            def __init__(self, body, version):
                super().__init__(body)
                self.headers = {'X-CPA-VERSION': version} if version else {}
        with tempfile.NamedTemporaryFile('w') as key:
            key.write('synthetic-key\n')
            key.flush()
            for version in ('8.0.30', '8.0.43', '8.0.47', '8.0.43-dirty', '8.0.47-dirty', None):
                with self.subTest(version=version), patch.object(app, 'KEY', Path(key.name)), \
                        patch.object(app, 'urlopen', return_value=Response(b'{"proxy-url":""}', version)) as urlopen:
                    result = app.api('/config')
                self.assertEqual(result, {'proxy-url': ''})
                self.assertEqual(result.version, version)
                self.assertEqual(signin.SigninManager.tested(result), version in ('8.0.30', '8.0.43', '8.0.47'))
                self.assertEqual(urlopen.call_args.kwargs, {'timeout': 18})

class FixtureIsolationTests(unittest.TestCase):
    def test_fixtures_never_target_the_real_auth_dir(self):
        import fixture_safety
        self.assertEqual(store.AUTH_DIR, '/var/lib/cliproxyapi/auth')
        gateway = FakeGateway()
        self.assertEqual(store.AUTH_DIR, gateway.auth_dir)
        gateway.close()
        self.assertEqual(store.AUTH_DIR, '/var/lib/cliproxyapi/auth')
        self.assertFalse(os.path.exists(gateway.auth_dir))
        with tempfile.TemporaryDirectory() as directory, patch.object(store, 'AUTH_DIR', store.AUTH_DIR), \
                patch.multiple(app, urlopen=app.urlopen, claude_request=app.claude_request), \
                patch.multiple(app.routing_policy, urlopen=app.routing_policy.urlopen, STATE=app.routing_policy.STATE):
            fixture_safety.isolate(app, Path(directory))
            self.assertEqual(store.AUTH_DIR, str(Path(directory) / 'auth'))
            self.assertTrue(os.path.isdir(store.AUTH_DIR))
        self.assertEqual(store.AUTH_DIR, '/var/lib/cliproxyapi/auth')

if __name__ == '__main__':
    unittest.main()
