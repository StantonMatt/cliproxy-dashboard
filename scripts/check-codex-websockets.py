#!/usr/bin/env python3
"""Read-only WebSocket policy preflight; stdout contains only booleans/key paths.

Exit 0 requires explicit canonical steering=false and no possible Codex tier
setter. Exit 1 means unsafe/implicit policy; exit 2 means unreadable/invalid YAML.
No management requests, credential reads, inference, or usage-queue access.
"""
import argparse
import json
from pathlib import Path
import re

import yaml


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError('duplicate mapping key')
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_config(text):
    return yaml.load(text, Loader=UniqueLoader)


def inspect_config(config):
    if not isinstance(config, dict):
        raise ValueError('configuration must be a mapping')
    missing = object()

    def at(path):
        node = config
        for key in path.split('.'):
            if not isinstance(node, dict):
                raise ValueError('configuration block must be a mapping')
            if key not in node:
                return missing
            node = node[key]
        return node

    steering_paths = ('upstream.codex.response-steering',
                      'oauth.providers.codex.response-steering', 'codex.response-steering')
    present = [(path, at(path)) for path in steering_paths if at(path) is not missing]
    effective = present[0][1] if present else False
    if type(effective) is not bool:
        raise ValueError('steering must be a boolean')
    explicit = at(steering_paths[0]) is False
    setters = []
    # Check both spellings conservatively, even when canonical presence wins.
    for base in ('requests.payload', 'payload'):
        payload = at(base)
        if payload is missing:
            continue
        if not isinstance(payload, dict):
            raise ValueError('payload must be a mapping')
        if set(payload) - {'default', 'default-raw', 'override', 'override-raw', 'filter'}:
            raise ValueError('unknown payload rule family')
        for family in ('default', 'default-raw', 'override', 'override-raw'):
            rules = payload.get(family, [])
            if not isinstance(rules, list):
                raise ValueError('rules must be a list')
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict) or not isinstance(rule.get('models', []), list):
                    raise ValueError('invalid rule')
                models = rule.get('models', [])
                for model in models:
                    if not isinstance(model, dict) or not isinstance(model.get('protocol', ''), str):
                        raise ValueError('invalid model selector')
                # Ignore model/header/source constraints: future routes or clients
                # can satisfy them. An omitted protocol can match Codex too.
                codex = any(m.get('protocol', '').strip().lower() in ('', 'codex') for m in models)
                params = rule.get('params', {})
                if not isinstance(params, dict) or any(not isinstance(k, str) for k in params):
                    raise ValueError('invalid params')
                for key in params:
                    # SJSON's leading ':' forces an object key, so ':service_tier' still writes it.
                    normalized = re.sub(r'\\(.)', r'\1', key.strip()).lstrip(':')
                    if codex and (normalized == 'service_tier' or normalized.startswith('service_tier.')):
                        # Never echo attacker-controlled parameter names/values.
                        setters.append(f'{base}.{family}[{index}].params.service_tier')
    return {
        'config_valid': True,
        'steering_explicit_false': explicit,
        'steering_default_false': not present,
        'steering_disabled': effective is False,
        'steering_key_paths': [path for path, _ in present],
        'payload_safe': not setters,
        'tier_setter_key_paths': sorted(set(setters)),
        'rollout_ready': explicit and effective is False and not setters,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/cliproxyapi/config.yaml'))
    args = parser.parse_args()
    try:
        result = inspect_config(load_config(args.config.read_text()))
    except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError):
        print(json.dumps({'config_valid': False, 'rollout_ready': False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result['rollout_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
