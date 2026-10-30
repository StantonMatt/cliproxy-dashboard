# fast-mode-policy 0.1.2

Native CLIProxyAPI C/JSON ABI plugin (ABI 1, schema 6), built against CLIProxyAPI
v8.0.7, revision `97f244b8ddb9cbf564b6e6faab0159102cca8617`. `go.mod` pins
v8.0.7 and `go.sum` pins its archive; no upstream source is copied into this repo.
The module proxy origin for this tag was verified against that revision.

It edits only the Codex request's `service_tier` after credential selection.
See [the policy and deployment guide](../../docs/fast-mode-policy.md) for the
contract, defaults, status lifecycle and websocket limitation.

Build on Linux amd64 with a C compiler and Go >= 1.26 (tested with 1.26.4):

```sh
plugins/fast-mode-policy/build.sh /tmp/fast-mode-build
```

`GO=/absolute/path/to/go` selects a toolchain. The script uses `heavy` from `PATH` when available, otherwise builds directly,
and prints the resulting
`fast-mode-policy-v0.1.2.so` path. Its default output is
`$XDG_CACHE_HOME/personal-codex-router/fast-mode-policy`, or the same directory
under `/tmp` when XDG_CACHE_HOME is unset. Output inside this repository is
refused; generated headers are discarded. Existing versioned output with
identical bytes is reused, and different bytes are refused. Use a fresh output
directory during development. Installed versioned libraries are never replaced.

Tests (the build requires Go to be on PATH, or GO above; unit tests use `go`):

```sh
(cd plugins/fast-mode-policy && go test -v .)
python3 -m unittest discover -s tests -p test_install_fast_mode_plugin.py -v
heavy python3 tests/fast_mode_isolated.py --library /tmp/fast-mode-build/fast-mode-policy-v0.1.2.so
```

The isolated test accepts `--binary`, defaulting to the installed 8.0.7 executable.
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
