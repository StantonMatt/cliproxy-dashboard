# Mac T3 Codex model catalog

The Mac's T3 `codex` provider uses `~/.t3/codex-home/config.toml` and the desktop gateway. Set its top-level `model_catalog_json` to `/Users/user/.local/share/codex-router-models/models.json`. Leave the user's `~/.codex` and T3 `codex_direct` alone: those have independent provider/login behavior.

`scripts/sync-mac-model-catalog.py` mirrors the desktop's published catalog over the Mac's existing noninteractive SSH alias `router`. The desktop's existing sync remains authoritative for account-aware model filtering. Copying its result keeps both machines' catalogs identical without moving API keys or duplicating that filtering on the Mac. This is a separate mirror, not a replacement for the desktop's untracked `sync.py`; the desktop does not need to install this script.

The mirror checks JSON, unique unprefixed slugs, nonempty visible models, and size (16 MiB maximum). For changed content, it starts a temporary local Codex app-server with the candidate catalog override and requires `model/list` to match the visible slugs exactly. It then atomically replaces `models.json` with the original desktop bytes. A lock prevents overlapping refreshes. Connection (8 seconds), SSH keepalive (5 seconds, one missed reply), total fetch (30 seconds), and local parser (25 seconds) timeouts bound failures. Parser cleanup allows 3 seconds before killing only the validation process it started.

A failed fetch, invalid response, or parser rejection leaves the previous file untouched and exits 1. No remote output, credentials, or exception text is logged. If the desktop is off, the old catalog remains available; inference through the desktop gateway still requires the desktop. The user's separate `codex_direct` fallback remains available. The first successful sync must happen before enabling `model_catalog_json`, so an offline first install cannot leave T3 pointing at a missing file.

## Install on the Mac

Prerequisites: `/opt/homebrew/bin/python3`, `/opt/homebrew/bin/codex`, working batch SSH to `router`, and the desktop's existing catalog timer. Do not restart the gateway or T3.

1. Back up any existing destination files to a private timestamped directory under `~/.cache/agent-scratch/personal-codex-router/` before replacing them. Create `~/.local/share/codex-router-models/` with mode 0700. Install `scripts/sync-mac-model-catalog.py` there as `sync.py` (mode 0600 is sufficient; Python runs it).
2. Run `/opt/homebrew/bin/python3 ~/.local/share/codex-router-models/sync.py`. Confirm it succeeds and the catalog contains the expected models before editing T3's config.
3. Back up `~/.t3/codex-home/config.toml` privately. Insert only the top-level `model_catalog_json` setting, before any TOML table. Preserve all other bytes and file permissions. Never print the whole config; inspect only `grep -nE '^model_catalog_json ' ~/.t3/codex-home/config.toml`.
4. Replace `__HOME__` in `scripts/com.user.codex-router-model-catalog.plist` with the Mac's absolute home directory and install it at `~/Library/LaunchAgents/com.user.codex-router-model-catalog.plist` (0600). The template uses absolute binary paths because LaunchAgents do not inherit the interactive shell's PATH.
5. Run `plutil -lint` on the installed plist, then `launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.user.codex-router-model-catalog.plist"`. For an existing task-owned installation, boot it out before reloading the updated plist. It runs at login/load and every 300 seconds; no sudo or persistent helper process is required.
6. Inspect `launchctl print "gui/$(id -u)/com.user.codex-router-model-catalog"` for a successful run and `sync.log` for its result. Start a temporary T3 thread with `--provider codex --model gpt-6.2-astra --wait` and require an actual reply, then remove its project.

The desktop and Mac each refresh every 5 minutes, so a gateway change can take roughly 10 minutes to reach the Mac while both machines are awake and reachable. Sleeping/offline machines catch up on a subsequent scheduled refresh. T3 can cache an already running provider's discovery. On the installed November 19 build, the first thread attempt still rejected `gpt-6.2-astra` after the config edit. An explicit instance-scoped `server.refreshProviders` RPC with `{"instanceId":"codex","refreshModels":true,"fresh":true}` refreshed it without restarting T3 or its existing conversations. The temporary local auth session required `orchestration:read` as well as `providers:manage` and was revoked afterward. The subsequent live thread replied `OK.`. T3's normal provider health probes rediscover models while background provider work is active; an idle client may show its cached list until a refresh. The 10-minute bound above applies to the file, not an inactive T3 picker. Confirm availability through an actual thread rather than assuming a config edit refreshed discovery.

## Failure check and rollback

Record the SHA-256 of `models.json`, then run:

```sh
/opt/homebrew/bin/python3 ~/.local/share/codex-router-models/sync.py --source-host 127.0.0.1 --ssh-port 1
```

Require exit 1 and the same file SHA-256. This simulates an unreachable catalog source without changing the gateway or SSH configuration. The unit tests also cover empty/malformed catalogs and local parser rejection.

To uninstall, boot out only `gui/$(id -u)/com.user.codex-router-model-catalog`, remove its plist, and restore the backed-up T3 config (or remove only the added top-level setting if the config has since changed). Remove the mirror directory only after the config no longer references it. Do not alter the desktop timer or the user's `~/.codex`.

Focused tests: `python3 -m unittest discover -s tests -p test_mac_model_catalog.py -v`.
