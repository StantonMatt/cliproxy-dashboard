# Prefer the earliest weekly reset

September 30, 2026 note: this records the September 23 activation. Since [September 26](routing-selector-2026-09-26.md), the controller reconciles the saved routing selection rather than always preferring the earliest reset. The shared dashboard allows routing changes, reset policy changes, and reset redemption; the read-only description below is historical.

Enabled September 23, 2026. The router now gives new sessions to the healthy account with available allowance whose weekly window resets first. Priority is re-evaluated every minute from the dashboard's existing quota observations.

At activation, `account-b@example.com` had 43% remaining and resets September 26 at 09:42:56 America/Fortaleza. It received priority 100; `account-a@example.com` retains effective priority 0. This is shared gateway policy, so it applies to Mac and Linux clients using the shared model route. Since October 16, 2026, both accounts use plain model names without account prefixes; see [Model routes](standard-management-setup.md#model-routes).

Healthy existing sessions keep their current account, including subagent affinity. The one-hour affinity TTL and gateway failover settings are unchanged, except that each [conversation move](move-conversations-2026-10-17.md) alternates the TTL between `1h0m0s` and `1h0m1s`. A reset rollover changes the preferred account for new sessions. Reported exhausted windows or unavailable accounts are skipped. Missing, stale, invalid, or expired quota observations hold the last priorities until a fresh observation arrives; they are never interpreted as unused allowance.

## Runtime

- Controller: `/opt/personal-codex-router/prefer-earliest-reset.py`
- Unit: `personal-codex-router-preference.service` (oneshot, user `cliproxyapi`)
- Timer: `personal-codex-router-preference.timer`, every 60 seconds, enabled at boot
- Private policy state and original-priority backup: `/var/lib/personal-codex-router-routing`
- The controller uses the existing loopback-only management API to PATCH only `priority`. No OAuth credentials are read by the controller, no new network listener is created, and the model gateway was not restarted.
- The dashboard remains read-only. It displays the preferred account and “Earliest weekly reset” in System.

The underlying selector remains round-robin within an equal-priority tier. The controller determines priority using reset times; there is no invented `earliest-reset` native strategy setting.

## Validation

- Ten unit tests passed, covering quota rollover, exhausted short and weekly windows, missing/stale observations, interrupted-update recovery, manual priority protection, and credential-safe rollback, alongside the existing dashboard tests.
- Five streamed requests through an isolated instance of the then-installed CLIProxyAPI 7.3.11 proved initial preference, preserved existing-session affinity, new-session preference, preserved affinity after rollover, and new-session selection after rollover.
- Isolated rollback restored absent original priority fields. Production gateway files were unchanged by that test, and temporary credentials/processes were removed.
- A live unprefixed `gpt-6-astra` test returned `ROUTER_RESET_PRIORITY_OK`. A unique recorded usage row confirmed `account-b@example.com` supplied the response at 2026-09-23T11:16:52Z.
- The next timer invocation completed with no redundant priority writes. Gateway, dashboard, and Tailscale remained active.
- Browser verification showed the preferred-account label and routing status. Actual quota exhaustion was not forced; gateway failover was separately tested with copied invalid credentials (see `failover-verification.json`).

## Rollback

On Linux, stop the controller timer and any active invocation, then restore original priority fields through the supported API:

```sh
sudo systemctl disable --now personal-codex-router-preference.timer
sudo systemctl stop personal-codex-router-preference.service
sudo -u cliproxyapi python3 /opt/personal-codex-router/prefer-earliest-reset.py --rollback
```

Rollback refuses unexpected account-file names or priorities outside the controller's 0/100 range. It patches only the priority fields, preserving current OAuth credentials and other settings. The dashboard returns to the underlying round-robin label on refresh.

Dashboard runtime backup: `/home/user/.local/state/personal-codex-router/backups/earliest-reset-20260923T111520Z`.

Upstream references: [v7.3.11 routing semantics](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.11/config.example.yaml), [supported auth-field patch handler](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.11/internal/api/handlers/management/auth_files_fields.go).
