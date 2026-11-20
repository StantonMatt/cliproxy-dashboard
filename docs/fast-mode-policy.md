# Fast-mode policy

The [8.0.79 preparation](gateway-upgrade-8.0.79-2026-11-19.md) passed the
installed v0.2.0 plugin and WebSocket gates on candidate 8.0.79 and rollback
8.0.47. 8.0.47 remains live. Keep response steering explicitly false; candidate
payload rules still run after account policy. The backup prerequisite is resolved by disabling local KV; session/date-pin/replay
state stays in memory and is lost on restart as on 8.0.47.

`fast-mode-policy` 0.2.0 runs inside CLIProxyAPI 8.0.47 using its native C/JSON
plugin ABI. Each Codex account has a speed level: **normal**, **fast** or
**ultrafast**. Wire values are `priority` for Fast and `ultrafast` for Ultrafast;
normal means the field is absent. The plugin only touches requests whose
`service_tier` is `fast`, `priority` or `ultrafast`. Other values pass through
unchanged, and an absent tier stays absent: the plugin never adds a tier.

| Level | Requested `fast`/`priority` | Requested `ultrafast` |
| --- | --- | --- |
| normal | removed | removed |
| fast | kept | `priority` |
| ultrafast, model allowed, Responses or Chat client | `ultrafast` | kept |
| ultrafast, model allowed, other client formats | kept | `priority` |
| ultrafast, model not allowed or unknown | kept | `priority` |

"Allowed" means the request's model is in the account's `ultrafast_models`
allowlist. The plugin reads the selected upstream model (`req.Model`), falling
back to the body's `model`, then trims, lowercases and strips a trailing `(…)`
thinking suffix and any `prefix/` before an exact match. An empty or unknown
model is never upgraded. Only the `openai-response` and `openai` client formats
are upgraded, because only their Codex translators forward `ultrafast`; the
Claude, Gemini and Interactions translators drop it, so the plugin maps a
requested ultrafast to `priority` for them. Upgrades are allowlist-only and fail
closed: an upstream 400 that mentions plan or model support cools that model down
for 12 hours (upstream `sdk/cliproxy/auth/conductor_cooldown.go:1052-1059` in
8.0.47, `isModelSupportResultError`).

The dashboard atomically writes
`/var/lib/personal-codex-router/fast-mode-policy.json`, mode 0640, owned by
`cliproxyapi`, using a same-directory temporary file, fsync and rename:

```json
{
  "version": 2,
  "updated_at": "2026-11-01T12:00:00Z",
  "accounts": {"<auth_index>": {"level": "ultrafast", "ultrafast_models": ["gpt-6-astra", "gpt-6.1-sol"],
                                "allow_fast": true, "supports_ultrafast": true}},
  "files": {"codex-example.json": {"level": "ultrafast", "ultrafast_models": ["gpt-6-astra", "gpt-6.1-sol"],
                                   "allow_fast": true, "supports_ultrafast": true}}
}
```

Schema 2 is backward compatible with schema 1. For each entry a valid `level`
wins; without one the schema 1 fields apply exactly as in 0.1.x (`allow_fast:
false` removes fast tiers; otherwise `map_unsupported_ultrafast` maps ultrafast to
priority unless `supports_ultrafast` is true). `map_unsupported_ultrafast` only
affects such entries; levels always enforce the allowlist. The dashboard also
writes the schema 1 fields so a 0.1.x library still enforces each level sensibly
after a rollback: `allow_fast` is false only at normal, and `supports_ultrafast`
is true only at the ultrafast level with a non-empty allowlist. (At fast it stays
false, so 0.1.2 maps ultrafast to priority.)

The dashboard writes every Codex account under both identifiers, with identical
values. Index entries take precedence without merging fields from the filename
entry. File-backed IDs exposed to the plugin are relative auth paths; the plugin
uses their basename as the filename fallback. Missing, unreadable or malformed
policy (including a non-string `level` or allowlist entry) and unknown accounts
default to fast with ultrafast mapped to priority, and nothing is upgraded. Policy read failures
log a rate-limited error without printing policy contents. Reads are cached by
inode, mtime, size and mode; dashboard atomic replacement is detected on the next
request without a config reload.

Native file-backed Codex indexes are derived from `codex:<absolute auth path>`.
Token refresh and gateway restart preserve them; renaming/moving a file or
changing the auth directory changes the index. Account failover selects a new
credential and re-evaluates the original request against that account's policy.
The same-account executor's internal retry can reuse its previous decision.

## Model catalog

Fast and Ultrafast availability follow each account's model catalog, independently
of its plan label. The dashboard reads that catalog through gateway management
`/api-call` (GET
`https://chatgpt.com/backend-api/codex/models?client_version=0.166.0`, with
`Authorization: Bearer $TOKEN$`, `ChatGPT-Account-Id` and `originator:
codex_cli_rs`; the gateway substitutes the token). Models whose `service_tiers`
include `priority`, `fast` or `ultrafast` form `fast_models`; those with
`ultrafast` form `ultrafast_models`.

The catalog is read once after each dashboard start, then every 6 hours, and
whenever the account's plan type changes, from the regular gateway poll. A failed
read keeps the last good list and waits 15 minutes before retrying. Only model
ids, display names and tier ids are kept, in
`/var/lib/personal-codex-router/codex-model-catalog.json` (0640), so a restart
does not lose the list. An account whose catalog has never loaded has no
ultrafast models and still offers Normal/Fast. A known catalog with no fast-capable
model shows Normal without a slider, including after a failed read that retains
that list. The saved choice stays intact, but the policy uses `level: normal` and
`allow_fast: false` until a fast-capable model returns, when the saved choice
applies again. The plugin does not inspect credentials or guess capability.

The speed confirmation describes Fast as using up to 2.5× the Codex allowance of
Normal, and Ultrafast up to 8×; rates vary by model.

At registration and every plugin reconfiguration, the plugin atomically writes
`/var/lib/cliproxyapi/fast-mode-plugin-status.json` (0640):

```json
{
  "plugin": "fast-mode-policy",
  "version": "0.2.0",
  "pid": 12345,
  "loaded_at": "2026-10-02T23:59:00Z",
  "policy_path": "/var/lib/personal-codex-router/fast-mode-policy.json",
  "map_unsupported_ultrafast": true,
  "audit": false,
  "policy_schema": 2
}
```

`policy_schema` is the capability marker: 0.1.x statuses omit it (treated as 1).
Selecting Ultrafast from another level requires live enforcement,
`policy_schema` 2 or more, and a catalog listing ultrafast models.
Views expose `levels`, `fast_available` and
`fast_unavailable_reason` (`no_fast_models` or null), alongside the Ultrafast
fields. `level` and `allowed` describe the saved choice. `POST /api/fast-mode`
rejects Fast and Ultrafast for Normal-only accounts with 409 code
`no_fast_models`; Normal can always be selected. Selecting Ultrafast from another
level otherwise returns 409 with a code (`catalog_pending`, `no_models`, `plugin_not_enforced`,
`plugin_outdated`). A level already saved as ultrafast stays saved if the plugin
is later rolled back; 0.1.x then enforces its schema 1 mirror.

The dashboard infers the tier each request ran at from its recorded requested
tier, model, and the level and allowlist in effect at the request timestamp.
Controls keep a bounded history of level changes, each with the allowlist then in
effect; catalog-driven effective-level changes and, while ultrafast runs,
allowlist changes add entries with reason `catalog` too. These preserve rows
before and after a Normal-only override without changing the saved choice.
Schema 1 history entries (`allow_fast` only) still read as
normal or fast, retaining the old ultrafast pass-through inference for accounts
with the legacy `(25x)` plan label. That label affects only old history, never
the enforced policy. Request rows do not record the client format:
Codex clients use the Responses API, so rows are inferred as `openai-response`
requests. At fast, ultrafast reads as priority; at ultrafast, a fast or
ultrafast request on an allowed model reads as ultrafast and on any other model as
priority. Rows report `fast_mode_level` alongside `fast_mode_allowed` (false only
at normal) and `forwarded_service_tier`. The Codex backend echoes `default` or `auto`
for priority requests that measurably ran faster (verified 2026-10-03), so
default, auto and missing served-tier echoes do not change that inference; only a
priority or ultrafast echo while the toggle was off counts, as "Still fast".
Legacy rows without a recorded requested tier remain unknown (`—`). The collector
normalizes an omitted request tier to normal; a missing, null or empty
`response_service_tier` remains unknown. The Speed column shows the inferred
forwarded tier, with a tooltip for upgrades or caps. Current plugin availability
controls the "Not enforced" card state without historical enforcement tracking.

Daily backups include the optional durable
`/var/lib/personal-codex-router/fast-mode-controls.json`, including change history.
Each saved account keeps `level` and the schema 1 `allow_fast`, and every history
entry keeps `allow_fast`, so an older dashboard can still read the file after a
rollback (it sees ultrafast as fast on). The model catalog cache is rebuilt from
the provider and needs no backup.
The dashboard regenerates `fast-mode-policy.json` from those controls and current
account metadata. The plugin status is transient and must be recreated by the
running gateway; neither derived policy nor status needs a separate backup.

The dashboard treats enforcement as live only when this non-disabled status PID
matches the running `cliproxyapi` MainPID, whose executable or command line
identifies `cli-proxy-api`. Status timestamps may include fractional seconds.
Shutdown removes a status file only if it still belongs to that library instance;
a retired version cannot delete a new version's marker. Registration fails if
status publication fails, and failed registration/reconfiguration clears its owned marker. A newly loaded plugin
cannot silently claim live enforcement. Use the installer to disable: editing
config alone does not mark the status disabled.

Use the [README installation and update procedure](../README.md#update-an-existing-installation)
for full installs: the plugin is the final step, with exit 4 on failure after the
rest is installed. Current loaded versions are reused, explicit disables are
preserved, and scoped installs and `--skip-fast-mode-plugin` leave it unchanged.
Installation works before Codex sign-in; policy and controls may be absent until
the dashboard sees accounts. A missing policy uses the default Fast behavior
above, without Ultrafast upgrades.

The direct build/deploy route remains available:

```sh
plugins/fast-mode-policy/build.sh /tmp/fast-mode-build
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --library /tmp/fast-mode-build/fast-mode-policy-v0.2.0.so
sudo python3 scripts/install-fast-mode-plugin.py --library /tmp/fast-mode-build/fast-mode-policy-v0.2.0.so
```

For direct deployment, update the dashboard first so it can write schema 2
policies. After the plugin's verified reload, eligible accounts can select
Ultrafast on the next overview poll; no gateway restart is needed.

Omitting `--library` asks the root installer to build as the unprivileged checkout
owner through `runuser` in a private `/tmp` output directory. A root-owned checkout
requires a separately built `--library`. Install the C compiler (`gcc libc6-dev`);
the build provisions pinned, checksum-verified Go 1.26.10 if needed. See the
[build guide](../plugins/fast-mode-policy/README.md) for toolchain selection and caching.
A dry run builds nothing and prints only a public plugin-settings diff, omitting
the rest of the secret-bearing config. For an offline config diff, use
`--dry-run --config /tmp/config.v8.yaml`.

The installer creates root-owned directories and installs a root:root 0644 library
at `/opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.2.0.so`.
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
        version: 0.2.0
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

Compare the status PID to MainPID and confirm version 0.2.0, `policy_schema: 2`,
the policy path and mapping flag. The installer itself also checks `/proc/<pid>/exe` or cmdline.
The policy file is written by the dashboard; deployment does not create or change
account policies. Audit defaults off; its allowlisted fields are described below.

Disable through a verified config reload (exit 3 means runtime deactivation remains unconfirmed):

```sh
sudo python3 scripts/install-fast-mode-plugin.py --dry-run --disable
sudo python3 scripts/install-fast-mode-plugin.py --disable
```

The installer marks status `disabled: true` after the logged reload, so the
dashboard shows enforcement as not live. A reload timeout retains the old marker.
Keep the immutable library in place. Full installs preserve this disable.
Re-enable explicitly by rerunning the direct deployment command above.
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
`audit: true` decision logs contain only validated 16-digit hex auth indexes,
the level (`normal`, `fast`, `ultrafast` or `v1`), allowlist membership, and
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
and repairing metadata, enabled candidates require a readable library and check
both units' effective `systemctl show` sandbox properties, service identities,
directory traversal and read/create/rename modes for policy, controls, status,
system-bus and process paths. Policy and controls may be absent; preflight checks
access using their future service owner and modes (0640 and 0600 respectively).
Disabled or plugin-free candidates check only config, backups and the marker outputs the installer writes, including
both old and new status paths when they differ. They require neither a policy nor
a library nor service access to those paths. An enabled candidate requires an
explicit absolute `plugins.dir`; missing or relative directories are never
resolved against the installer's working directory. Explicit and automatic
rollbacks use the restored candidate's requirements.

Hidden, read-only or privately mounted required paths produce a public
`Preflight:` diagnostic without changing config. Unrecognized filesystem
remapping is refused rather than guessed. `--dry-run` remains an offline config
diff and does not perform this deployment preflight.
