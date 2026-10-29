#!/usr/bin/env python3
"""Let the official Claude Code client renew its own expiring OAuth token."""
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

CREDENTIALS = Path.home() / '.claude/.credentials.json'
STATE = Path.home() / '.local/state/personal-codex-router/claude-keepalive.json'
LOCAL_BIN = Path.home() / '.local/bin'
CLAUDE_FALLBACK = LOCAL_BIN / 'claude'
COLLECTOR = 'personal-codex-router-claude-usage.service'
PROMPT = 'Reply with OK only.'


def minimal_environment():
    # Claude Code 2.1.283 bundle audit found auth/target overrides:
    # CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_OAUTH_REFRESH_TOKEN,
    # CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR, CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR,
    # CLAUDE_CODE_GATEWAY_TOKEN{,_FILE_DESCRIPTOR}, CLAUDE_CODE_SESSION_ACCESS_TOKEN,
    # CLAUDE_CODE_HOST_AUTH_ENV_VAR, CLAUDE_CODE_HOST_CREDS_FILE,
    # CLAUDE_CODE_API_BASE_URL, CLAUDE_CODE_CUSTOM_OAUTH_URL,
    # CLAUDE_CODE_OAUTH_CLIENT_ID, CLAUDE_CODE_OAUTH_SCOPES,
    # CLAUDE_CONFIG_DIR, CLAUDE_SECURESTORAGE_CONFIG_DIR, ANTHROPIC_CONFIG_DIR,
    # CLAUDE_LOCAL_OAUTH_{API,APPS,CONSOLE}_BASE, USE_{LOCAL,STAGING}_OAUTH,
    # ANTHROPIC_{API_KEY,AUTH_TOKEN,BASE_URL,CUSTOM_HEADERS,UNIX_SOCKET,PROFILE},
    # CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,MANTLE,ANTHROPIC_AWS,
    # ANTHROPIC_GOOGLE_CLOUD}, CLAUDE_CODE_SKIP_VERTEX_AUTH, provider-specific
    # ANTHROPIC_{AWS,BEDROCK,BEDROCK_MANTLE,VERTEX,FOUNDRY,GOOGLE_CLOUD}_*
    # keys/targets, AWS_* credentials/endpoints, GOOGLE_APPLICATION_CREDENTIALS,
    # GOOGLE_{CLOUD_PROJECT,CLOUD_QUOTA_PROJECT,OAUTH_ACCESS_TOKEN}, GCLOUD_PROJECT,
    # CLOUDSDK_AUTH_* and AZURE_* credentials; apiKeyHelper is a setting, with
    # CLAUDE_CODE_API_KEY_HELPER_TTL_MS controlling its cache. Proxy controls:
    # HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY (also lowercase),
    # grpc_proxy, no_grpc_proxy, AGENT_PROXY_URL, AGENT_PROXY_AUTH_TOKEN,
    # NODE_USE_ENV_PROXY, CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER.
    # None can be inherited: build from this allowlist, never a denylist.
    env = {key: os.environ[key] for key in
           ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR')
           if key in os.environ}
    env.setdefault('HOME', str(Path.home()))
    env['PATH'] = str(LOCAL_BIN) + os.pathsep + os.environ.get('PATH', os.defpath)
    return env


def expires_at(credentials):
    # Select expiry only; never access token fields or retain credentials.
    raw = json.loads(credentials.read_text(), object_hook=lambda row: {
        key: value for key, value in row.items() if key in ('claudeAiOauth', 'expiresAt')})
    expiry = raw.get('claudeAiOauth', {}).get('expiresAt')
    if type(expiry) not in (int, float) or not math.isfinite(expiry):
        raise ValueError('invalid expiry')
    return expiry / 1000


def save_attempt(state, now):
    fd, name = tempfile.mkstemp(prefix=state.name + '.', dir=state.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump({'last_attempt_at': now}, output, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(state)
        directory = os.open(state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def keepalive(credentials=CREDENTIALS, state=STATE):
    now = time.time()
    if expires_at(credentials) > now + 300:
        return 0
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.parent.chmod(0o700)
    fd = os.open(state.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Claude keepalive already running')
            return 0
        now = time.time()
        previous_expiry = expires_at(credentials)
        if previous_expiry > now + 300:
            return 0
        if state.exists():
            last = json.loads(state.read_text()).get('last_attempt_at')
            if type(last) not in (int, float) or not math.isfinite(last):
                raise ValueError('invalid attempt time')
            if now - last < 3600:
                print('Claude keepalive rate limited')
                return 0
        env = minimal_environment()
        executable = shutil.which('claude', path=env['PATH'])
        if executable is None and CLAUDE_FALLBACK.is_file() and os.access(CLAUDE_FALLBACK, os.X_OK):
            executable = str(CLAUDE_FALLBACK)
        if executable is None:
            print('Claude keepalive executable unavailable')
            return 1
        # Persist before dispatch, including failed or timed-out attempts.
        save_attempt(state, now)
        with tempfile.TemporaryDirectory(prefix='claude-keepalive-') as working:
            try:
                subprocess.run([executable, '-p', '--model', 'haiku', '--tools', '',
                                '--max-turns', '1', '--no-session-persistence',
                                '--setting-sources', '', '--strict-mcp-config', PROMPT],
                               cwd=working, env={**env, 'TMPDIR': working}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            except subprocess.TimeoutExpired:
                pass  # Renewal may have completed before the request timed out.
        renewed_expiry = expires_at(credentials)
        if renewed_expiry > previous_expiry and renewed_expiry > time.time():
            subprocess.run(['systemctl', '--user', 'start', COLLECTOR], check=True, timeout=10,
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print('Claude keepalive token valid; usage collection started')
        else:
            print('Claude keepalive ran but stored login did not renew')
        return 0


def main():
    try:
        return keepalive()
    except Exception:
        print('Claude keepalive unexpected error')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
