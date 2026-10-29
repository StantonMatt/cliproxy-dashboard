"""The small subset of legacy/native v8 configuration used by repo scripts."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess

import yaml

FALLBACK_BINARY = Path('/opt/cliproxyapi/8.0.7/cli-proxy-api')
RETRY = ('request-retry', 'max-retry-credentials', 'max-retry-interval')
COOLDOWN = ('disable-cooling', 'save-cooldown-status')
MANAGEMENT = ('allow-remote', 'secret-key', 'disable-control-panel', 'disable-auto-update-panel')
AFFINITY = ('strategy', 'session-affinity', 'session-affinity-ttl', 'session-affinity-subagents')
PATHS = {
    **{key: (('remote-management', key), ('management', key)) for key in MANAGEMENT},
    'usage-statistics-enabled': (('usage-statistics-enabled',),
        ('observability', 'usage', 'usage-statistics-enabled')),
    **{key: ((key,), ('routing', 'retry', key)) for key in RETRY},
    **{key: ((key,), ('routing', 'cooldown', key)) for key in COOLDOWN},
    'force-model-prefix': (('force-model-prefix',), ('routing', 'force-model-prefix')),
    'antigravity-credits': (('quota-exceeded', 'antigravity-credits'),
        ('oauth', 'providers', 'antigravity', 'antigravity-credits')),
    **{key: (('routing', key), ('routing', key)) for key in AFFINITY},
}
MISSING = object()


def layout(config):
    if not isinstance(config, dict):
        raise ValueError('Expected a configuration mapping')
    version = config.get('config-version')
    if version is not None and (type(version) is not int or version != 8):
        raise ValueError('Unsupported config-version; inspect the configuration manually')
    nested = version == 8 or any(key in config for key in ('server', 'management', 'oauth', 'observability'))
    if nested:
        for legacy, native in PATHS.values():
            if legacy == native:
                continue
            if legacy[0] != 'quota-exceeded' and legacy[0] in config:
                raise ValueError('Mixed legacy/v8 configuration; inspect it manually')
            node = config
            for key in legacy:
                if not isinstance(node, dict) or key not in node:
                    break
                node = node[key]
            else:
                raise ValueError('Mixed legacy/v8 configuration; inspect it manually')
    return 'v8' if nested else 'legacy'


def setting_path(config, setting):
    return PATHS[setting][layout(config) == 'v8']


def get(config, setting, default=MISSING):
    node = config
    for key in setting_path(config, setting):
        if not isinstance(node, dict) or key not in node:
            if default is MISSING:
                raise ValueError('Missing configuration setting: ' + setting)
            return default
        node = node[key]
    return copy.deepcopy(node)


def _mapping_child(node, key):
    if not isinstance(node, yaml.MappingNode):
        raise ValueError('Expected a mapping while editing: ' + key)
    matches = [value for name, value in node.value if name.value == key]
    if len(matches) > 1:
        raise ValueError('Duplicate configuration key: ' + key)
    return matches[0] if matches else None


def validated_edit(original, updates, allow_add=()):
    """Edit scalar spans, retaining unrelated bytes/comments; validate the result.

    Existing keys are required, except explicitly permitted additions to an
    existing block mapping. Flow mappings and aliases are rejected if an edit
    cannot be proven to change only the requested settings.
    """
    text = original.decode()
    config = yaml.safe_load(text)
    expected = copy.deepcopy(config)
    tree = yaml.compose(text)
    edits = []
    for setting, value in updates.items():
        path = setting_path(config, setting)
        target, node = expected, tree
        for key in path[:-1]:
            if not isinstance(target.get(key), dict):
                raise ValueError('Missing configuration block: ' + key)
            target, node = target[key], _mapping_child(node, key)
        scalar = _mapping_child(node, path[-1])
        encoded = json.dumps(value, ensure_ascii=False)
        if scalar is None:
            if setting not in allow_add or node.flow_style or not node.value:
                raise ValueError('Missing configuration setting: ' + setting)
            # Insert before the first field, without changing surrounding comments.
            first = node.value[0][0].start_mark
            indent = first.column
            offset = first.index - indent
            edits.append((offset, offset, ' ' * indent + path[-1] + ': ' + encoded + '\n'))
        else:
            if not isinstance(scalar, yaml.ScalarNode) or scalar.style in ('|', '>'):
                raise ValueError('Expected a plain scalar setting: ' + setting)
            if scalar.start_mark.index == scalar.end_mark.index:
                # An implicit null is marked immediately after the colon.
                encoded = ' ' + encoded
            edits.append((scalar.start_mark.index, scalar.end_mark.index, encoded))
        target[path[-1]] = value
    for start, stop, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[stop:]
    try:
        actual = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError('Unexpected configuration change') from None
    if actual != expected:
        raise ValueError('Unexpected configuration change')
    return text.encode()


def write_same_inode(path, candidate, original):
    """Keep the gateway's watched inode and refuse a changed preparation input."""
    with path.open('r+b') as stream:
        if stream.read() != original:
            raise ValueError('Config changed during preparation')
        stream.seek(0)
        stream.write(candidate)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def isolated_policy(config):
    """Normalize either schema into a legacy policy for v7/v8 test instances."""
    nested = layout(config) == 'v8'
    routing = copy.deepcopy(config.get('routing', {}))
    if not isinstance(routing, dict):
        raise ValueError('Expected a routing mapping')
    if nested:
        for key in ('retry', 'cooldown', 'force-model-prefix'):
            routing.pop(key, None)
    result = {'routing': routing} if 'routing' in config else {}
    absent = object()
    for setting in (*RETRY, *COOLDOWN, 'force-model-prefix'):
        value = get(config, setting, absent)
        if value is not absent:
            result[setting] = value
    quota = copy.deepcopy(config.get('quota-exceeded', {}))
    if not isinstance(quota, dict):
        raise ValueError('Expected a quota-exceeded mapping')
    credits = get(config, 'antigravity-credits', None)
    if credits is not None:
        quota['antigravity-credits'] = credits
    if quota or 'quota-exceeded' in config:
        result['quota-exceeded'] = quota
    return result


def resolve_gateway_binary():
    """Read the effective systemd executable, with a known v8 fallback.

    This is a metadata query only; no unit is started, stopped, or changed.
    Call lazily so an explicit --binary (and --help) needs no systemd access.
    """
    try:
        result = subprocess.run(['systemctl', 'show', 'cliproxyapi', '--property=ExecStart', '--value'],
            capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_BINARY
    matches = re.findall(r'(?:^|\{)\s*path=([^;]+?)\s*;', result.stdout)
    if len(matches) == 1:
        path = Path(matches[0])
        if path.is_absolute() and not re.search(r'\s', str(path)):
            return path
    return FALLBACK_BINARY
