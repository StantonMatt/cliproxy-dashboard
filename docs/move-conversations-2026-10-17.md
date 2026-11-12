# Move conversations (2026-10-17)

CLIProxyAPI 8.0.30 does not expose an affinity-clear endpoint. The dashboard’s
opt-in “Move all conversations now” action therefore changes the parsed value
of `routing.session-affinity-ttl`. A new duration makes the gateway replace its
selector, which releases every Codex and Claude binding at once: the move
always covers all providers, whichever routing row asked for it. Idle
conversations pick an account on their next request; in-flight requests finish
on the account they already selected. Cooldowns survive the replacement.

## TTL values

The helper accepts a current TTL of `1h`, `1h0m0s` or `1h0m1s`. It writes
`1h0m1s` when the current value is `1h` or `1h0m0s`, and `1h0m0s` when it is
`1h0m1s`, so every move changes the parsed duration (3600 s and 3601 s). Values
that parse the same, such as `1h` and `60m`, would not replace the selector, so
any other value, `60m` included, is refused. After the first move both values
have the same length, so the in-place rewrite never shrinks the file; the
helper refuses any edit that would. The dashboard shows both values as `1h`.

## Request and result

The dashboard (`cliproxyapi`) makes requests in
`/run/personal-codex-router-move-request/`, its own `RuntimeDirectory`
(`cliproxyapi:cliproxyapi`, 0770). A request is an empty file named `request`,
created with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode 0640, which the dashboard
unit's `UMask=0077` narrows to 0600. Nothing changes its mode afterwards: the
helper only `lstat`s and removes it, which needs the directory's group
permissions, not the file's. The dashboard records its inode and creation
time. If the name already exists, a move is in progress.
Only the `cliproxyapi` user, members of the `cliproxyapi` group (currently
none) and root can create entries in the directory. Even so, the helper never
reads a request's content, so the request carries none, and a stray entry can
at most cause one TTL flip per minute. Living in `/run`, the directory and any request in
it disappear when the dashboard stops or the machine reboots.

`personal-codex-router-move.path` starts `personal-codex-router-move.service`
when `request` exists. The helper runs as `User=root`, `Group=cliproxyapi`, with
an empty capability bounding set. Without `CAP_DAC_OVERRIDE`, root has no
special file access: it removes the request only through the directory’s group
permissions, and it edits the config as its owner. `ProtectSystem=strict`
leaves everything read-only except the config file and the request directory
(`ReadWritePaths=`) plus the helper’s own state and runtime directories. It has
no network, no devices and a `@system-service` system-call filter.

The config must be a regular file, not a symlink, owned by `root:cliproxyapi`
with mode exactly `0640` or `0660`. Mode `0660` supports dashboard API-key
config saves and is intentional; the helper preserves either accepted mode.
World permissions, execute permissions and special mode bits are refused as
`config_unsafe`, as are an unexpected owner or group.

The helper never opens the request. It `lstat`s the entry, notes whether its
`ctime` is more than 15 s from now (`stale_request`), and always removes it
(`rmdir` for a directory, `unlink` otherwise), whatever it is. If it can’t, the
outcome is `failed` (`consume_failed`), even when the request was stale. If the
entry is already gone, at the `lstat` or at the removal, the dashboard withdrew
it: the helper does nothing and writes no result. Removing the entry is what
decides who owns it, so a withdrawn request never moves anything. The helper
takes the request with the standard library alone and imports its YAML modules
only afterwards, so even on a loaded machine it takes the request well inside
the dashboard’s 2 s start window.

It writes `/run/personal-codex-router-move/result.json` (root:cliproxyapi 0640)
atomically, with the request’s inode as `request_inode`, an `at` timestamp
taken after it removed the request, `outcome` and a stable `code`:

- `written`: the new TTL is on disk, on the same inode, with the same owner and
  mode, byte for byte as validated.
- `refused`: nothing was written, for example `too_soon`, `stale_request`,
  `unsupported_ttl`, `config_unsafe` or `affinity_disabled`.
- `failed`: it failed before anything was written, for example
  `consume_failed`, `backup_failed` or `write_failed`.
- `uncertain`: the write began and a later step failed. The result carries the
  value, and the last-flip time is recorded because the gateway may have
  reloaded. If the write itself fails partway, the helper puts the original
  bytes back on the same inode as a best effort.

The dashboard accepts a result only if `request_inode` is its request’s inode,
the result file’s modification time is later than the request’s creation, and
`at` is no earlier than it. tmpfs can reuse a consumed request’s inode, so the
time checks keep an older result from matching a newer request. The dashboard
waits up to 8 s for a matching result, then confirms the reload through the
authenticated loopback management readback. It never retries automatically.
The helper doesn’t inspect the gateway process.

Whatever happens during the wait, including an exception, the dashboard then
withdraws its request if it is still there with the same inode. If the request
is still untaken about 2 s after it was created, the dashboard withdraws it
and stops waiting; the path unit normally starts the helper within
milliseconds, so an untaken request means it isn’t watching. A withdrawn
request means the helper never took it, so nothing moved.

If anything fails after the request exists, the dashboard reports `not_moved`
only when it confirms it removed its own request. Otherwise the helper may
have taken it and flipped the TTL, so the move is `uncertain`. The same rule
covers the helper’s `consume_failed`: the helper couldn’t remove the request,
so the path unit’s next run could still take it, and only the dashboard’s own
withdrawal makes that `not_moved`. Any other refusal or failure came after the
helper removed the request; it is `uncertain` only if the dashboard’s request
is somehow still there and can’t be removed.

## Dashboard outcomes

Each 200 or 202 routing response to a request with `move: true` carries
`move: {"outcome", "code"}`. A 400 or 503 never does, including one from a
provider’s apply: the frontend reads no body from it and shows “Not confirmed
· refresh and try again”.
The move runs only after every requested provider applied its selection: HTTP
200 with `status: "active"`. Anything else is `not_moved`/`not_applied`.

| Outcome | Code | Meaning |
|---|---|---|
| `moved` | `moved` | The gateway reported the new TTL. |
| `not_moved` | `not_applied` | The routing change was not active: held, pending, failed or in neutral fallback. |
| `not_moved` | `unavailable` | The request directory or the helper is missing, or the gateway version is untested. |
| `not_moved` | `no_time` | The request deadline can’t cover the TTL read and the whole wait. |
| `not_moved` | `error` | The TTL couldn’t be read, the request couldn’t be created, or a later step failed after the dashboard withdrew its own request. |
| `not_moved` | `in_progress` | A request already exists. |
| `not_moved` | `helper_not_started` | The request was withdrawn untaken. |
| `not_moved` | `too_soon` | The dashboard created a request under 2 s ago, or the helper refused because the last move was under 60 s ago. |
| `not_moved` | `helper_refused` | The helper refused for another reason. |
| `not_moved` | `helper_failed` | The helper failed before writing anything; for `consume_failed`, only once the dashboard removed its own request. |
| `uncertain` | `helper_timeout` | The helper took the request but no matching result arrived. |
| `uncertain` | `reload_not_observed` | The gateway didn’t report the written TTL in time, or the written value matched the old one. |
| `uncertain` | `error` | The helper reported `uncertain` or an unknown outcome; it reported `consume_failed` and the dashboard didn’t remove the request itself; it refused or failed while the dashboard’s request stayed and couldn’t be removed; or a step failed after the request existed and the dashboard couldn’t confirm it withdrew it. |

The helper’s own outcome and code go to the dashboard’s journal as one JSON line
(`"event": "move_conversations"`), not to the frontend.

A plain save (**New conversations only**, or any change while moves are
unavailable) uses the move gate’s rule too: unless every requested provider is
`active`, it shows “Saved · not applied yet”, now including neutral fallback,
which used to show “Saved”.

**Try again** appears after every outcome except `moved` and `unavailable`. It
re-posts the current selection with `move: true`. This is a design decision:
Try again re-runs every server-side check, so a retry that can’t succeed just
fails fast, and the dashboard doesn’t need to classify which failures are
worth retrying.

## Interval and timeouts

Moves are at least 60 s apart, enforced by the helper’s stored last-flip time
only. Separately, the dashboard creates at most one request every 2 s, timed
in memory from the last request file it created; a request inside that
interval is `too_soon` and starts nothing. Every request starts the helper
once, even one the helper refuses, so this caps starts at about 5 per 10 s,
well below the service’s `StartLimitBurst=30` even for a scripted loop. It
only paces helper starts: a refused move holds back the next one by 2 s at
most, never by the helper’s minute. A stored time in the future, after a clock step, counts
as stale. Before creating a request the dashboard checks once that the request
deadline still covers the TTL read, the 8 s result wait, the 5 s reload
wait and a last TTL read (17 s in all, inside the 45 s request deadline);
every wait is also bounded by the deadline. `TimeoutStartSec=10` covers
the sub-second run; the helper defers `SIGTERM` until it has written its
result, so a timeout can’t cut the write short, and `TimeoutStopSec=5` bounds a
genuine hang.

## Backups

Before each write the helper saves the previous config to
`/var/lib/personal-codex-router-move/backups/` and keeps the newest 5. They are
root-only (directory 0700, files 0600) and contain the gateway’s secrets.

A restore writes in place, on the inode the gateway watches, and then
truncates. If the backup is shorter than the live file, the gateway’s watcher
can read stale trailing bytes between the write and the truncate, the case the
helper refuses as `edit_shrinks`. So restore a backup only when it isn’t
shorter than the live file. A backup taken before the first move has the TTL
`1h`, which is shorter than a live `1h0m1s`; the procedure below first changes
it to `1h0m0s`, which parses to the same duration and has the same length. If
the backup is still shorter, it refuses and leaves the config alone: reconcile
the other differences manually.

```sh
sudo systemctl stop personal-codex-router-move.path
sudo python3 -I - /var/lib/personal-codex-router-move/backups/<backup>.yaml <<'EOF'
import sys
from pathlib import Path
import yaml
sys.path.insert(0, '/opt/personal-codex-router')
import gateway_config
config = Path('/etc/cliproxyapi/config.yaml')
live, backup = config.read_bytes(), Path(sys.argv[1]).read_bytes()
note = ''
if len(backup) < len(live) and gateway_config.get(yaml.safe_load(backup), 'session-affinity-ttl') == '1h':
    backup = gateway_config.validated_edit(backup, {'session-affinity-ttl': '1h0m0s'})
    note = ' with the TTL 1h0m0s, the same duration as the backup’s 1h'
if len(backup) < len(live):
    sys.exit('The backup is shorter than the live config; nothing was restored.')
gateway_config.write_same_inode(config, backup, live)
print(f'Restored{note}.')
EOF
sudo systemctl start personal-codex-router-move.path
```

The gateway hot-reloads the restored file, which is itself a move if the TTL
differs.

## Other config tools

Every move changes the config’s SHA-256, and the TTL never returns to its
original bytes, so tools pinned to an earlier hash refuse from the first move
on. Moves share no lock with these or other manual config tools. Before running
one, stop `personal-codex-router-move.path` (as above) and start it again
afterwards.

`scripts/install-fast-mode-plugin.py --rollback <backup>` restores the backup’s
`config.before.yaml` only while the live config’s hash equals the manifest’s
`candidate_sha256`, the config that deployment wrote. `--disable` builds its
change from the live config, so it keeps the moved TTL and needs none of this.
For an exact rollback after moves, leave the original backup untouched and
make a merged copy beside it. The merged `config.before.yaml` carries the live
TTL, and its manifest pins the live hash. The script below first checks that
the TTL is the only change since that deployment: the live config with the
backup’s TTL put back must hash to `candidate_sha256`. Otherwise it refuses;
reconcile the other changes manually or use `--disable`.

```sh
sudo systemctl stop personal-codex-router-move.path
backup=/var/lib/personal-codex-router-backups/fast-mode-plugin/<timestamp>
sudo python3 -I - "$backup" "$backup-merged" <<'EOF'
import hashlib, json, os, sys
from pathlib import Path
import yaml
sys.path.insert(0, '/opt/personal-codex-router')
import gateway_config

def ttl_span(raw):
    text = raw.decode()
    node = yaml.compose(text)
    for key in gateway_config.setting_path(yaml.safe_load(text), 'session-affinity-ttl'):
        node = gateway_config._mapping_child(node, key)
    return text, node.start_mark.index, node.end_mark.index

def ttl(raw):
    text, start, stop = ttl_span(raw)
    return text[start:stop]

def with_ttl(raw, value):
    text, start, stop = ttl_span(raw)
    return (text[:start] + value + text[stop:]).encode()

digest = lambda data: hashlib.sha256(data).hexdigest()
backup, merged = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads((backup / 'manifest.json').read_bytes())
before = (backup / 'config.before.yaml').read_bytes()
live = Path('/etc/cliproxyapi/config.yaml').read_bytes()
if digest(before) != manifest['before_sha256']:
    sys.exit('The backup does not match its manifest.')
if digest(with_ttl(live, ttl(before))) != manifest['candidate_sha256']:
    sys.exit('The config changed in more than the TTL since that deployment.')
merged_before = with_ttl(before, ttl(live))
os.mkdir(merged, 0o700)
for name, data in (('config.before.yaml', merged_before),
                   ('manifest.json', (json.dumps({'before_sha256': digest(merged_before),
                                                  'candidate_sha256': digest(live)}, indent=2) + '\n').encode())):
    fd = os.open(merged / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
print(f'Merged backup: {merged}')
EOF
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --rollback "$backup-merged"
sudo python3 scripts/install-fast-mode-plugin.py --rollback "$backup-merged"
sudo systemctl start personal-codex-router-move.path
```

The rollback then leaves the TTL as it is, so it releases no pins.

`scripts/rollback-dashboard.py` restores the pre-dashboard config only while
the live config’s hash equals `enabled_sha256` in `docs/activation.json`. On
the current nested v8 config it refuses before that check anyway, and removing
the dashboard follows the manual, reviewed procedure in
[Removing the dashboard after migration](gateway-config-v8-migration.md#removing-the-dashboard-after-migration).
Moves add three points to that procedure:

1. Disable moves before anything else:
   `sudo systemctl disable --now personal-codex-router-move.path`.
2. Treat the moved TTL as a later routing change and keep its live value in the
   reviewed candidate. `1h0m0s` and `1h0m1s` are within a second of `1h`;
   writing `1h` back would shrink the file in place and itself release every
   pin.
3. Don’t re-pin `docs/activation.json`; that document forbids changing the
   activation hashes to get past the guard.

The same applies to a legacy-layout config after a gateway binary rollback,
where the hash check is what refuses.

A move takes no routing controller lock. It runs inside the dashboard’s own
routing-change lock, so it never overlaps another routing change made from the
dashboard. The minute timer can reconcile priorities during a move; a move only
releases pins, and each conversation’s next request picks an account under
whatever priorities are then current.

## Installation

Only the full `scripts/install-operations.py --apply` enables moves. It installs
the helper, `gateway_config.py`, both move units and the dashboard unit with the
request directory, enables the path unit, and checks after the restart that the
path unit is active and the request directory exists. The scoped
`--account-access-only` and `--claude-watch-only` installs ship the dashboard
but don’t install or update the helper or the move units. Moves keep using
what the last full install set up, so run a full install whenever the helper
or its units change.

The dashboard offers a move only when the request directory exists,
`/opt/personal-codex-router/move-conversations.py` is installed, and the
gateway’s build, from the `X-CPA-VERSION` header on its config response, is
in `MOVE_TESTED_GATEWAYS` in `dashboard/server.py`. Its background refresh
checks these; it doesn’t query systemd. It turns moves off only on positive
evidence, missing paths or a config read that names an untested build; a
failed config read keeps the previous answer, so a brief gateway hiccup doesn’t
close a choice the user has open. A move during an outage still fails safely,
because its own TTL read fails first. Each move checks
the version again on its own TTL read and reports `unavailable` on an untested
build, before creating a request. Otherwise routing
changes apply immediately, as before. A stopped or failed path unit isn’t
visible before a move: the move reports `helper_not_started` within about 2 s
instead.

## Recovery

A request that the helper can’t remove (for example, after a permissions change
on the request directory) makes the path unit retrigger the helper several
times a second, each run reporting `consume_failed`. The dashboard withdraws its
own request when the wait ends. Anything else left at the request path keeps
retriggering: after 30 starts in 10 s (about 4 s in testing), the service’s
start limit fails the path unit. Moves are still offered; each reports
`in_progress` while the entry remains, then `helper_not_started` until the path
unit is started again. Dashboard requests never reach that limit: at most one
every 2 s. To recover:

```sh
sudo rm -rf /run/personal-codex-router-move-request/request
sudo systemctl reset-failed personal-codex-router-move.path personal-codex-router-move.service
sudo systemctl start personal-codex-router-move.path
```

## Keyboard

`RULES.md` from the design asked for Enter on the select to apply **New
conversations only**. That shortcut is removed, an approved deviation: some
browsers deliver Enter before the select’s value updates, so it applied the
previous pending value, and where Enter opens the picker it couldn’t reopen
it.

On 2026-10-29 the inline choice row was replaced by a small modal (`#routing-dialog`), so the
layout no longer shifts. It offers the same choices and sends the same requests.
- The modal opens with focus on **New conversations only**; Enter or Space activates it.
- Tab cycles inside the modal.
- Escape, Cancel or a backdrop click revert the select and return focus to it.
- A failed request keeps the modal open with the error inside.
- With one configured Codex account (`gateway.codex_account_count < 2`), only Claude routing is
  shown.

## Gateway upgrades

Moves are disabled on any gateway version not in `MOVE_TESTED_GATEWAYS`
(`dashboard/server.py`), 8.0.30, 8.0.43 and 8.0.47 in this checkout, because on another build the
TTL readback could report `moved` even if the gateway didn’t release its pins.
Legacy 8.0.7 is unverified for moves and would disable them. The staged 8.0.47
upgrade retains 8.0.30 as its rollback target. The
list is separate from sign-in’s `TESTED_GATEWAYS` in `dashboard/signin.py`:
sign-in’s check verifies credential persistence and never touches affinity, so
adding a version there must not enable moves.

Before adding a version, verify that on that build a TTL change replaces the
selector and so releases every pin:

1. Re-check the upstream source: the normalized routing state in
   `service_config.go` and the inode watch behavior in
   `internal/watcher/events.go`.
2. Run that build in isolation: on loopback, with fake credentials and a mock
   upstream, never the live gateway or its config. Pin a session with a
   request, flip the TTL in its config in place, send the next request in that
   session, and confirm the log shows `cache miss, new binding`.

`tests/sandbox_move_check.py` doesn’t prove this. It runs the helper under its
production unit’s sandbox against a lab config, with a fake gateway that only
reads that config back, so it proves the helper’s sandbox and the
dashboard/helper protocol, not the gateway’s behavior. Re-run it after
changing the helper, its units or the dashboard’s move code.

The [8.0.47 preparation](gateway-upgrade-8.0.47-2026-10-29.md) exercised both Codex and Claude pins against the exact installed binary with `tests/gateway_upgrade_isolated.py`. Both changed from a cache hit to `cache miss, new binding` after an in-place TTL edit. 8.0.30 remains live; deploying the new dashboard and restarting the gateway are future cutover steps.
