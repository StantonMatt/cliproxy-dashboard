# Private phone previews

With Tailscale connected, bookmark **[Linux Previews](http://router:8400/)** on the phone. The Mac and Linux GUI do not need to stay open. Linux and the chosen development server must remain running.

The permanent **[Personal Router dashboard](http://router:8409/)** is also linked from the preview portal. It shows account balances, usage, health, backup status and reset balances. The dashboard no longer shows notice tiles; Linux desktop notifications continue. Refresh, routing changes, automatic reset policy changes, and reset redemption work. Raw gateway management routes are blocked. The Mac-only address `router.localhost:18320` refers to the phone itself when opened on the phone and cannot reach the Mac's tunnel.

The allowed devices are Linux itself, the personal Mac (`100.64.0.11`), and **User's S25+** (`100.64.0.12`). The preview service listens only on Linux's Tailscale IPv4 address `100.64.0.10`, ports `8400–8409`. The host firewall, service network policy, and application source-address checks restrict access. It does not use Funnel, expose a public endpoint, enable subnet routing, or open management/SSH access to the phone.

The Linux Tailscale device is named `router`, with full DNS name `router.tailnet-example.ts.net`. MagicDNS is enabled for the tailnet and Linux accepts Tailscale DNS. The short and full names resolve through Linux system DNS and load the dashboard; the short address is also verified in Chrome. The user confirmed the shared addresses work on the phone and Linux, then confirmed the Mac works after enabling its Tailscale DNS setting with `tailscale set --accept-dns=true`. The operating-system hostname, Tailscale IP, and Mac SSH alias remain unchanged. The proxy explicitly accepts both new names, retaining the original accepted hosts and IP fallback. Generated portal/CLI links use `address: router` in `/etc/personal-codex-router/previews.json`.

Devices must use Tailscale DNS to resolve the shared name. If a client cannot resolve the short name, try `http://router.tailnet-example.ts.net:8409/` and check its Tailscale DNS setting. The original `http://100.64.0.10:8409/` dashboard and `http://100.64.0.10:8400/` portal remain valid fallbacks. No global DNS provider, public endpoint, or wider device access was added. To undo the naming changes, remove config `address` and only the new `aliases` entries, run `sudo tailscale set --hostname=user-desktop --accept-dns=false`, and disable MagicDNS in the admin console only if no later setup relies on it. Config changes reload automatically.

## Use a project preview

Ask Codex in the project's existing task to start/reuse its development server on Linux loopback and register the **HTTP port**. It must manage only its own process. No other project's source needs to be edited just to forward its website.

For a development website already listening on `127.0.0.1:3000`:

```sh
sudo router-preview add my-project 3000
sudo router-preview list
```

The command prints a private URL and adds the project to the phone bookmark. A registration lasts **four hours** by default. Set `--hours 1` for a shorter session, up to 24 hours. Repeating the same registration extends its lifetime and retains its URL. Expired slots remain reserved until explicitly removed, reducing accidental reuse of old bookmarks.

```sh
sudo router-preview add my-project 3000 --hours 1
sudo router-preview remove my-project
```

Removal blocks new requests immediately and closes active proxy connections within one second. Expiry closes active connections too. These commands register/remove access only; they never start, restart, or stop the project's development process. Stop a temporary server in the task that started it when finished.

If an existing local preview tool requires a named Host header, retain its setup and specify the name:

```sh
sudo router-preview add my-project 3285 --host my-project.localhost
```

This is an example, not a registration of the existing listeners. The service always connects to `127.0.0.1`; it never uses the Host header as a network destination. Common management/database ports are refused. Never register an admin console, database, proxy, or credential endpoint as a project preview.

## Behavior and limits

- Root paths, query strings, relative resources, normal HTTP methods, and WebSocket upgrades are forwarded. The proxy rewrites the upstream Host and same-origin Origin for the selected local website. Removing a preview closes existing WebSocket connections.
- Use relative frontend API/WebSocket URLs when possible. A site hard-coded to fetch `localhost`, another port, or a production OAuth callback may need project-specific development configuration in its own task. Separate backend ports are not automatically exposed.
- Traffic is encrypted between devices by Tailscale. The browser URL is HTTP, so browser features requiring an HTTPS secure context (service workers, some camera/location APIs, secure cookies) may not work. This workflow is for development reviews, not production hosting.
- The portal on port `8400` is read-only and has no browser registration or system-management API. Preview configuration is root-owned in `/etc/personal-codex-router/previews.json`; updates use the Linux CLI. Ports `8401–8408` are project slots; `8409` is reserved for the dashboard with a fixed loopback target and an explicit route/method allowlist. Dashboard POSTs permit refresh, Codex/Claude routing and linking, account sign-in/switches, fast-mode policy, and Codex/Claude reset controls; the exact paths are in `scripts/router-preview.mjs`. Raw gateway management routes and dashboard WebSocket upgrades are blocked. Port `8409` cannot be assigned to a project. Unexpected hosts, cross-site API requests/origins, or other source devices are rejected. User-initiated top-level GET navigation from another app is allowed; cross-site iframes and POSTs are blocked.
- If a device is reinstalled and receives a different Tailscale address, inspect its identity before updating **all three** controls: preview configuration, the service's `IPAddressAllow` list, and `/etc/tailscale/host-guard.nft`. Do not allow the whole tailnet.

## Verification and rollback

Automated tests cover HTTP paths/origins, spoofed forwarding headers, rejected hosts/devices, expiry, and WebSocket round-trips/revocation. A temporary loopback sample on port `8421` was used for the phone check; it has a 30-minute process lifetime and preview registration. The sample never lists project files or invokes account/reset APIs.

To stop private previews without affecting SSH, Codex, the gateway, or unrelated dev servers:

```sh
sudo systemctl disable --now personal-codex-router-preview.service
```

To remove the firewall addition, remove only the rule with comment `Personal project previews IPv4`, validate using `sudo nft --check --file /etc/tailscale/host-guard.nft`, then reload the dedicated guard. Preserve every other existing rule. The pre-deployment guard is in snapshot `20260924T150109Z` for comparison; do not blindly overwrite later firewall changes.
