import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_gateway_cli', ROOT / 'scripts/claude-gateway.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class ClaudeGatewayCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / 'gateway.key'
        self.key.write_text('synthetic-private-client-key-123456789\n')
        self.key.chmod(0o600)

    def test_only_child_gets_gateway_credentials_and_arguments_survive(self):
        original = {name: 'synthetic-direct-value' for name in cli.CLEAR_ENV}
        original['ANTHROPIC_AUTH_TOKEN'] = 'synthetic-old-gateway-value'
        with patch.dict(os.environ, original), patch.object(cli.os, 'execvpe') as execute:
            before = dict(os.environ)
            cli.run(['--resume', 'session with spaces'], self.key)
            self.assertEqual(dict(os.environ), before)
        program, args, env = execute.call_args.args
        self.assertEqual(program, 'claude')
        self.assertEqual(args, ['claude', '--resume', 'session with spaces'])
        self.assertTrue(all(name not in env for name in cli.CLEAR_ENV))
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], self.key.read_text().strip())
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:8317')
        self.assertEqual(env['ENABLE_TOOL_SEARCH'], 'true')
        self.assertEqual(env['CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING'], '1')

    def test_missing_or_insecure_key_never_falls_back_to_direct_auth(self):
        with patch.object(cli.os, 'execvpe') as execute:
            for mode in (0o644, 0o640, 0o400, 0o700):
                self.key.chmod(mode)
                with self.assertRaises(ValueError):
                    cli.run([], self.key)
            self.key.unlink()
            with self.assertRaises(OSError):
                cli.run([], self.key)
            execute.assert_not_called()

    def test_symlink_or_malformed_key_is_refused(self):
        alias = self.key.with_name('alias')
        alias.symlink_to(self.key)
        with patch.object(cli.os, 'execvpe') as execute:
            with self.assertRaises(OSError):
                cli.run([], alias)
            for value in ('', 'short', 'x' * 5000, 'x' * 32 + '\n' + 'y' * 32):
                self.key.write_text(value)
                with self.assertRaises(ValueError):
                    cli.run([], self.key)
            execute.assert_not_called()
