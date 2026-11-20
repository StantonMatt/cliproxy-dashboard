# Codex WebSockets

CLIProxyAPI 8.0.79 has been live since 2026-11-20; its
[upgrade](gateway-upgrade-8.0.79-2026-11-19.md) passed the installed v0.2.0 plugin
and WebSocket gates on 8.0.79 and on the 8.0.47 rollback. Keep response steering
explicitly false; gateway payload rules still run after account policy. Local KV
is disabled, so session/date-pin/replay state stays in memory and is lost on
restart as on 8.0.47.

Codex can keep a Responses WebSocket open through the gateway instead of making
an HTTP streaming request for each turn. Both ends must opt in: the selected
client provider needs `supports_websockets = true`, and each file-backed gateway
Codex credential needs top-level metadata `websockets: true`. The ordinary
`/v1/responses` route already exists and requires the normal client key. `ws-auth`
is an unrelated AI Studio setting; do not change it.

The [OpenAI WebSocket-mode guide](https://developers.openai.com/api/docs/guides/websocket-mode)
(accessed 2026-10-27) describes persistent connections, incremental inputs and
`previous_response_id` continuation. It reports **up to roughly 40% faster
end-to-end execution for rollouts with 20+ tool calls**. That is a workload-specific
OpenAI observation, not a benchmark or promise for this gateway's subscription
upstream. Connection reuse worked in the isolated tests below. Real latency,
reconnection rates, billing and client UI compatibility remain acceptance checks.
Linux loopback has little client-side RTT to save; the upstream leg and long tool
chains offer the more plausible benefit. Mac traffic keeps using the existing
SSH/Tailscale route; no additional public listener is needed.

## Keep response steering off

WebSocket **transport** and experimental full-duplex **response steering** are
separate. Use the canonical nested setting:

```yaml
upstream:
  codex:
    response-steering: false
```

With steering false, sequential turns still reuse an upstream WebSocket and keep
continuation IDs. Every turn re-enters account policy enforcement. With steering
true, follow-up creates bypass the fast-mode plugin in **8.0.30 and 8.0.43**.
The installed Codex CLI sends a `generate: false` warmup before its first actual
inference: duplex mode protects the warmup but lets the denied account's first
user-turn inference retain priority. Do not enable duplex while relying on the
per-account fast-mode toggle. A duplex follow-up 429 before `response.created`
also has a known missing failure-usage record; successful-response accounting was
verified separately. Serial error handling may close the socket and require client
recovery; transparent failover of every follow-up is not guaranteed.

8.0.47 adds per-frame before/after-auth hooks in upstream `0fe38224`, fixing
the tested duplex create-follow-up and native-warmup policy bypass. The initial
old expectation failed and prompted the source reassessment recorded in the
[8.0.47 preparation](gateway-upgrade-8.0.47-2026-10-29.md). Keep steering explicitly
false for this rollout: full-duplex/error interactions and the 8.0.30 rollback
still need that safeguard. The plugin warning is unconditional
and does not establish a bypass on 8.0.47.

Later payload rules can restore `service_tier` after the plugin removes it.
Check `requests.payload` and historical `payload`, including `default`,
`default-raw`, `override`, and `override-raw`. No Codex rule should set that field,
including a conditional rule or a rule for a currently unused model. A client
`service_tier = "default"` is a preference, not enforcement: T3 turn selection,
profiles or explicit requests may override it.

The preflight prints only booleans and fixed key paths, never config values:

```sh
sudo -n python3 -B scripts/check-codex-websockets.py
```

Exit 0 requires explicit canonical false and no detected Codex tier setter.
Exit 1 flags unsafe or implicit policy (absence means default false but still
requires making it explicit); exit 2 means unreadable/invalid YAML or an unsupported
shape. This is a conservative policy check, not a general gateway config validator.
It considers omitted protocol constraints as possible Codex matches and deliberately
does not try to prove model/header/source/condition gates unreachable. It checks
both payload spellings even when one would win precedence. Historical steering
aliases are resolved in canonical → `oauth.providers.codex` → `codex` order.

## Morning rollout (separate authorization required)

**Recommend after the [8.0.47 cutover](gateway-upgrade-8.0.47-2026-10-29.md) and its existing acceptance gates are green.**
The candidate and rollback versions passed these tests. Changing one variable
at a time makes rollback and attribution clearer. Put the explicit-false safeguard into the
fresh config retained for the upgrade, then enable credential/client transport as
a separate step. Do not enable steering as part of either change. This document's
commands were prepared and checked offline; they were not applied to production.

1. On Linux, use the reviewed checkout containing these files. Confirm the
   effective binary, record the initial PID, run the preflight, and capture a fresh
   private config backup. Keep management on loopback, existing account identities,
   priorities, model routes and routing affinity unchanged. Follow the same-inode,
   fsync and guarded-input conventions in
   [gateway_config.py](../scripts/gateway_config.py) and the
   [v8 migration procedure](gateway-config-v8-migration.md). Never dump the config,
   management response or credential files into a terminal or report.

   ```sh
   cd /home/user/Projects/personal-codex-router
   systemctl show cliproxyapi --property=MainPID --property=ExecStart
   sudo -n python3 -B scripts/check-codex-websockets.py
   ```

2. Add canonical false **in place**, retaining unrelated bytes and comments. The
   following Linux command prepares and validates a candidate, backs up the exact
   original outside the repo, checks current bytes again, writes the same inode
   with fsync, and waits for this PID's new reload log. It does not restart/reload a
   service through systemd or PATCH the config. It refuses aliases/duplicate YAML
   keys, unsafe flow/empty blocks needing a manual edit, and any tier-setting rule.
   The pre-existing plugin must already be enabled and healthy. Run in an idle
   config-edit window; do not run the preference editor or another config writer
   concurrently. The collector keeps ownership of its queue; do not drain it. The command
   also verifies account-file identities and timer states did not change; if an
   OAuth refresh races that check, stop and inspect rather than repeating blindly.

   ```sh
   sudo -n python3 -B - <<'PY'
   import copy, importlib.util, json, os, pathlib, sys, time
   import yaml
   sys.excepthook = lambda *_: print('Config edit failed; stop and inspect privately', file=sys.stderr)
   repo = pathlib.Path.cwd()
   sys.path.insert(0, str(repo / 'scripts'))
   import gateway_config as gc
   def module(name, filename):
       spec = importlib.util.spec_from_file_location(name, repo / 'scripts' / filename)
       mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
       return mod
   install = module('install', 'install-fast-mode-plugin.py')
   check = module('wscheck', 'check-codex-websockets.py')
   path = pathlib.Path('/etc/cliproxyapi/config.yaml')
   original = path.read_bytes()
   cfg, tree = install.validated_config(original)
   enabled, item = install.status_settings(cfg)
   pid = install.main_pid()
   assert enabled and install.verify_status(pid, item)
   expected = copy.deepcopy(cfg)
   expected.setdefault('upstream', {}).setdefault('codex', {})['response-steering'] = False
   assert check.inspect_config(expected)['rollout_ready']
   gc.PATHS['response-steering'] = (('codex', 'response-steering'),
                                  ('upstream', 'codex', 'response-steering'))
   def child(node, key):
       assert isinstance(node, yaml.MappingNode)
       return next((v for k, v in node.value if k.value == key), None)
   upstream = child(tree, 'upstream')
   if upstream is None:
       candidate = original + (b'' if original.endswith(b'\n') else b'\n') + b'upstream:\n  codex:\n    response-steering: false\n'
   elif child(upstream, 'codex') is None:
       assert not upstream.flow_style and upstream.value
       first = upstream.value[0][0].start_mark
       offset, indent = first.index - first.column, first.column
       text = original.decode()
       addition = ' ' * indent + 'codex:\n' + ' ' * (indent + 2) + 'response-steering: false\n'
       candidate = (text[:offset] + addition + text[offset:]).encode()
   else:
       candidate = gc.validated_edit(original, {'response-steering': False}, allow_add=('response-steering',))
   actual, _ = install.validated_config(candidate)
   assert actual == expected and check.inspect_config(actual)['rollout_ready']
   backup = pathlib.Path('/var/lib/cliproxyapi-upgrade-backups') / (time.strftime('websockets-%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(time.time_ns()))
   backup.mkdir(mode=0o700, parents=True, exist_ok=False)
   for name, data in [('config.before.yaml', original), ('config.candidate.yaml', candidate)]:
       fd = os.open(backup / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
       with os.fdopen(fd, 'wb') as stream:
           stream.write(data); stream.flush(); os.fsync(stream.fileno())
   before = install.metadata(path)
   runtime_before = install.runtime_snapshot(cfg)
   cursor = install.journal_cursor()
   if candidate != original:
       gc.write_same_inode(path, candidate, original)
       end = time.monotonic() + 30
       while time.monotonic() < end:
           assert install.main_pid() == pid
           logs = install.command('journalctl', '-u', 'cliproxyapi', '--after-cursor=' + cursor,
                                  '_PID=' + str(pid), '--no-pager', '-o', 'cat')
           if 'config successfully reloaded' in logs:
               break
           time.sleep(.2)
       else:
           raise SystemExit('Reload unconfirmed; stop here and inspect the private backup')
   assert install.main_pid() == pid and install.metadata(path) == before
   assert path.read_bytes() == candidate and install.verify_status(pid, item)
   runtime_after = install.runtime_snapshot(actual)
   assert runtime_after[:2] == runtime_before[:2]
   assert runtime_before[2] != 'success' or runtime_after[2] == 'success'
   print(json.dumps({'same_pid': True, 'same_inode_owner_mode': True, 'steering_explicit_false': True}))
   print('Private backup directory:', backup)
   PY
   sudo -n python3 -B scripts/check-codex-websockets.py
   ```

   Isolated tests changed canonical steering false→true→false by this watched-inode
   method on **both binaries**, kept the PID, and proved new-socket behavior
   changed accordingly. A restart window is not required for this key. The generic
   reload log alone does not prove request policy; the behavior check below is
   still required. Existing sockets snapshot steering: a config edit does not
   revoke a previously established duplex socket. Close/reconnect such sessions
   before relying on the safeguard. On any verification failure, stop before
   credential/client enablement; reconcile current bytes with the private backup.

3. Enable `websockets` on the **one existing file-backed Codex credential** using
   only the scoped fields PATCH. Run the following on Linux; the key remains in
   process memory, not shell arguments or printed output. It selects exactly one
   Codex file-backed entry, preserves other metadata, verifies the runtime flag
   and stable public identity/routing fields, and records the previous flags in a
   private rollback file. It never reads raw credential files. Check the account
   in the local dashboard beforehand if the set has changed; the command refuses
   a different count (the Free account was removed on October 29). Keep the private
   rollback file until acceptance is complete.

   ```sh
   sudo -n python3 -B - true <<'PY'
   import json, os, pathlib, subprocess, sys, time, urllib.request
   sys.excepthook = lambda *_: print('Credential operation failed; stop and inspect privately', file=sys.stderr)
   enabled = {'true': True, 'false': False}[sys.argv[1]]
   key = pathlib.Path('/etc/cliproxyapi/dashboard-management-key').read_text().strip()
   http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
   def call(route, body=None):
       req = urllib.request.Request('http://127.0.0.1:8317/v0/management' + route,
           data=json.dumps(body).encode() if body is not None else None,
           method='PATCH' if body is not None else 'GET',
           headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
       with http.open(req, timeout=10) as response:
           return json.load(response)
   def pid():
       return subprocess.check_output(['systemctl', 'show', 'cliproxyapi', '--property=MainPID', '--value'], text=True).strip()
   before_pid = pid()
   rows = call('/auth-files')['files']
   codex = [r for r in rows if r.get('provider') == 'codex' and r.get('source') == 'file' and not r.get('runtime_only', False)]
   assert len(codex) == 1 and all(r.get('name') for r in codex)
   stable = ('id', 'name', 'provider', 'email', 'account', 'account_id', 'auth_index', 'prefix', 'priority', 'disabled')
   snapshot = lambda r: {k: r.get(k) for k in stable}
   backup = pathlib.Path('/var/lib/cliproxyapi-upgrade-backups') / (time.strftime('websocket-flags-%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(time.time_ns()) + '.json')
   fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as stream:
       json.dump([{'name': r['name'], 'websockets': r.get('websockets', False)} for r in codex], stream)
       stream.flush(); os.fsync(stream.fileno())
   for row in codex:
       call('/auth-files/fields', {'name': row['name'], 'websockets': enabled})
       after = next(r for r in call('/auth-files')['files'] if r.get('name') == row['name'])
       assert after.get('websockets') is enabled and snapshot(after) == snapshot(row)
   assert pid() == before_pid
   print(json.dumps({'one_credential_verified': True, 'websockets': enabled, 'same_pid': True}))
   print('Private previous-flags file:', backup)
   PY
   ```

   The scoped handler persists the boolean and synchronizes runtime attributes;
   isolated tests checked both saved JSON and management-list values and observed
   new connections switch HTTP→WS→HTTP with the same gateway PID. The production
   command intentionally does not open credential files to repeat that check.
   If a PATCH/verification fails partway through, stop and set the flag false
   with the same command using `false`; do not proceed with partial enablement.
   An unchanged health/status snapshot may be inspected privately too, but status
   can legitimately change as active requests run.

4. Edit the selected provider **inside each effective client home**, preserving
   its URL, credentials, model selection and other settings. Back up each config
   privately on its own host first. These paths/provider IDs were established by
   the 2026-10-27 research; verify the selected provider again before editing.

   | Client | Config file | Existing provider table |
   |---|---|---|
   | Linux CLI and Linux T3 | `/home/user/.codex/config.toml` (T3 resolves here) | `[model_providers.personal_codex_gateway]` |
   | Linux desktop app | `/home/user/.local/share/codex-shared-client/codex-home/config.toml` | `[model_providers.personal_codex_gateway]` |
   | Mac CLI | `/Users/user/.codex/config.toml` | `[model_providers.linux_gateway]` |
   | Mac T3 | `/Users/user/.t3/codex-home/config.toml` | `[model_providers.personal_codex_gateway]` |

   Set `supports_websockets = true` in each table. For a one-session CLI check the
   exact overrides are:

   ```sh
   # Linux CLI (start a fresh session)
   codex -c model_providers.personal_codex_gateway.supports_websockets=true
   # Mac CLI, run on the Mac
   codex -c model_providers.linux_gateway.supports_websockets=true
   ```

   For persistent edits, this host-local command changes only an existing plain
   provider table's boolean (or inserts it), checks TOML semantics against the
   intended single-field change, and saves an exact private backup. It requires
   Python 3.11+ (`tomllib`); use the host's `python3`, with Homebrew PATH on Mac.
   Do not run it while another process edits the config. Set the two arguments
   from the table, and run once per distinct file on the appropriate host:

   ```sh
   python3 - /home/user/.codex/config.toml personal_codex_gateway true <<'PY'
   import copy, os, pathlib, re, sys, time, tomllib
   sys.excepthook = lambda *_: print('Client edit failed; stop and inspect privately', file=sys.stderr)
   path, provider, value = pathlib.Path(sys.argv[1]), sys.argv[2], {'true': True, 'false': False}[sys.argv[3]]
   original = path.read_bytes(); text = original.decode(); cfg = tomllib.loads(text)
   assert cfg['model_provider'] == provider
   expected = copy.deepcopy(cfg)
   expected['model_providers'][provider]['supports_websockets'] = value
   header = re.compile(r'^\[model_providers\.' + re.escape(provider) + r'\][ \t]*(?:#.*)?$', re.M)
   found = list(header.finditer(text)); assert len(found) == 1
   begin = found[0].end()
   next_table = re.search(r'^\[', text[begin:], re.M)
   end = begin + next_table.start() if next_table else len(text)
   section = text[begin:end]
   field = re.compile(r'^(\s*supports_websockets\s*=\s*)(true|false)([ \t]*(?:#.*)?)$', re.M)
   encoded = str(value).lower()
   if field.search(section):
       section, count = field.subn(lambda m: m[1] + encoded + m[3], section); assert count == 1
   else:
       section = '\nsupports_websockets = ' + encoded + '\n' + section
   candidate = (text[:begin] + section + text[end:]).encode()
   assert tomllib.loads(candidate.decode()) == expected
   backup = path.with_name(path.name + '.before-websockets-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(time.time_ns()))
   fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'wb') as stream:
       stream.write(original); stream.flush(); os.fsync(stream.fileno())
   with path.open('r+b') as stream:
       assert stream.read() == original
       stream.seek(0); stream.write(candidate); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
   print('provider_websockets_verified=' + encoded)
   PY
   ```

   T3 alternatively accepts launch arguments
   `-c model_providers.personal_codex_gateway.supports_websockets=true` for its
   Codex provider. `T3CODE_CODEX_LAUNCH_ARGS` overrides the configured launch-argument
   string, so inspect it privately for conflicting flags. No separate inference
   WebSocket switch in T3 or app-server `--listen ws://…` is needed: `--listen`
   controls app-server RPC. Removed `features.responses_websockets*` flags are
   unnecessary. See the [official Codex configuration reference](https://developers.openai.com/codex/config-reference).

5. Start fresh CLI/T3 sessions and reopen the desktop client as needed so the
   provider is reread and old sockets disconnect. Do not interrupt unrelated
   sessions. If an old duplex session cannot be closed, do not claim its account
   policy is enforced by this change. Check one short, authorized conversation per
   client, with a first turn, follow-up and reconnect. Confirm normal operation
   and the intended account/model route, then exercise priority on a denied
   account and, only when explicitly authorized to spend priority quota, an allowed
   account. Do not enable fast mode on either real account just to run a probe.

   Observe logs locally without displaying raw lines: they can include account
   filenames, URLs and session IDs. Note the UTC time just before the probe, then
   run this finite summary after it, replacing the timestamp with that start. It
   reports booleans only:

   ```sh
   sudo -n journalctl -u cliproxyapi --since '2026-10-27 09:00:00 UTC' --no-pager -o cat |
     python3 -c 'import json,sys; s=sys.stdin.read(); print(json.dumps({"upstream_ws_seen":"codex websockets: upstream connected" in s,"upstream_reuse_logged":"reused=true" in s,"duplex_warning_seen":"follow-up websocket turns bypass the fast-mode policy" in s,"policy_decision_seen":"fast-mode-policy decision" in s}))'
   ```

   `codex websockets: upstream connected` confirms the upstream WS path; a
   downstream upgrade alone can still use upstream HTTP if the credential flag is
   false. `reused=true` is supplementary, not required: serial reuse can be silent.
   The plugin's warning is unconditional for downstream WebSockets in v0.1.2 and
   can appear with steering false; it does **not** prove duplex is active. Correlate
   a narrow, otherwise idle probe window privately; a summary over concurrent
   sessions cannot attribute transport to a particular client. Do not turn on
   request/body logging. Do not publish auth indexes or request/session IDs.

   The account toggle's per-turn behavior is proven against the mock by the tests
   below. Live logs and the dashboard's original requested tier alone do not prove
   what was sent upstream. A policy decision log confirms the hook ran; it may not
   contain the resulting tier. Read the effective-tier field privately if the
   installed gateway exposes it; the existing collector currently drops that
   additive field. Without safe outbound/effective-tier evidence, record live
   fast-mode enforcement as inferred from pinned isolated tests, not newly proven.
   Let the existing collector record the real turns; confirm dashboard usage and
   collector health without calling the destructive usage-queue endpoint yourself.

## Rollback

1. Set `supports_websockets = false` in all four provider tables using the same
   host-local edit command with `false`, or replace/remove corresponding T3 launch
   overrides (including the environment override). A fresh Linux CLI can force
   `codex -c model_providers.personal_codex_gateway.supports_websockets=false`;
   on Mac CLI use `linux_gateway` instead.
2. Rerun the scoped credential command above with its `true` argument changed to
   `false`. It PATCHes **only** `websockets` for the same Codex file entry,
   validates the boolean and preserves identity/priority/routing fields. Keep
   management on Linux loopback. It takes its own fresh previous-flags snapshot;
   use the earlier private snapshot if reconciling the pre-rollout state later.
3. Close/reconnect all affected client sessions. Credential false and client false
   govern new connections; an existing upstream/duplex socket is **not** retired
   by the flag PATCH. Verify new traffic uses HTTP and normal collector health,
   without a second usage-queue reader. Leave explicit steering false in place;
   transport rollback does not require reverting that safeguard or the gateway
   executable. No gateway restart is required for the credential flags.
4. If the config edit itself must be undone, use the exact fresh private
   `config.before.yaml` and `config.candidate.yaml`. First compare current bytes
   against the candidate and reconcile any subsequent config changes. Restore
   bytes with `gateway_config.write_same_inode`, not atomic replacement, and
   repeat PID/inode/owner/mode and new reload-log verification from step 2. Do not
   restore a backup that enables steering while WebSocket clients can still
   connect. Coordinate any binary rollback with the separate upgrade runbook.

## Regression evidence

Run from this checkout with the plugin from the [full install](../README.md#update-an-existing-installation); each integration command goes
through `heavy`. The mock accepts only the textual CONNECT target
`chatgpt.com:443`, terminates TLS locally and never resolves/dials an upstream.
Other background catalog/update CONNECT attempts are rejected and counted. All
configs, OAuth files, client homes and keys are synthetic. The tests consume only
their own disposable gateway usage queues.

```sh
python3 -B tests/codex_websockets_isolated.py --check-only
~/.local/bin/heavy python3 -B tests/codex_websockets_isolated.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api
~/.local/bin/heavy python3 -B tests/codex_websockets_isolated.py --binary /opt/cliproxyapi/8.0.47/cli-proxy-api
# Existing broader plugin suite, parameterized for either steering mode:
~/.local/bin/heavy python3 -B tests/fast_mode_isolated.py --binary /opt/cliproxyapi/8.0.79/cli-proxy-api --library /opt/personal-codex-router/plugins/linux/amd64/fast-mode-policy-v0.2.0.so --response-steering false
# Repeat with --response-steering true and with the 8.0.47 rollback binary.
```

On 2026-10-27, 8.0.30 and 8.0.43 each passed **39 new integration assertions, 24 completed
synthetic responses, and 6 fixture test methods** (including a family/protocol
matrix). The installed Linux Codex 0.164.0 ran successfully with isolated homes
and no inherited credentials: warmup→first inference reused one upstream socket,
with serial enforcement and a clearly labelled duplex expected policy failure.
The expected-failure assertions deliberately require the known bypass; they fail
if upstream behavior changes, prompting reassessment rather than silently keeping
this limitation. The broader suite passed 53 HTTP policy cases plus its failover,
reload, status and WebSocket checks on each binary in each steering mode.

On 2026-10-29, 8.0.47 and the 8.0.30 rollback each passed **39 assertions,
24 completed synthetic responses and six fixture tests** with native Codex
**0.166.0**. Both versions passed the broader **53 HTTP cases in each steering
mode**. Upstream `0fe38224` adds ordinary interceptor hooks to subsequent duplex
business frames in 8.0.47. Its initial old expected-bypass assertion failed,
triggering a source reassessment; the final tests require denied follow-up and
native-warmup enforcement on exactly 8.0.47 while retaining the expected bypass
on 8.0.30/8.0.43. This does not authorize steering enablement. New evidence and
cleanup manifests are under
`~/.cache/agent-scratch/personal-codex-router/upgrade-8.0.47/` and summarized in the
[upgrade evaluation](gateway-upgrade-evaluation-v8.0.47.json).

The serial matrix verifies denied and allowed first/follow-up/reconnect frames,
connection reuse, exact continuation IDs, scoped credential false→true→false
persistence/runtime transport HTTP→WS→HTTP under one PID, steering hot reload,
and a real late tier override that the preflight detects. Each completed mock
response has a unique token tuple; usage checks match the exact multiset of
account, input/output/total, reasoning and cached tokens for every turn. These
are successful-turn tests, not a claim that every rejected request is accounted
for. Synthetic served tiers do not establish billing semantics.

Results and process manifests are under
`~/.cache/agent-scratch/personal-codex-router/websockets/implementation-20261027/`.
Every owned gateway/native Codex process exited and every mock listener closed;
the new harness removes synthetic auth, configs, CA keys, copied libraries and
disposable client homes after saving sanitized results. No persistent server or
browser tab was started. Research background and the separate rejected-turn
findings remain in
`~/.cache/agent-scratch/personal-codex-router/websockets/research-20261027/report.md`.
No live inference, live usage-queue request, rollout, client setting change or
Mac operation was performed during implementation.
