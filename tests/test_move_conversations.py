"""Dashboard side of "Move all conversations now": request handoff, matching and outcomes."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import server as dashboard

START_INTERVAL = dashboard.MOVE_START_INTERVAL
TESTED = dashboard.MOVE_TESTED_GATEWAYS[0]


class Clock:
    """A fake monotonic clock; sleeping advances it and lets the fake helper run."""

    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.on_sleep = on_sleep

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)
        if self.on_sleep:
            self.on_sleep()


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


class MoveFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'state'
        (self.state / 'claude').mkdir(parents=True)
        self.request_dir = self.root / 'request-dir'
        self.request_dir.mkdir(mode=0o770)
        self.request = self.request_dir / 'request'
        self.result = self.root / 'result.json'
        self.helper_path = self.root / 'move-conversations.py'
        self.helper_path.write_text('')
        for target, name, value in ((dashboard.routing_policy, 'STATE', self.state),
                                    (dashboard, 'MOVE_REQUEST_DIR', self.request_dir),
                                    (dashboard, 'MOVE_RESULT', self.result),
                                    (dashboard, 'MOVE_HELPER', self.helper_path)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Each test starts with no recent request; most run moves back to back, so only throttle tests pace them.
        for patcher in (patch.dict(dashboard.MOVE_THROTTLE, created_at=None),
                        patch.object(dashboard, 'MOVE_START_INTERVAL', 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The dashboard journals each helper result on stderr.
        patcher = patch('sys.stderr', new_callable=io.StringIO)
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        self.gateway_version = TESTED
        self.file_ttl = '1h'
        self.gateway_ttl = '1h'
        self.reloads = True
        self.helper_outcome = 'written'
        self.helper_value = 'auto'
        self.helper_code = 'synthetic'
        self.writes_result = True
        self.helper_removes = True
        self.seen_requests = []
        self.config_calls = 0
        self.clock = Clock(self.fake_helper)

    def write_result(self, inode, outcome='written', code='written', value=None, modified_ns=None, at=None):
        self.result.write_text(json.dumps({'request_inode': inode, 'outcome': outcome, 'code': code,
                                           'value': value, 'at': stamp(time.time()) if at is None else at}))
        if modified_ns is not None:
            os.utime(self.result, ns=(modified_ns, modified_ns))

    def fake_helper(self):
        """Take the request as the helper does: by lstat and unlink, never reading it."""
        try:
            info = os.lstat(self.request)
        except FileNotFoundError:
            return
        self.seen_requests.append({'inode': info.st_ino, 'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode),
                                   'regular': stat.S_ISREG(info.st_mode)})
        if self.helper_removes:
            self.request.unlink()
        if not self.writes_result:
            return
        value = None
        if self.helper_outcome == 'written':
            value = '1h0m0s' if self.file_ttl == '1h0m1s' else '1h0m1s'
            self.file_ttl = value
            if self.reloads:
                self.gateway_ttl = value
        if self.helper_value != 'auto':
            value = self.helper_value
        # A coarse file clock can match the request's time; the real helper starts long after it.
        self.write_result(info.st_ino, self.helper_outcome, self.helper_code, value,
                          modified_ns=info.st_mtime_ns + 1_000_000)

    def call(self, path, timeout=None):
        self.assertEqual(path, '/config')
        self.assertIsNotNone(timeout)
        self.config_calls += 1
        return dashboard.signin.ManagementResponse({'routing': {'session-affinity-ttl': self.gateway_ttl}},
                                                   self.gateway_version)

    def create_request(self):
        fd = dashboard._open_move_request(self.request_dir)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        return info.st_ino, info.st_mtime_ns

    def deadline(self, seconds=45):
        return dashboard.RequestDeadline(seconds=seconds, clock=self.clock)

    def move(self, deadline=None, **kwargs):
        options = dict(call=self.call, clock=self.clock, sleep=self.clock.sleep)
        options.update(kwargs)
        return dashboard.move_conversations(deadline or self.deadline(), **options)


class MoveProtocolTests(MoveFixture):
    def test_moved_request_is_an_empty_file_and_is_consumed(self):
        # Created 0640 under the dashboard unit's UMask=0077, so 0600; the helper only lstats and removes it,
        # which needs the directory's group permission, never the file's.
        old_umask = os.umask(0o077)
        try:
            self.assertEqual(self.move(), {'outcome': 'moved', 'code': 'moved'})
        finally:
            os.umask(old_umask)
        self.assertEqual(self.seen_requests, [{'inode': self.seen_requests[0]['inode'], 'size': 0, 'mode': 0o600,
                                               'regular': True}])
        self.assertEqual(list(self.request_dir.iterdir()), [])
        self.assertEqual(self.gateway_ttl, '1h0m1s')

    def test_helper_outcomes_map_to_stable_codes(self):
        for outcome, code, expected in (
                ('refused', 'too_soon', ('not_moved', 'too_soon')),
                ('refused', 'stale_request', ('not_moved', 'helper_refused')),
                ('refused', 'unsupported_ttl', ('not_moved', 'helper_refused')),
                ('refused', None, ('not_moved', 'helper_refused')),
                # Failed means nothing was written.
                ('failed', 'backup_failed', ('not_moved', 'helper_failed')),
                ('uncertain', 'readback_mismatch', ('uncertain', 'error')),
                ('bogus', 'synthetic', ('uncertain', 'error'))):
            with self.subTest(outcome=outcome, code=code), \
                    contextlib.redirect_stderr(io.StringIO()) as log:
                self.helper_outcome, self.helper_code = outcome, code
                self.assertEqual(self.move(), dict(zip(('outcome', 'code'), expected)))
                # The helper's own code goes to the journal, never to the frontend.
                line = json.loads(log.getvalue())
                self.assertEqual(line, {'event': 'move_conversations', 'helper_code': code,
                                        'helper_outcome': outcome})

    def test_consume_failed_is_not_moved_only_after_a_confirmed_withdrawal(self):
        self.helper_outcome, self.helper_code = 'failed', 'consume_failed'
        # The helper couldn't remove the request; the dashboard removes its own, so no later run can take it.
        self.helper_removes = False
        self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'helper_failed'})
        self.assertEqual(list(self.request_dir.iterdir()), [])
        # The request is gone without our withdrawal: a retriggered helper run may have taken it.
        self.helper_removes = True
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'error'})
        # Our request is still there and can't be removed, so a later run may still take it.
        self.helper_removes = False
        with patch.object(dashboard, '_withdraw_move_request', return_value='kept'):
            self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'error'})
        self.request.unlink()

    def test_other_refusals_and_failures_need_the_request_gone(self):
        for outcome, code, expected in (('refused', 'too_soon', 'too_soon'), ('refused', 'stale_request', 'helper_refused'),
                                        ('failed', 'backup_failed', 'helper_failed')):
            with self.subTest(outcome=outcome, code=code):
                self.helper_outcome, self.helper_code = outcome, code
                self.helper_removes = True
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': expected})
                # A result for a request still present that the dashboard removes is still definite.
                self.helper_removes = False
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': expected})
                self.assertEqual(list(self.request_dir.iterdir()), [])
                # One that stays and can't be withdrawn could start another run.
                with patch.object(dashboard, '_withdraw_move_request', return_value='kept'):
                    self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'error'})
                self.request.unlink()

    def test_unsafe_helper_codes_are_not_logged(self):
        self.helper_outcome, self.helper_code = 'refused', 'Not A Code\nforged'
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'helper_refused'})
        self.assertIsNone(json.loads(log.getvalue())['helper_code'])

    def test_helper_starts_are_throttled_but_refusals_hold_nothing_back(self):
        self.assertEqual(START_INTERVAL, 2.0)
        with patch.object(dashboard, 'MOVE_START_INTERVAL', START_INTERVAL):
            self.assertEqual(self.move()['outcome'], 'moved')
            # A request inside the interval never creates a file, so it can't start the helper.
            self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'too_soon'})
            self.assertEqual((len(self.seen_requests), list(self.request_dir.iterdir())), (1, []))
            # The interval runs from the last request file, not from the last move.
            self.clock.now = dashboard.MOVE_THROTTLE['created_at'] + START_INTERVAL - 0.01
            self.assertEqual(self.move()['code'], 'too_soon')
            self.clock.now += 0.01
            self.helper_outcome, self.helper_code = 'refused', 'too_soon'
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'too_soon'})
            # A refusal holds the next try back only by the start interval; the helper's stored interval decides.
            self.clock.now = dashboard.MOVE_THROTTLE['created_at'] + START_INTERVAL
            self.helper_outcome = 'written'
            self.assertEqual(self.move()['outcome'], 'moved')
            self.assertEqual(len(self.seen_requests), 3)
            # Failures before any file exists don't start the helper, so they don't count.
            self.clock.now += START_INTERVAL
            self.request.write_text('earlier')
            self.assertEqual(self.move()['code'], 'in_progress')
            self.request.unlink()
            self.assertEqual(self.move()['outcome'], 'moved')
        self.assertFalse(hasattr(dashboard, 'MOVE_MIN_INTERVAL'))

    def test_result_for_another_request_is_ignored(self):
        self.clock.on_sleep = lambda: (self.fake_helper(), self.write_result(
            12345678, value='1h0m1s', modified_ns=time.time_ns() + 10**9))
        self.writes_result = False
        started = self.clock()
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'helper_timeout'})
        self.assertLessEqual(self.clock() - started, dashboard.MOVE_RESULT_WAIT + 0.1)
        self.assertEqual(self.gateway_ttl, '1h')

    def test_stale_result_with_a_reused_inode_is_ignored(self):
        inode, created_ns = self.create_request()
        self.request.unlink()
        created = created_ns / 1e9
        cases = {
            'older file': dict(modified_ns=created_ns - 10**9, at=stamp(created + 1)),
            'same file time': dict(modified_ns=created_ns, at=stamp(created + 1)),
            'older timestamp': dict(modified_ns=created_ns + 10**9, at=stamp(created - 1)),
            'naive timestamp': dict(modified_ns=created_ns + 10**9, at=dt.datetime.now().isoformat()),
        }
        for name, times in cases.items():
            with self.subTest(name):
                self.write_result(inode, value='1h0m1s', **times)
                self.assertIsNone(dashboard._read_move_result(inode, created_ns, self.result))
        self.write_result(inode, value='1h0m1s', modified_ns=created_ns + 1, at=stamp(created + 0.001))
        self.assertEqual(dashboard._read_move_result(inode, created_ns, self.result)['request_inode'], inode)
        self.assertIsNone(dashboard._read_move_result(inode + 1, created_ns, self.result))
        for content in ('', 'not json', '[]', '{"request_inode": 1}'):
            with self.subTest(content=content):
                self.result.write_text(content)
                self.assertIsNone(dashboard._read_move_result(1, 0, self.result))

    def test_stale_result_is_ignored_during_a_move(self):
        # The helper takes this request but its result predates it, as a stale result on a reused inode would.
        def stale():
            if os.path.lexists(self.request):
                info = os.lstat(self.request)
                self.request.unlink()
                self.write_result(info.st_ino, value='1h0m1s', modified_ns=info.st_mtime_ns - 10**9)
        self.clock.on_sleep = stale
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'helper_timeout'})
        self.assertEqual(self.config_calls, 1, 'an unmatched result is never confirmed')

    def test_untaken_request_stops_early_as_helper_not_started(self):
        self.clock.on_sleep = None
        started = self.clock()
        self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'helper_not_started'})
        elapsed = self.clock() - started
        self.assertGreaterEqual(elapsed, dashboard.MOVE_START_WAIT)
        self.assertLessEqual(elapsed, dashboard.MOVE_START_WAIT + 0.1)
        self.assertEqual(dashboard.MOVE_START_WAIT, 2.0)
        self.assertEqual(list(self.request_dir.iterdir()), [], 'the untaken request is withdrawn')

    def test_taken_request_without_result_is_helper_timeout(self):
        self.writes_result = False
        started = self.clock()
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'helper_timeout'})
        self.assertEqual(len(self.seen_requests), 1)
        self.assertEqual(dashboard.MOVE_RESULT_WAIT, 8.0)
        self.assertGreaterEqual(self.clock() - started, dashboard.MOVE_RESULT_WAIT)
        self.assertLessEqual(self.clock() - started, dashboard.MOVE_RESULT_WAIT + 0.1)

    def test_taken_request_keeps_waiting_past_the_early_stop(self):
        taken = []
        def slow_helper():
            if self.clock() - started >= 1 and not taken:
                info = os.lstat(self.request)
                self.request.unlink()
                taken.append(info)
            elif taken and self.clock() - started >= 3 and not self.result.exists():
                value = '1h0m1s'
                self.gateway_ttl = value
                self.write_result(taken[0].st_ino, value=value, modified_ns=taken[0].st_mtime_ns + 1)
        started = self.clock()
        self.clock.on_sleep = slow_helper
        self.assertEqual(self.move(), {'outcome': 'moved', 'code': 'moved'})

    def test_request_is_withdrawn_on_every_exit_including_exceptions(self):
        # A withdrawn request was never taken, so the error is a definite not_moved.
        for name, patcher in (
                ('result read raises', patch.object(dashboard, '_read_move_result', side_effect=RuntimeError('x'))),
                ('sleep raises', patch.object(self.clock, 'on_sleep', Mock(side_effect=RuntimeError('x'))))):
            with self.subTest(name), patcher:
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'error'})
                self.assertEqual(list(self.request_dir.iterdir()), [])
        with patch.object(self.clock, 'on_sleep', Mock(side_effect=KeyboardInterrupt())):
            with self.assertRaises(KeyboardInterrupt):
                self.move()
        self.assertEqual(list(self.request_dir.iterdir()), [])
        self.assertEqual(self.seen_requests, [])
        for name, setup, expected in (
                ('not started', lambda: setattr(self.clock, 'on_sleep', None), 'helper_not_started'),
                ('whole wait', lambda: (setattr(self.clock, 'on_sleep', None),
                                        setattr(dashboard, 'MOVE_START_WAIT', 60)), 'helper_not_started'),
                ('moved', lambda: setattr(self.clock, 'on_sleep', self.fake_helper), 'moved')):
            with self.subTest(name), patch.object(dashboard, 'MOVE_START_WAIT', dashboard.MOVE_START_WAIT):
                setup()
                self.assertEqual(self.move()['code'], expected)
                self.assertEqual(list(self.request_dir.iterdir()), [])

    def test_withdrawal_leaves_a_different_request_alone(self):
        def replace():
            if os.path.lexists(self.request) and not self.seen_requests:
                # Allocate the replacement first so it cannot reuse the request's inode.
                other = self.request_dir / 'other'
                other.write_text('another-request')
                os.replace(other, self.request)
                self.seen_requests.append('replaced')
        self.clock.on_sleep = replace
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'helper_timeout'})
        self.assertEqual(self.request.read_text(), 'another-request')
        self.assertEqual(dashboard._withdraw_move_request(self.request_dir, 0), 'gone')
        self.assertTrue(self.request.exists())
        self.request.unlink()
        self.assertEqual(dashboard._withdraw_move_request(self.request_dir, 0), 'gone')
        inode, _ = self.create_request()
        with patch.object(dashboard.os, 'unlink', side_effect=PermissionError('denied')):
            self.assertEqual(dashboard._withdraw_move_request(self.request_dir, inode), 'kept')
        self.assertEqual(dashboard._withdraw_move_request(self.request_dir, inode), 'removed')
        self.assertFalse(os.path.lexists(self.request))

    def test_existing_entry_is_in_progress_and_left_alone(self):
        for name, create in (('file', lambda: self.request.write_text('earlier')),
                             ('directory', lambda: self.request.mkdir()),
                             ('symlink', lambda: self.request.symlink_to(self.helper_path))):
            with self.subTest(name):
                create()
                before = os.lstat(self.request)
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'in_progress'})
                after = os.lstat(self.request)
                self.assertEqual((after.st_ino, after.st_mode), (before.st_ino, before.st_mode))
                self.assertEqual([p.name for p in self.request_dir.iterdir()], ['request'])
                if self.request.is_dir() and not self.request.is_symlink():
                    self.request.rmdir()
                else:
                    self.request.unlink()
        self.assertEqual(self.seen_requests, [])

    def test_request_create_failure_is_not_moved(self):
        for error in (OSError('read-only'), PermissionError('denied')):
            with self.subTest(error=type(error)), patch.object(dashboard, '_open_move_request', side_effect=error):
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'error'})
        self.assertEqual(list(self.request_dir.iterdir()), [])
        self.assertEqual(self.seen_requests, [])
        # The mode comes from the create alone; nothing can fail between creating and recording it.
        with patch.object(dashboard.os, 'fchmod', side_effect=AssertionError('fchmod')):
            self.assertEqual(self.move(), {'outcome': 'moved', 'code': 'moved'})

    def test_errors_after_the_request_exists_are_uncertain_unless_withdrawn(self):
        real_close = os.close
        def close_then_fail(fd):
            real_close(fd)
            raise OSError('close')
        def taken_then_fail(*args):
            self.fake_helper()
            raise RuntimeError('x')
        for name, patcher, expected in (
                # The inode is known, so the dashboard withdraws its own request.
                ('close raises', patch.object(dashboard.os, 'close', side_effect=close_then_fail), 'not_moved'),
                # The helper already took it, so the TTL may have flipped.
                ('taken, then the wait raises', patch.object(dashboard, '_read_move_result',
                                                             side_effect=taken_then_fail), 'uncertain'),
                ('withdrawal unconfirmed', patch.object(dashboard, '_withdraw_move_request', return_value='kept'),
                 'uncertain')):
            with self.subTest(name), patcher:
                if name == 'withdrawal unconfirmed':
                    self.clock.on_sleep = Mock(side_effect=RuntimeError('x'))
                self.assertEqual(self.move(), {'outcome': expected, 'code': 'error'})
                self.clock.on_sleep = self.fake_helper
                for entry in self.request_dir.iterdir():
                    entry.unlink()
        # Without an inode the request can't be withdrawn safely, so it is left for the helper.
        with patch.object(dashboard.os, 'fstat', side_effect=OSError('fstat')):
            self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'error'})
        self.assertEqual([p.name for p in self.request_dir.iterdir()], ['request'])

    def test_missing_value_and_readback_failures_are_uncertain(self):
        self.helper_value = None
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'reload_not_observed'})
        self.helper_value = 'auto'
        self.reloads = False
        started = self.clock()
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'reload_not_observed'})
        self.assertLessEqual(self.clock() - started, dashboard.MOVE_RESULT_WAIT + dashboard.MOVE_RELOAD_WAIT + 1)

    def test_readback_equal_before_never_reports_moved(self):
        # An earlier flip to 1h0m1s was written but the gateway never reloaded it.
        self.file_ttl, self.gateway_ttl, self.reloads = '1h0m1s', '1h', False
        self.assertEqual(self.move(), {'outcome': 'uncertain', 'code': 'reload_not_observed'})
        self.assertEqual(self.config_calls, 1, 'an unprovable readback must not be polled')

    def test_unreadable_ttl_before_writes_nothing(self):
        for call in (Mock(side_effect=OSError('down')),
                     Mock(return_value=dashboard.signin.ManagementResponse({'routing': {}}, TESTED))):
            with self.subTest(call=call):
                self.assertEqual(self.move(call=call), {'outcome': 'not_moved', 'code': 'error'})
        self.assertEqual(self.seen_requests, [])
        self.assertEqual(list(self.request_dir.iterdir()), [])

    def test_deadline_is_checked_once_before_the_request(self):
        self.assertEqual(dashboard.MOVE_MIN_SECONDS, 2 * dashboard.MOVE_TTL_READ + dashboard.MOVE_RESULT_WAIT
                         + dashboard.MOVE_RELOAD_WAIT)
        self.assertEqual(self.move(deadline=self.deadline(dashboard.MOVE_MIN_SECONDS - 0.5)),
                         {'outcome': 'not_moved', 'code': 'no_time'})
        self.assertEqual((self.seen_requests, self.config_calls), ([], 0))
        # A slow TTL read uses up part of the budget; no second check stops the move.
        def slow(path, timeout=None):
            if not self.config_calls:
                self.clock.now += timeout
            return self.call(path, timeout)
        deadline = self.deadline(dashboard.MOVE_MIN_SECONDS)
        self.assertEqual(self.move(deadline=deadline, call=slow), {'outcome': 'moved', 'code': 'moved'})
        # Every wait stays inside the request deadline.
        for name, change in (('no helper', lambda: setattr(self.clock, 'on_sleep', None)),
                             ('no result', lambda: setattr(self, 'writes_result', False)),
                             ('no reload', lambda: setattr(self, 'reloads', False))):
            with self.subTest(name):
                self.clock.on_sleep, self.writes_result, self.reloads = self.fake_helper, True, True
                change()
                deadline = self.deadline(dashboard.MOVE_MIN_SECONDS)
                self.assertNotEqual(self.move(deadline=deadline)['outcome'], 'moved')
                self.assertLessEqual(self.clock(), deadline.ends_at + 0.05)
                self.assertEqual(list(self.request_dir.iterdir()), [])

    def test_untested_gateway_is_unavailable_and_writes_nothing(self):
        for version in ('8.0.7', None, '8.0.30-dirty', '8.0.43-dirty', ''):
            with self.subTest(version=version):
                self.gateway_version = version
                self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'unavailable'})
        # A bare dict has no X-CPA-VERSION, so it is untested too.
        self.assertEqual(self.move(call=Mock(return_value={'routing': {'session-affinity-ttl': '1h'}})),
                         {'outcome': 'not_moved', 'code': 'unavailable'})
        self.assertEqual((self.seen_requests, list(self.request_dir.iterdir())), ([], []))
        self.assertEqual(self.gateway_ttl, '1h')
        # Moves have their own list: sign-in's, verified without touching affinity, never enables or disables them.
        self.assertEqual(dashboard.MOVE_TESTED_GATEWAYS, ('8.0.30', '8.0.43'))
        self.gateway_version = '9.9.9'
        with patch.object(dashboard.signin, 'TESTED_GATEWAYS', ('9.9.9',)):
            self.assertEqual(self.move()['code'], 'unavailable')
        self.gateway_version = TESTED
        with patch.object(dashboard.signin, 'TESTED_GATEWAYS', ()):
            self.assertEqual(self.move()['outcome'], 'moved')
        with patch.object(dashboard, 'MOVE_TESTED_GATEWAYS', ('9.9.9',)):
            self.assertEqual(self.move()['code'], 'unavailable')
        self.assertEqual(self.move()['outcome'], 'moved')

    def test_each_validated_build_can_move(self):
        for version in ('8.0.30', '8.0.43'):
            with self.subTest(version=version):
                self.gateway_version = version
                self.assertEqual(self.move()['outcome'], 'moved')

    def test_unavailable_when_runtime_directory_or_helper_is_missing(self):
        self.assertTrue(dashboard.move_available())
        self.helper_path.unlink()
        self.assertFalse(dashboard.move_available())
        self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'unavailable'})
        self.helper_path.write_text('')
        self.request_dir.rmdir()
        self.assertFalse(dashboard.move_available())
        self.assertEqual(self.move(), {'outcome': 'not_moved', 'code': 'unavailable'})
        self.assertEqual(self.config_calls, 0)
        # Availability is a path check; the dashboard never queries systemd.
        self.assertNotIn('subprocess', vars(dashboard))
        self.assertFalse(hasattr(dashboard, 'move_unit_active'))


class MoveAvailabilityTests(MoveFixture):
    def gateway(self):
        state = dashboard.GatewayState(Mock(), Mock())
        state.signins = Mock()
        state.access = Mock()
        state.access.reconcile.side_effect = lambda files, api: files
        return state

    def snapshot(self, state):
        with patch.object(dashboard, 'PREFERENCE', self.state / 'status.json'), \
                patch.object(dashboard, 'billing_records', return_value={}):
            return state.snapshot()

    def fetch(self, state, version=TESTED):
        def api(path, *args, **kwargs):
            if path == '/auth-files':
                return {'files': []}
            if version is OSError:
                raise OSError('down')
            return dashboard.signin.ManagementResponse({'routing': {'session-affinity-ttl': '1h0m1s'}}, version)
        with patch.object(dashboard, 'api', api), patch.object(dashboard, 'claude_routing_projection', return_value=[]):
            state._fetch_locked()

    def test_snapshot_copies_move_available_without_any_io(self):
        state = self.gateway()
        with patch.object(dashboard, 'move_available', Mock(side_effect=AssertionError('checked in snapshot'))):
            self.assertIs(self.snapshot(state)['move_available'], False)
            state.data['move_available'] = True
            self.assertIs(self.snapshot(state)['move_available'], True)

    def test_background_refresh_computes_move_available(self):
        state = self.gateway()
        self.fetch(state)
        self.assertIs(state.data['online'], True)
        self.assertIs(state.data['move_available'], True)
        # A build read as untested offers no move.
        for version in ('8.0.7', None):
            with self.subTest(version=version):
                self.fetch(state, version)
                self.assertIs(state.data['move_available'], False)
                # A failed config read is no evidence either way, so the last answer stands.
                self.fetch(state, OSError)
                self.assertEqual((state.data['error'], state.data['move_available']),
                                 ('Routing configuration temporarily unavailable', False))
                self.fetch(state)
                self.assertIs(state.data['move_available'], True)
                self.fetch(state, OSError)
                self.assertIs(state.data['move_available'], True)
        # Missing helper paths turn it off even when the config can't be read.
        self.helper_path.unlink()
        self.fetch(state, OSError)
        self.assertIs(state.data['move_available'], False)
        self.helper_path.write_text('')
        self.fetch(state, OSError)
        self.assertIs(state.data['move_available'], False)
        self.helper_path.unlink()
        self.fetch(state)
        self.assertIs(state.data['move_available'], False)
        with patch.object(dashboard, 'move_available', Mock(side_effect=AssertionError('checked in snapshot'))):
            self.assertIs(self.snapshot(state)['move_available'], False)


class RoutingMoveHandlerTests(MoveFixture):
    ENDPOINTS = {'/api/routing': ('codex',), '/api/claude-routing': ('claude',),
                 '/api/routing-all': ('codex', 'claude')}

    def handler(self, path, data):
        body = json.dumps(data).encode() if not isinstance(data, bytes) else data
        handler = object.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {'Host': 'router.localhost', 'X-Router-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.connection = Mock()
        handler.send = Mock()
        handler.server = Mock()
        handler.server.state.snapshot.return_value = {}
        return handler

    def post(self, path, data, results=None, move=None):
        results = results or {}
        def reconcile(overview, value, call=None, provider='codex'):
            result = results.get(provider, {'status': 'active', 'policy': value['mode']})
            if isinstance(result, Exception):
                raise result
            return dict(result)
        handler = self.handler(path, data)
        mover = Mock(return_value={'outcome': 'moved', 'code': 'moved'}) if move is None else move
        with patch.object(dashboard.routing_policy, 'reconcile', side_effect=reconcile) as reconciled, \
                patch.object(dashboard, 'watch_accounts', return_value=[]), \
                patch.object(dashboard, 'move_conversations', mover):
            handler.do_POST()
        code, body = handler.send.call_args.args
        return code, body, mover, reconciled

    def test_absent_and_false_move_keep_the_previous_response_bytes(self):
        for path, providers in self.ENDPOINTS.items():
            for data in ({'mode': 'round-robin'}, {'mode': 'round-robin', 'move': False}):
                with self.subTest(path=path, data=data):
                    code, body, mover, _ = self.post(path, data)
                    parts = {p: {'status': 'active', 'policy': 'round-robin'} for p in providers}
                    expected = json.dumps(parts[providers[0]] if len(providers) == 1 else parts).encode()
                    self.assertEqual((code, body), (200, expected))
                    mover.assert_not_called()

    def test_invalid_move_is_rejected_before_any_change(self):
        for path in self.ENDPOINTS:
            for value in ('true', 1, None, {}, []):
                with self.subTest(path=path, value=value):
                    code, body, mover, reconciled = self.post(path, {'mode': 'round-robin', 'move': value})
                    self.assertEqual((code, json.loads(body)), (400, {'error': 'Invalid routing selection'}))
                    reconciled.assert_not_called()
                    mover.assert_not_called()

    def test_move_position_and_status_codes(self):
        for path, providers in self.ENDPOINTS.items():
            with self.subTest(path=path):
                code, body, mover, _ = self.post(path, {'mode': 'round-robin', 'move': True})
                result = json.loads(body)
                self.assertEqual(code, 200)
                self.assertEqual(result['move'], {'outcome': 'moved', 'code': 'moved'})
                if len(providers) == 2:
                    self.assertEqual(set(result), {'codex', 'claude', 'move'})
                    self.assertNotIn('move', result['codex'])
                else:
                    self.assertEqual(result['status'], 'active')
                mover.assert_called_once()
                self.assertIsInstance(mover.call_args.args[0], dashboard.RequestDeadline)
                # The dashboard's own management call carries the gateway build for the version gate.
                self.assertIs(mover.call_args.kwargs['call'], dashboard.api)

    def test_unchanged_selection_with_move_reapplies_and_moves(self):
        code, body, mover, reconciled = self.post('/api/routing', {'mode': 'round-robin', 'move': True})
        self.assertEqual(code, 200)
        self.assertEqual(reconciled.call_count, 1)
        mover.assert_called_once()

    def test_held_fallback_and_partial_results_do_not_move(self):
        held = {'status': 'held', 'hold_reason': 'Waiting'}
        cases = (('/api/routing', {'codex': held}, 202),
                 ('/api/claude-routing', {'claude': held}, 202),
                 ('/api/routing', {'codex': {'status': 'fallback'}}, 200),
                 ('/api/routing', {'codex': {'status': 'active', 'pending_plan': True}}, 202),
                 ('/api/routing-all', {'claude': held}, 202),
                 ('/api/routing-all', {'codex': {'status': 'fallback'}}, 200))
        for path, results, status in cases:
            with self.subTest(path=path, results=results):
                code, body, mover, _ = self.post(path, {'mode': 'round-robin', 'move': True}, results)
                self.assertEqual(code, status)
                self.assertEqual(json.loads(body)['move'], {'outcome': 'not_moved', 'code': 'not_applied'})
                mover.assert_not_called()

    def test_error_responses_carry_no_move(self):
        # The frontend reads no body from these, so a move field would only contradict the doc.
        cases = (('/api/routing', {'codex': dashboard.routing_policy.Hold('busy')}, 503),
                 ('/api/claude-routing', {'claude': ValueError('bad')}, 400),
                 ('/api/routing', {'codex': TypeError('bad')}, 400),
                 ('/api/routing-all', {'claude': OSError('private')}, 503),
                 ('/api/routing-all', {'codex': ValueError('bad')}, 503))
        for path, results, status in cases:
            with self.subTest(path=path, results=results):
                code, body, mover, _ = self.post(path, {'mode': 'round-robin', 'move': True}, results)
                self.assertEqual(code, status)
                result = json.loads(body)
                self.assertNotIn('move', result)
                for part in result.values():
                    self.assertNotIn('move', part if isinstance(part, dict) else {})
                mover.assert_not_called()

    def test_move_exception_keeps_confirmed_routing_response(self):
        for path in self.ENDPOINTS:
            with self.subTest(path=path):
                code, body, _, _ = self.post(path, {'mode': 'round-robin', 'move': True},
                                             move=Mock(side_effect=RuntimeError('private detail')))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)['move'], {'outcome': 'uncertain', 'code': 'error'})
                self.assertNotIn(b'private detail', body)

    def test_handler_runs_the_real_move_under_its_deadline(self):
        self.clock.now = time.monotonic()
        original = dashboard.move_conversations
        def real(deadline, call=None):
            return original(deadline, call=self.call, clock=time.monotonic,
                            sleep=lambda seconds: (time.sleep(0.001), self.fake_helper()))
        for path in self.ENDPOINTS:
            with self.subTest(path=path):
                code, body, _, _ = self.post(path, {'mode': 'round-robin', 'move': True}, move=real)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)['move'], {'outcome': 'moved', 'code': 'moved'})
                self.assertEqual(list(self.request_dir.iterdir()), [])

    def test_handler_reports_an_exception_mid_wait_by_whether_the_request_was_taken(self):
        original = dashboard.move_conversations
        def taken_then_fail(seconds):
            self.fake_helper()
            raise RuntimeError('private detail')
        for name, sleep, expected in (('withdrawn', Mock(side_effect=RuntimeError('private detail')), 'not_moved'),
                                      ('taken', taken_then_fail, 'uncertain')):
            with self.subTest(name):
                def real(deadline, call=None):
                    return original(deadline, call=self.call, clock=time.monotonic, sleep=sleep)
                code, body, _, _ = self.post('/api/routing', {'mode': 'round-robin', 'move': True}, move=real)
                self.assertEqual((code, json.loads(body)['move']), (200, {'outcome': expected, 'code': 'error'}))
                self.assertNotIn(b'private detail', body)
                self.assertEqual(list(self.request_dir.iterdir()), [])

    def test_retry_endpoint_is_removed(self):
        handler = self.handler('/api/routing-move', {'scope': 'all'})
        handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 404)
        self.assertFalse(hasattr(dashboard.Handler, '_retry_move'))

    def test_origin_and_read_only_gates_still_apply(self):
        for headers in ({'X-Router-Read-Only': '1'}, {'Origin': 'http://evil.test'}):
            handler = self.handler('/api/routing', {'mode': 'round-robin', 'move': True})
            handler.headers.update(headers)
            handler.do_POST()
            self.assertEqual(handler.send.call_args.args[0], 403)


PREVIEW_SCRIPT = '''
import runpy, sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]).parent / 'dashboard')]
import server as app
MOVE_PATHS = ('MOVE_REQUEST_DIR', 'MOVE_RESULT', 'MOVE_HELPER')
PRODUCTION = tuple(str(getattr(app, name)) for name in MOVE_PATHS)
def audit(event, args):
    # Fail closed on any file or process access to a production move path or unit.
    if event in ('open', 'os.link', 'os.unlink', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.chmod', 'subprocess.Popen'):
        text = ' '.join(str(arg) for arg in (args[1] if event == 'subprocess.Popen' else args))
        if any(path in text for path in PRODUCTION):
            raise AssertionError(f'Fixture touched a production move path: {event}')
sys.addaudithook(audit)
class Server:
    def __init__(self, address, handler):
        pass
    def serve_forever(self):
        for name, production in zip(MOVE_PATHS, PRODUCTION):
            path = getattr(app, name)
            assert Path(directory) in path.parents and str(path) != production, (name, path)
            assert not path.exists(), (name, path)
        assert app.move_available() is False
        assert self.state.snapshot()['move_available'] is False
        moved = app.move_conversations(app.RequestDeadline(seconds=45))
        assert moved['code'] == 'unavailable', moved
        print('isolated')
    def server_close(self):
        pass
filename, directory = sys.argv[2:]
with patch.object(app, 'ThreadingHTTPServer', Server):
    sys.argv = [filename, '--port', '18429', '--state-dir', directory]
    runpy.run_path(filename, run_name='__main__')
'''


class MoveFixtureIsolationTests(unittest.TestCase):
    PREVIEWS = ['fast_mode_fixture.py', 'signin_preview.py']

    def test_every_dashboard_preview_entry_point_is_isolated(self):
        # A preview that serves a real dashboard snapshot must go through isolate().
        tests = ROOT / 'tests'
        previews = sorted(path.name for path in tests.glob('*.py') if not path.name.startswith('test_')
                          and 'GatewayState(' in path.read_text() and 'serve_forever' in path.read_text())
        self.assertEqual(previews, self.PREVIEWS)
        for name in previews:
            self.assertIn('isolate(app,', (tests / name).read_text(), name)

    def test_previews_never_reach_production_move_paths(self):
        for filename in self.PREVIEWS:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', PREVIEW_SCRIPT, str(ROOT / 'tests'),
                                         str(ROOT / 'tests' / filename), directory],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('isolated', result.stdout)

    def test_isolate_redirects_every_move_path(self):
        import types
        sys.path.insert(0, str(ROOT / 'tests'))
        self.addCleanup(sys.path.remove, str(ROOT / 'tests'))
        from fixture_safety import isolate
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            signin = types.SimpleNamespace(credential_replace=types.SimpleNamespace(AUTH_DIR=None))
            app = types.SimpleNamespace(routing_policy=types.SimpleNamespace(), signin=signin)
            isolate(app, directory)
            for name in ('MOVE_REQUEST_DIR', 'MOVE_RESULT', 'MOVE_HELPER'):
                self.assertIn(directory, getattr(app, name).parents, name)
                self.assertFalse(getattr(app, name).exists(), name)


if __name__ == '__main__':
    unittest.main()
