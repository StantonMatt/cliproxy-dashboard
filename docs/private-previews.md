# Private phone previews

With Tailscale and Tailscale DNS connected, bookmark **[Linux Previews](https://router.tailnet-example.ts.net:8400/)** on the phone or Mac. The Mac and Linux GUI do not need to stay open. Linux and the chosen development server must remain running.

The permanent **[Personal Router dashboard](https://router.tailnet-example.ts.net:8409/)** is linked from the portal. It shows accounts, usage, health, backup status and reset balances. Refresh, routing changes, account sign-in, automatic reset policies and reset redemption work. Raw gateway management routes stay blocked. The computer-only address `router.localhost:18320` refers to the phone itself when opened on the phone.

Only Linux itself, the personal Mac (`100.64.0.11`), and User's S25+ (`100.64.0.12`) are allowed. The service listens on Linux's Tailscale IPv4 address `100.64.0.10`, ports `8400–8409`. The host firewall, systemd network policy and application source-address checks restrict access. This does not use Funnel, expose a public endpoint, enable subnet routing or expand phone management/SSH access.

The certificate covers `router.tailnet-example.ts.net`. Generated links always use this full name over HTTPS. Old HTTP bookmarks using `router`, the full name, the IP, or another explicitly configured alias redirect on the same port to HTTPS with their path and query preserved. Source, Host and Origin checks apply before the redirect. HTTPS URLs using the short name or IP have a certificate-name mismatch; use the full name. The old IP HTTP bookmark redirects and therefore still requires Tailscale DNS at its destination. On the Mac, enable Tailscale DNS if needed with `tailscale set --accept-dns=true`.

## Use a project preview

Ask Codex in the project's task to start or reuse its development server on Linux loopback and register its **HTTP port**. It must manage only its own processes. No other project's source needs to change just to forward its website.

```sh
sudo -n router-preview add my-project 3000
sudo -n router-preview list
```

The command prints `https://router.tailnet-example.ts.net:84xx/` and adds the project to the portal. Registrations last **four hours** by default, up to 24 hours. Registering the same project again extends its lifetime and retains its slot. Expired slots remain reserved until explicitly removed.

```sh
sudo -n router-preview add my-project 3000 --hours 1
sudo -n router-preview remove my-project
```

For standing demos, use `--permanent`. It holds one of the eight slots until removed; plain re-registration keeps it permanent. `--permanent` and `--hours` cannot be combined, and choosing a timed duration for a permanent preview requires removing it first.

```sh
sudo -n router-preview add my-project 3000 --permanent
```

Do not use `tailscale serve` for links opened from the Mac or phone: measured on 2026-11-10, it was about six times slower from the Mac than a preview slot for the same file and backend.

Removal blocks new requests immediately and closes active proxy connections within one second. Expiry closes active connections too. Registration does not start, restart or stop the development process. Stop a temporary server in the task that started it when finished.

For a development server that needs a named Host header:

```sh
sudo -n router-preview add my-project 3285 --host my-project.localhost
```

The proxy always connects to `127.0.0.1`; the Host header never selects a network destination. Common management/database ports are refused. Never register an admin console, database, proxy or credential endpoint.

## Behavior and limits

- HTTPS provides a browser secure context for sign-ins, secure cookies, service workers and browser APIs. Site-specific browser permissions still apply. This is for development reviews, not production hosting.
- Root paths, queries, methods and WebSocket upgrades are forwarded to loopback HTTP. The upstream Host and same-origin Origin are rewritten for the local website. Spoofed forwarding metadata is removed; the proxy sets `X-Forwarded-Proto: https`. Absolute redirects to the upstream localhost, loopback IP or configured Host origin are rewritten to the external HTTPS origin. `Domain=localhost` cookie attributes are removed; other cookie attributes are retained.
- Use relative frontend API/WebSocket URLs. Hard-coded localhost addresses, separate backend ports and production OAuth callbacks may still require project-specific configuration. Existing cookies and local storage under HTTP do not migrate to the HTTPS origin; sign in again. A localhost-only OAuth callback may still need an SSH tunnel.
- Port `8400` is read-only, without browser registration/system-management APIs. Root-owned `/etc/personal-codex-router/previews.json` controls registrations. Ports `8401–8408` remain the eight project slots; `8409` retains a fixed dashboard target and explicit method/route allowlist. Dashboard WebSocket upgrades and raw gateway management are blocked. Unexpected hosts, origins, cross-site API requests and unapproved devices are rejected. User-initiated top-level GET navigation from another app is allowed; cross-site iframes and POSTs are blocked.
- If a device receives a different Tailscale address, inspect its identity before updating all three controls: config, service `IPAddressAllow`, and `/etc/tailscale/host-guard.nft`. Do not allow the whole tailnet.
- Existing `tailscale serve` entries on `10443/10444` are separate and are unaffected.

## Certificate delivery and renewal

The proxy uses Node HTTPS and a small TCP dispatcher: a TLS ClientHello goes to HTTPS; plain HTTP receives a `308` redirect. There are no additional public listeners or slot changes. Plain WebSocket upgrades are rejected; apps should use WSS after loading over HTTPS.

`personal-codex-router-preview.service` retains `DynamicUser`, `ProtectSystem`, `ProtectHome` and its device network allowlist. Systemd `LoadCredential=preview.crt:…` and `LoadCredential=preview.key:…` deliver private copies from `/var/lib/personal-codex-router-preview-tls/current/{cert,key}.pem`. No world-readable private keys are needed. Missing or invalid credentials fail closed.

The root oneshot `personal-codex-router-preview-cert.service` runs `/opt/personal-codex-router/renew-preview-cert.py`. It invokes `tailscale cert --cert-file=<private-candidate>/cert.pem --key-file=<private-candidate>/key.pem <full-DNS-name>` with a `0077` umask, validates the pair, hostname and expiry, then atomically switches the `current` symlink to a private generation directory. Directories are `0700`; files are `0600`. It restarts an active proxy only when the certificate/key fingerprint differs from the last successfully applied pair. A failed restart is retried next run. Old key generations are removed after success.

`personal-codex-router-preview-cert.timer` runs weekly with a persistent catch-up and up to one hour of jitter. Tailscale decides when its cached certificate needs renewal. A certificate change requires a restart to refresh systemd's credential copies; this briefly interrupts active requests/WSS, including dashboard requests and other previews. Do not substitute `systemctl reload` for renewal. Direct test runs using `ROUTER_PREVIEW_CERT`/`ROUTER_PREVIEW_KEY` also support SIGHUP certificate reload, retaining the previous certificate if validation fails.

## Deployment and verification

From the reviewed checkout, use the scoped installer rather than a full operations deployment:

```sh
python3 scripts/install-operations.py --preview-only
sudo -n python3 scripts/install-operations.py --preview-only --apply
```

This backs up replaced preview files and config under `/var/lib/personal-codex-router-backups/preview-deployments/`, preserves existing registrations/aliases, derives the current full DNS name from `tailscale status --json`, and adds `tls_hostname` to config. It installs the proxy, CLI, renewal helper and three preview units. It reloads systemd, starts the certificate oneshot for the first root `tailscale cert` issuance, enables the renewal timer, then enables/restarts only the preview service. It does not rewrite firewall rules or Tailscale settings, or restart the dashboard/gateway. Expect a brief interruption for active previews such as `website-pagar` on `8407`. Account/watch scoped deployments require this TLS migration first; full installs include it automatically.

First issuance requires Tailscale HTTPS certificates already enabled for the tailnet. If issuance fails, inspect the journal; do not weaken key permissions or fall back to HTTP. To repeat issuance/renewal after correcting an error:

```sh
sudo -n systemctl start personal-codex-router-preview-cert.service
sudo -n systemctl status personal-codex-router-preview.service personal-codex-router-preview-cert.timer --no-pager
sudo -n journalctl -u personal-codex-router-preview-cert.service -n 30 --no-pager
sudo -n systemctl list-timers personal-codex-router-preview-cert.timer --no-pager
sudo -n router-preview list
curl -I http://router:8400/
curl --fail https://router.tailnet-example.ts.net:8400/
curl --fail https://router.tailnet-example.ts.net:8409/api/health
sudo -n openssl x509 -in /var/lib/personal-codex-router-preview-tls/current/cert.pem -noout -subject -dates
```

The HTTP check must return `308` with the full HTTPS name. Check the portal, dashboard and each active project from the Mac and phone, including a real sign-in where applicable. Check an unapproved device remains blocked. Existing host firewall/network controls are unchanged. Automated tests use only freshly generated self-signed certificates and isolated loopback ports; they cover TLS, all-port redirects, local Location and cookie rewriting, spoofed headers, hosts/devices/origins, dashboard routes, certificate rotation/reload, expiry and WebSocket revocation.

## Rollback

Record the installer's `pre_deploy_snapshot`. Disable renewal first so it cannot restart the proxy during rollback:

```sh
sudo -n systemctl disable --now personal-codex-router-preview-cert.timer
sudo -n systemctl stop personal-codex-router-preview-cert.service
```

Verify that snapshot using `sudo -n python3 /opt/personal-codex-router/router-backup.py verify /var/lib/personal-codex-router-backups/preview-deployments/<snapshot>`. Restore only the previous `router-preview.mjs`, `router-preview.py` (both installed copies) and `personal-codex-router-preview.service` from its `files/` tree, restoring the ownership/modes recorded in `manifest.json` (scripts `0755`, unit `0644`, root-owned). Preserve the current registrations rather than replacing config with an older snapshot; the old proxy ignores `tls_hostname`. If the snapshot already used TLS, retain its corresponding renewal helper and units too.

For the pre-HTTPS snapshot, substitute its recorded name and restore these four files:

```sh
PREVIEW_SNAPSHOT=/var/lib/personal-codex-router-backups/preview-deployments/<snapshot>
sudo -n install -o root -g root -m 0755 "$PREVIEW_SNAPSHOT/files/opt/personal-codex-router/router-preview.mjs" /opt/personal-codex-router/router-preview.mjs
sudo -n install -o root -g root -m 0755 "$PREVIEW_SNAPSHOT/files/opt/personal-codex-router/router-preview.py" /opt/personal-codex-router/router-preview.py
sudo -n install -o root -g root -m 0755 "$PREVIEW_SNAPSHOT/files/usr/local/bin/router-preview" /usr/local/bin/router-preview
sudo -n install -o root -g root -m 0644 "$PREVIEW_SNAPSHOT/files/etc/systemd/system/personal-codex-router-preview.service" /etc/systemd/system/personal-codex-router-preview.service
sudo -n systemctl daemon-reload
sudo -n systemctl restart personal-codex-router-preview.service
```

 Returning to the old HTTP proxy also returns its secure-context limitation. Keep the root-only certificate state for a later retry or remove it deliberately when HTTPS is abandoned. Do not alter firewall rules, Tailscale Serve, dashboard or gateway services for this rollback.

To stop all previews entirely without affecting SSH or the development servers, disable both the preview service and renewal timer:

```sh
sudo -n systemctl disable --now personal-codex-router-preview-cert.timer personal-codex-router-preview.service
```
