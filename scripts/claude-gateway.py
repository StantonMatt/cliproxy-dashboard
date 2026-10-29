#!/usr/bin/env python3
"""Opt-in Claude CLI launcher; never changes the parent shell or direct collectors."""
import os
from pathlib import Path
import stat
import sys

KEY = Path.home() / '.config/personal-codex-router/claude-gateway.key'
GATEWAY_ENV = {
    'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8317',
    'ENABLE_TOOL_SEARCH': 'true',
    'CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING': '1',
    'CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY': '1',
}
# A per-invocation environment keeps systemd collectors, keepalive and redemption
# on their current direct credentials. No shell exports or Claude settings edits.
CLEAR_ENV = (
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_CUSTOM_HEADERS',
    'CLAUDE_CODE_OAUTH_REFRESH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR',
    'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR', 'CLAUDE_CODE_GATEWAY_TOKEN',
    'CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_SESSION_ACCESS_TOKEN',
    'CLAUDE_CODE_HOST_AUTH_ENV_VAR', 'CLAUDE_CODE_HOST_CREDS_FILE',
    'CLAUDE_CODE_API_BASE_URL', 'ANTHROPIC_UNIX_SOCKET', 'ANTHROPIC_PROFILE',
    'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_MANTLE',
    'CLAUDE_CODE_USE_ANTHROPIC_AWS', 'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD',
)


def run(argv, key_path=KEY):
    # Open without following a symlink, then inspect the same file descriptor.
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in (0, os.getuid())):
            raise ValueError('Gateway key must be owned by root or this user, mode 0600')
        token = source.read(4097).strip()
    if not 32 <= len(token) <= 4096 or any(c.isspace() for c in token):
        raise ValueError('Invalid gateway key file')
    env = {k: v for k, v in os.environ.items() if k not in CLEAR_ENV}
    env.update(GATEWAY_ENV)
    env['ANTHROPIC_AUTH_TOKEN'] = token
    # Preserve existing proxy exceptions while ensuring the gateway stays local.
    bypass = ','.join(filter(None, (env.get('NO_PROXY'), env.get('no_proxy'), '127.0.0.1,localhost')))
    env['NO_PROXY'] = env['no_proxy'] = bypass
    os.execvpe('claude', ['claude', *argv], env)


if __name__ == '__main__':
    try:
        run(sys.argv[1:])
    except (OSError, ValueError):
        # Neither path contents nor credentials are included in errors.
        print('Claude gateway launch failed. Check the private key and Claude executable.', file=sys.stderr)
        sys.exit(1)
