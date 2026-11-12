# API-key accounts

The backend supports `claude-api-key`, `codex-api-key`, `gemini-api-key`,
`xai-api-key`, and `openai-compatibility` on CLIProxyAPI 8.0.47. No live
configuration write, account addition/removal, deployment, or restart was used
to validate this work.

## Dashboard contract

`POST /api/accounts/api-keys/add` accepts `{provider, fields}`. All providers
require a printable ASCII `api_key` of 8–4096 characters. A shorter key is
rejected because a last-four label could expose its entire value. `base_url`
must be HTTPS, with no credentials, query, fragment, whitespace, or backslash.
Omitted native-provider URLs use the provider's standard API endpoint; Codex
and xAI require an explicit effective base URL in upstream config.

OpenAI-compatible accounts additionally require a unique lowercase `name`
matching `[a-z0-9-]{1,40}`, an explicit `base_url`, and 1–200 `models`. A model
can be a string or `{id, alias?}` (`name` is also accepted instead of `id`).
The `models` field also accepts comma/newline-separated text: entries are
trimmed, empty entries dropped and IDs deduplicated before applying the same
1–200 limit and validation.
Aliases default to the model ID and must be unique within the entry. Upstream
v8 receives `models: [{name, alias}]` and `keys: [{api-key}]`; its v0 projection
calls the latter `api-key-entries`.

Success is HTTP 200 `{key, provider, label}`. Native labels contain only the
last four key characters, prefixed by `API key ••••`; compatible labels use
the provider name. Validation returns HTTP 400 `{error: "invalid_field",
field}`. An existing credential within the same provider kind returns HTTP
409 `{error: "duplicate"}`. A duplicate compatible-provider name is an invalid
`name` field. Malformed envelopes return `invalid_request`. Unsafe backup
conditions fail closed with HTTP 503 `backup_unavailable`; repeated CAS
conflicts return HTTP 409 `account_changed`; divergent readback returns HTTP
503 `unconfirmed`. Transport failures return a fixed HTTP 503 error without
upstream response details.

The existing host, CSRF, origin, read-only, no-store, and request-deadline
checks cover the add route. Both preview-proxy allowlists include it.

Overview `add_providers` appends five entries with `kind: "api_key"` and
field descriptors; existing OAuth entries have `kind: "oauth"`. API accounts
are grouped under their separate provider IDs. Account projections contain
`kind`, a deterministic hash key, a safe label, base-URL hostname only,
`auth_index`, `confirm_text`, models as
`{count, items: [{id, display_name?}]} | null`, and unsupported usage refresh.
Config models take precedence over the management model-definitions fallback.
Today's request counts join the existing usage summary by provider and `auth_index` and
have 24 local-hour buckets. API accounts never enter OAuth quota polling or
Claude/Codex routing membership and identities.

## Disconnect integration

Use `api_keys.owns(key)` to dispatch an API account before resolving an OAuth
filename. The implemented seam is:

```python
state.api_keys.remove(key, auth_index=None, confirm_text=None, deadline=None)
```

The HTTP handler requires `confirmed: true` and passes the optional index.
The manager re-reads config and compares `confirm_text` against the shared
server-defined confirmation: the compatible-provider custom name or the last
four key characters. Matching trims outer spaces and ignores case; a mismatch
returns HTTP 400 `confirm_mismatch`. An omitted `auth_index` is accepted. A
supplied index must match. Removing one key from an
existing multi-key compatible group preserves the group's other keys and
settings. Empty groups are removed. The result retains the existing
`{disconnected: true, key, last_ready}` shape. Tailscale disconnect policy is
owned by the parallel OAuth/disconnect implementation; this change does not
alter its existing transport guard.

## Writes and installation

Each write reads the single provider list and its strong ETag (falling back
to root GET only when the list is absent). The exact local config file must
hash to that ETag before a full backup is saved and fsynced. Backups use the
existing `/var/lib/personal-codex-router/disconnected` directory, owned by the
dashboard service and mode `0700`; files are exclusive, non-symlink, mode
`0600`. Repo locations and unsafe source/backup paths are refused. No new
directory or service-unit permission is required.

The manager PUTs only `/v8/management/config/api-keys/<channel>` with
`If-Match`, strips runtime auth indexes, and reads the list back. It retries
one pre-write config change or HTTP 412 using a fresh list, ETag, and backup.
No other config key is sent. Successful persistence triggers upstream's
asynchronous hot reload and the dashboard's normal refresh. Readback proves
persisted config, not provider connectivity or completed runtime reload.

`api_keys.py` is included in both explicit backend install manifests. Full
and watch installs already include every dashboard Python module. Existing
installer directory provisioning is reused; a usage-only install still does
not provision disconnect backups and therefore cannot write until the
private directory exists.

## Gateway write access

Stock systemd hardening (`ProtectSystem=strict`, `User=cliproxyapi`) makes
`/etc/cliproxyapi/config.yaml` read-only to the gateway. Management config
writes fail until a service drop-in adds
`ReadWritePaths=/etc/cliproxyapi/config.yaml` and the file is owned by
`root:cliproxyapi` with mode `0660`, followed by a gateway restart. These are
operator steps; the dashboard does not change service permissions or restart it.
A failed gateway PUT returns “The gateway can't save its config.” without
changing the account configuration. The pre-write private backup is retained.
The dashboard refuses writes if the source config has any world permissions.

## Verified upstream evidence

The supplied `add-accounts/upstream` location contains flat snapshots, not a
Git checkout. The fuller cached `upstream` checkout reports `v8.0.43` from
`git describe --tags`. Relevant files were independently fetched from the
official pinned `v8.0.47` tag to verify the following contracts:

- [`internal/api/server_management.go:95–158`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/api/server_management.go#L95): native and compatible v0 CRUD routes. Raw key GETs are returned by `config_lists.go:172`, `607`, `866`, `1521`, and `1722`.
- [`internal/api/server_management_v8.go:21–27`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/api/server_management_v8.go#L21): generic v8 config routes.
- [`internal/api/handlers/management/config_v8.go:23–40`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/api/handlers/management/config_v8.go#L23): on-disk SHA-256 ETags and HTTP 412. Lines 125 and 197–213 establish whole-list replacement, persistence, and asynchronous reload. `handler.go:463–473` establishes comment-preserving v0 persistence/reload.
- [`internal/config/config_v8.go:67–71`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/config/config_v8.go#L67): family/channel mapping. Lines 390–417 define groups with `keys` and compatible conversion to `api-key-entries`. `config_normalization.go:281–298` requires effective Codex/xAI base URLs.
- [`internal/api/handlers/management/config_auth_index.go:290–326`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/api/handlers/management/config_auth_index.go#L290): v8 config injects per-key runtime auth indexes, with a deterministic synthesized fallback. Lines 576–850 establish native/compatible v0 index projections.
- [`internal/watcher/synthesizer/config.go:185–199`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/watcher/synthesizer/config.go#L185): native API keys synthesize provider auth records without a credential-file path. `auth_files.go:649–680` hides pathless non-runtime-only records and describes the visible inventory shape. `sdk/cliproxy/auth/types.go:788–815` shows why visible API auth rows must be excluded: their `account` field can contain the raw API key.

Read-only live checks on 2026-10-30 reported gateway version `8.0.47`, a v8
ETag, no `api-keys` families, and three auth inventory rows. All three were
file-backed and not runtime-only. Only counts, field names/types, and those
classification booleans were printed; no inventory values or config content
were printed. Since no API keys are configured live, API-key inventory
behavior is source-verified and fixture-tested rather than observed live.
