# Claude gateway preparation — 15 October 2026

**Current status (2026-10-27):** Claude routing is active for both subscriptions on stock CLIProxyAPI 8.0.30; T3 `claudeAgent_gateway` is enabled on Linux and Mac, and Linux shell `claude` uses `scripts/claude-gateway.py`. Inference listens on firewall-restricted `*:8317`, with Mac inference direct over Tailscale; management remains loopback-only via SSH forwards. See [current routing](claude-routing.md). The phase-1 artifacts, unapplied procedures and loopback-binding assumptions below are historical; do not replay the old candidate over current settings.

Phase 2 is live. See [Phase-2 results — 2026-10-15](#phase-2-results--2026-10-15)
for the CLI acceptance results and remaining verification limits. The preparation
sections below preserve the phase-1 snapshot: at that point no Claude login,
live configuration, client settings, dashboard deployment or service restart had
been performed. Existing uncommitted upgrade and fast-mode work was preserved.

At phase-2 acceptance on October 15, the live service was verified as
`/opt/cliproxyapi/8.0.30/cli-proxy-api`, revision
`5be2cdab0869cbaa3d401891f740aebf7e15aa99`, PID 1083839. The older upgrade
document preserves its preparation-time blocker and now records the separate
[Codex sign-in replacement](gateway-upgrade-8.0.30-2026-10-15.md#codex-sign-in-replacement)
that resolved it. That repair is independent of Claude activation.

## Phase-1 prepared artifacts (historical)

- `/etc/cliproxyapi/config.yaml.claude-candidate`: exact prospective config,
  `root:cliproxyapi`, 0640, matching the live config's owner and mode.
- `~/.config/personal-codex-router/claude-gateway.key`: new random client key,
  `user:user`, 0600. Never print it. It is not active yet.
- `~/.config/personal-codex-router/t3-claude-provider.candidate.json`: private
  0600 **merge fragment**, containing the new provider and key. It is not a
  replacement for the complete T3 settings file. It contains no subscription
  credential. Treat it as a secret, keep it outside Git and remove it after use.
- `scripts/claude-gateway.py`: opt-in CLI launcher, not installed or activated.
- Dashboard compatibility changes in `dashboard/server.py` and
  `dashboard/account_access.py`, not deployed.

The dedicated key distinguishes Claude clients operationally and can be revoked
without changing the existing Codex client key. Stock `access.api-keys` does not
scope a key to a provider: this key can access other exposed models too. Loopback
binding and private local file permissions remain the access boundary.

## Configuration and source evidence

All upstream code references below are pinned to
[v8.0.30 / 5be2cdab](https://github.com/router-for-me/CLIProxyAPI/tree/5be2cdab0869cbaa3d401891f740aebf7e15aa99).
The tarball for that revision was inspected directly. The nested v8 mapping is in
[`internal/config/config_v8.go:69`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/config/config_v8.go#L69).

The candidate adds this provider-specific block:

```yaml
upstream:
  claude:
    disable-claude-cloak-mode: true
    disable-cloaking-model-list: true
    model-level-cooling: false
    header-defaults:
      inject-diagnostics: false
      client-request-id: passthrough
      stabilize-device-profile: false
      anthropic-beta-strip: []
      anthropic-beta-append: []
```

It also appends the private new key to `access.api-keys`, retaining the existing
entry. It leaves every other existing value unchanged.

Reasons:

- Disable cloak mode so both CLI and `sdk-ts` retain their caller system prompt,
  without the cloak's replacement identity prompt, injected date/reminders,
  sensitive-word obfuscation or cloaked tool aliases. An explicit credential
  `cloak_mode` overrides this shared default; verify new files have no contrary
  override, or explicitly set `cloak_mode: never` after login.
- Disable diagnostics **injection** explicitly. Existing caller diagnostics can
  still pass through. This does not strip legitimate caller fields or betas.
- Pass through `x-client-request-id` only when the caller supplies it. Do not
  synthesize a request ID. A client using a custom base URL may omit it.
- Leave device-profile stabilization off; do not pin the user's OS/architecture
  to the gateway's default Mac profile. This does not disable the OAuth wire
  fingerprint described below.
- Keep beta append/strip lists empty. Do not opt out of experimental betas,
  adaptive thinking, effort, extended context or prompt caching in the clients.
- Return original Claude model IDs rather than cloaked model-list IDs. Keep
  normal Anthropic model names so the client's model capability detection works.
- Keep Claude model-level cooling false: shared five-hour/seven-day quota
  rejection should cool the entire selected credential. Ordinary model-only
  429s and overage-only/Fable refusals still receive narrower treatment in source;
  false does not turn every possible refusal into a whole-account cooldown.

Contracts:
[`config.example.yaml:412`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/config.example.yaml#L412),
[`claude_executor_cloaking.go:1482`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_cloaking.go#L1482),
[`claude_fingerprint_policy.go:96`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_fingerprint_policy.go#L96),
[`claude_executor_request.go:1436`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_request.go#L1436),
and [`claude_ratelimit.go:25`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/helps/claude_ratelimit.go#L25).

`requests.passthrough-headers` controls upstream **response** header forwarding;
it is not a switch for preserving incoming Anthropic request headers. Its
existing value is retained. See
[`sdk_config.go:89`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/config/sdk_config.go#L89).
The Claude Responses `tool-search` setting is left alone: it concerns Codex
Responses-to-Claude translation, not Claude Code's native Messages tool search.

Key-names-only diff (no scalar values):

```text
added:
  upstream.claude.disable-claude-cloak-mode
  upstream.claude.disable-cloaking-model-list
  upstream.claude.header-defaults.anthropic-beta-append
  upstream.claude.header-defaults.anthropic-beta-strip
  upstream.claude.header-defaults.client-request-id
  upstream.claude.header-defaults.inject-diagnostics
  upstream.claude.header-defaults.stabilize-device-profile
  upstream.claude.model-level-cooling
changed:
  access.api-keys
removed: []
```

### Remaining transformations and protocol limits

**8.0.30 cannot provide byte-for-byte passthrough for both clients through a
configuration change.** The [Anthropic gateway protocol guide](https://code.claude.com/docs/en/llm-gateway-protocol)
requires verbatim `anthropic-beta` / `anthropic-version`, unchanged `cache_control`
wherever it appears, and preservation of block-form `system`. Those are the phase-2
comparison targets, not guarantees established by the candidate.

The detector confirms only `cli`, `sdk-cli`, and `claude-vscode`, with additional
header/body signals. T3's `sdk-ts` is explicitly unconfirmed, even when the shared
cloak switch is off. See
[`claude_client_detection.go:67`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/helps/claude_client_detection.go#L67).
Changing the entrypoint to impersonate CLI is not part of this plan.

Both streaming and nonstreaming native Messages paths still:

- Replace downstream authentication with the selected gateway-owned OAuth bearer
  token; the new client key must never reach Anthropic.
- Rewrite `metadata.user_id` using that credential's account UUID and selected
  device from its device-ID pool, plus an agent/session UUID. Confirmed native
  clients preferentially retain their valid caller session UUID; unconfirmed
  clients use the gateway's session extraction path. Matching session headers are
  generated from the same UUID. Unknown identity fields, including parent fields,
  are preserved by the rebuild where valid. This prevents account A's identity
  being sent with account B's bearer token. There is no supported OAuth switch to
  disable it.
- Regenerate the billing attribution CCH over the final body, adding a billing
  system block when absent, except the narrow measured helper exception. The
  system prompt's substantive content is not cloaked, but attribution can still
  change. No supported OAuth CCH-off switch was found.
- Sanitize thinking/signature history and tool-call IDs, remove empty web-search
  domain arrays, supply a missing `max_tokens`, remove thinking/effort when
  `tool_choice` forces a tool, and apply model/transport normalization. These
  compatibility paths are not controlled by the cloak switch.
- Enforce the four-breakpoint cache limit and normalize invalid TTL order. Native
  confirmed requests own placement, but these later checks can still alter an
  invalid or unusual cache shape. With cloaking off, `sdk-ts` existing cache
  markers generally suppress insertion; absent markers can still be inserted.
  Subagent/probe TTL rules also apply. No general cache-mutation-off switch exists.

For `sdk-ts`, the real OAuth credential always selects the strict CLI fingerprint.
Its User-Agent/software headers become the measured CLI baseline, its Anthropic
version becomes `2023-06-01`, and managed beta headers are reassembled by model,
body and request type. Unknown newer caller betas are forwarded by this revision,
but managed betas can be filtered/reordered/added. Native confirmed CLI retains
more caller headers and betas, while still getting required OAuth/cache betas and
model-specific gating. Neither beta append/strip nor credential `headers` can
turn OAuth into unrestricted header passthrough on `api.anthropic.com`.

Thinking, effort and the `context-1m` beta have explicit support in the beta
builder; preserve their paired body fields and test the actual selected models.
No body rewrite of caller `system` to plain text is requested. Leave per-auth
`rebuild_mid_system_message` false/absent and payload rewrite rules absent.

Source paths for these exceptions:
[`claude_credential_identity.go:266`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/helps/claude_credential_identity.go#L266),
[`claude_executor_execute.go:68`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_execute.go#L68),
[`claude_executor_stream.go:71`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_stream.go#L71),
[`claude_executor_request.go:1086`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_request.go#L1086),
[`claude_executor_cloaking.go:1892`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_executor_cloaking.go#L1892),
and [`claude_signing.go:190`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/runtime/executor/claude_signing.go#L190).

### Routing and spending

Routing strategy, session TTL, subagent affinity and rebind policy are **global**.
The provider configs and auth metadata do not offer overrides for these fields.
The service constructs one selector using
[`service_config.go:65`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/cliproxy/service_config.go#L65)
and [`RoutingConfig`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/config/config_types.go#L389).
A `24h` TTL parses, but changing it here also changes Codex; therefore it is **not**
in the candidate. `session-affinity-rebind-on: quota` would likewise change Codex.
It also means quota **and auth/model-support** failures, not literally quota only.
A separate gateway or a maintained provider-specific selector change would be
needed for independent 24h/quota-only behavior; neither is implemented here.

Retained, including in consumed management configuration:

```yaml
routing:
  strategy: round-robin
  session-affinity: true
  session-affinity-ttl: 1h
  session-affinity-subagents: true
  force-model-prefix: false
  cooldown:
    disable-cooling: false
    save-cooldown-status: true
  retry:
    request-retry: 0
    max-retry-credentials: 2
    max-retry-interval: 0
```

The omitted rebind-on setting retains default `any`. Claude sessions inherit the
same one-hour policy and can rebind after transient as well as quota/auth failures.
Subagents follow a discoverable parent binding. Affinity is provider-scoped and
outranks priorities; it is in memory, so a gateway restart loses bindings.
Use Claude A priority 100, Claude B priority 0, both with empty prefix. That prefers
A for new sessions without moving a healthy session already bound to B. B serves
when A is unavailable. The Codex preference controller only writes Codex auths,
so it cannot reset Claude priorities. Do not set global fill-first or alter the
Codex auth files to implement this preference.

**There is no pre-dispatch “subscription only / no overage” enforcement setting
in 8.0.30.** Rate-limit classification reacts to provider responses; it cannot
prevent a successful request from spending enabled extra usage. Stripping a
fallback beta is not a reliable spending control. Before login/cutover, the user
must verify **Extra usage off**, no auto-reload, and no spending allowance on
**both** claude.ai accounts. Avoid `/fast` and overage-only models, including Fable
where not included in the plan. Gateway priority does not consult the direct
usage collector and cannot switch away from A before Anthropic permits an overage
charge. B's setup-token watch cannot establish its spending settings; check B in
the browser. Neither account's current browser spending state was changed or
verified here. No purchases or credit redemptions were performed.

## Dashboard compatibility and deployment

The main poll, controller/timer, device sign-in resolver, last-ready confirmation,
fast-mode controls and reset helpers already restrict management auth rows to
`type == codex`. They continue to do so. Current account-access code has **no
Free-plan auto-disable loop**: polling preserves the user's disabled preference
for every plan. Claude credentials must never enter Codex claim refresh or status
mutation helpers even if their email matches a Codex account.

Changes:

- `AccountAccess.reconcile` independently filters non-Codex records;
  `maybe_refresh` ignores them; `set_disabled_verified` rejects them before any
  management call.
- Every dashboard usage query, including bounds, accounts, models, recent rows
  and hours, filters to Codex. Old schemas without a provider column and old rows
  with NULL/empty provider remain readable as pre-mixed-provider history.
- The single queue consumer continues recording all providers with their provider
  field intact. It must not be duplicated or discard Claude records. Claude
  inference history stays in SQLite but is not displayed as Codex activity.
- Claude usage cards, direct collectors, keepalive and redeemer are unchanged.
  Health and Codex account counts remain limited to the existing Codex pool.

Tests first demonstrated the missing access-helper boundaries and mixed-provider
usage leakage, then passed after the fixes. Validation: **247 focused tests**:
244 across compatibility, account access, dashboard, routing, sign-in, usage
collection, operations, installer and fast-mode modules, plus 3 CLI launcher
checks. Python compilation and `git diff --check` pass. There is no frontend
change; no live browser or subscription inference test was performed.

Installer dry-run was executed:

```sh
python3 scripts/install-operations.py --account-access-only
```

Prepared deployment command, **not run**:

```sh
sudo -n python3 scripts/install-operations.py --account-access-only --apply
```

Its explicit manifest includes both changed backend files and the existing
account-access dependencies. It backs up replaced runtime files under
`/var/lib/personal-codex-router-backups/account-access-deployments`, restarts only
`personal-codex-router-dashboard` and `personal-codex-router-preview`, and checks
that gateway/shared Codex processes did not change. It does not deploy the CLI
launcher or change Claude direct collectors. Complete authoritative review in the
parent workflow before publishing; the implementation worker did not run it.

## Candidate validation

Credential-free machine-readable evidence: [validation results](claude-gateway-validation-2026-10-15.json).

Two isolated 8.0.30 processes ran sequentially on loopback, with empty auth dirs,
synthetic client/management keys and disabled plugins, discovery, panel updates
and request-body logs. No real auth credential, management secret or client key
was placed in either instance. Scratch lived under
`~/.cache/agent-scratch/personal-codex-router/claude-gateway-phase1/`.

For both the sanitized baseline and sanitized candidate, GET then same-value
PATCH then GET `/v8/management/config` succeeded, exercising strict nested-v8
validation. `/v0/management/auth-files` returned an empty list. The candidate's
Claude switches read back correctly. The complete consumed routing objects were
equal and matched the retained values above.

Baseline process PID 1115907 / port 54269 and candidate PID 1115929 / port 57509
were terminated and reaped. No persistent process or browser tab was started.
Temporary source and fixture files are removed at task completion; the private
candidate/key/provider fragment remain intentionally staged.

## Two gateway-owned logins — phase 2 only

First deploy the dashboard compatibility changes, verify no-overage settings in
both accounts, and ensure no Claude client is sending inference yet. Do **not**
copy `~/.claude` credentials or the B setup-token into the gateway. OAuth creates
separate gateway-owned refreshable credentials and leaves those direct logins
available for collectors and fallback. Provider-wide session limits or token
revocation behavior still require observing both direct collectors afterwards.

Use the management API without `is_webui`. This avoids opening a callback listener.
The OAuth redirect is `http://localhost:54545/callback`. After browser approval it
can show connection refused: copy the complete address and submit it privately to
the management callback API. No 54545 SSH tunnel is required for this manual flow,
even when the browser runs on the Mac. The existing management tunnel may still
be used to access loopback management separately.

The alternate CLI is `sudo -n /opt/cliproxyapi/8.0.30/cli-proxy-api -config
/etc/cliproxyapi/config.yaml -claude-login -no-browser`, but do not use it for this
plan: its callback server also listens on all interfaces. Likewise avoid
`GET /anthropic-auth-url?is_webui=true`, whose forwarder explicitly binds
`0.0.0.0:54545`. Keep management on Linux loopback.

Run the following in a private terminal **only when ready for phase 2**. It is a
procedure, not a command executed during preparation. The script reads the
management key privately, never prints auth rows or provider error bodies, and
hides the pasted callback URL. A login lasts up to five minutes. It starts A and
then B only after the preceding flow completes. Check the browser's account and
organization before approving each URL; switch browser profile/account between
iterations.

```sh
sudo -n python3 - <<'PY'
import getpass, json, sys, time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, ProxyHandler, build_opener

sys.stdin = open('/dev/tty')  # the script itself was supplied on stdin
key = Path('/etc/cliproxyapi/dashboard-management-key').read_text().strip()
client = build_opener(ProxyHandler({}))
def api(path, body=None, method=None):
    req = Request('http://127.0.0.1:8317/v0/management' + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  method=method, headers={'Authorization': 'Bearer ' + key,
                                         'Content-Type': 'application/json'})
    try:
        with client.open(req, timeout=20) as response:
            return json.load(response)
    except Exception:
        raise SystemExit('Management request failed; inspect status privately, do not retry a login blindly.') from None

def codex(rows):
    return sorted((r.get('name'), r.get('auth_index'), r.get('disabled'), r.get('priority'))
                  for r in rows if r.get('type') == 'codex')

# Establish account identity deliberately; do not infer A/B from login order alone.
for label, priority in [('Claude A', 100), ('Claude B', 0)]:
    expected_email = input(label + ' expected claude.ai email: ').strip()
    if not expected_email:
        raise SystemExit('Expected email required')
    before = api('/auth-files')['files']
    assert not any(r.get('type') == 'claude' and r.get('email') == expected_email for r in before), 'Claude identity already exists; stop instead of overwriting it'
    flow = api('/anthropic-auth-url')  # no is_webui: no callback listener
    print('Sign into the correct account and organization, then open this authorization URL:')
    print(flow['url'])
    callback = getpass.getpass('Paste full localhost:54545/callback URL (hidden): ')
    redirect = urlsplit(callback)
    assert redirect.scheme == 'http' and redirect.netloc == 'localhost:54545' and redirect.path == '/callback'
    assert parse_qs(redirect.query).get('state') == [flow['state']], 'Callback state mismatch'
    api('/oauth-callback', {'provider': 'anthropic', 'state': flow['state'],
                           'redirect_url': callback}, 'POST')
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = api('/get-auth-status?' + urlencode({'state': flow['state']}))
        if status.get('status') == 'ok':
            break
        if status.get('status') == 'error':
            raise SystemExit('OAuth failed; inspect privately and verify browser account before retrying.')
        time.sleep(1)
    else:
        raise SystemExit('OAuth timed out')
    rows = api('/auth-files')['files']
    assert codex(rows) == codex(before), 'Codex routing changed; stop and investigate'
    matches = [r for r in rows if r.get('type') == 'claude' and r.get('email') == expected_email]
    assert len(matches) == 1, 'Expected one matching Claude identity; inspect only safe metadata and stop'
    row = matches[0]
    # note is the supported operator label; label itself is derived from email.
    api('/auth-files/fields', {'name': row['name'], 'note': label,
        'priority': priority, 'prefix': '', 'cloak_mode': 'never',
        'rebuild_mid_system_message': False}, 'PATCH')
    fresh = [r for r in api('/auth-files')['files']
             if r.get('type') == 'claude' and r.get('name') == row['name']
             and r.get('auth_index') == row['auth_index'] and r.get('email') == expected_email]
    assert len(fresh) == 1 and fresh[0].get('priority', 0) == priority
    assert fresh[0].get('note') == label
    print(label + ' saved and priority verified; confirm the direct usage collector still works.')
PY
```

If the browser approved the wrong account, stop before applying clients; inspect
only the management list's safe type/name/email fields and disable that new Claude
entry through `/auth-files/status` before retrying. Do not mutate Codex rows or
print/download authentication contents. If there are multiple organizations for
one email, select the intended organization explicitly and rework the identity
selection before any PATCH; the example fails closed on ambiguity. A prefix PATCH
can be set to `claude-a`/`claude-b` for explicit account-specific diagnostic models,
but this plan uses `prefix: ''` on both so ordinary requests share the pool. Prefix
is not exposed in this management list: verify it through a scalar-redacting,
root-only parser or isolated model-list behavior, never raw auth-file output.

Files land under `/var/lib/cliproxyapi/auth/` as
`claude-<8-char organization/account hash>-<email>.json`, falling back to
`claude-<email>.json` if neither UUID exists. Label is the credential email;
`note` provides “Claude A” / “Claude B”. Do not rename files or change identity
fields to set a display label. Verify only key names for token/device fields and
owner/mode; never display them. Hot reload admits the files without restarting.

Sources:
[`auth_files_provider_oauth.go:37`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/api/handlers/management/auth_files_provider_oauth.go#L37),
[`oauth_callback.go:18`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/api/handlers/management/oauth_callback.go#L18),
[`auth_files_oauth_callback.go:54`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/api/handlers/management/auth_files_oauth_callback.go#L54),
[`sdk/auth/claude.go:26`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/sdk/auth/claude.go#L26),
[`filename.go:20`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/auth/claude/filename.go#L20),
and [`auth_files_fields.go:603`](https://github.com/router-for-me/CLIProxyAPI/blob/5be2cdab0869cbaa3d401891f740aebf7e15aa99/internal/api/handlers/management/auth_files_fields.go#L603).

## Staged client configuration

Use a new T3 instance **Claude**, driver `claudeAgent`, ID
`claudeAgent_gateway`. Keep **Claude A** (`claudeAgent`) and **Claude B**
(`claudeAgent_claude_b`) available as direct fallbacks. The staged fragment adds:

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:8317
ANTHROPIC_AUTH_TOKEN=<read privately from claude-gateway.key>
ENABLE_TOOL_SEARCH=true
CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING=1
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
NO_PROXY=127.0.0.1,localhost
```

It also sets `ANTHROPIC_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`, and
`CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,MANTLE}` to empty strings to mask inherited
alternatives. There is **no `CLAUDE_CODE_OAUTH_TOKEN` entry**. Ensure T3's host
process has no inherited OAuth token, credential file descriptor, host-auth
selector, Unix socket or alternative provider override before activating the
instance; do not print environment values to check this. Empty settings env values
mask inherited provider flags, as documented in the [environment reference](https://code.claude.com/docs/en/env-vars).
Do not globally export the gateway settings into the desktop session.

`ENABLE_TOOL_SEARCH=true` restores deferred MCP tools on a custom base URL;
`tool_reference` blocks must survive the gateway. Fine-grained streaming is off by
default behind a custom base URL, so explicitly enable it. Model discovery is
optional convenience, enabled here to discover original Claude model IDs. Do not
set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, disable caching, disable adaptive
thinking, or bypass fast-mode entitlement checks. See the
[gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol#feature-pass-through),
[connection guide](https://code.claude.com/docs/en/llm-gateway-connect), and
[environment variables](https://code.claude.com/docs/en/env-vars).

T3's existing B entry has an environment array of objects with `name`, `value`,
`sensitive`, and `valueRedacted`. Its sensitive value is empty/redacted in
`~/.t3/userdata/settings.json`; the corresponding private secret file exists and
is 0600. No credential bytes were read. The installed AppImage's bundled
`apps/server/dist/binCli-D-vPGZz9.mjs` was inspected:

- `src/settingsSecrets.ts`, `mapSettingsSecrets` (around line 57051) maps an env
  secret to `provider-env-<base64url(instanceId)>-<base64url(variableName)>.bin`.
- `ServerSecretStore` (around line 54119) stores raw secret bytes under
  `~/.t3/userdata/secrets/`, directory 0700, files 0600, with atomic replacement.
  This is filesystem protection, not evidence of encrypted-at-rest storage.
- `persistSettingsSecrets` / `moveInlineSettingsSecrets` remove sensitive inline
  values from persisted settings, replacing them with empty `value` and
  `valueRedacted: true`; materialization resolves the secret on launch.
- `startWatcher` (around line 57737) watches settings and reloads them. Closing T3
  is not required for edits made through its settings UI. Direct external edits
  can race T3's cached settings writes, so do the optional manual merge with T3
  fully closed. Never close T3 during an active turn merely to apply this plan.

Audited AppImage SHA-256:
`a42d3dd2d1f56b036c6e5d2ffa6cd9ea8db0c08351cbc5b86790f349d4f759be`.
Recheck the shape after a T3 update. Do not manually insert a redacted placeholder
without its secret-store entry; that resolves to an empty token.

Preferred apply method: create the instance in T3's provider settings, add these
environment rows, mark `ANTHROPIC_AUTH_TOKEN` sensitive, and transfer the key
privately (for example `wl-copy < ~/.config/personal-codex-router/claude-gateway.key`,
then clear the clipboard after saving). Leave both fallback instances intact.
The staged JSON is the alternative for an offline merge: it contains the real
new key with `sensitive: true` and no redaction flag, so T3 can migrate it into
its secret store. **Never print or paste that JSON into a chat or issue.**

Optional manual merge, **not executed**: wait for active work to finish and close
T3 normally first, then run as the desktop user. No subscription secrets are
copied into this fragment. The complete settings file is retained and backed up
privately; only the new instance is added.

```sh
python3 - <<'PY'
import json, os, time
from pathlib import Path
p = Path.home() / '.t3/userdata/settings.json'
fragment = Path.home() / '.config/personal-codex-router/t3-claude-provider.candidate.json'
settings = json.loads(p.read_text())
new = json.loads(fragment.read_text())['providerInstances']
assert set(new) == {'claudeAgent_gateway'}
assert not set(new).intersection(settings.get('providerInstances', {}))
backup = fragment.with_name('t3-settings-before-claude-' + time.strftime('%Y%m%dT%H%M%S') + '.json')
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as f:
    f.write(p.read_bytes())
settings.setdefault('providerInstances', {}).update(new)
temporary = p.with_name('settings.json.claude-new')
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
    f.flush()
    os.fsync(f.fileno())
os.replace(temporary, p)
print('New Claude instance staged in settings; start T3 and verify secret migration.')
PY
```

After launch, verify the new entry's token is empty/redacted, the matching secret
file is 0600, and the instance can send a test turn. Select **Claude** as the
preferred Claude provider for new threads/projects; move idle existing Claude
threads to that instance or create replacement threads as needed. Existing A/B
threads keep their old instance until explicitly changed. This phase does not
silently migrate active threads. A/B remain explicit fallback choices.

### Terminal CLI

Use the launcher directly after phase-2 activation:

```sh
python3 /home/user/Projects/personal-codex-router/scripts/claude-gateway.py
```

For ordinary interactive `claude`, add this **only to the interactive shell
configuration** after verification; it is a staged snippet, not applied:

```bash
if [[ $- == *i* ]]; then
  claude() {
    python3 /home/user/Projects/personal-codex-router/scripts/claude-gateway.py "$@"
  }
fi
```

Do not export the function, replace `~/.local/bin/claude`, or edit
`~/.claude/settings.json`. The launcher checks the root/user-owned 0600 key using
a no-symlink file descriptor, removes inherited direct-token and provider-target
selectors, sets the gateway variables only for its child, and execs the existing
`claude` executable. It fails closed on a missing/insecure key. Arguments including
`--resume` are preserved. Subagents inherit the selected parent's gateway env.
`command claude` bypasses the interactive function for a deliberate direct CLI
fallback. The systemd collectors/keepalive/redeemer do not inherit shell functions
and continue finding the original Claude executable and direct credentials.
No wrapper was invoked against real Claude during phase 1. A saved-login warning
is not a reason to run `/logout` against the shared Claude configuration: that
could break A's direct collector. Verify the per-process gateway credential and
target instead, keeping the saved direct login.

### Accepted feature losses and network boundary

The intended automatic account pool covers model inference through Messages and
its count-tokens endpoint. `ANTHROPIC_BASE_URL` does not proxy every network
operation performed by Claude Code or an MCP server. External tool calls still
reach their own services; some Claude control-plane checks reach Anthropic
directly. Do not claim that every packet or subscription feature traverses CPA.

Remote Control is unavailable with a non-first-party base URL. claude.ai account
connectors and voice depend on first-party account/control-plane behavior and
are not supplied by this gateway credential; treat them as unavailable for the
new instance, with the existing direct clients retained for fallback. Local MCP
servers are separate and should continue working. Subscription-specific UI,
extra-usage prompts, reset controls, server-managed settings, synced account
features, and entitlements cannot be assumed to follow the gateway-selected A/B
account. Do not use a local client entitlement prompt as a no-spend guarantee.
Fast mode has separate direct availability checks and can spend extra usage;
it is not enabled or bypassed here. If optional
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is later desired, it also disables
feature-flag-dependent features; it is deliberately absent from this minimal
candidate. The API's thinking, images, tools and prompt-cache features remain
test targets, not accepted losses.

## Apply and rollback — phase 2 only

Order: review the implementation in the parent workflow; deploy dashboard
compatibility; verify spending settings on both subscriptions; apply the candidate
in place (same inode) and verify hot reload; perform A then B OAuth; verify both
direct collectors; activate one new T3 instance and one CLI invocation; run the
checks below; then make the interactive function/default provider changes.
Never restart the model gateway. Do not apply an old candidate over later changes.

The staged config was derived from live SHA-256
`f4659fc43eeaba1385bbc6f1022db45a5fdc4cca06163366d986fb7fe7e30852`.
Candidate SHA-256:
`62ff7346440e98f12db2dcef414eae5a89534485a28451629b33584e4518c996`.
If either differs at apply time, regenerate/revalidate the candidate and its
key-names-only diff. Do not bypass this guard.

Exact guarded config-apply procedure, **not run**:

```sh
sudo -n python3 - <<'PY'
import hashlib, os, time
from pathlib import Path
live = Path('/etc/cliproxyapi/config.yaml')
candidate = live.with_name('config.yaml.claude-candidate')
assert hashlib.sha256(live.read_bytes()).hexdigest() == 'f4659fc43eeaba1385bbc6f1022db45a5fdc4cca06163366d986fb7fe7e30852'
assert hashlib.sha256(candidate.read_bytes()).hexdigest() == '62ff7346440e98f12db2dcef414eae5a89534485a28451629b33584e4518c996'
info = live.stat()
backup_dir = Path('/var/lib/cliproxyapi-claude-backups')
backup_dir.mkdir(mode=0o700, exist_ok=True)
backup_dir.chmod(0o700)
backup = backup_dir / ('config-before-claude-' + time.strftime('%Y%m%dT%H%M%S') + '.yaml')
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as f:
    f.write(live.read_bytes())
    f.flush()
    os.fsync(f.fileno())
# Keep the watched inode: atomic replacement is unreliable for the gateway's
# config watcher (see gateway-config-v8-migration.md).
import sys
sys.path.insert(0, '/home/user/Projects/personal-codex-router/scripts')
from gateway_config import write_same_inode
original = live.read_bytes()
assert hashlib.sha256(original).hexdigest() == 'f4659fc43eeaba1385bbc6f1022db45a5fdc4cca06163366d986fb7fe7e30852'
write_same_inode(live, candidate.read_bytes(), original)
assert live.stat().st_ino == info.st_ino
print('Config rewritten in place for hot reload; private backup:', backup)
PY
```

Then use a private management-key reader to GET `/v8/management/config` and assert
only the expected Claude switches and key membership changed, without printing
scalars. Confirm the same `systemctl show cliproxyapi -p MainPID --value`, port
8317/loopback binding, Codex account count/priorities and routing fields, and a
normal Codex session. A config-save failure in the management UI is not a reason
to relax `ProtectSystem` or restart the gateway.

Rollback clients first: remove the interactive function (`unset -f claude` in the
current shell), restore the prior shell configuration, and select the retained
T3 A/B instances for idle threads. Remove only `claudeAgent_gateway` using T3's
provider UI so its secret is removed consistently; do not restore the entire old
T3 settings file over unrelated newer settings. Disable only the two newly added
Claude gateway auth entries using `/auth-files/status` and verify their safe
metadata. Keep their files private for deliberate recovery; do not delete auths
or restore older token backups automatically.

For a full config rollback, write the just-created private backup back with the
same-inode `write_same_inode` helper **only if the live config still equals the reviewed
candidate hash**. Otherwise remove only the appended new client key and the new
Claude subtree from the current config, preserving later changes, then validate
that rollback candidate in an empty-auth isolated instance before hot reload.
Verify unchanged gateway PID and Codex behavior again. The dashboard's provider
filtering is safe to retain; if reverting it, restore only the replaced runtime
files from the account-access deployment backup and restart dashboard/preview
only. Never roll back rotated OAuth credentials, collectors, timers or Codex
routing as part of this task.

## Phase-2 acceptance plan

Use small synthetic prompts and files first. Record only allowlisted metadata,
status codes, model names, account labels, token counts and comparison booleans;
keep subscription credentials and actual prompts out of shared evidence.

1. **Control plane and Codex preservation:** verify both gateway-owned Claude auths
   by safe metadata, A priority 100/B priority 0/empty prefixes, original Codex
   auths/priorities and saved dashboard selection, gateway PID, loopback management,
   direct A/B usage collection and keepalive/redeemer health. Verify extra usage
   off in both browsers before any inference; no real credit redemption test.
2. **CLI and T3 SDK:** send separate two-turn sessions via the launcher (`cli`) and
   new T3 instance (`sdk-ts`). Correlate gateway access/affinity logs with session,
   provider and selected auth, without dumping request bodies/headers. Verify
   the selected auth's identity and that the client key is never upstream auth.
   Confirm cloak/diagnostics settings by consumed config. Stock logs have no
   reliable per-request “native passthrough=true” marker. `sdk-ts` will remain
   unconfirmed by source; do not label that native passthrough. An isolated,
   instrumented synthetic request comparison is needed to prove which fields
   survive. Do not turn on raw live request logging to manufacture that proof.
3. **Protocol fidelity:** with isolated synthetic captures or redacting comparison
   instrumentation, compare system block arrays, tool schemas, `cache_control`,
   thinking/signatures, effort, context-management and caller beta sets before
   and after the executor. Allowlist only the identity/auth/CCH exceptions above.
   Record SDK beta/fingerprint/cache changes explicitly. If preserved-thinking
   fails from rewriting, stop cutover and keep direct fallback until resolved.
4. **Streaming and tools:** verify incremental text and thinking, SSE pings and
   terminal events, cancellation, a local file tool call, a large write with
   fine-grained input streaming, an MCP tool discovered through tool search, and
   the following tool-result continuation. Confirm no renamed/missing tool calls.
5. **Subagents:** spawn a child with a parent reference, verify both hit the same
   Claude account, then continue parent and child. Check each surface separately;
   a request lacking recognizable session/parent metadata cannot be guaranteed
   sticky just because affinity is enabled.
6. **Thinking/effort/images:** continue signed thinking across at least three turns,
   vary supported effort, and ask about a small synthetic PNG. Check for 400s,
   dropped thinking/signatures, effort silently removed, or unsupported image data.
7. **Compaction/resume/context:** compact a synthetic conversation, continue tools
   and thinking, exit/resume the CLI session and resume an idle T3 thread. Verify
   account continuity within the retained TTL. Select an actually available
   `claude-…[1m]` model and confirm `context-1m` and paired fields reach upstream;
   model availability and plan inclusion must be checked before requesting a large
   context. A beta on a tiny prompt proves protocol acceptance, not a full 1M-token
   capacity or billing guarantee.
8. **Prompt cache:** after turn 1, send two mostly identical follow-up turns with
   stable tools/system. Require positive cache-read tokens when caching is
   eligible, and explain minimum token lengths/TTL if a tiny prompt misses. Check
   each client separately and repeat across a subagent. Rebinding can lose cache.
9. **Forced failover:** in a bounded, otherwise idle Claude test session, temporarily
   disable **only Claude A** through management, send the next request, confirm B
   serves it, then re-enable A and verify the session stays on B while a fresh
   session prefers A. Restore A on any error. This tests availability failover,
   not genuine 429 semantics. Exercise 429 and transient 5xx semantics separately
   in an isolated fake-upstream/synthetic-auth fixture, never by invalidating real
   credentials, draining subscription quota or changing Codex auths. Expect global
   `any` rebinding, not the unavailable provider-specific quota-only policy.
10. **Stickiness and final checks:** compare selected auth across turns, models,
    children, compaction, resume, A recovery and idle expiry. A 24-hour affinity
    guarantee is not achievable in this candidate; document one-hour expiry and
    transient-rebind behavior. Repeat dashboard counts, health and Codex routing
    checks; remove only task-owned test processes and temporary capture artifacts.

Open limits before phase 2: no real OAuth login/inference/browser checks, no
measured `sdk-ts` wire capture, no no-overage enforcement in CPA, global routing
cannot supply the desired independent long TTL/rebind policy, and new OAuth
logins may interact with provider session limits. The staged setup keeps direct
fallback and direct allowance collection intact while these are evaluated.

## Phase-2 results — 2026-10-15

Acceptance checks ran **16:41–16:44 local (UTC−3)** against the live gateway on
8317, using Claude Code **2.1.300** through `scripts/claude-gateway.py`. The
checkout was verified as `/home/user/Projects/personal-codex-router`, clean
`main` at `15de7e1` before this documentation change. These results cover the
nine requested test-worker checks; they do not complete every broader acceptance
item above. The earlier T3 smoke test was supplied as context, not rerun here.

1. **Classification — source-supported; live wire classification unverified.**
   The exact upstream revision linked above was inspected again. Its native
   allowlist contains `cli`, `sdk-cli` and `claude-vscode`; `sdk-ts` cannot become
   a confirmed native client. A normal CLI request is eligible when its strong
   header/body signals pass. Stock selector/access logs do not expose that
   detector result, so this run does not claim per-request confirmation for the
   CLI. Both auths privately verified `cloak_mode: never`,
   `rebuild_mid_system_message: false` and empty prefixes. Consumed
   `/v8/management/config` confirmed cloak/model-list cloaking disabled,
   diagnostics injection false, request-ID passthrough, device stabilization
   false and empty beta append/strip lists. Thus the T3 `sdk-ts` smoke traffic
   uses the **unconfirmed, uncloaked OAuth path**, with strict CLI fingerprint
   reconstruction; disabling cloaking does not make it native passthrough.
   Confirmed CLI retains more caller headers, but OAuth auth/identity, billing
   CCH and the compatibility/cache checks in “Remaining transformations” still
   apply. Neither client has demonstrated byte-for-byte passthrough. No log
   verbosity, request-body logging or gateway configuration was changed.

2. **Streaming, tools and resumed thinking — PASS.** A Sonnet 5.5 session used
   `Read` on a synthetic local file, returned tool-result continuation, and
   resumed with `--resume` for reasoning. Stream JSON contained incremental
   `content_block_delta` events, `message_start` and terminal `message_stop`
   events. Later resumed turns, still at low effort, emitted signed thinking
   blocks with **287**, then **76 thinking tokens**; nonempty signatures were
   checked without displaying their contents. The second signed turn accepted
   the previous signed history. A later failover resume accepted that history
   on B too. The original marker and prior computed results survived. All CLI
   invocations exited 0, with no permission denials or thinking-signature 400s.
   A per-invocation `alwaysThinkingEnabled` setting was used for these later
   turns; no persistent Claude settings were edited.

3. **Prompt cache — PASS.** The second turn's response `usage` reported
   `cache_read_input_tokens: 3868` and `cache_creation_input_tokens: 194`.
   Subsequent A-bound turns read **4037** and **4437** tokens. These are
   per-turn response fields, not cumulative `modelUsage` totals. The existing
   collector recorded the second turn at 16:41:19 with `failed=0` and
   `cached_tokens=3868`. The DB's generic cached-token field alone does not
   distinguish cache creation from reads: on failover it recorded 5344 while
   the response explicitly reported zero reads and 5344 creation tokens.

4. **Stickiness — PASS within the observed session.** Management auth filenames
   were mapped privately to their `note`; only account labels are reported.
   Selector logs showed a new **Claude A** binding at 16:41:15 and hits at
   16:41:17, 16:41:19, 16:41:47 and 16:42:13 for the same truncated session
   identifier. The existing one-hour TTL and global `any` rebind policy were
   retained. Idle expiry, compaction and 24-hour continuity were not tested.

5. **Image input — PASS.** A generated **96 × 64 PNG** was read by Haiku 5.5
   through the local `Read` tool. The model correctly identified both rectangle
   colors in left-to-right order, and tool-result continuation completed with
   `failed=0`. This is one CLI image test; the CLI made multiple API calls for
   initialization, tool use and continuation.

6. **Opus 5.5 / 1M model name — PASS for a small request.** One short CLI
   invocation using **`claude-opus-5-5[1m]`**, low effort, returned the expected
   marker. CLI `modelUsage` resolved canonical model `claude-opus-5-5` and
   `contextWindow: 1000000`; the gateway model list advertised the canonical
   Opus, Sonnet and Haiku 5.5 names. The collector recorded two successful Opus
   API calls from that invocation, including CLI initialization; the final
   marker response used 9 output tokens. No large context was sent. The alias
   and tiny request succeeded; upstream `context-1m` beta preservation, actual
   million-token capacity and billing behavior were not wire-verified.

7. **Forced failover and recovery — PASS.** Only Claude A was disabled through
   management `/auth-files/status`, from **16:43:27.658 to 16:43:33.307**, with
   identity/readback checks and unconditional restoration in `finally`.
   A fresh Haiku session bound to **B** at 16:43:27. The existing A-bound Sonnet
   session then logged at 16:43:30:

   ```text
   session-affinity: cache hit but auth unavailable, reselected
   auth=Claude B provider=mixed model=claude-sonnet-5-5
   ```

   That resumed turn completed without an error and retained the synthetic
   marker, scheduling result and latest reasoning result. No loss of those
   checked conversation details was observed. Its cache reads dropped to **0**
   and it created **5344** tokens on B. After A was re-enabled, the same session
   stayed on **B** at 16:43:33, with **5319 cache-read tokens**. A fresh session
   preferred **A** at 16:43:36. Final management readback confirmed A
   `disabled=false`, `status=active`, priority 100; B remained active at priority
   0. This establishes disabled-account availability failover, not real 429 or
   transient-5xx behavior.

8. **Codex and dashboard preservation — PASS.** One small `gpt-6-luna`
   `/v1/responses` call through **8317** returned HTTP **200**,
   `status=completed` and the expected marker. The normal collector recorded it
   at **16:44:05**, `failed=0`. Dashboard `/api/health`, with
   `X-Router-Request: 1`, returned `ok=true`, gateway healthy and collection
   healthy. Both Codex identities, disabled states and priorities matched the
   pre-test management snapshot; the consumed routing object also matched.
   Gateway PID remained **1083839**, active throughout. No Codex auth or routing
   mutation, config edit, reload, stop or restart was performed.

9. **Direct Claude collectors — PASS for dashboard status.** Final overview
   reported **Claude A: ok** and **Claude B: ok**, with no errors. Their reported
   check times were **16:41:03** and **16:42:51** local respectively, after the
   gateway-owned logins. These existing collectors retained their direct
   credentials; none was reconfigured or manually restarted. These are the
   dashboard's latest observations, not newly forced post-failover collections.

Across the CLI window, the journal recorded **17 Messages requests, all HTTP
200**, and the existing SQLite consumer recorded **17 Claude rows, all
`failed=0`**. This includes CLI initialization calls. No second consumer of the
destructive gateway usage queue was started.

The CLI emitted this expected warning on every invocation:

```text
⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth source is set and takes precedence over your claude.ai login · Unset it to load your organization's connectors
```

The first failover harness attempt stopped **before any status mutation** with
this local fixture error (scratch path abbreviated):

```text
PermissionError: [Errno 13] Permission denied: '<scratch>/baseline-auths.json'
```

The root-created, mode-0600 snapshot was returned to the task user's ownership,
and the check passed on rerun. An initial DB lookup used a UTC text prefix
against local-offset timestamps and matched no rows; the corrected local-time
query produced the evidence above. No gateway/provider test error occurred.

Only this document was changed. Scratch under
`~/.cache/agent-scratch/personal-codex-router/acceptance/` was removed after
verification, including synthetic files, private snapshots and CLI captures.
Only finite task-owned CLI/test processes were started; they exited and the
restoration watchdog was cancelled. No persistent server or browser tab was
started or stopped. No commit, push, branch operation, agent launch or review
loop was run.

Remaining broader acceptance work includes measured SDK/CLI wire comparison,
T3 cache/resume and image checks, cancellation/pings, fine-grained large tool
input and MCP discovery, compaction, idle expiry, isolated 429/5xx fixtures,
and full-context capacity. This test worker did not inspect browser spending
settings or change account spending controls; the spending limits described
above remain applicable.
