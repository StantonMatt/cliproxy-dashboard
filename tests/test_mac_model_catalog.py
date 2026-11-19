import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mac_catalog', ROOT / 'scripts/sync-mac-model-catalog.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def catalog(*slugs):
    return json.dumps({'models': [{'slug': slug, 'visibility': 'list'} for slug in slugs]}).encode()


class CatalogTests(unittest.TestCase):
    def test_offline_invalid_and_rejected_refresh_preserve_last_good_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / 'models.json'
            previous = catalog('old-model')
            target.write_bytes(previous)
            for failure in (subprocess.CalledProcessError(255, 'ssh'),
                            subprocess.TimeoutExpired('ssh', 30)):
                with patch.object(sync, 'fetch_catalog', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        sync.refresh(directory, 'router', 22, Path('codex'), directory)
                self.assertEqual(target.read_bytes(), previous)
            for invalid in (b'', b'{', b'{"models": []}', catalog('duplicate', 'duplicate'),
                            catalog('account/prefixed'), catalog(''),
                            b'{"models": [{"slug":"hidden", "visibility":"hide"}]}',
                            b'x' * (sync.MAX_BYTES + 1)):
                with patch.object(sync, 'fetch_catalog', return_value=invalid):
                    with self.assertRaises((ValueError, KeyError, TypeError)):
                        sync.refresh(directory, 'router', 22, Path('codex'), directory)
                self.assertEqual(target.read_bytes(), previous)
            with patch.object(sync, 'fetch_catalog', return_value=catalog('new-model')):
                with patch.object(sync, 'validate_catalog', side_effect=ValueError('invalid schema')):
                    with self.assertRaises(ValueError):
                        sync.refresh(directory, 'router', 22, Path('codex'), directory)
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(list(directory.glob('.models-*')), [])

    def test_initial_failure_does_not_publish_empty_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with patch.object(sync, 'fetch_catalog', side_effect=OSError('offline')):
                with self.assertRaises(OSError):
                    sync.refresh(directory, 'router', 22, Path('codex'), directory)
            self.assertFalse((directory / 'models.json').exists())

    def test_publish_validates_candidate_before_atomic_replace_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / 'models.json'
            previous = catalog('old-model')
            target.write_bytes(previous)
            fresh = catalog('gpt-6.2-astra', 'gpt-6.2-sol')

            def validate(path, expected, codex, codex_home):
                self.assertEqual(expected, {'gpt-6.2-astra', 'gpt-6.2-sol'})
                self.assertEqual(path.parent, directory)
                self.assertEqual(path.read_bytes(), fresh)
                self.assertEqual(target.read_bytes(), previous)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with patch.object(sync, 'fetch_catalog', return_value=fresh):
                with patch.object(sync, 'validate_catalog', side_effect=validate) as validator:
                    sync.refresh(directory, 'router', 22, Path('codex'), directory)
                    sync.refresh(directory, 'router', 22, Path('codex'), directory)
                    validator.assert_called_once()
            self.assertEqual(target.read_bytes(), fresh)
            self.assertEqual(list(directory.glob('.models-*')), [])

    def test_overlapping_refresh_does_no_network_or_publication(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with (directory / 'sync.lock').open('a') as lock:
                sync.fcntl.flock(lock, sync.fcntl.LOCK_EX | sync.fcntl.LOCK_NB)
                with patch.object(sync, 'fetch_catalog') as fetch:
                    sync.refresh(directory, 'router', 22, Path('codex'), directory)
                    fetch.assert_not_called()

    def test_fetch_has_noninteractive_connection_and_total_timeouts(self):
        with patch.object(sync.subprocess, 'run') as run:
            run.return_value.stdout = catalog('model')
            sync.fetch_catalog('127.0.0.1', 1)
            args, kwargs = run.call_args
            self.assertIn('BatchMode=yes', args[0])
            self.assertIn('ConnectTimeout=8', args[0])
            self.assertIn('ServerAliveCountMax=1', args[0])
            self.assertEqual(args[0][-4:], ['-p', '1', '127.0.0.1', sync.SOURCE_COMMAND])
            self.assertEqual(kwargs['timeout'], 30)
            self.assertTrue(kwargs['check'])
        with self.assertRaises(ValueError):
            sync.fetch_catalog('-ProxyCommand=bad', 22)

    def test_cli_parser_protocol_accepts_exact_list_and_rejects_filtering(self):
        # A real child exercises pipe buffering, notifications and multi-line reads.
        fake = '''#!{python}
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('id') == 1:
        print(json.dumps({{'id': 1, 'result': {{}}}}), flush=True)
    elif message.get('id') == 2:
        sys.stdout.write(json.dumps({{'method':'notification'}}) + '\\n' + json.dumps(
            {{'id':2, 'result': {{'data':[{{'id':'gpt-6.2-astra'}}], 'nextCursor':None}}}}) + '\\n')
        sys.stdout.flush()
'''.format(python=sys.executable)
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            binary = directory / 'codex'
            binary.write_text(fake)
            binary.chmod(0o700)
            sync.validate_catalog(directory / 'candidate', {'gpt-6.2-astra'}, binary, directory)
            with self.assertRaises(ValueError):
                sync.validate_catalog(directory / 'candidate', {'gpt-6.2-sol'}, binary, directory)
            binary.write_text('#!' + sys.executable + '\nraise SystemExit(1)\n')
            with self.assertRaises(ValueError):
                sync.validate_catalog(directory / 'candidate', {'gpt-6.2-sol'}, binary, directory)

    def test_launchagent_runs_at_login_and_every_five_minutes_without_keepalive(self):
        source = ROOT / 'scripts/com.user.codex-router-model-catalog.plist'
        config = plistlib.loads(source.read_bytes().replace(b'__HOME__', b'/Users/test'))
        self.assertEqual(config['StartInterval'], 300)
        self.assertTrue(config['RunAtLoad'])
        self.assertNotIn('KeepAlive', config)
        self.assertEqual(config['ProgramArguments'], [
            '/opt/homebrew/bin/python3', '/Users/test/.local/share/codex-router-models/sync.py'])


if __name__ == '__main__':
    unittest.main()
