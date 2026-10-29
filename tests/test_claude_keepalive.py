from contextlib import redirect_stderr, redirect_stdout
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('claude_keepalive', ROOT / 'scripts/claude-token-keepalive.py')
keepalive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keepalive)

# Auth/target controls found in the installed Claude Code 2.1.283 bundle.
AUTH_TARGET_ENV = '''
CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_REFRESH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR
CLAUDE_CODE_GATEWAY_TOKEN CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR
CLAUDE_CODE_SESSION_ACCESS_TOKEN CLAUDE_CODE_HOST_AUTH_ENV_VAR CLAUDE_CODE_HOST_CREDS_FILE
CLAUDE_CODE_API_BASE_URL CLAUDE_CODE_CUSTOM_OAUTH_URL CLAUDE_CODE_OAUTH_CLIENT_ID
CLAUDE_CODE_OAUTH_SCOPES CLAUDE_CONFIG_DIR CLAUDE_SECURESTORAGE_CONFIG_DIR
CLAUDE_LOCAL_OAUTH_API_BASE CLAUDE_LOCAL_OAUTH_APPS_BASE CLAUDE_LOCAL_OAUTH_CONSOLE_BASE
USE_LOCAL_OAUTH USE_STAGING_OAUTH ANTHROPIC_CONFIG_DIR
ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS
ANTHROPIC_UNIX_SOCKET ANTHROPIC_PROFILE ANTHROPIC_AWS_API_KEY ANTHROPIC_AWS_BASE_URL
ANTHROPIC_AWS_WORKSPACE_ID ANTHROPIC_BEDROCK_BASE_URL ANTHROPIC_BEDROCK_MANTLE_BASE_URL
ANTHROPIC_VERTEX_BASE_URL ANTHROPIC_VERTEX_PROJECT_ID ANTHROPIC_FOUNDRY_API_KEY
ANTHROPIC_FOUNDRY_AUTH_TOKEN ANTHROPIC_FOUNDRY_BASE_URL ANTHROPIC_FOUNDRY_RESOURCE
ANTHROPIC_GOOGLE_CLOUD_BASE_URL ANTHROPIC_GOOGLE_CLOUD_LOCATION
ANTHROPIC_GOOGLE_CLOUD_PROJECT ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID
CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
CLAUDE_CODE_USE_MANTLE CLAUDE_CODE_USE_ANTHROPIC_AWS CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD
CLAUDE_CODE_SKIP_VERTEX_AUTH CLAUDE_CODE_API_KEY_HELPER_TTL_MS
AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_BEARER_TOKEN_BEDROCK
AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE
AWS_ENDPOINT_URL AWS_ENDPOINT_URL_BEDROCK AWS_ENDPOINT_URL_BEDROCK_RUNTIME
AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_AUTHORIZATION_TOKEN
AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_FULL_URI
AWS_CONTAINER_CREDENTIALS_RELATIVE_URI GOOGLE_APPLICATION_CREDENTIALS
GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_QUOTA_PROJECT GOOGLE_OAUTH_ACCESS_TOKEN GCLOUD_PROJECT
CLOUDSDK_AUTH_ACCESS_TOKEN CLOUDSDK_AUTH_ACCESS_TOKEN_FILE
CLOUDSDK_AUTH_AUTHORIZATION_TOKEN_FILE CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_TENANT_ID AZURE_AUTHORITY_HOST
AZURE_CLIENT_CERTIFICATE_PATH AZURE_CLIENT_CERTIFICATE_PASSWORD AZURE_FEDERATED_TOKEN_FILE
HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy
grpc_proxy no_grpc_proxy AGENT_PROXY_URL AGENT_PROXY_AUTH_TOKEN NODE_USE_ENV_PROXY
CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER NODE_OPTIONS NODE_EXTRA_CA_CERTS
NODE_TLS_REJECT_UNAUTHORIZED SSL_CERT_DIR SSL_CERT_FILE
'''.split()


class ClaudeKeepaliveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = self.root / 'credentials.json'
        self.state = self.root / 'state/claude-keepalive.json'
        self.now = 1790790000
        self.access = 'private-access-test-value'
        self.refresh = 'private-refresh-test-value'
        self.write_expiry(self.now - 1)
        for target, options in (
                ('time.time', {'return_value': self.now}),
                ('shutil.which', {'return_value': '/fixture/claude'}),
                ('subprocess.run', {'return_value': subprocess.CompletedProcess([], 0)})):
            # Imported scripts are intentionally not registered in sys.modules.
            owner, name = target.split('.')
            patcher = patch.object(getattr(keepalive, owner), name, **options)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, name, mocked)

    def write_expiry(self, expiry):
        self.credentials.write_text(json.dumps({'claudeAiOauth': {
            'expiresAt': expiry * 1000, 'accessToken': self.access, 'refreshToken': self.refresh}}))

    def invoke(self):
        return keepalive.keepalive(self.credentials, self.state)

    def test_still_valid_does_nothing(self):
        self.write_expiry(self.now + 301)
        before = self.credentials.read_bytes()
        self.assertEqual(self.invoke(), 0)
        self.run.assert_not_called()
        self.which.assert_not_called()
        self.assertFalse(self.state.parent.exists())
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_expired_and_nearly_expired_run_once_then_start_collector(self):
        for remaining in (-1, 0, 300):
            with self.subTest(remaining=remaining):
                self.state.unlink(missing_ok=True)
                self.write_expiry(self.now + remaining)
                working = []

                def renew(argv, **kwargs):
                    if argv[0] == '/fixture/claude':
                        self.assertEqual(argv, ['/fixture/claude', '-p', '--model', 'haiku',
                            '--tools', '', '--max-turns', '1', '--no-session-persistence',
                            '--setting-sources', '', '--strict-mcp-config', 'Reply with OK only.'])
                        self.assertEqual(kwargs['timeout'], 90)
                        self.assertTrue(Path(kwargs['cwd']).is_dir())
                        self.assertEqual(list(Path(kwargs['cwd']).iterdir()), [])
                        working.append(Path(kwargs['cwd']))
                        self.assertTrue(kwargs['env']['PATH'].startswith(str(keepalive.LOCAL_BIN) + ':'))
                        self.assertEqual(json.loads(self.state.read_text()), {'last_attempt_at': self.now})
                        for stream in ('stdin', 'stdout', 'stderr'):
                            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
                        self.write_expiry(self.now + 28800)
                    return subprocess.CompletedProcess(argv, 0)

                self.run.reset_mock()
                self.run.side_effect = renew
                self.assertEqual(self.invoke(), 0)
                self.assertEqual(self.run.call_count, 2)
                self.assertEqual(self.run.call_args.args[0],
                    ['systemctl', '--user', 'start', keepalive.COLLECTOR])
                self.assertTrue(self.run.call_args.kwargs['check'])
                self.assertFalse(working[0].exists())
                self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.state.with_suffix('.lock').stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)

    def test_failed_attempts_and_hourly_rate_limit(self):
        self.run.return_value = subprocess.CompletedProcess([], 1)
        self.assertEqual(self.invoke(), 0)
        for elapsed in (0, 900, 3599):
            self.time.return_value = self.now + elapsed
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.call_count, 1)
        self.time.return_value = self.now + 3600
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.call_count, 2)

    def test_both_children_receive_only_allowlisted_environment(self):
        parent = {key: 'fixture-override' for key in AUTH_TARGET_ENV}
        allowed = {'HOME': str(self.root), 'USER': 'fixture-user', 'LOGNAME': 'fixture-user',
                   'PATH': '/fixture/bin:/usr/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                   'XDG_RUNTIME_DIR': str(self.root / 'runtime')}
        parent.update(allowed)
        parent.update(TMPDIR='/fixture/untrusted-temp', UNLISTED_FUTURE_OVERRIDE='fixture-override')
        expected = {**allowed, 'PATH': str(keepalive.LOCAL_BIN) + ':' + allowed['PATH']}

        def renew(argv, **kwargs):
            child_env = kwargs['env']
            self.assertFalse(set(AUTH_TARGET_ENV) & child_env.keys())
            if argv[0] == '/fixture/claude':
                self.assertEqual(child_env, {**expected, 'TMPDIR': kwargs['cwd']})
                self.assertTrue(Path(child_env['TMPDIR']).is_dir())
                self.write_expiry(self.now + 28800)
            else:
                self.assertEqual(child_env, expected)
            return subprocess.CompletedProcess(argv, 0)

        self.run.side_effect = renew
        with patch.dict(os.environ, parent, clear=True):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.call_count, 2)

    def test_minimal_environment_supplies_missing_home_and_path(self):
        with patch.dict(os.environ, {}, clear=True):
            env = keepalive.minimal_environment()
            self.assertEqual(env, {'HOME': str(Path.home()),
                                  'PATH': str(keepalive.LOCAL_BIN) + ':' + os.defpath})

    def test_success_without_renewal_logs_and_is_rate_limited(self):
        for remaining in (-1, 0, 300):
            with self.subTest(remaining=remaining):
                self.state.unlink(missing_ok=True)
                self.write_expiry(self.now + remaining)
                self.time.return_value = self.now
                self.run.reset_mock()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(self.invoke(), 0)
                    self.time.return_value = self.now + 900
                    self.assertEqual(self.invoke(), 0)
                self.assertEqual(output.getvalue(),
                    'Claude keepalive ran but stored login did not renew\n'
                    'Claude keepalive rate limited\n')
                self.assertEqual(self.run.call_count, 1)
                self.assertEqual(json.loads(self.state.read_text()), {'last_attempt_at': self.now})
                self.time.return_value = self.now + 3600
                self.assertEqual(self.invoke(), 0)
                self.assertEqual(self.run.call_count, 2)

    def test_expiry_must_advance_and_be_in_future(self):
        for before, after in ((self.now + 300, self.now + 299),
                              (self.now - 2, self.now - 1),
                              (self.now - 2, self.now)):
            with self.subTest(before=before, after=after):
                self.state.unlink(missing_ok=True)
                self.write_expiry(before)
                self.run.reset_mock()

                def update_expiry(argv, **kwargs):
                    self.write_expiry(after)
                    return subprocess.CompletedProcess(argv, 0)

                self.run.side_effect = update_expiry
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(self.invoke(), 0)
                self.assertEqual(self.run.call_count, 1)
                self.assertEqual(output.getvalue(),
                                 'Claude keepalive ran but stored login did not renew\n')

    def test_timeout_is_rate_limited_and_temporary_directory_is_removed(self):
        self.run.side_effect = subprocess.TimeoutExpired(['claude'], 90, output=self.access)
        self.assertEqual(self.invoke(), 0)
        working = Path(self.run.call_args.kwargs['cwd'])
        self.assertFalse(working.exists())
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.call_count, 1)

    def test_concurrent_invocation_does_not_dispatch(self):
        self.state.parent.mkdir()
        with self.state.with_suffix('.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke(), 0)
        self.run.assert_not_called()

    def test_missing_cli_is_a_fixed_error(self):
        self.which.return_value = None
        output = io.StringIO()
        with patch.object(keepalive, 'CLAUDE_FALLBACK', self.root / 'missing'), redirect_stdout(output):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(output.getvalue(), 'Claude keepalive executable unavailable\n')
        self.assertFalse(self.state.exists())
        self.run.assert_not_called()

    def test_known_install_fallback(self):
        executable = self.root / 'claude'
        executable.write_text('fixture, never executed')
        executable.chmod(0o700)
        self.which.return_value = None
        with patch.object(keepalive, 'CLAUDE_FALLBACK', executable):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.run.call_args.args[0][0], str(executable))

    def test_tokens_never_accessed_or_published(self):
        class ExpiryOnly(dict):
            def get(self, key, *args):
                self.assert_key(key)
                return super().get(key, *args)

            @staticmethod
            def assert_key(key):
                if key not in ('claudeAiOauth', 'expiresAt'):
                    raise AssertionError('token field accessed')

        # The decoder discards all fields except the expiry path.
        original_loads = json.loads

        def loads(content, **kwargs):
            result = original_loads(content, **kwargs)
            if kwargs.get('object_hook'):
                self.assertEqual(result, {'claudeAiOauth': {'expiresAt': (self.now - 1) * 1000}})
                result = ExpiryOnly(claudeAiOauth=ExpiryOnly(result['claudeAiOauth']))
            return result

        output = io.StringIO()
        before = self.credentials.read_bytes()
        with patch.object(keepalive.json, 'loads', side_effect=loads), redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(self.invoke(), 0)
        public = output.getvalue() + self.state.read_text() + repr(self.run.call_args_list)
        for token in (self.access, self.refresh):
            self.assertNotIn(token, public)
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_unexpected_errors_have_fixed_logs(self):
        output = io.StringIO()
        with patch.object(keepalive, 'keepalive', side_effect=OSError(self.access)), redirect_stdout(output):
            self.assertEqual(keepalive.main(), 1)
        self.assertEqual(output.getvalue(), 'Claude keepalive unexpected error\n')


if __name__ == '__main__':
    unittest.main()
