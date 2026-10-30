"""Host settings shared by installers and installed operational helpers.

No credentials belong here. Legacy discovery is used only until site.json exists.
"""
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat

SITE = Path('/etc/personal-codex-router/site.json')
HERE = Path(__file__).resolve().parent
REPO = HERE.parent if HERE.name == 'scripts' else HERE
LEGACY_PREVIEWS = SITE.with_name('previews.json')


def default_user(repo=None):
    name = os.environ.get('SUDO_USER')
    return pwd.getpwnam(name) if name else pwd.getpwuid((repo or REPO).stat().st_uid)


def tailscale_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise RuntimeError('Each device needs a Tailscale IPv4 address') from None
    if address not in ipaddress.ip_network('100.64.0.' + '0/10'):
        raise RuntimeError('Device addresses must be Tailscale IPv4 addresses')
    return str(address)


def validate(value, repo=None):
    if not isinstance(value, dict) or set(value) - {'desktop_user', 'devices', 'tailscale_hostname', 'claude_b', 'source_checkout'}:
        raise RuntimeError('Invalid site config fields')
    name = value.get('desktop_user')
    if 'desktop_user' in value and (not isinstance(name, str) or not name.strip()):
        raise RuntimeError('desktop_user must name an existing Linux user')
    try:
        user = pwd.getpwnam(name) if name else default_user(repo)
    except (KeyError, TypeError):
        raise RuntimeError('desktop_user must name an existing Linux user') from None
    result = {'desktop_user': user.pw_name, 'devices': []}
    if 'source_checkout' in value:
        checkout = value['source_checkout']
        if (not isinstance(checkout, str) or not Path(checkout).is_absolute()
                or any(ord(c) < 32 for c in checkout)):
            raise RuntimeError('source_checkout must be an absolute checkout path')
        result['source_checkout'] = checkout
    devices = value.get('devices', [])
    if not isinstance(devices, list):
        raise RuntimeError('devices must be a list')
    for device in devices:
        if (not isinstance(device, dict) or set(device) != {'name', 'tailscale_ip'}
                or not isinstance(device['name'], str) or not device['name'].strip()
                or any(ord(c) < 32 for c in device['name'])):
            raise RuntimeError('Each device needs a name and tailscale_ip')
        result['devices'].append({'name': device['name'], 'tailscale_ip': tailscale_ip(device['tailscale_ip'])})
    if len({d['tailscale_ip'] for d in result['devices']}) != len(devices):
        raise RuntimeError('Device addresses must be unique')
    hostname = value.get('tailscale_hostname')
    if hostname is not None:
        if not isinstance(hostname, str) or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', hostname):
            raise RuntimeError('Invalid tailscale_hostname')
        result['tailscale_hostname'] = hostname
    claude = value.get('claude_b')
    if claude is not None:
        if (not isinstance(claude, dict) or set(claude) != {'email', 'plan'}
                or not all(isinstance(claude[k], str) and claude[k].strip() for k in ('email', 'plan'))
                or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', claude['email'])
                or any(ord(c) < 32 for text in claude.values() for c in text)):
            raise RuntimeError('claude_b needs an email and plan, without control characters')
        result['claude_b'] = dict(claude)
    return result


def load(path=None, *, repo=None, legacy=True):
    """Explicit paths never inherit another machine's legacy configuration."""
    target = Path(path) if path is not None else SITE
    if target.exists():
        info = target.stat()
        if path is None and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
            raise RuntimeError('site.json must be root-owned and not group/world-writable')
        try:
            return validate(json.loads(target.read_text()), repo)
        except (ValueError, UnicodeError):
            raise RuntimeError('site.json is not valid JSON') from None
    if path is not None:
        raise RuntimeError('Site config does not exist')
    value = {}
    if legacy:
        if LEGACY_PREVIEWS.exists():
            old = json.loads(LEGACY_PREVIEWS.read_text())
            value['devices'] = [{'name': d['name'], 'tailscale_ip': ip}
                                for d in old.get('devices', []) for ip in d['ips']]
            if old.get('dns'):
                value['tailscale_hostname'] = old['dns']
        # This installed unit contains only watch metadata, never its setup-token.
        unit = Path(default_user(repo).pw_dir) / '.config/systemd/user/personal-codex-router-claude-b-usage.service'
        if unit.exists():
            environment = {}
            for line in unit.read_text().splitlines():
                if line.startswith('Environment='):
                    for item in shlex.split(line.partition('=')[2]):
                        key, sep, text = item.partition('=')
                        if sep:
                            environment[key] = text
            email, plan = environment.get('CLAUDE_B_EMAIL'), environment.get('CLAUDE_B_PLAN')
            if not email or not plan:
                raise RuntimeError('Legacy Claude B unit needs both CLAUDE_B_EMAIL and CLAUDE_B_PLAN before migration')
            value['claude_b'] = {'email': email, 'plan': plan}
    return validate(value, repo)


def desktop(site=None):
    return pwd.getpwnam((site if site is not None else load())['desktop_user'])
