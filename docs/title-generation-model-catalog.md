# Automatic task title generation: catalogue repair

**Current status (2026-10-29):** CLIProxyAPI 8.0.30 is live with catalogue updates enabled; 8.0.47 is installed and staged, with cutover pending ([runbook](gateway-upgrade-8.0.47-2026-10-29.md)). Account prefixes were removed on October 16; see [current model routes and plans](standard-management-setup.md#model-routes). The repair procedure and acceptance results below record September 27.

Before the September 27 repair, the Mac desktop app logs showed `ThreadMetadataGenerationService` failing because the gateway rejected `gpt-6-luna` as an unknown model. The then-installed CLIProxyAPI 7.3.11 service started with `-local-model`, preventing startup and periodic catalogue refresh. Its embedded catalogue predated GPT-6 Luna. The later [8.0.7 upgrade](gateway-upgrade-8.0.7-2026-10-01.md) and [8.0.30 activation](gateway-upgrade-8.0.30-2026-10-15.md) retained catalogue updates.

An isolated loopback instance of the installed binary, launched without that flag, loaded the updated catalogue. Separate structured streaming title requests passed using both personal accounts and each response reported `gpt-6-luna`. Temporary access-token copies had refresh disabled and were removed with the isolated instance. The production gateway was not restarted during the isolated tests. The remote Codex Pro catalogue retains the older task models as well as Astra and adds native GPT-6 Sol and Luna.

Prepared change and original service backup:
`/var/backups/personal-codex-router/title-model-catalog-20260926T235924Z/`

The proposed systemd override removes only `-local-model` from ExecStart. The complete effective unit passed `systemd-analyze verify`. No model aliases, authentication, routing, affinity, reset controls, or task defaults need to change. Once enabled, the built-in updater refreshes the catalogue at startup and every three hours.

Application requires one gateway restart. This version closes open response streams when stopped and keeps affinity bindings in memory, so existing bindings are recreated after restart. Apply in an agreed maintenance window or a verified quiet interval. Active project tasks were still running when this repair was prepared. Both project tasks subsequently completed. The user chose to wait for completion, and the override was then installed and the gateway restarted on September 27.

Apply (root, after confirming the gateway can restart):
```
install -d -m 0755 /etc/systemd/system/cliproxyapi.service.d
install -m 0644 /var/backups/personal-codex-router/title-model-catalog-20260926T235924Z/model-catalog-updates.conf /etc/systemd/system/cliproxyapi.service.d/model-catalog-updates.conf
systemctl daemon-reload
systemctl restart cliproxyapi
```

After applying, verify `/v1/models` includes native Luna on both account routes, run a small structured title request through the production router, check routing and affinity settings, and verify automatic generation or regeneration in the desktop UI. Computer Use currently refuses access to the Codex app, so its title dialog has not been inspected.

Rollback removes only this new override and reloads/restarts the service in another safe window; it does not restore old auth files, quota state, or subscription settings. The original unit is saved in the backup directory.

## Applied and verified, September 27

Installed `/etc/systemd/system/cliproxyapi.service.d/model-catalog-updates.conf` after both named project tasks completed. Gateway is active. `/v1/models` exposes `gpt-6-luna` and the account-a@example.com and account-b@example.com account-prefixed routes for `gpt-6-luna`. Structured streaming title-generation tests passed independently through both production account routes, each reporting native `gpt-6-luna`. The Mac's configured router endpoint also lists Luna and Astra.

Before/after comparison confirmed routing settings, account priorities and the `highest-remaining-quota` selection were preserved. No model aliases were added. No reset credits were used. Saved live verification is in `verification-before.json` and `verification-after.json` in the private backup directory. The desktop title/regenerate UI remains untested because Computer Use denied access to the Codex app; existing fallback titles may need regeneration.
