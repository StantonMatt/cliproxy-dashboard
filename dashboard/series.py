"""Read-only widget queries. Full UTC hours use the collector's indexed rollup.

Boundary hours use raw records, including the current hour, so an in-flight
rollup cannot double count them. Local calendar boundaries are computed with
zoneinfo; repeated hours retain their distinct ISO offsets.
"""
import bisect
import datetime as dt
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
COUNT_METRICS = {'requests', 'failures', 'tokens', 'input_tokens', 'output_tokens', 'cached_tokens'}
METRICS = COUNT_METRICS | {'success_rate', 'cache_hit_rate', 'avg_duration'}
FIELDS = ('requests', 'failed', 'input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens',
          'latency_ms', 'latency_ms_count', 'cache_read_tokens', 'cache_read_tokens_count',
          'cache_creation_tokens', 'cache_creation_tokens_count')


def date(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Invalid date') from None


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def hour_floor(value):
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def local_boundary(value, tz):
    # Resolve nonexistent wall times (for example Santiago's skipped midnight)
    # to the first real instant after the gap.
    return value.astimezone(UTC).astimezone(tz)


def floor(value, bucket, tz):
    local = value.astimezone(tz)
    if bucket == 'minute':
        return local.replace(second=0, microsecond=0)
    if bucket == 'hour':
        return local_boundary(local.replace(minute=0, second=0, microsecond=0), tz)
    day = local.date() - dt.timedelta(days=local.weekday() if bucket == 'week' else 0)
    return local_boundary(dt.datetime.combine(day, dt.time(), tzinfo=tz), tz)


def next_bucket(value, bucket, tz):
    if bucket == 'minute':
        return (value.astimezone(UTC) + dt.timedelta(minutes=1)).astimezone(tz)
    if bucket == 'hour':
        # A fold can repeat this wall hour, and a half-hour DST change can make
        # the next bucket shorter than 60 minutes. Choose the next real boundary.
        wall = value.replace(minute=0, second=0, microsecond=0)
        candidates = [local_boundary((wall + dt.timedelta(hours=step)).replace(fold=fold), tz)
                      for step in (0, 1) for fold in (0, 1)]
        future = [candidate for candidate in candidates if candidate.astimezone(UTC) > value.astimezone(UTC)]
        return min(future, key=lambda candidate: candidate.astimezone(UTC))
    day = value.date() + dt.timedelta(days=7 if bucket == 'week' else 1)
    return local_boundary(dt.datetime.combine(day, dt.time(), tzinfo=tz), tz)


def buckets(since, until, bucket, tz, first=None):
    if first is not None:
        since = max(since, first)
    result = []
    current = floor(since, bucket, tz)
    while current.astimezone(UTC) < until:
        result.append(current)
        if len(result) > 1000:
            raise ValueError('Too many buckets (maximum 1000)')
        following = next_bucket(current, bucket, tz)
        if following.astimezone(UTC) <= current.astimezone(UTC):
            raise ValueError('Invalid timezone boundary')
        current = following
    return result


def unit(metric):
    return 'percent' if metric in ('success_rate', 'cache_hit_rate') else 'ms' if metric == 'avg_duration' else 'tokens' if 'tokens' in metric else 'count'


def parameters(endpoint, query, now):
    allowed = {'since', 'until'}
    allowed |= {'metric', 'group', 'bucket', 'tz', 'compare', 'provider', 'account'} if endpoint == 'series' else (
        {'metric', 'by', 'top', 'provider', 'account'} if endpoint == 'breakdown' else
        {'mode', 'metric', 'tz'} if endpoint == 'heatmap' else set())
    if set(query) - allowed:
        raise ValueError('Invalid parameter')
    for key, values in query.items():
        if len(values) != 1:
            raise ValueError('Invalid ' + key)
    p = {key: value[0] for key, value in query.items()}
    p['since'] = date(p.get('since', '1970-01-01T00:00:00Z'))
    p['until'] = date(p['until']) if 'until' in p else now
    if p['since'] >= p['until']:
        raise ValueError('Invalid date range')
    for key, pattern in (('provider', r'[a-z0-9-]{1,40}'), ('account', r'[A-Za-z0-9_-]{1,255}')):
        if key in p and not re.fullmatch(pattern, p[key]):
            raise ValueError('Invalid ' + key)
    if endpoint == 'retry':
        return p
    p.setdefault('metric', 'requests')
    if p['metric'] not in (METRICS if endpoint == 'series' else COUNT_METRICS if endpoint == 'breakdown' else {'requests', 'failures', 'tokens'}):
        raise ValueError('Invalid metric')
    if endpoint in ('series', 'heatmap'):
        p.setdefault('tz', 'UTC')
        try:
            if not 1 <= len(p['tz']) <= 128:
                raise ValueError()
            p['zone'] = ZoneInfo(p['tz'])
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError('Invalid tz') from None
    if endpoint == 'series':
        p.setdefault('group', 'none')
        p.setdefault('bucket', 'day')
        p.setdefault('compare', '0')
        if p['group'] not in {'none', 'provider', 'account', 'model', 'outcome'} or p['group'] == 'outcome' and p['metric'] != 'requests':
            raise ValueError('Invalid group')
        if p['bucket'] not in {'minute', 'hour', 'day', 'week'}:
            raise ValueError('Invalid bucket')
        if p['compare'] not in {'0', '1'}:
            raise ValueError('Invalid compare')
        duration = p['until'] - p['since']
        if p['bucket'] == 'minute' and duration > dt.timedelta(hours=6) or p['bucket'] == 'hour' and duration > dt.timedelta(days=100):
            raise ValueError('Range too long for bucket')
    elif endpoint == 'breakdown':
        p.setdefault('by', 'model')
        if p['by'] not in {'model', 'provider', 'account', 'failure_reason'}:
            raise ValueError('Invalid by')
        raw = p.get('top', '8')
        if not re.fullmatch(r'[0-9]{1,2}', raw) or not 1 <= int(raw) <= 20:
            raise ValueError('Invalid top')
        p['top'] = int(raw)
    else:
        p.setdefault('mode', 'weekhour')
        if p['mode'] not in {'weekhour', 'calendar'}:
            raise ValueError('Invalid mode')
    return p


class History:
    def __init__(self, con, gateway, helpers, now):
        self.con, self.helpers, self.now = con, helpers, now
        self.gateway = gateway
        self.lookup = helpers['lookup'](gateway)
        self.columns = {r[1] for r in con.execute('PRAGMA table_info(usage)')}
        self.rollup = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_hourly' AND type='table'").fetchone())
        self.boundary = helpers['boundary'](con)
        if self.boundary and not self.rollup:
            raise sqlite3.OperationalError('Hourly history missing after retention')
        self.providers = [r[0] for r in con.execute('SELECT DISTINCT provider FROM usage_hourly')] if self.rollup else []
        self.first = None
        if self.rollup:
            # (provider,hour) index supplies each minimum without scanning the rollup.
            stamps = [con.execute('SELECT min(hour) FROM usage_hourly WHERE provider=? AND hour IS NOT NULL', (provider,)).fetchone()[0]
                      for provider in self.providers]
            self.first = min((date(s) for s in stamps if s), default=None)
        if self.first is None:
            # A brand-new database may have only the current unrolled hour.
            # Prefer its indexed timestamp before the legacy date fallback.
            row = con.execute('SELECT request_time FROM usage WHERE request_time IS NOT NULL '
                              'ORDER BY request_time,id LIMIT 1').fetchone() if 'request_time' in self.columns else None
            if row is not None:
                self.first = date(row[0])
            else:
                row = con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ',timestamp) FROM usage "
                                  'WHERE julianday(timestamp) IS NOT NULL '
                                  'ORDER BY julianday(timestamp),timestamp LIMIT 1').fetchone()
                if row is not None:
                    self.first = date(row[0])

    def effective(self, since, until, *, raw=False):
        if self.boundary:
            cutoff = date(self.boundary)
            if raw:
                since = max(since, cutoff)
            else:
                # Match usage_summary: archived partial hours are included whole.
                if since < cutoff:
                    since = hour_floor(since)
                if until < cutoff and until != hour_floor(until):
                    until = hour_floor(until) + dt.timedelta(hours=1)
        return since, until

    def raw(self, since, until, bucket='hour', *, failures=False, p=None):
        if since >= until:
            return []
        p = p or {}
        provider = "coalesce(nullif(provider, ''), 'codex')" if 'provider' in self.columns else "'codex'"
        def field(name):
            return name if name in self.columns else 'NULL'
        grain = "%Y-%m-%dT%H:%M:00Z" if bucket == 'minute' else "%Y-%m-%dT%H:00:00Z"
        kind = field('failure_kind') if failures else 'NULL'
        stats = ['count(*) AS requests'] + [f'sum({field(f)}) AS {f}' for f in FIELDS if f != 'requests' and not f.endswith('_count')]
        stats += [f'count({field(f.removesuffix("_count"))}) AS {f}' for f in FIELDS if f.endswith('_count')]
        where, args = [], []
        windows = []
        if 'request_time' in self.columns:
            # Exact lexical UTC seeks avoid applying date functions to the indexed column.
            def key(value):
                return value.strftime('%Y-%m-%dT%H:%M:%S.') + f'{value.microsecond:06d}000Z'
            # Separate indexed and legacy seeks: OR makes SQLite abandon the
            # partial failed-row index on large histories. The sets are disjoint.
            windows = [('request_time >= ? AND request_time < ?', [key(since), key(until)]),
                       ('request_time IS NULL AND julianday(timestamp) >= julianday(?) AND '
                        'julianday(timestamp) < julianday(?)', [iso(since), iso(until)])]
        else:
            windows = [('julianday(timestamp) >= julianday(?) AND julianday(timestamp) < julianday(?)',
                        [iso(since), iso(until)])]
        if failures:
            where.append('failed')
        if p.get('provider'):
            predicate, provider_args = self.helpers['provider_filter'](provider, self.gateway, p['provider'])
            where.append(predicate)
            args.extend(provider_args)
        if p.get('account'):
            where.append('auth_index = ?')
            args.append(p['account'])
        selection = ("SELECT strftime('" + grain + "', timestamp) AS hour, " + provider
                     + ' AS provider, auth_index, model, ' + kind + ' AS kind, ' + ', '.join(stats)
                     + ' FROM usage WHERE ')
        rows = []
        for window, window_args in windows:
            rows.extend(dict(r) for r in self.con.execute(selection + ' AND '.join([window] + where)
                        + ' GROUP BY 1,2,3,4,5', window_args + args))
        return rows

    def rows(self, since, until, p, *, bucket='hour', tz=UTC):
        if not self.rollup or bucket == 'minute':
            return self.raw(since, until, bucket, p=p)
        first_hour, last_hour = hour_floor(since), hour_floor(until)
        full_start = first_hour + dt.timedelta(hours=1) if since != first_hour else first_hour
        # Always read the current partial hour from raw, even with a future until.
        full_end = min(last_hour, hour_floor(self.now))
        rows = []
        special = set()
        if full_start < full_end:
            for provider in self.providers:
                sql = 'SELECT * FROM usage_hourly WHERE provider=? AND hour>=? AND hour<?'
                args = [provider, iso(full_start), iso(full_end)]
                if p.get('provider'):
                    predicate, provider_args = self.helpers['provider_filter']('provider', self.gateway, p['provider'])
                    sql += ' AND ' + predicate
                    args.extend(provider_args)
                if p.get('account'):
                    sql += ' AND auth_index=?'
                    args.append(p['account'])
                for record in self.con.execute(sql, args):
                    row = dict(record)
                    stamp = date(row['hour'])
                    # UTC rollups cannot split half-hour/quarter-hour local boundaries.
                    # Re-read only those edge hours from raw while detail is retained.
                    left = floor(stamp, bucket, tz).astimezone(UTC)
                    right = floor(stamp + dt.timedelta(hours=1, microseconds=-1), bucket, tz).astimezone(UTC)
                    if left != right and (not self.boundary or stamp >= date(self.boundary)):
                        special.add(stamp)
                    else:
                        rows.append(row)
        intervals = []
        if since < min(full_start, until):
            intervals.append((since, min(full_start, until)))
        if max(full_start, full_end) < until:
            intervals.append((max(full_start, full_end), until))
        intervals += [(stamp, stamp + dt.timedelta(hours=1)) for stamp in sorted(special)]
        for start, end in intervals:
            # Minute aggregates preserve fractional-offset local bucket edges.
            rows.extend(self.raw(start, end, 'minute', p=p))
        return rows

    def identity(self, group, row):
        provider, auth = row.get('provider'), row.get('auth_index')
        base = dict(provider=provider, auth_index=None, email=None, account_key=None)
        if group == 'none':
            return dict(base, key='total', label='Total', provider=None)
        if group == 'provider':
            return dict(base, key='provider:' + str(provider), label=str(provider))
        if group == 'model':
            model = row.get('model') or 'unknown'
            return dict(base, key=f'model:{provider}:{model}', label=model)
        if group == 'outcome':
            value = row['outcome']
            return dict(base, key='outcome:' + value, label='Completed' if value == 'ok' else 'Failed', provider=None)
        if group == 'failure_reason':
            kind = row.get('kind')
            kind = kind if kind in self.helpers['labels'] else 'unknown'
            return dict(base, key='failure_reason:' + kind, label=self.helpers['labels'][kind], provider=None)
        account = self.lookup.get((provider, auth), {})
        email = self.helpers['email'](account.get('email'))
        key = account.get('name', account.get('key'))
        return dict(base, key=f'account:{provider}:{auth or ""}', label=email or key or auth or 'Unknown account',
                    auth_index=auth, email=email, account_key=key)


def add(target, row):
    for field in FIELDS:
        target[field] = target.get(field, 0) + (row.get(field) or 0)
    for field in ('cached_tokens', 'total_tokens', 'output_tokens'):
        target[field + '_known'] = target.get(field + '_known', False) or row.get(field) is not None


def components_value(metric, components):
    totals = {}
    prompt = cached = coverage = 0
    for (provider, _), stats in components.items():
        add(totals, stats)
        if metric != 'cache_hit_rate':
            continue
        claude = provider in ('claude', 'claude-api-key')
        input_tokens = stats.get('input_tokens', 0)
        split = input_tokens + stats.get('cache_read_tokens', 0) + stats.get('cache_creation_tokens', 0)
        full = stats.get('cache_read_tokens_count', 0) == stats.get('requests', 0) == stats.get('cache_creation_tokens_count', 0)
        prompt += (input_tokens if not claude else split if full else
                   max(input_tokens, stats.get('total_tokens', 0) - stats.get('output_tokens', 0))
                   if stats.get('total_tokens_known') and stats.get('output_tokens_known') else split)
        cached += stats.get('cache_read_tokens' if claude else 'cached_tokens', 0)
        coverage += stats.get('cache_read_tokens_count', 0) if claude else stats.get('requests', 0) if stats.get('cached_tokens_known') else 0
    requests = totals.get('requests', 0)
    if metric == 'success_rate':
        return 100 * (requests - totals.get('failed', 0)) / requests if requests else None
    if metric == 'cache_hit_rate':
        return max(0, min(100, 100 * cached / prompt)) if prompt > 0 and coverage > 0 else None
    if metric == 'avg_duration':
        count = totals.get('latency_ms_count', 0)
        return totals.get('latency_ms', 0) / count if count else None
    return totals.get({'failures': 'failed', 'tokens': 'total_tokens'}.get(metric, metric), 0)


def aggregate(history, rows, group, metric, starts=None):
    entities, values, totals = {}, {}, {}
    times = [start.astimezone(UTC) for start in starts] if starts is not None else None
    for row in rows:
        if not row.get('hour'):
            continue
        index = bisect.bisect_right(times, date(row['hour'])) - 1 if times is not None else 0
        if index < 0:
            continue
        variants = [(None, row)]
        if group == 'outcome':
            variants = [(outcome, dict(row, outcome=outcome, requests=count)) for outcome, count in
                        (('ok', (row.get('requests') or 0) - (row.get('failed') or 0)), ('failed', row.get('failed') or 0))]
        for _, item in variants:
            identity = history.identity(group, item)
            key = identity['key']
            entities[key] = identity
            # The client computes promptUsage on each account before combining providers.
            component = (item['provider'], item.get('model') if group == 'model' else item.get('auth_index'))
            add(values.setdefault(key, {}).setdefault(index, {}).setdefault(component, {}), item)
            add(totals.setdefault(key, {}).setdefault(component, {}), item)
    if group == 'none' and not entities:
        entities['total'] = history.identity('none', {})
        totals['total'], values['total'] = {}, {}
    size = len(starts) if starts is not None else 1
    return [dict(identity, values=[components_value(metric, values.get(key, {}).get(i, {})) for i in range(size)],
                 total=components_value(metric, totals[key])) for key, identity in entities.items()]


def series(history, p):
    raw = p['bucket'] == 'minute'
    since, until = history.effective(p['since'], p['until'], raw=raw)
    first = history.first
    if raw and history.boundary:
        first = max(first or date(history.boundary), date(history.boundary))
    first_day = floor(first, 'day', p['zone']).astimezone(UTC) if first is not None else None
    if first_day is not None and first_day < until:
        since = max(since, first_day)
    starts = buckets(since, until, p['bucket'], p['zone'], first_day) if first is not None else []
    rows = history.rows(since, until, p, bucket=p['bucket'], tz=p['zone'])
    current = aggregate(history, rows, p['group'], p['metric'], starts)
    result = {k: p[k] for k in ('metric', 'group', 'bucket', 'tz')}
    now = history.now
    index = bisect.bisect_right([s.astimezone(UTC) for s in starts], now) - 1
    filling = (index if 0 <= index < len(starts) and
               now < next_bucket(starts[index], p['bucket'], p['zone']).astimezone(UTC) else None)
    result.update(since=iso(since), until=iso(until), unit=unit(p['metric']),
                  first_recorded=iso(history.first) if history.first else None, archived_before=history.boundary,
                  buckets=[s.isoformat() for s in starts], current=filling, series=current, previous=None)
    if p['compare'] == '1':
        local_start, local_end = since.astimezone(p['zone']), until.astimezone(p['zone'])
        if since == floor(since, 'day', p['zone']).astimezone(UTC) and local_start.date() == local_end.date():
            previous_day = local_start.date() - dt.timedelta(days=1)
            prev_since = local_boundary(dt.datetime.combine(previous_day, dt.time(), tzinfo=p['zone']), p['zone']).astimezone(UTC)
            prev_until = (local_end - dt.timedelta(days=1)).astimezone(UTC)
        else:
            duration = until - since
            prev_since, prev_until = since - duration, since
        prev_since, prev_until = history.effective(prev_since, prev_until, raw=raw)
        # Previous buckets retain alignment even when there was no history yet.
        prev_starts = buckets(prev_since, prev_until, p['bucket'], p['zone']) if starts else []
        previous = aggregate(history, history.rows(prev_since, prev_until, p, bucket=p['bucket'], tz=p['zone']),
                             p['group'], p['metric'], prev_starts)
        identities = {row['key']: row for row in current + previous}
        for target, target_starts in ((current, starts), (previous, prev_starts)):
            present = {row['key'] for row in target}
            for key, identity in identities.items():
                if key not in present:
                    target.append(dict(identity, values=[components_value(p['metric'], {})] * len(target_starts),
                                       total=components_value(p['metric'], {})))
        result['previous'] = dict(since=iso(prev_since), until=iso(prev_until),
                                  buckets=[s.isoformat() for s in prev_starts], series=previous)
    current.sort(key=lambda row: (-(row['total'] or 0), row['key']))
    if result['previous'] is not None:
        order = {row['key']: i for i, row in enumerate(current)}
        result['previous']['series'].sort(key=lambda row: order[row['key']])
    return result


def breakdown(history, p):
    since, until = history.effective(p['since'], p['until'])
    if p['by'] == 'failure_reason':
        if history.boundary and p['metric'] not in ('requests', 'failures'):
            # Token totals for archived failed requests cannot be reconstructed.
            since = max(since, date(history.boundary))
        raw_since = max(since, date(history.boundary)) if history.boundary else since
        rows = history.raw(raw_since, until, failures=True, p=p)
        if history.boundary and since < date(history.boundary):
            archived = history.rows(since, min(until, date(history.boundary)), p)
            # Reason evidence was pruned; retain failed metrics under unknown.
            for row in archived:
                failed = row.get('failed') or 0
                row = dict(row, kind='unknown', requests=failed)
                # Archived token counts cannot be separated into failed vs successful rows.
                if p['metric'] not in ('requests', 'failures'):
                    continue
                rows.append(row)
    else:
        rows = history.rows(since, until, p)
    groups = aggregate(history, rows, p['by'], p['metric'])
    groups.sort(key=lambda row: (-(row['total'] or 0), row['key']))
    total = sum(row['total'] or 0 for row in groups)
    selected, rest = groups[:p['top']], groups[p['top']:]
    share = lambda value: 100 * value / total if total else 0
    result_rows = [dict({k: v for k, v in row.items() if k not in ('values', 'total')}, value=row['total'], share=share(row['total'])) for row in selected]
    other_value = sum(row['total'] or 0 for row in rest)
    return dict(metric=p['metric'], by=p['by'], unit=unit(p['metric']), since=iso(since), until=iso(until), total=total,
                rows=result_rows, other=dict(count=len(rest), value=other_value, share=share(other_value)) if rest else None,
                archived_before=history.boundary)


def heatmap(history, p):
    since, until = history.effective(p['since'], p['until'])
    tz = p['zone']
    day_values = {}
    cells = [[0 for _ in range(24)] for _ in range(7)]
    counts = [[0 for _ in range(24)] for _ in range(7)]
    first = max(since, history.first) if history.first else until
    # Denominator is recorded calendar days, including zero-activity days.
    # A repeated DST hour contributes both occurrences to that day's one cell.
    observed_days = buckets(first, until, 'day', tz) if history.first and first < until else []
    for observed in observed_days:
        for hour in range(24):
            counts[observed.weekday()][hour] += 1
    for row in history.rows(since, until, p, bucket='hour', tz=tz):
        if not row.get('hour'):
            continue
        local = date(row['hour']).astimezone(tz)
        value = row.get({'failures': 'failed', 'tokens': 'total_tokens'}.get(p['metric'], 'requests')) or 0
        day = local.date().isoformat()
        day_values[day] = day_values.get(day, 0) + value
        cells[local.weekday()][local.hour] += value
    common = dict(mode=p['mode'], metric=p['metric'], tz=p['tz'], first_recorded=iso(history.first) if history.first else None)
    if p['mode'] == 'weekhour':
        cells = [[value / counts[d][h] if counts[d][h] else None for h, value in enumerate(row)] for d, row in enumerate(cells)]
        return dict(common, cells=cells, max=max((v or 0 for row in cells for v in row), default=0))
    # Calendar includes requested dates before history as explicit null cells.
    days = []
    for stamp in buckets(since, until, 'day', tz):
        key = stamp.date().isoformat()
        value = None if history.first is None or next_bucket(stamp, 'day', tz).astimezone(UTC) <= history.first else day_values.get(key, 0)
        days.append(dict(date=key, value=value))
    return dict(common, days=days, max=max((d['value'] or 0 for d in days), default=0))


def query(path, params, db, gateway, *, helpers, now=None):
    now = now or dt.datetime.now(UTC)
    endpoint = path.rsplit('/', 1)[-1]
    p = parameters(endpoint, params, now)
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=3)
    try:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN')
        if endpoint == 'retry':
            return helpers['retry'](con, iso(p['since']), iso(p['until']))
        history = History(con, gateway, helpers, now)
        return {'series': series, 'breakdown': breakdown, 'heatmap': heatmap}[endpoint](history, p)
    finally:
        con.close()
