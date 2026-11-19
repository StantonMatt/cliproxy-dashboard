"""Passive v8.0.47 auth-files observations; never scheduler/cooldown evidence.

Contract: sdk/cliproxy/auth/quota_signals.go and management/auth_files.go.
Claude requires weekly allowance and may add the Fable slot; Codex requires at least one valid slot.
Present slots must be complete, with distinct window lengths. Unknown namespaces stay out.
"""
import usage_adapters as usage


def payload(file, now):
    quota = file.get('quota')
    if not isinstance(quota, dict):
        return None
    observed = usage.epoch(quota.get('observed_at'))
    if observed is None or observed > now:
        return None
    raw = quota.get('signals')
    if not isinstance(raw, dict):
        return None
    headers = {k.casefold(): v for k, v in raw.items() if isinstance(k, str)
               and isinstance(v, str) and len(v) <= 512 and all(ord(c) >= 32 for c in v)}
    result = {}
    if file.get('type') == 'claude':
        # Only a Fable response carries 7d_oi, the Fable weekly window.
        for slot, kind in (('5h', 'five_hour'), ('7d', 'seven_day'), ('7d_oi', 'seven_day_overage_included')):
            prefix = 'anthropic-ratelimit-unified-' + slot + '-'
            # Inspect raw keys so an invalid/dropped 5h header is not mistaken for absence.
            if slot != '7d' and not any(isinstance(k, str) and k.casefold().startswith(prefix) for k in raw):
                continue
            used = usage.numeric(headers.get(prefix + 'utilization'))
            reset = usage.numeric(headers.get(prefix + 'reset'))
            status = headers.get(prefix + 'status')
            if (any(isinstance(k, str) and k.casefold().startswith(prefix) and k.casefold() not in headers for k in raw) or
                    used is None or not 0 <= used <= 1 or
                    prefix + 'reset' in headers and reset is None or
                    (reset is None and (used != 0 or now - observed > 900)) or
                    (reset is not None and not now < reset <= observed + 8 * 86400) or
                    status not in (None, 'allowed', 'allowed_warning', 'rejected')):
                if slot == '7d_oi':
                    # A bad Fable slot is left out; it must not discard the primary readings.
                    continue
                return None
            # A rejected response may carry rounded utilization below 1.
            result[kind] = dict(utilization=100 if status == 'rejected' else used * 100,
                                resets_at=usage.stamp(reset) if reset is not None else None)
            if status is not None:
                result[kind]['status'] = status
    elif file.get('type') == 'codex':
        limits = {}
        for slot in ('primary', 'secondary'):
            prefix = 'x-codex-' + slot + '-'
            keys = [k.casefold() for k in raw if isinstance(k, str) and k.casefold().startswith(prefix)]
            if not keys:
                continue
            # Invalid values filtered out of headers still make this a present, invalid slot.
            if any(k not in headers for k in keys):
                return None
            used = usage.numeric(headers.get(prefix + 'used-percent'))
            minutes = usage.numeric(headers.get(prefix + 'window-minutes'))
            reset = usage.numeric(headers.get(prefix + 'reset-at'))
            after = usage.numeric(headers.get(prefix + 'reset-after-seconds'))
            if (prefix + 'reset-at' in headers and reset is None or
                    prefix + 'reset-after-seconds' in headers and (after is None or after < 0)):
                return None
            if reset is None and after is not None and after >= 0:
                reset = observed + after
            if (used is None or not 0 <= used <= 100 or minutes not in (300, 10080)
                    or reset is None or not now < reset <= observed + 8 * 86400):
                return None
            limits[slot + '_window'] = dict(used_percent=used, reset_at=reset,
                                            limit_window_seconds=minutes * 60)
        if not limits or len({w['limit_window_seconds'] for w in limits.values()}) != len(limits):
            return None
        reached = headers.get('x-codex-limit-reached')
        if reached not in (None, 'true', 'false'):
            return None
        if reached == 'true':
            # A global rejection cannot prove allowance in any present slot.
            for row in limits.values():
                row['used_percent'] = 100
        limits['allowed'] = reached != 'true'
        limits['limit_reached'] = reached == 'true'
        result['rate_limit'] = limits
        plan = headers.get('x-codex-plan-type')
        if plan in ('free', 'plus', 'pro', 'team', 'business', 'enterprise', 'edu'):
            result['plan_type'] = plan
    else:
        return None
    return result, usage.stamp(observed)
