import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('rollback_dashboard', Path(__file__).resolve().parents[1] / 'scripts/rollback-dashboard.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class RollbackDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs').mkdir()
        self.config = self.root / 'config.yaml'
        self.config.write_bytes(b'enabled config\n')
        self.before = b'original config\n'
        (self.root / 'config.before.yaml').write_bytes(self.before)
        (self.root / 'docs/activation.json').write_text(json.dumps({
            'backup': str(self.root),
            'enabled_sha256': hashlib.sha256(self.config.read_bytes()).hexdigest(),
            'before_sha256': hashlib.sha256(self.before).hexdigest(),
        }))
        for target, value in ((app, 'PROJECT'), (app, 'CONFIG'), (app.routing_policy, 'STATE')):
            mock = patch.object(target, value, self.config if value == 'CONFIG' else self.root)
            mock.start()
            self.addCleanup(mock.stop)

    def test_preview_reports_baseline_or_neutral_without_mutations(self):
        for recorded in (False, True):
            if recorded:
                (self.root / 'baseline.json').write_text('{}')
            with self.subTest(recorded=recorded), patch.object(app.os, 'geteuid', return_value=0), \
                    patch.object(app.subprocess, 'run') as run, \
                    patch.object(app.routing_policy, 'rollback') as rollback, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                app.main([])
            self.assertIn('baseline account priorities' if recorded else 'neutral account priorities', output.getvalue())
            self.assertIn('Disable the routing preference timer', output.getvalue())
            self.assertIn('Suspend saved routing intent until a new selection is saved', output.getvalue())
            run.assert_not_called()
            rollback.assert_not_called()
            self.assertEqual(self.config.read_bytes(), b'enabled config\n')

    def test_apply_stops_routing_before_restoring_priorities_and_config(self):
        events = []
        def run(command, check, **kwargs):
            self.assertTrue(check)
            self.assertEqual(self.config.read_bytes(), b'enabled config\n')
            self.assertEqual(kwargs, {'user': 'cliproxyapi', 'group': 'cliproxyapi'}
                             if command[0] == app.sys.executable else {})
            events.append(command)
        with patch.object(app.os, 'geteuid', return_value=0), \
                patch.object(app.subprocess, 'run', side_effect=run), \
                patch.object(app.routing_policy, 'rollback') as rollback, \
                contextlib.redirect_stdout(io.StringIO()):
            app.main(['--apply'])
        rollback.assert_not_called()
        self.assertEqual(events, [
            ['systemctl', 'disable', '--now', 'personal-codex-router-preference.timer'],
            ['systemctl', 'stop', 'personal-codex-router-preference.service'],
            [app.sys.executable, '/opt/personal-codex-router/dashboard/routing_policy.py', '--rollback'],
            ['systemctl', 'disable', '--now', 'personal-codex-router-usage.timer'],
            ['systemctl', 'stop', 'personal-codex-router-usage.service'],
        ])
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_priority_failure_preserves_enabled_config(self):
        command = [app.sys.executable, '/opt/personal-codex-router/dashboard/routing_policy.py', '--rollback']
        with patch.object(app.os, 'geteuid', return_value=0), \
                patch.object(app.subprocess, 'run', side_effect=[None, None, subprocess.CalledProcessError(1, command)]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                app.main(['--apply'])
        self.assertEqual(run.call_count, 3)
        run.assert_called_with(command, user='cliproxyapi', group='cliproxyapi', check=True)
        self.assertEqual(self.config.read_bytes(), b'enabled config\n')


if __name__ == '__main__':
    unittest.main()
