import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
import io

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runner = module('test_runner', 'run-tests.py')
nightly = module('nightly_runner', 'nightly-tests.py')


class TestRunnerTests(unittest.TestCase):
    def test_discovery_includes_every_cjs_and_both_billing_timezones(self):
        checks = list(runner.checks())
        names = [name for name, *_ in checks]
        for path in (ROOT / 'tests').glob('*.cjs'):
            self.assertEqual(sum(name.startswith(path.name) for name in names), 2 if path.name == 'test_billing_dates.cjs' else 1)
        self.assertIn('test_billing_dates.cjs TZ=America/Fortaleza', names)
        self.assertIn('test_billing_dates.cjs TZ=America/New_York', names)

    def test_failure_timeout_and_launch_error_continue_to_final_summary(self):
        checks = [('failure', ['false'], {}, 1), ('timeout', ['wait'], {}, 1),
                  ('missing', ['missing'], {}, 1), ('success', ['true'], {}, 1)]
        output = io.StringIO()
        failed = Mock(pid=1234, wait=Mock(return_value=1))
        timed_out = Mock(pid=1235, wait=Mock(side_effect=[subprocess.TimeoutExpired('wait', 1), -9]))
        passed = Mock(pid=1236, wait=Mock(return_value=0))
        with patch.object(runner, 'checks', return_value=iter(checks)), redirect_stdout(output), \
                patch.object(runner.os, 'killpg') as kill, \
                patch.object(runner.subprocess, 'Popen', side_effect=[failed, timed_out, FileNotFoundError(), passed]) as run:
            self.assertEqual(runner.run(), 1)
        self.assertEqual(run.call_count, 4)
        kill.assert_called_once_with(1235, runner.signal.SIGKILL)
        self.assertTrue(all(call.kwargs['start_new_session'] for call in run.call_args_list))
        self.assertIn('1 passed; 3 failed; 4 checks.', output.getvalue())

    def test_real_processes_report_success_failure_and_timeout_then_continue(self):
        checks = [('failure', [sys.executable, '-c', 'raise SystemExit(7)'], {}, 5),
                  ('timeout', [sys.executable, '-c', 'import time; time.sleep(60)'], {}, 0.05),
                  ('success', [sys.executable, '-c', 'pass'], {}, 5)]
        output = io.StringIO()
        with patch.object(runner, 'checks', return_value=iter(checks)), redirect_stdout(output):
            self.assertEqual(runner.run(), 1)
        self.assertIn('FAIL failure (exit 7,', output.getvalue())
        self.assertIn('FAIL timeout (exit 124,', output.getvalue())
        self.assertIn('1 passed; 2 failed; 3 checks.', output.getvalue())

    def test_nightly_runs_source_checkout_through_heavy_notifies_only_failure_and_prunes_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            (logs / 'unrelated.log').write_text('keep')
            for n in range(20):
                (logs / f'tests-20000101T000000-{n:02}.log').write_text('old')
            with patch.object(nightly.site_config, 'load', return_value={'source_checkout': str(ROOT)}), \
                    patch.object(nightly.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch.object(nightly, 'notify') as notify, redirect_stdout(io.StringIO()):
                self.assertEqual(nightly.run(logs), 0)
                command = run.call_args.args[0]
                self.assertEqual(command[0], str(Path.home() / '.local/bin/heavy'))
                self.assertEqual(command[-1], str(ROOT / 'scripts/run-tests.py'))
                self.assertEqual(run.call_args.kwargs['cwd'], ROOT)
                notify.assert_not_called()
                run.return_value.returncode = 1
                self.assertEqual(nightly.run(logs), 1)
                notify.assert_called_once()
                log = notify.call_args.args[0]
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(logs.glob('tests-*.log'))), 14)
            self.assertTrue((logs / 'unrelated.log').exists())

    def test_missing_source_configuration_is_logged_and_notified(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(nightly.site_config, 'load', return_value={}), \
                patch.object(nightly, 'notify', return_value=False) as notify, \
                patch.object(nightly.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
            self.assertEqual(nightly.run(Path(directory)), 1)
            run.assert_not_called()
            self.assertIn('source_checkout', notify.call_args.args[0].read_text())


if __name__ == '__main__':
    unittest.main()
