# Gateway 8.0.79 upgrade preparation — 2026-11-19

**8.0.47 remains live. 8.0.79 is installed and staged; no cutover was performed.**
**The backup prerequisite is resolved by disabling local KV; the coordinator must apply the reviewed safeguard during cutover.**
The implementation worker did not review-loop, commit, push, deploy the dashboard,
change live config, activate a drop-in, reload systemd, or signal/restart the live
gateway. The coordinator must review the final changes and select an idle window.

Selected [v8.0.79](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.79),
published 2026-11-19 13:11:56 UTC, revision
`6faa8d20b2673404cff70a2f2d9537ae0d0e684d`.
The installed [8.0.47](gateway-upgrade-8.0.47-2026-10-29.md) is the rollback.
Public/synthetic evidence is in
[gateway-upgrade-evaluation-v8.0.79.json](gateway-upgrade-evaluation-v8.0.79.json).

## Prepared artifacts

- Official plugin-capable `CLIProxyAPI_8.0.79_linux_amd64.tar.gz`, 26,498,640 bytes.
  SHA-256 `7cbb9709ed939ff3684e10592a45cc538ee82d5da759daa7e18c7e1dc3b32acd`
  matches upstream `checksums.txt` and GitHub's asset digest. `checksums.txt`
  SHA-256 `749d129edb7e38370f30b974f0d368a74d1e79886353b152a0935fab3bae9c78`
  also matches its GitHub digest. Both downloaded into a new empty scratch directory.
- `/opt/cliproxyapi/8.0.79/cli-proxy-api`: root:root 0755, 78,610,216 bytes;
  parent directory root:root 0755. Executable SHA-256
  `4968bd27b04f813849add020b95a26dc7abf3e30810b97d291c0d88f495fe601`.
  `-h` prints `CLIProxyAPI Version: 8.0.79, Commit: 6faa8d20, BuiltAt: 2026-11-19T13:12:31Z`
  without starting a listener. Prior installations remain intact.
- Installed, unchanged fast-mode-policy v0.2.0 library:
  `/opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.2.0.so`,
  SHA-256 `1261eeab031edb7b506b42f9a72be70362430cf5a6a117a9c0627ad07022a0f6`.
  No plugin rebuild or installation is needed.
- `/etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.79.conf.staged`:
  root:root 0644, SHA-256
  `1907b28f1ea996b045171801592365d8378ca62becf05c7c2ad918ec197657a7`.
  The `.staged` suffix is ignored by systemd. Full content:

  ```ini
  [Service]
  # Prepared 2026-11-19; ignored until the coordinator chooses an idle cutover window.
  # Activate only with the reviewed 8.0.79 runbook; park the 8.0.47 drop-in first.
  ExecStart=
  ExecStart=/opt/cliproxyapi/8.0.79/cli-proxy-api -config /etc/cliproxyapi/config.yaml
  ```

The active command also omits `-local-model`. All existing drop-ins, including
`model-catalog-updates.conf`, `zz-config-write.conf`, old staged files and rollback
files, remain untouched. No config was installed or edited.

## Verification

Run from the assigned `gateway-8.0.79` worktree based on main `0fceeec`;
`E` denotes `$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.79`.
All gateway processes use synthetic auth, loopback listeners, and non-forwarding
loopback TLS mocks. Background external CONNECT attempts are rejected. No
production OAuth file was opened, no request was sent to the live gateway, and
no live usage queue was consumed. Every finite integration/full-suite job used
`heavy`. These are successful synthetic compatibility gates, not live acceptance.

```sh
E=$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.79
heavy python3 -B tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --assume-tested --scratch "$E/replace"
heavy python3 -B tests/gateway_replace_integration.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --scratch "$E/replace"
heavy python3 -B tests/gateway_upgrade_isolated.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --scratch "$E" --output "$E/routing-8.0.79.json"
heavy python3 -B tests/gateway_upgrade_delta_isolated.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --scratch "$E" --output "$E/delta-8.0.79.json"
for version in 8.0.79 8.0.47; do
  for steering in false true; do
    heavy python3 -B tests/fast_mode_isolated.py --binary /opt/cliproxyapi/$version/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.2.0.so --response-steering "$steering" --scratch "$E"
  done
  heavy python3 -B tests/codex_websockets_isolated.py --binary /opt/cliproxyapi/$version/cli-proxy-api --scratch "$E/websockets-$version"
done
heavy python3 -B tests/sandbox_plugin_check.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.2.0.so --output "$E/sandbox-8.0.79.json"
heavy python3 -B -m unittest discover -s tests -q
```

- **Credential replacement:** 50 randomized runs and 12 in-flight refresh races
  passed on each binary; unchanged PATCH-only negative control, next refresh,
  priority/enable sequencing and isolated restart persistence passed. Seeds and
  confirmed/unconfirmed outcomes are retained in the evaluation JSON. The locked
  six-field writer was not changed.
- **Routing/config/move/failover/Claude:** 76/76 assertions with local KV disabled. Existing model routes,
  same-inode TTL pin release for both providers, 429/503 same-request failover and
  sticky rebind, strict nested-v8 PATCH, no-key rejection, Claude thinking and SDK
  wire shape passed. New checks cover supplied retry 5 / interval 45 / transient
  cooldown 15, model-level Claude cooling, matching 404 continue and 503 overload
  stop rules. Stop makes exactly one upstream attempt. Rule match text is synthetic.
- **Usage:** 39/39 records persisted by the unchanged collector, both providers;
  zero quarantines/failed spools. The collector's original queue URL was asserted
  then rewritten to the disposable port before opening. Additive effective tier
  and structured fields do not break reduction. Detailed new modality billing
  semantics and all rejected-turn accounting are not claimed.
- **Delta:** 38/38 assertions. Existing auth precedence, gzip/identity negotiation,
  compressed SSE and Codex/private metadata policy; no local database with KV disabled;
  in-flight authentication/count shape; output budget normalization; unsupported
  `uniqueItems` removal with literal property retention; tunnel auth/realm
  separation; instruction baseline start/reuse/caller isolation and private marker
  stripping; management cache key rotation rejects the old key.
- **Fast-mode:** 107/107 HTTP policy cases in each of four runs (both binaries,
  steering false/true), plus failover, policy/config reload, same-PID library-path
  replacement, marker lifecycle and isolated restart. Installed v0.2.0,
  `policy_schema: 2` used unchanged. Denied follow-up tiers are removed on both
  versions in serial and tested duplex cases; enabling steering remains forbidden.
- **WebSockets:** 39/39 assertions, 24 completed responses and six fixture tests
  on each binary. Native Codex CLI 0.171.0 used disposable homes and synthetic auth;
  warmup/first inference reuse, continuation IDs, reconnect, HTTP→WS→HTTP flags,
  current-PID steering reload, exact token/account multisets, and the actual late
  payload override hazard passed. No native/client credential was inherited.
- **Sandbox:** unchanged v0.2.0 registered under equivalent `cliproxyapi` identity,
  UMask, privilege/capability, address-family, PrivateTmp, ProtectSystem/Home,
  kernel/cgroup restrictions, with extra localhost-only egress. Current-PID marker
  and a private-temp content-addressed `.so` mapping passed; transient unit and
  state/runtime directories were cleaned up.
- **Full Python suite:** 1,592 tests passed in 123.010 seconds, zero failures (one final run for the KV-disabled follow-up). Existing SQLite
  ResourceWarnings and toolchain-install diagnostics were emitted.
- **Offline merged-unit verify:** exit 0, only two unrelated installed XFS
  CPUAccounting warnings. Documentation shell/Python syntax, changed Python AST,
  and `git diff --check` are checked separately in the evaluation.

### KV-disabled follow-up

The backup prerequisite is resolved in this checkout; applying the safeguard to
live configuration remains the coordinator's cutover step. Both binaries started
with `server.kv-store.disabled: true`. 8.0.79 passed **76/76 routing/config checks**,
saved **39/39 usage records** with zero quarantines/failed spools, and passed
**38/38 delta checks**. The auth directory contained no database extension or
bbolt page-zero magic before and after the routing gate. Strict nested-v8 PATCH
preserved the key without unknown/removed/deprecated config warnings.

8.0.47 served both providers with the key present, but strict PATCH returned 400
and logged `unrecognized configuration section "server.kv-store"`. After the
tested same-inode removal, strict PATCH and the remaining routing/failover/usage
gates passed: **71/71 assertions**, **35/35 usage records**, zero quarantines or
failed spools. Rollback must remove the key first; startup tolerance alone is
not config compatibility.

The backup and config-helper focused suites passed **16 and 18 tests**. Synthetic
receipt fixtures confirm recursive hidden-directory coverage and original private
0600 file / 0700 directory modes under the backup runner's 0077 umask. A bbolt,
short invalid database and empty database all fail before SQLite is called, naming
the file and the disabled-KV safeguard. Real WAL-backed SQLite snapshots still
include committed WAL data and omit uncommitted transactions.

The 8.0.79 install example passed **9/9 checks**: startup and authenticated models,
unauthenticated refusal, absent management with an empty secret, unchanged startup
config, same-PID management activation, strict PATCH, key persistence, no KV file,
and no config warnings. The check waits for the file watcher's readiness log before
editing; an initial attempt raced watcher setup and timed out, then exited cleanly.
No clean-machine/system-service installation or real provider request was performed.
The example unit's offline verification also passed, with only the two existing
unrelated XFS CPUAccounting warnings. All seven shell blocks and fourteen Python
heredocs in this record passed syntax checks, as did changed Python ASTs and
`git diff --check`.

The follow-up left live PID **3067**, executable
`/opt/cliproxyapi/8.0.47/cli-proxy-api`, `NRestarts=0`, ExecStart and the three active
drop-ins unchanged. No reload, signal, config edit or deployment was performed.
All recorded isolated gateway processes exited, all seven final gateway/mock ports
are closed, and all synthetic temporary directories were removed. No transient
unit or persistent process was started. Small synthetic evidence remains under
`$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.79-kv` (0700).

```sh
K=$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.79-kv
for version in 8.0.79 8.0.47; do
  heavy python3 -B tests/gateway_upgrade_isolated.py --binary /opt/cliproxyapi/$version/cli-proxy-api --scratch "$K" --output "$K/routing-$version.json"
done
heavy python3 -B tests/gateway_upgrade_delta_isolated.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --scratch "$K" --output "$K/delta-8.0.79.json"
python3 -B -m unittest discover -s tests -p test_backups.py -q
python3 -B -m unittest discover -s tests -p test_gateway_config.py -q
heavy python3 -B -m unittest discover -s tests -q
```

The first unchanged routing run failed its old SDK profile expectation: the
candidate normalized legacy 2.1.304 to `claude-cli/2.1.321 (external, cli)`.
Source at v8.0.63 (`b4e7dbd2`) explains the baseline change. The final fixture
requires current native SDK profile/system preservation and separately requires
legacy normalization; the original failure remains in private scratch and the
public reassessment summary. No production behavior was changed to pass it.

Only after the matching gates passed, all three exact-version sets gained
`8.0.79`: sign-in replacement, conversation moves, and account-add tested status.
Existing versions remain accepted; dirty/prefixed/suffixed versions are refused
where exact tested status is required. Account-add's broader supported-v8 policy
is unchanged. No allowlist was deployed.

## Upstream 8.0.48 → 8.0.79 assessment

All 32 release notes and immutable release-tag diffs were assessed, covering
299 non-merge commits. The evaluation preserves each tag revision, full commit
inventory, changed-path inventory, source focus and diff hash. Import migrations,
non-active providers, Home, TUI, catalog and documentation changes are included;
they do not establish runtime acceptance for those paths. Source focus links
below refer to the exact release tag, not mutable main. This is compatibility
assessment, not an independent security audit of every upstream line.

### Decisions and material risks

1. **Resolved — disable local KV.** v8.0.55 enables local bbolt by default at
   `<auth-dir>/kv.db`; the selected configuration sets
   `server.kv-store.disabled: true`. Session, date-pin and replay state stay in
   memory, preserving 8.0.47 behavior and avoiding permanent Claude date pins.
   The nested-v8 routing/usage and delta gates on 8.0.79 create no database;
   strict PATCH accepts the key without config warnings. Backup now checks the
   SQLite header and fails clearly on any non-SQLite database, rather than
   silently skipping it or attempting an unsafe live copy. No bbolt backup is
   implemented. Recursive auth backups already include private `.codex-refresh`
   receipts at their original 0600 file / 0700 directory modes, verified with
   synthetic fixtures. 8.0.47 starts and serves both providers with this key,
   but strict PATCH rejects it and warns: remove the safeguard in place before
   rollback as shown below. The live config has not been edited.
2. **New refresh journal and rollback semantics.** v8.0.59 stores immutable
   claims/secret-bearing receipts below `<auth-dir>/.codex-refresh`, outside the
   credential JSON, with private directory/file modes. Uncertain post-send
   refresh results fail closed rather than reusing a possibly spent grant;
   pre-send failures and wake deferral are refined later. Receipts may contain
   credentials: never log, publish, delete casually, or restore them over later
   tokens. Fresh whole-auth-directory backups must retain this state. 8.0.47
   does not implement this protection, so a binary rollback after an uncertain
   refresh requires explicit reconciliation. Synthetic refresh races passed;
   real provider rotation/outage recovery remains untested.
3. **Plugin ABI remains compatible.** Native ABI stays 1. JSON schema is 9 at
   v8.0.48 and 10 from v8.0.49 onward, adding multi-provider models and multi-entry
   OAuth. The plugin source uses the pinned v8.0.7 schema-6 SDK; its policy schema
   2 is a separate contract. Ordinary request interceptor body adapters are
   unchanged except the JSON import. Additive response `Kind`, usage, attribution
   and scheduler capabilities do not force opt-in. `dlopen` subprocess preflight,
   quiesce failure handling, ownership and shadow caching improve host handling.
   Root-owned `/opt` is unwritable to the service, so the tested PrivateTmp
   fallback remains important. No rebuild or redesign was performed.
4. **Tiers and payload order.** Existing priority/ultrafast policy, per-account
   failover and serial/duplex hooks passed with the installed plugin. v8.0.76
   enforces payload rules as a final barrier: later overrides or new regex/path
   replacements can restore/change a tier after the plugin. Live key inventory
   has no `requests.payload`; the coordinator must rerun the keyless preflight
   before cutover. Keep `upstream.codex.response-steering: false`; successful
   create/native-warmup tests do not validate every interrupt/error interaction.
5. **Retry/cooldown behavior is broader than a 429 fix.** Equivalent model names
   share cooldown, bounded expiry restores listings, forced cooldown survives
   persistence/disabled-cooling handling, and terminal pool 429s carry reset
   hints. Codex nested/untyped WS/SSE errors preserve status and Retry-After;
   pre-commit errors can fail over while post-commit replay is guarded. Request
   refusals are classified without poisoning credentials; rule classification
   reaches usage failures. The supplied retry, cooling, 404 continue and overload
   stop shapes passed synthetic checks. Exact nested RetryInfo/error-frame,
   uncertain-refresh, forced-cooldown persistence and every partial-output retry
   path are source-assessed, not all fault-injected. Only one live Codex account
   remains; rotation cannot create another account's capacity.
6. **Management stays compatible but gains surface.** Existing `/v0/management`
   auth-files fields/status PATCH, config and usage queue remain available;
   `/v8` strict config and model-definition routes persist. New credential fields
   (`oauth_provider`, cooldown details), plugin priority and in-flight counters
   are additive. Scoped v8 plugin upload/delete identity fixes do not replace
   the built-in locked Codex writer. Management secret caching is bounded
   (64 entries, one-hour TTL, HMAC digest over hash+provided key); synthetic
   rotation rejected the previous key. Keep management loopback-only.
7. **New tunnel is always registered.** `/v1/tunnel` is authenticated with
   inference API keys, isolates realms by key, and supports query-string keys
   plus permissive WS origins. It relays device-to-device data and does not
   itself grant management. Missing/wrong keys and cross-key discovery/UUID
   isolation passed; packet routing/load/revocation races are source-only here.
   This is additional authenticated attack surface on the existing inference
   listener; preserve firewall restrictions and do not use query credentials
   in links/logs. No new listener/firewall change was made.
8. **Claude wire/default changes need live acceptance.** v8.0.63 advances the
   native baseline; older SDK callers can be normalized/cloaked differently.
   v8.0.62 disables default upstream connection reuse and reverts post-stream
   draining; v8.0.66 makes automatic cross-model refusal fallback opt-in/default
   false, so a refusal may surface instead of silently using another model.
   v8.0.59 aligns new Claude authorization with native scopes; v8.0.78 makes
   `org:create_api_key` optional but defaults it on. Existing refresh scope stays
   unchanged. No new login or scope grant occurred. Fable/model-only 429,
   tool-result/media/schema, thinking/cache-control and error classification
   fixes were assessed. Current native SDK and legacy normalization tests passed;
   real Claude/T3 signatures and long conversations remain cutover acceptance.
9. **Usage/compression/metadata.** Structured modality/cache/search/tool/iteration/
   cost fields grow additively; consumed queue slots are cleared. Existing queue
   collection and successful-turn token accounting passed. Existing compression
   defaults remain: negotiated HTTP/WS compression on, upstream HTTP request
   compression off. Gzip/identity and metadata filtering passed. All codecs,
   negotiated WS compression, detailed billing and every rejected turn remain
   unverified. The collector continues to discard fields it does not store.
10. **Other defaults/features.** `server.shutdown-timeout` is opt-in/default 0;
    do not assume restart drains in-flight requests. `session-affinity-rebind-on:
    never`, connection pools, commentary, payload text replacements and scheduler
    continuity are opt-in. Home-backed continuity is irrelevant without Home.
    Antigravity pool-wide TOS pause ends default-off at v8.0.72; violating
    credentials still deactivate. v8.0.77 embeds GPT-6.2; keep catalog refresh
    enabled by omitting `-local-model`. v8.0.78 instruction management requires
    an explicit reserved marker/session; its isolation and stripping passed.
    v8.0.79 strips unsupported schema `uniqueItems` without removing same-named
    properties, tested at the wire.

### Config key assessment

Only key names/types were captured from the live config; no values, keys, tokens
or raw auth files were printed. All present paths remain recognized in the
candidate nested-v8 mapping/validation. Shared root `quota-exceeded` is still
allowed; no migration is needed. Existing plugin custom keys remain plugin-owned.
No removed/deprecated present key was identified; the earlier ignored
`server.discovery.advertise-management` key is absent. New optional keys include
KV, shutdown timeout, per-key pools, native entrypoints, fallback, Claude OAuth
scope, and payload replacement. Migration warnings now emit only on persistence,
so absence of a startup warning alone is not used as proof. The synthetic strict
PATCH is separate from the static live-key assessment; no production PATCH ran.

### Release-by-release source assessment

| Tag / revision | Material assessment | Tagged source focus |
| --- | --- | --- |
| v8.0.48 / `dbb706215a3a` | Structured provider usage and incomplete-stream accounting; Codex renewal partition state and quarantines; Claude tool additions; plugin multi-provider models schema 9; Home WebSocket restriction. | [sdk/cliproxy/usage/manager.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.48/sdk/cliproxy/usage/manager.go) |
| v8.0.49 / `250d598175db` | Opt-in visible Codex commentary for Claude clients preserves replay; multi-entry OAuth plugin schema 10. Gemini/Antigravity sanitizers do not select new live providers. | [internal/runtime/executor/helps/codex_commentary.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.49/internal/runtime/executor/helps/codex_commentary.go) |
| v8.0.50 / `f7331b519773` | Per-API-key connection pools are opt-in and credential-isolated; rejected Codex refresh remains quarantined; Anthropic insufficient-credit 400 becomes 402; same-format plugin SSE framing and replay sanitation. | [internal/config/config_types.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.50/internal/config/config_types.go) |
| v8.0.51 / `7cad1cb7df48` | Do not auto-refresh Codex without refresh_token/SIWC; preserve disabled refresh backoff; OAuth plugin entry metadata and translator role/schema fixes. | [sdk/cliproxy/auth/conductor_refresh.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.51/sdk/cliproxy/auth/conductor_refresh.go) |
| v8.0.52 / `6e6afab99a19` | Plugin model streams become owned by plugin instance and reject registration after teardown; pool dedup keys include settings; panic cleanup; broad moego JSON/base64 migration. | [internal/pluginhost/model_stream_bridge.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.52/internal/pluginhost/model_stream_bridge.go) |
| v8.0.53 / `01b24f1f95dc` | Disabled rejected-refresh credentials stop spinning; Fable-only 429 remains model-scoped; targeted OAuth re-login preserves identity; dlopen/init subprocess preflight; native-entrypoints; Codex output budgets retained. | [internal/pluginhost/loader_unix.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.53/internal/pluginhost/loader_unix.go) |
| v8.0.54 / `78a9cc9aaa8a` | Abort plugin replacement on failed quiesce and coordinate bounded usage replay; cached modality usage fields; Codex input-ID sanitation; mid-conversation Claude effort selection. | [internal/pluginhost/host.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.54/internal/pluginhost/host.go) |
| v8.0.55 / `e6e5708e677a` | Default-on local bbolt at auth-dir/kv.db; permanent KV-backed Claude session date pins; remote compaction v2 support on compatible routes. Default DB is incompatible with SQLite backups; the selected safeguard disables local KV. | [sdk/cliproxy/kv_store.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.55/sdk/cliproxy/kv_store.go) |
| v8.0.56 / `585f97deac9a` | Classify policy/cyber refusals as request-caused 400s; bounded error buffering; preserve terminal 429 WS frames and usage evidence; image routing and TTFT corrections. | [sdk/api/handlers/openai/openai_responses_websocket.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.56/sdk/api/handlers/openai/openai_responses_websocket.go) |
| v8.0.57 / `c2f402b45643` | Clear consumed usage queue references; unsupported count_tokens 501 remains availability-neutral; hosted image compatibility and provider model/header fixes. | [sdk/cliproxy/usage/manager.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.57/sdk/cliproxy/usage/manager.go) |
| v8.0.58 / `e5e6ad1e1b91` | Preserve terminal response before admission-release failures; Codex positive output budgets below 16 raised to 16; cooldown retry diagnostics; Claude/Gemini translation repairs. | [sdk/cliproxy/auth/conductor_execution.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.58/sdk/cliproxy/auth/conductor_execution.go) |
| v8.0.59 / `c5251b57b3fa` | Durable Codex refresh claims and secret-bearing receipts prevent replay after uncertain refresh outcome; oauth_provider additive credential field; targeted re-login metadata; Claude authorization scope/state alignment. | [internal/auth/codex/refresh_journal.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.59/internal/auth/codex/refresh_journal.go) |
| v8.0.60 / `91e0dee827ab` | Reusable content-addressed plugin shadows with writable-library-local .shadow preference and private-temp fallback; native Codex survives SIWC recovery; Claude post-stream drain introduced then reverted in 62; transcript/tool repair. | [internal/pluginhost/shadow.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.60/internal/pluginhost/shadow.go) |
| v8.0.61 / `681ce41b6e44` | SIWC receipt/rotation/window/wake handling; explicit session-affinity-rebind-on never (not default); usage modality plugin additions; orphan tool-output repair. | [internal/config/config_types.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.61/internal/config/config_types.go) |
| v8.0.62 / `4b3f2c7ec7cd` | Claude default connection reuse disabled and drain reverted; equivalent model spellings share cooldown; weighted sticky-session fairness; credential-scoped plugin 429; stable native Claude field/cache-control handling. | [internal/auth/claude/utls_transport.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.62/internal/auth/claude/utls_transport.go) |
| v8.0.63 / `6db7d4b839d8` | Runtime auth-dir watcher updates correctly; Claude native fingerprint moves to 2.1.321/package 0.128.0/runtime v26.12.1; login-proxy flag; model registry additions. | [internal/watcher/config_reload.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.63/internal/watcher/config_reload.go) |
| v8.0.64 / `905637daa3f2` | Request error-rule classification reaches usage fail.code; WS keepalive starts after upgrade; Claude native cli-bg and schema repairs; compatible Responses compaction. | [sdk/cliproxy/auth/conductor_request_scoped_errors.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.64/sdk/cliproxy/auth/conductor_request_scoped_errors.go) |
| v8.0.65 / `65de937cf19b` | Bounded cooldown expiry restores advertised models; local Claude cooldown emits ratelimit headers; bounded upstream error logs; optional sensitive-word replacements and Home cloak options. | [internal/registry/model_registry.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.65/internal/registry/model_registry.go) |
| v8.0.66 / `fa11a573b252` | Cross-model Claude server-side fallback now opt-in, default false; actual serving model surfaced; scheduler plugin rejection preserves retry rounds. | [internal/runtime/executor/claude_executor_cloaking.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.66/internal/runtime/executor/claude_executor_cloaking.go) |
| v8.0.67 / `8bba7a77c6bc` | Large weighted-pool fairness and durable forced-cooldown preservation; sponsor docs only otherwise. | [sdk/cliproxy/auth/conductor_cooldown.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.67/sdk/cliproxy/auth/conductor_cooldown.go) |
| v8.0.68 / `42f9adb9acd3` | Always-registered /v1/tunnel uses inference API keys and per-key realms, accepts query keys, permits cross-origin WS; adds attack surface without a separate opt-in. Nonzero startup failures; refresh recovery backoff; translator and TUI fixes. | [internal/wstunnel/handler.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.68/internal/wstunnel/handler.go) |
| v8.0.69 / `f7ae8feeb371` | server.shutdown-timeout permits bounded graceful HTTP draining, defaults to zero/immediate close; WebSocket managers own hijacked connections. | [internal/api/server.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.69/internal/api/server.go) |
| v8.0.70 / `905e70c48b47` | Authenticated standalone in-flight counts; one-hour bounded HMAC management-auth cache with hash/key identity; terminal 429 carries pool deadline; plugin runtime priority; optional Antigravity TOS pause and sensitive patterns; Muse relay. | [internal/api/handlers/management/auth_cache.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.70/internal/api/handlers/management/auth_cache.go) |
| v8.0.71 / `bd9c766f3484` | Scheduler ExcludeCandidates propagates through fallback/delegation; moego/bbolt dependency update; Gemini Interactions format fixes. | [sdk/cliproxy/auth/conductor_selection.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.71/sdk/cliproxy/auth/conductor_selection.go) |
| v8.0.72 / `e96d4c481885` | Antigravity pool-wide TOS pause default returns to disabled; individual violating credential still deactivated. No Antigravity active-route change. | [internal/config/config_types.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/config/config_types.go) |
| v8.0.73 / `43806c865da7` | v8 plugin upload/delete preserves credential identity; provider-isolated model lookup; Gemini Retry-After/RetryInfo handling; Claude JSON-schema output mapping. Built-in Codex locked writer remains separate. | [internal/api/handlers/management/auth_files_crud.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.73/internal/api/handlers/management/auth_files_crud.go) |
| v8.0.74 / `14144df573c7` | Responses WS create failures fail over before downstream commitment; duplex quota errors after response.created enter replay guard; broad moego regexp/JSON migration. Steering remains off. | [sdk/api/handlers/openai/openai_responses_websocket.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.74/sdk/api/handlers/openai/openai_responses_websocket.go) |
| v8.0.75 / `c19787b25262` | Opt-in payload text replacement rules, preserved empty replacements; default no text mutation without rules. | [internal/config/payload_replace.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.75/internal/config/payload_replace.go) |
| v8.0.76 / `7df56074b252` | Nested/untyped WS/SSE errors preserve status and Retry-After including overload; final payload-rule barrier can override plugin decisions; bounded opt-in scheduler continuity; unavailable Claude Thread owner returns thread_not_found; expired Claude refresh is terminal. | [internal/runtime/executor/codex_executor_terminal.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.76/internal/runtime/executor/codex_executor_terminal.go) |
| v8.0.77 / `a5e8f69e5630` | Payload regex/system/developer/path replacements; PDF/media/tool-result translations; optional executor usage attribution and cache-write TTL counts; GPT-6.2 catalogs and Codex CLI baseline. | [internal/config/payload_replace.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.77/internal/config/payload_replace.go) |
| v8.0.78 / `db7f177d6b0e` | Explicit caller/session-scoped instruction start/reuse protocol persists prompt baseline in KV with 24h TTL and 32KiB bound; configurable Claude create_api_key authorization scope (default on); response interceptor Kind additive; migration warnings only on persistence; Mac wake deferral. | [internal/runtime/executor/helps/codex_session_instructions.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.78/internal/runtime/executor/helps/codex_session_instructions.go) |
| v8.0.79 / `6faa8d20b267` | Strip unsupported uniqueItems schema keyword while preserving literal properties; Home-backed scheduler continuity only when Home selected; Antigravity probe/changelist changes are outside active providers. | [internal/runtime/executor/helps/codex_tool_schema.go](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.79/internal/runtime/executor/helps/codex_tool_schema.go) |

Real OAuth/provider refresh, quotas, real billing, live T3/Claude/dashboard UI,
Mac access, provider signatures, sustained load, complete media/tool/schema
matrices, Home/other-provider operation and Windows installation remain outside
this preparation. The full release inventory does not turn source-only findings
into runtime proof.

## Live baseline and cleanup

Before and after, the live process remained **PID 3067**, executable
`/opt/cliproxyapi/8.0.47/cli-proxy-api`, `NRestarts=0`.
`ActiveEnterTimestamp` and `ExecMainStartTimestamp` both remained
`Wed 2026-09-16 11:55:38 -03`. These are systemd's captured timestamps, not an
inferred upgrade date. ExecStart and active DropInPaths were byte-identical:

- `model-catalog-updates.conf`
- `zz-config-write.conf`
- `zz-gateway-8.0.47.conf`

The directory list before/after is retained in the JSON: those three active files,
`zz-gateway-8.0.30.conf.rollback-8.0.47`, `zz-gateway-8.0.43.conf.staged`, and
`zz-gateway-8.0.7.conf.rollback-8.0.30` are unchanged; only
`zz-gateway-8.0.79.conf.staged` was added. Private hash/owner/mode comparison of
**84 baseline files** under `/etc/cliproxyapi`, the drop-in directory, deployed
`/opt/personal-codex-router`, and `/opt/cliproxyapi/8.0.47` found zero changes.
No production auth contents were opened or hashed; their byte identity is not
claimed. The live queue and inference listener were never called.

All **30 recorded owned gateway/native-client/harness PIDs** exited. All recorded
isolated ports, including replacement 18417/18419, are closed. The sandbox unit's
LoadState is `not-found`; its state/runtime directories and mock were removed.
No persistent server or browser tab was started. Final cleanup removed duplicate
archive/binary/source trees and synthetic auth/config/CA/client-home/plugin
copies, retaining small public metadata, sanitized results, synthetic logs and
private baseline hashes under `$HOME/.cache/agent-scratch/personal-codex-router/upgrade-8.0.79`
(mode 0700). Installed binary and ignored staged drop-in remain. No fresh
production credential backup was taken during preparation.


## Worktree changes

Changes are uncommitted in the assigned branch from main `0fceeec`. The worker
ran no authoritative review loop and made no commit/push/deployment. The
coordinator owns review and the cutover window.

- `README.md`
- `config/gateway.example.yaml`
- `config/cliproxyapi.example.service`
- `scripts/router-backup.py`
- `scripts/gateway_config.py`
- `tests/test_backups.py`
- `tests/test_gateway_config.py`
- `docs/standard-management-setup.md`
- `dashboard/account_add.py`
- `dashboard/server.py`
- `dashboard/signin.py`
- `docs/account-access.md`
- `docs/codex-websockets.md`
- `docs/fast-mode-policy.md`
- `docs/gateway-config-v8-migration.md`
- `docs/gateway-upgrade-8.0.79-2026-11-19.md`
- `docs/gateway-upgrade-evaluation-v8.0.79.json`
- `tests/codex_websockets_isolated.py`
- `tests/fast_mode_isolated.py`
- `tests/gateway_upgrade_delta_isolated.py`
- `tests/gateway_upgrade_isolated.py`
- `tests/test_add_accounts.py`
- `tests/test_move_conversations.py`
- `tests/test_signin_retry.py`

The release checker selects semantic versions dynamically and needs no pin
change. Install-operations has no gateway-version pin; its account-access scoped
manifest includes all three allowlist modules. The fresh-install example/unit
use 8.0.79 after isolated example startup, management hot reload and strict PATCH
validation with KV disabled; a clean-machine installation was not repeated. Historical dated records remain historical.

## Cutover — user-selected idle window only

**Do not execute these commands as preparation.** First finish the parent's
review-fix-loop, publish/review the intended change, and use that reviewed tree.
The new allowlists must be deployed with the gateway cutover. The preparation
left `/opt/personal-codex-router` unchanged, so merely restarting the gateway
would leave the old dashboard refusing sign-in and conversation moves on 8.0.79.
No nested-v8 migration, plugin reinstall or credential restoration is needed.
The selected KV safeguard must be applied using the same-inode step below.

One gateway restart interrupts active requests and loses in-memory affinity
bindings. Agree an idle window across both machines, finish pending sign-ins,
and stop clients from sending requests until checks finish. The cutover smoke
below deliberately exercises the one Codex and two Claude accounts; if one is disabled, expired or
quota-limited, agree how to validate it before proceeding. Do not silently
enable accounts or change their priority, prefix, saved routing or fast policy.

The coordinator selected `server.kv-store.disabled: true`; no bbolt backup is
needed. This keeps session/date-pin/replay state in memory, so that state is
lost on restart as on 8.0.47. The commands below apply the safeguard only after
fresh private backups and pausing configuration writers. Do not enable local
KV, rename a database, or silently skip one in backups.

Preflight (retain these shell variables for subsequent blocks):

```sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)" # reviewed checkout containing this record
printf '%s  %s\n' 4968bd27b04f813849add020b95a26dc7abf3e30810b97d291c0d88f495fe601 /opt/cliproxyapi/8.0.79/cli-proxy-api | sha256sum -c -
printf '%s  %s\n' 167fff6edf264f71b7e28923b71f97cf1b49957db1612a6e5e8e570f42aeb112 /opt/cliproxyapi/8.0.47/cli-proxy-api | sha256sum -c -
sudo -n test -f /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.79.conf.staged
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.79.conf
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.43.conf
sudo -n test ! -e /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf.rollback-8.0.79
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
sudo -n python3 -B scripts/check-codex-websockets.py
python3 scripts/install-operations.py --account-access-only
CUTOVER_DIR=/var/lib/cliproxyapi-upgrade-backups/cutover-8.0.79-$(date -u +%Y%m%dT%H%M%SZ)
sudo -n install -d -o root -g root -m 0700 "$CUTOVER_DIR"
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path
system = ['personal-codex-router-' + u for u in ('usage.timer', 'preference.timer', 'ops.timer', 'backup.timer', 'request-outcomes.timer', 'provider-status.timer', 'retention.timer', 'release-check.timer', 'move.path', 'dashboard.service')]
def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
# User-manager state is captured separately by the invoking desktop user below.
(Path(sys.argv[1]) / 'resume-system.json').write_text(json.dumps([u for u in system if active(u)]))
(Path(sys.argv[1]) / 'before-service.txt').write_bytes(subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '-pMainPID', '-pExecStart', '-pActiveEnterTimestamp']))
PY
python3 - <<'PY' | sudo -n tee "$CUTOVER_DIR/resume-user.json" >/dev/null
import json, subprocess
units = ['personal-codex-router-' + u for u in ('claude-usage.timer', 'claude-b-usage.timer', 'claude-keepalive.timer')]
print(json.dumps([u for u in units if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', u]).returncode == 0]))
PY
```

Pause timer/path activations and wait for existing invocations rather than killing
them. Keep the dashboard running for the scoped installer's mandatory overview
preflight; all users must remain idle and pending sign-ins must be finished.
Stop the dashboard immediately after that installer, before selecting the new
binary. Leave the live gateway running until the single restart below.
If any step fails after this point, recover with the rollback/resume blocks;
do not leave the timers paused.

```sh
sudo -n systemctl stop personal-codex-router-usage.timer personal-codex-router-preference.timer personal-codex-router-ops.timer personal-codex-router-backup.timer personal-codex-router-request-outcomes.timer personal-codex-router-provider-status.timer personal-codex-router-retention.timer personal-codex-router-release-check.timer personal-codex-router-move.path
systemctl --user stop personal-codex-router-claude-usage.timer personal-codex-router-claude-b-usage.timer personal-codex-router-claude-keepalive.timer
python3 - <<'PY'
import subprocess, time
units = ['personal-codex-router-' + u for u in ('usage.service', 'preference.service', 'ops.service', 'backup.service', 'request-outcomes.service', 'provider-status.service', 'retention.service', 'release-check.service', 'move.service')]
user = ['personal-codex-router-' + u for u in ('claude-usage.service', 'claude-b-usage.service', 'claude-keepalive.service')]
deadline = time.monotonic() + 330
while True:
    states = [subprocess.check_output(['systemctl', *(['--user'] if is_user else []), 'show', u, '-pActiveState', '--value'], text=True).strip() for is_user, names in ((False, units), (True, user)) for u in names]
    if all(s in ('inactive', 'failed') for s in states):
        break
    if time.monotonic() >= deadline:
        raise SystemExit('An invocation did not finish; recover without cutting over')
    time.sleep(1)
PY
sudo -n test ! -e /run/personal-codex-router-move-request/request
sudo -n systemctl start personal-codex-router-usage.service
sudo -n python3 /opt/personal-codex-router/router-backup.py run
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
b = Path(sys.argv[1])
for source, name in [('/etc/cliproxyapi', 'config'), ('/var/lib/cliproxyapi/auth', 'auth'),
                     ('/etc/systemd/system/cliproxyapi.service.d', 'dropins'),
                     ('/opt/personal-codex-router/dashboard', 'dashboard')]:
    subprocess.run(['cp', '-a', source, str(b / name)], check=True)
(b / 'effective-unit.txt').write_bytes(subprocess.check_output(['systemctl', 'cat', 'cliproxyapi']))
(b / 'sha256.json').write_text(json.dumps({str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in b.rglob('*') if p.is_file()}, indent=2))
print('Fresh private backups:', b)
PY
# Capture owner/mode/inode and preserve the watched inode; no YAML reserialization.
# The original config was privately backed up above. Diagnostics never print values.
sudo -n python3 - "$PWD/scripts" "$CUTOVER_DIR" <<'PYKV'
import json, stat, sys
from pathlib import Path
import yaml
sys.excepthook = lambda *_: print('KV edit failed; inspect privately and recover before proceeding', file=sys.stderr)
sys.path.insert(0, sys.argv[1])
import gateway_config
path = Path('/etc/cliproxyapi/config.yaml')
before = path.stat()
metadata = lambda s: {'uid': s.st_uid, 'gid': s.st_gid, 'mode': stat.S_IMODE(s.st_mode), 'inode': s.st_ino}
record = Path(sys.argv[2]) / 'config-before-kv-stat.json'
with record.open('x') as stream:
    json.dump(metadata(before), stream)
original = path.read_bytes()
candidate = gateway_config.disable_local_kv(original)
gateway_config.write_same_inode(path, candidate, original)
assert metadata(path.stat()) == metadata(before), 'Owner/mode/inode changed'
assert yaml.safe_load(path.read_bytes())['server']['kv-store']['disabled'] is True
print('KV disabled; owner, mode and inode unchanged:', metadata(before))
PYKV
sudo -n python3 - <<'PYGUARD'
from pathlib import Path
import sys, yaml
sys.excepthook = lambda *_: print('Preflight failed; inspect privately before proceeding', file=sys.stderr)
c = yaml.safe_load(Path('/etc/cliproxyapi/config.yaml').read_bytes())
assert c.get('server', {}).get('kv-store', {}).get('disabled') is True, 'STOP: local KV safeguard missing'
assert c['management']['allow-remote'] is False
print('Backup prerequisite and loopback-only management preflight passed')
PYGUARD
# Recheck payload/steering policy against the edited config before selecting the new binary.
sudo -n python3 -B scripts/check-codex-websockets.py
# Inspect and apply the reviewed dashboard manifest; it restarts only dashboard/preview.
sudo -n python3 scripts/install-operations.py --account-access-only --apply
# The installer needs the running dashboard for preflight; stop it for the cutover/check window.
sudo -n systemctl stop personal-codex-router-dashboard.service
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.47.conf.rollback-8.0.79
sudo -n mv -T /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.79.conf.staged /etc/systemd/system/cliproxyapi.service.d/zz-gateway-8.0.79.conf
sudo -n systemctl daemon-reload
sudo -n systemctl restart cliproxyapi
systemctl is-active cliproxyapi
systemctl show cliproxyapi -p MainPID -p ExecStart -p DropInPaths
```

The two `mv` operations remove the stale 8.0.47 header from the effective unit
and make rollback explicit. Verify no later-sorting 8.0.7 drop-in is active. Leave the ignored 8.0.43
staged file parked; never activate both candidates. Neither the staged nor active
8.0.79 command may add `-local-model`.
The scoped dashboard installer has its own reviewed file manifest and backups;
it does call `daemon-reload`, which is allowed only during this chosen cutover.
Do not run a full router install or nested-to-legacy migration.

## Post-restart checks — before resuming clients

The smoke check expects one Codex and two Claude file credentials, already enabled.
No account removal, re-login or roster change belongs in this cutover.

Keep the preference timer, move path and dashboard paused during the three
account smoke requests below. This future live check uses real inference and
briefly disables the Claude peer for each Claude request, restoring it in `finally`.
It requires all three accounts initially enabled; it never enables a previously
disabled account. It must run only in the agreed idle window. Do not execute
it as part of isolated preparation.

```sh
# The restart returns before the listener is up; wait up to 60 s for management to answer 401.
python3 - <<'PY'
import time, urllib.error, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        client.open('http://127.0.0.1:8317/v0/management/config', timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Gateway management did not answer within 60 s; roll back')
    time.sleep(1)
PY
sudo -n test ! -e /var/lib/cliproxyapi/auth/kv.db
sudo -n python3 - <<'PY'
import json, subprocess, sys, urllib.request, uuid, yaml
from pathlib import Path
sys.excepthook = lambda *_: print('Smoke check failed; inspect privately and follow rollback', file=sys.stderr)
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
c = yaml.safe_load(Path('/etc/cliproxyapi/config.yaml').read_bytes())
management_key = Path('/etc/cliproxyapi/dashboard-management-key').read_text().strip()
def call(path, body=None, management=False, method=None):
    key = management_key if management else c['access']['api-keys'][0]
    req = urllib.request.Request('http://127.0.0.1:8317' + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'session_id': str(uuid.uuid4())})
    with client.open(req, timeout=90) as r:
        # Only the management middleware sets the version header; inference is checked by its body.
        if management:
            assert r.headers['X-CPA-VERSION'] == '8.0.79'
        return json.load(r)
def mgmt(path, body=None, method=None):
    return call('/v0/management' + path, body, True, method)
rows = mgmt('/auth-files')['files']
pools = {p: sorted([r for r in rows if r.get('type') == p], key=lambda r: r['name']) for p in ('codex', 'claude')}
assert {p: len(pool) for p, pool in pools.items()} == {'codex': 1, 'claude': 2}, 'Stop: expected one Codex and two Claude accounts'
assert all(not r.get('disabled') for pool in pools.values() for r in pool), 'Stop: all three accounts must already be enabled'
assert any(m['id'] == 'gpt-6.1-terra' for m in call('/v1/models')['data']), 'Terra missing from catalog'
before = {r['name']: (r['auth_index'], r.get('priority', 0), r.get('prefix', ''), r.get('disabled', False)) for pool in pools.values() for r in pool}
for provider, pool in pools.items():
    for index, target in enumerate(pool):
        peers = [r for r in pool if r is not target]
        try:
            for peer in peers:
                mgmt('/auth-files/status', {'name': peer['name'], 'disabled': True}, 'PATCH')
            current = mgmt('/auth-files')['files']
            enabled = [r for r in current if r.get('type') == provider and not r.get('disabled')]
            assert len(enabled) == 1 and enabled[0]['auth_index'] == target['auth_index']
            if provider == 'codex':
                result = call('/v1/responses', {'model': 'gpt-6-luna', 'stream': False, 'store': False, 'instructions': 'Reply only UPGRADE_OK.', 'input': 'Reply UPGRADE_OK.'})
                text = ''.join(p.get('text', '') for item in result.get('output', []) for p in item.get('content', []) if p.get('type') == 'output_text')
                assert result.get('status') == 'completed' and 'UPGRADE_OK' in text
            else:
                result = call('/v1/messages', {'model': 'claude-haiku-5-5', 'max_tokens': 32, 'stream': False, 'messages': [{'role': 'user', 'content': 'Reply only UPGRADE_OK.'}]})
                assert any('UPGRADE_OK' in p.get('text', '') for p in result.get('content', []))
            print(provider, 'account', index + 1, 'smoke passed')
        finally:
            for peer in peers:
                mgmt('/auth-files/status', {'name': peer['name'], 'disabled': peer.get('disabled', False)}, 'PATCH')
after = {r['name']: (r['auth_index'], r.get('priority', 0), r.get('prefix', ''), r.get('disabled', False)) for r in mgmt('/auth-files')['files'] if r['name'] in before}
assert after == before, 'Account routing metadata changed'
pid = int(subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '-pMainPID', '--value']))
status = json.loads(Path('/var/lib/cliproxyapi/fast-mode-plugin-status.json').read_text())
assert status['pid'] == pid and status['plugin'] == 'fast-mode-policy' and status['version'] == '0.2.0' and status.get('policy_schema') == 2 and not status.get('disabled')
assert '/cliproxy-pluginhost/' in Path(f'/proc/{pid}/maps').read_text()
assert not Path('/var/lib/cliproxyapi/auth/kv.db').exists(), 'Unexpected persistent KV; stop and inspect privately'
# Preserve inference/firewall bindings; management must remain loopback-only.
assert c['management']['allow-remote'] is False
print('Both providers/accounts and current-PID plugin marker passed; no credentials displayed')
PY
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v1/models
curl --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8317/v0/management/config
sudo -n systemctl start personal-codex-router-usage.service
sudo -n systemctl start personal-codex-router-dashboard.service
# Type=simple returns before the listener and first gateway poll; allow up to 60 s.
python3 - <<'PY'
import json, time, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        req = urllib.request.Request('http://127.0.0.1:8320/api/health', headers={'X-Router-Request': '1'})
        with client.open(req, timeout=5) as r:
            if json.load(r)['health']['gateway']['status'] == 'healthy':
                break
    except (OSError, ValueError, KeyError):
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Dashboard did not report a healthy gateway within 60 s')
    time.sleep(1)
print('Dashboard and gateway ready; collection timer still paused')
PY
```

Expect 401 on both unauthenticated requests. Check the model list against the
pre-cutover routes without dumping keys. Privately compare config, plugin
policy, saved routing preferences and account metadata with the fresh backup;
OAuth refresh may legitimately rotate tokens, so do not compare or restore old
token values. The collector service must finish successfully and show fresh
rows for both providers and all three accounts in the existing database. Never add a second
live usage-queue reader. Inspect only version/startup/warning classifications
from the new PID's journal, keeping raw logs private.

Resume only previously active timers/path units using the saved state:

```sh
sudo -n python3 - "$CUTOVER_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path
units = json.loads((Path(sys.argv[1]) / 'resume-system.json').read_text())
if units:
    subprocess.run(['systemctl', 'start', *units], check=True)
PY
sudo -n cat "$CUTOVER_DIR/resume-user.json" | python3 -c 'import json,subprocess,sys; u=json.load(sys.stdin); subprocess.run(["systemctl","--user","start",*u],check=True) if u else None'
systemctl is-active cliproxyapi personal-codex-router-dashboard personal-codex-router-usage.timer personal-codex-router-preference.timer personal-codex-router-move.path
systemctl show personal-codex-router-usage.service personal-codex-router-preference.service -p Id -p Result -p ExecMainStatus
```

After the next collection/preference cycle, require healthy collection and no
routing drift. Open the dashboard in the normal browser and verify the one Codex
card, both Claude cards, routing controls, fast-mode enforcement, recent usage
and normal navigation. No sign-in is needed just to prove the version gate;
the isolated harness covers credential replacement. Finish by confirming the
gateway still listens on `*:8317` behind the nftables guard (loopback and the Mac
over Tailscale only), management and the dashboard backend stay loopback-only,
Mac inference works over Tailscale, and Mac management/dashboard work over SSH.

Do not enable Codex WebSockets during the cutover. Enable them afterwards only through
[the WebSockets rollout](codex-websockets.md), which requires response steering
to stay off.

## Rollback to 8.0.47

If a gate fails, retain private diagnostics, keep clients idle, restore any
temporary disabled flag before proceeding, and select 8.0.47. If the failure
preceded the gateway restart, restore the drop-in selection without restarting
an already healthy 8.0.47 process. The following block handles either file
activation state; restart only if a new gateway was actually started.
8.0.47 starts and serves requests with the KV key, but its strict config PATCH
rejects it with a migration warning. Remove only the reviewed safeguard in place
before starting the old binary; rollback therefore includes a config edit. Stop
8.0.79 before removing the key so its watcher cannot re-enable default KV. Keep
an already healthy 8.0.47 running. The helper refuses unexpected KV options
rather than discarding them. Run from the reviewed checkout:


```sh
# Detect the running executable, not the currently selected on-disk ExecStart.
RESTART_GATEWAY=$(sudo -n python3 - <<'PYSTATE'
import subprocess
from pathlib import Path
pid = int(subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '-pMainPID', '--value']))
if pid:
    executable = str(Path(f'/proc/{pid}/exe').resolve(strict=True))
    assert executable in ('/opt/cliproxyapi/8.0.47/cli-proxy-api', '/opt/cliproxyapi/8.0.79/cli-proxy-api'), 'Unexpected executable; inspect privately'
    print('yes' if executable == '/opt/cliproxyapi/8.0.79/cli-proxy-api' else 'no')
else:
    print('yes')
PYSTATE
)
if [ "$RESTART_GATEWAY" = yes ]; then
  # Drain before stopping; the queue is in memory. Failure loses uncollected smoke records.
  sudo -n systemctl start personal-codex-router-usage.service || echo 'Usage drain failed; uncollected smoke records will be lost'
  sudo -n systemctl stop cliproxyapi
fi
sudo -n python3 - "$PWD/scripts" <<'PYKVROLLBACK'
import stat, sys
from pathlib import Path
import yaml
sys.excepthook = lambda *_: print('Rollback KV edit failed; inspect privately before proceeding', file=sys.stderr)
sys.path.insert(0, sys.argv[1])
import gateway_config
path = Path('/etc/cliproxyapi/config.yaml')
metadata = lambda s: (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode), s.st_ino)
before = metadata(path.stat())
original = path.read_bytes()
candidate = gateway_config.remove_local_kv(original)
gateway_config.write_same_inode(path, candidate, original)
assert metadata(path.stat()) == before
assert 'kv-store' not in yaml.safe_load(path.read_bytes())['server']
print('Rollback KV key removed; owner, mode and inode unchanged')
PYKVROLLBACK
sudo -n python3 - <<'PY'
from pathlib import Path
d = Path('/etc/systemd/system/cliproxyapi.service.d')
new, staged = d / 'zz-gateway-8.0.79.conf', d / 'zz-gateway-8.0.79.conf.staged'
old, parked = d / 'zz-gateway-8.0.47.conf', d / 'zz-gateway-8.0.47.conf.rollback-8.0.79'
if new.exists():
    assert not staged.exists()
    new.rename(staged)
if parked.exists():
    assert not old.exists()
    parked.rename(old)
assert old.exists() and not new.exists()
PY
sudo -n systemctl daemon-reload
# Only start when the candidate was stopped above or the attempted cutover had failed.
if [ "$RESTART_GATEWAY" = yes ]; then
  sudo -n systemctl start cliproxyapi
fi
systemctl show cliproxyapi -p MainPID -p ExecStart -p ActiveState
# Wait for management to answer 401 before the collector reads the queue.
python3 - <<'PY'
import time, urllib.error, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 60
while True:
    try:
        client.open('http://127.0.0.1:8317/v0/management/config', timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('Gateway management did not answer within 60 s')
    time.sleep(1)
PY
sudo -n systemctl start personal-codex-router-usage.service
```

Keep the compatible dashboard: all applicable version sets retain 8.0.47. Apart
from removing the KV safeguard above, restore no config or OAuth files during rollback; doing so could resurrect
spent refresh tokens or undo later routing changes. Do not select 8.0.7: it
fails the replacement gate and is not validated for moves. Then run the resume
block, repeat dashboard/plugin/collection/access checks on 8.0.47, and tell the
user whether the cutover succeeded or was rolled back. Fresh private backups
are recovery material, not an automatic rollback script.

If an unexpected `kv.db` exists, stop and inspect it privately; do not silently
copy, delete or skip it. Always preserve `.codex-refresh` state privately. 8.0.47 does not implement the new refresh
journal's no-replay guard. A binary rollback cannot reconcile an uncertain
refresh outcome: stop and reconcile or reauthorize that account rather than
restoring old OAuth bytes or retrying an already consumed refresh grant. An
ordinary binary rollback removes only the reviewed KV safeguard, retaining all other settings.
