# fast-mode-policy 0.2.0

Native CLIProxyAPI C/JSON ABI plugin (ABI 1, schema 6), built against CLIProxyAPI
v8.0.7, revision `97f244b8ddb9cbf564b6e6faab0159102cca8617`. `go.mod` pins
v8.0.7 and `go.sum` pins its archive; no upstream source is copied into this repo.
The module proxy origin for this tag was verified against that revision.

It edits only the Codex request's `service_tier` after credential selection,
applying each account's speed level (normal, fast or ultrafast) from policy
schema 2, or the schema 1 `allow_fast` rules for entries without a level.
Ultrafast upgrades are allowlist-only (`ultrafast_models`) and limited to the
`openai-response` and `openai` client formats. The status file reports
`policy_schema: 2`.
See [the policy and deployment guide](../../docs/fast-mode-policy.md) for the
contract, defaults, status lifecycle and websocket limitation.

The full operations installer builds and hot-loads this plugin automatically.
For a direct build, use Linux amd64 with a C compiler
(`sudo apt install gcc libc6-dev`):

```sh
plugins/fast-mode-policy/build.sh /tmp/fast-mode-build
```

`GO=/absolute/path/to/go` selects a toolchain directly; it must meet the `go.mod` minimum.
Otherwise the build uses Go on PATH if sufficient, or downloads official Go
1.26.10 (the tested release) and verifies its pinned SHA-256 from
[the official listing](https://go.dev/dl/?mode=json&include=all) before extraction.
It caches the verified toolchain under
`${XDG_CACHE_HOME:-$HOME/.cache}/personal-codex-router/go/1.26.10/` for reuse.
Downloads run only as an unprivileged user; failures never fall back silently. The script uses `heavy` from `PATH` when available, otherwise builds directly,
and prints the resulting
`fast-mode-policy-v0.2.0.so` path. Its default output is
`$XDG_CACHE_HOME/personal-codex-router/fast-mode-policy`, or the same directory
under `/tmp` when XDG_CACHE_HOME is unset. Output inside this repository is
refused; generated headers are discarded. Existing versioned output with
identical bytes is reused, and different bytes are refused. Use a fresh output
directory during development. Installed versioned libraries are never replaced.

Tests (use the selected or cached Go binary for `go test`):

```sh
(cd plugins/fast-mode-policy && go test -v .)
python3 -m unittest discover -s tests -p test_install_fast_mode_plugin.py -v
heavy python3 tests/fast_mode_isolated.py --library /tmp/fast-mode-build/fast-mode-policy-v0.2.0.so \
  --binary /opt/cliproxyapi/8.0.47/cli-proxy-api --response-steering false
```

The isolated test requires `--binary` (for example the installed 8.0.47 executable)
and covers the schema 2 levels for Responses, Chat Completions and Claude clients.
It needs Python PyYAML, websockets >= 15 and openssl. It creates private temporary
state and dummy accounts under `/tmp`, captures only mock traffic, and uses
loopback ephemeral ports, never 8317. All gateway processes and its mock server
stop on completion; the printed evidence directory remains available for inspection.

Every exported C entry point recovers Go panics. Interception errors return an
empty result so the host preserves the original body. Errors never contain request
payloads, headers, credentials or policy contents. Policy errors and websocket
warnings are rate-limited. There are no workers or watchers inside the plugin.
Like every native in-process plugin, recovery cannot protect against a host ABI
violation, native memory corruption, fatal runtime errors or resource exhaustion.
