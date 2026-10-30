# Fast-mode policy

`fast-mode-policy` 0.1.2 runs inside live CLIProxyAPI 8.0.30 using its native C/JSON
plugin ABI. For each selected Codex account it removes `service_tier` when fast
is disabled and the requested value is `fast`, `priority` or `ultrafast`. Normal
means the field is absent. With fast allowed, it retains the request, except that
`map_unsupported_ultrafast: true` maps `ultrafast` to `priority` on accounts whose
policy does not declare ultrafast support. It never adds a tier or upgrades one.
Other values pass through the plugin unchanged; the gateway's own translator
can normalize `fast` to `priority` and remove `default` later.

Verified 2026-10-27: the plugin status reports version 0.1.2 with the live gateway
PID. The [8.0.30 upgrade record](gateway-upgrade-8.0.30-2026-10-15.md) documents
the installer's fix to match host event text without pinning source line numbers.
The [8.0.47 preparation](gateway-upgrade-8.0.47-2026-10-29.md) passed isolated
plugin and sandbox checks; that binary is staged, with cutover pending and
8.0.30 retained for rollback.

The dashboard atomically writes
`/var/lib/personal-codex-router/fast-mode-policy.json`, mode 0640, owned by
`cliproxyapi`, using a same-directory temporary file, fsync and rename:

```json
{
  "version": 1,
  "updated_at": "2026-10-02T23:59:00Z",
  "accounts": {"<auth_index>": {"allow_fast": true, "supports_ultrafast": false}},
  "files": {"codex-example.json": {"allow_fast": true, "supports_ultrafast": false}}
}
```

The dashboard writes every Codex account under both identifiers, with identical
values. Index entries take precedence without merging fields from the filename
entry. File-backed IDs exposed to the plugin are relative auth paths; the plugin
uses their basename as the filename fallback. Missing, unreadable or malformed
policy, unknown accounts and missing `allow_fast` default to allowing fast.
Unknown `supports_ultrafast` defaults false, so the enabled mapping still maps
ultrafast to priority with a missing or malformed policy. Policy read failures
log a rate-limited error without printing policy contents. Reads are cached by
inode, mtime, size and mode; dashboard atomic replacement is detected on the next
request without a config reload.

Native file-backed Codex indexes are derived from `codex:<absolute auth path>`.
Token refresh and gateway restart preserve them; renaming/moving a file or
changing the auth directory changes the index. Account failover selects a new
credential and re-evaluates the original request against that account's policy.
The same-account executor's internal retry can reuse its previous decision.

Ultrafast support on non-x25 accounts is unknown. Mapping it to priority avoids
relying on unknown upstream behavior: a rejection may trigger a 12-hour model
cooldown through upstream `conductor_cooldown.go:1768`. The dashboard's explicit
`supports_ultrafast` value controls the exception; the plugin does not guess plan
capability or inspect credentials.

At registration and every plugin reconfiguration, the plugin atomically writes
`/var/lib/cliproxyapi/fast-mode-plugin-status.json` (0640):

```json
{
  "plugin": "fast-mode-policy",
  "version": "0.1.2",
  "pid": 12345,
  "loaded_at": "2026-10-02T23:59:00Z",
  "policy_path": "/var/lib/personal-codex-router/fast-mode-policy.json",
  "map_unsupported_ultrafast": true,
  "audit": false
}
```

The dashboard infers whether a request ran fast from its recorded requested tier
and the account toggle at the request timestamp, mapping ultrafast to priority on
accounts without ultrafast support. The Codex backend echoes `default` or `auto`
for priority requests that measurably ran faster (verified 2026-10-03), so
default, auto and missing served-tier echoes do not change that inference; only a
priority or ultrafast echo while the toggle was off counts, as "Still fast".
Missing requested tiers remain unknown, and current plugin availability controls
the "Not enforced" card state without historical enforcement tracking.

The dashboard treats enforcement as live only when this non-disabled status PID
matches the running `cliproxyapi` MainPID, whose executable or command line
identifies `cli-proxy-api`. Status timestamps may include fractional seconds.
Shutdown removes a status file only if it still belongs to that library instance;
a retired version cannot delete a new version's marker. The v8.0.7 host retains
libraries when disabled and provides no disable callback. After a logged config
reload, the installer atomically marks the status `disabled: true`, so the dashboard
shows enforcement as not live. This marker reflects the configured disable; it
does not confirm runtime deactivation. Disable exits 3 and states that runtime
deactivation is unconfirmed. A reload timeout fails and retains the old marker.
Disabling through another editor does not mark this status automatically; use the
installer. Registration fails if status
publication fails, and failed registration/reconfiguration clears its owned marker.
A newly loaded plugin cannot silently claim live enforcement.

Build and deploy from this checkout after the orchestrator's review:

```sh
plugins/fast-mode-policy/build.sh /tmp/fast-mode-build
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --library /tmp/fast-mode-build/fast-mode-policy-v0.1.2.so
sudo python3 scripts/install-fast-mode-plugin.py --library /tmp/fast-mode-build/fast-mode-policy-v0.1.2.so
```

The build supports `GO=/absolute/path/to/go` when Go is outside PATH. Omitting
`--library` asks the root installer to build as the checkout owner (through
`runuser`) in a private `/tmp` output directory, using that user's heavy-job slots.
Go and a C compiler must then be available in the build environment. A root-owned
checkout requires a separately built `--library`. A dry run builds
nothing and prints only a public plugin-settings diff, omitting the rest of the
secret-bearing config. To test it without root or a live gateway, use
`--dry-run --config /tmp/config.v8.yaml`.

The installer creates root-owned directories and installs a root:root 0644 library
at `/opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.1.2.so`.
It refuses different bytes under an existing versioned filename. It preserves
other plugin entries; if active entries use another directory, deployment refuses
until their libraries are reconciled. No gateway restart is used. Runtime
verification uses only the gateway journal, public plugin status and process/file
metadata. The installer never reads a management key or calls the management API.
Its live settings are:

```yaml
plugins:
  enabled: true
  dir: /opt/personal-codex-router/plugins
  configs:
    fast-mode-policy:
      enabled: true
      priority: 100
      policy_path: /var/lib/personal-codex-router/fast-mode-policy.json
      status_path: /var/lib/cliproxyapi/fast-mode-plugin-status.json
      map_unsupported_ultrafast: true
      audit: false
      store:
        version: 0.1.2
```

`store.version` is the upstream host's version selector. Pinning it makes a
hot-reloaded config select that immutable library even when newer files exist.
A future release must use a new filename and update that selector; never overwrite
a loaded `.so`.

Before editing, the installer makes a private exact config backup and manifest
under `/var/lib/personal-codex-router-backups/fast-mode-plugin/<timestamp>/`,
prints that backup path, and checks preparation bytes remain unchanged. It writes
in place through `gateway_config.write_same_inode`, fsyncs, confirms the config
inode/owner/mode, waits for `config successfully reloaded` in new journal messages
from the same PID, then verifies a fresh status with the expected version, PID,
policy, mapping and audit settings. The reload message alone never confirms an
enabled runtime change. A fresh load also requires `pluginhost: plugin registered`
with the matching plugin, version and library path after the
captured journal cursor. That registration log precedes the snapshot swap;
status and registration together are the available keyless load evidence.
Library/version selection changes require the matching plugin/version/path in
`pluginhost: plugin hot reloaded`, emitted after the snapshot
swap. The installer matches event text and identity, not `host.go` line numbers.
This includes same-version path switches. Ordinary
reconfiguration and re-enabling a retained same-process library require fresh
matching status; v8.0.7 emits no new registration log for retained libraries.
Enable, config change, library/version switch and both explicit and automatic
rollback use these same confirmation rules. It compares
account-file identities and collector/preference timer states without opening OAuth credentials or consuming the usage queue. Unrelated
configuration values and bytes outside the plugin block are preserved. This is
a filesystem/service and plugin-runtime verification; it does not submit a live
model request. The isolated test supplies request-level verification.

Upstream `internal/pluginhost/host.go:231` silently swaps to an empty snapshot
for global disable; `host.go:260-261` skips individually disabled plugins and
`host.go:403-404` replaces the active maps and snapshot. There is no keyless
runtime confirmation of deactivation in v8.0.7: no host log and no plugin callback.
After verifying the config write, reload log, same PID and preserved operational
state, the installer marks both old and new status paths disabled and exits **3**.
Its message states that disable is applied in config, the reload was logged, and
runtime deactivation is unconfirmed. If the config is already unchanged and
disabled, it reports that no reload was needed and still exits 3. This distinction
also applies to rollbacks to disabled or absent plugin config, including automatic
rollback after a failed enable. It never reports those outcomes as fully verified.
The early watcher log is at `internal/watcher/config_reload.go:141-142`; runtime
plugin application is deferred to `sdk/cliproxy/service_config.go:172`.

Verify the public status and service state without printing the config:

```sh
systemctl show cliproxyapi --property=MainPID --value
sudo cat /var/lib/cliproxyapi/fast-mode-plugin-status.json
sudo journalctl -u cliproxyapi --since '5 minutes ago' --no-pager | rg 'config successfully reloaded|pluginhost: plugin registered|pluginhost: plugin hot reloaded'
systemctl is-active personal-codex-router-usage.timer personal-codex-router-preference.timer
```

Compare the status PID to MainPID and confirm version 0.1.2, the policy path and
mapping flag. The installer itself also checks `/proc/<pid>/exe` or cmdline.
The policy file is written by the dashboard; deployment does not create or change
account policies. Audit defaults off; when enabled it logs validated auth indexes
and fixed tier labels, never request contents, headers or tokens.

Disable through a verified config reload (exit 3 means runtime deactivation remains unconfirmed):

```sh
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --disable
sudo python3 scripts/install-fast-mode-plugin.py --disable
```

Keep the immutable library in place. Re-enable by rerunning the deployment command.
For an exact rollback to the config before a particular deployment, use its
printed backup directory:

```sh
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --rollback /var/lib/personal-codex-router-backups/fast-mode-plugin/<timestamp>
sudo python3 scripts/install-fast-mode-plugin.py --rollback /var/lib/personal-codex-router-backups/fast-mode-plugin/<timestamp>
```

Rollback checks both hashes and refuses if the current config has changed since
that deployment. Reconcile later edits manually or use `--disable` to preserve
them. Enabled rollback requires the same journal/status evidence. Disabled rollback
marks status disabled and exits 3 with runtime deactivation unconfirmed. Verification
failure attempts a guarded in-place restoration only
while current bytes still match the candidate. Binaries and policy files remain
untouched by rollback. A PID change or failed rollback verification requires
inspection of the private backup; the installer never tries a restart.

Downstream websocket connections log a warning once per session, with a global
one-per-second limit and bounded session memory. Session deduplication uses fixed
size hashes; warnings contain no session/request ID or header value. Optional
`audit: true` decision logs contain only validated 16-digit hex auth indexes and
known tier labels (`normal`, `priority`, `fast`, `ultrafast`, `default`, `auto`,
`absent`) or fixed `invalid`/`non-string` markers. Unknown strings and arbitrary
JSON values are never logged. Auth IDs, paths, request bodies, headers, policy
contents and raw error strings are omitted. The initial after-auth request
is covered. WebSocket transport is distinct from experimental native duplex
steering: with canonical `upstream.codex.response-steering: false`, ordinary serial
WebSocket turns re-enter account policy enforcement and still reuse the upstream
connection. With steering true, follow-up creates bypass the interceptor and can
retain a fast tier despite the account policy on 8.0.30 and 8.0.43. Codex's
warmup can make its first actual user inference such a follow-up. Existing sockets
must reconnect after changing steering or credential transport flags. Upstream
8.0.47 adds per-frame interception; its isolated follow-up/native-warmup tests
require enforcement rather than the old bypass. Keep steering false for this
rollout and rollback; full-duplex/error interactions are separate acceptance. The warning
above is emitted for downstream WebSockets even with steering false; it does not
prove duplex is active. HTTP and serial WebSockets are covered, but later gateway
payload rules can reintroduce a tier removed by the plugin on either path. Keep
steering explicitly false and forbid Codex tier-setting payload rules when relying
on every-turn enforcement; otherwise retain HTTP until the policy is verified.
See [Codex WebSockets](codex-websockets.md) for the read-only preflight, isolated
regressions, rollout and rollback. This plugin is not a complete enforcement
boundary for duplex or later tier overrides.

The status lives directly under the gateway's existing `StateDirectory=cliproxyapi`,
never under `auth/`. Both deployed services run as `cliproxyapi`. The gateway's
`ProtectSystem=strict` allows writes there but prevents writes to the dashboard's
state directory. The dashboard can read the gateway directory; it writes controls
and policy through `ReadWritePaths=/var/lib/personal-codex-router`. Both services
have `ProtectHome=yes` and `PrivateTmp=yes`, so shared files belong in `/var/lib`,
with libraries under the root-owned, read-only `/opt` tree. Neither service hides
`/proc`; the dashboard checks MainPID over the system bus and then `/proc/<pid>`.
No gateway unit change or restart is needed for these paths.

Before preparing backups or libraries, the installer checks its config, backup,
status-marker and, for an enabled candidate, library output paths. After installing
and repairing metadata, enabled candidates require an existing readable policy
and library, and check both units' effective `systemctl show` sandbox properties,
service identities, directory traversal and read/create/rename modes for policy,
controls, status, system-bus and process paths. Disabled or plugin-free candidates
check only config, backups and the marker outputs the installer writes, including
both old and new status paths when they differ. They require neither a policy nor
a library nor service access to those paths. An enabled candidate requires an
explicit absolute `plugins.dir`; missing or relative directories are never
resolved against the installer's working directory. Explicit and automatic
rollbacks use the restored candidate's requirements.

Hidden, read-only or privately mounted required paths produce a public
`Preflight:` diagnostic without changing config. Unrecognized filesystem
remapping is refused rather than guessed. `--dry-run` remains an offline config
diff and does not perform this deployment preflight.

Missing, null or empty `response_service_tier` means the served tier is unknown;
an omitted requested tier still means normal.
When a requested tier is known, the Mode cell shows that requested tier with a
“served tier was not reported” tooltip. Legacy rows with neither tier keep `—`.

Daily backups include the optional durable
`/var/lib/personal-codex-router/fast-mode-controls.json`, including change history.
The dashboard regenerates `fast-mode-policy.json` from those controls and current
account metadata. The plugin status is transient and must be recreated by the
running gateway; neither derived policy nor status needs a separate backup.
