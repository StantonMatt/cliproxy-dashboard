"""Exact, sortable UTC keys for RFC3339 usage timestamps."""
import datetime as dt
import re


def timestamp_key(value):
    """Preserve nanoseconds that datetime and SQLite date functions discard.

    Invalid or timezone-less timestamps have no key and are excluded from
    time windows. Keep the original timestamp in responses for display.
    """
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?'
        r'([Zz]|[+-]\d{2}:\d{2})', value)
    if match is None:
        return None
    seconds, fraction, zone = match.groups()
    try:
        parsed = dt.datetime.fromisoformat(seconds + ('+00:00' if zone.upper() == 'Z' else zone))
        utc = parsed.astimezone(dt.timezone.utc).isoformat(timespec='seconds')[:-6]
        return utc + '.' + (fraction or '').ljust(9, '0') + 'Z'
    except (ValueError, OverflowError):
        return None
