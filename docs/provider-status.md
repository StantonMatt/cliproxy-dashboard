# Official provider status

`collect-provider-status.py` fetches public feeds separately from the dashboard.
It atomically replaces
`/run/personal-codex-router-provider-status/status.json` (0644). The dashboard
only reads that snapshot; its loopback-only networking is unchanged.

## Sources researched on 2026-10-31

| Dashboard IDs | Status page | Machine-readable source | Observed result |
| --- | --- | --- | --- |
| `codex`, `codex-api-key` | https://status.openai.com/ | https://status.openai.com/proxy/status.openai.com (incident.io JSON) | Operational; no affected API/Codex components |
| `claude`, `claude-api-key` | https://status.claude.com/ | https://status.claude.com/api/v2/summary.json (Statuspage) | `none` / operational |
| `xai`, `xai-api-key` | https://status.x.ai/ | https://status.x.ai/api/status (xAI JSON) | HTTP 200, `application/json`; API product and all three API components `available` |
| `gemini-api-key` | https://status.cloud.google.com/ | https://status.cloud.google.com/incidents.json (Google incident array) | No active Gemini/Vertex AI incidents; operational within this feed's scope |
| `antigravity` | https://status.cloud.google.com/ | None establishing Antigravity health | Unknown; informational Google link only |
| `kimi`, `kimi-ai` | https://status.moonshot.cn/ | https://status.moonshot.cn/api/v2/summary.json (Statuspage) | `none` / operational |
| `devin` | https://www.devinstatus.com/ | https://www.devinstatus.com/api/v2/summary.json (Statuspage) | `none` / operational |

`status.devin.ai` redirects to the listed canonical host. `status.anthropic.com`
redirects to `status.claude.com`. The collector uses canonical HTTPS URLs and
rejects redirects. Moonshot's `.ai` and `status.kimi.com` hosts did not resolve.
`aistudio.google.com/status` serves an app shell, not a directly usable JSON
feed. `antigravity.google/status` returned 404. Meta's https://metastatus.com/
covers business products, not this coding provider; `status.meta.com` and
`status.llama.com` did not resolve. `meta` and `openai-compatibility` therefore
have no mapped entry. No login, credentials or private API was used.

## Normalisation and scope

OpenAI's `/api/v2/status.json` and `/api/v2/summary.json` work, but the latter
currently omits the separate Codex group and supplies no active incident list.
The incident.io proxy supplies `summary.structure.items[].group`,
`affected_components` (`component_id`, `status`), and `ongoing_incidents`
(`name`, `status`, `affected_components`, `component_impacts`). We select the
APIs and Codex groups, use the worst current selected component status, and
choose the most severe relevant active incident title. Consumer ChatGPT-only
incidents are excluded. An ongoing incident with impact history contributes
only its current selected-component impacts; expired impacts cannot revive an
outage through historical affected components. The affected-components fallback
is used only when the incident has no impact history. Missing API/Codex groups
fail to unknown.

Anthropic selects components named API or Claude Code. Consumer chat/Cowork
incidents are excluded when their affected components are known. If scoped
components are absent, its overall page indicator is used. Moonshot and Devin
use their overall page indicator. Statuspage `none` becomes `operational`;
`degraded_performance` becomes `minor`, `partial_outage` becomes `major`,
`major_outage`/incident.io `full_outage` become `critical`, and
`under_maintenance` becomes `maintenance`. Active scheduled maintenance can
provide the incident title; future or completed maintenance does not.

xAI's `/api/status` was verified on 2026-10-31 with the collector's
`PersonalCodexRouter-Status/1.0` User-Agent, superseding earlier 403/404 probes.
Both Grok CLI OAuth (`xai`) and API-key (`xai-api-key`) accounts use the worst
status of the `api` product and all its components (currently global, US and EU).
The page-wide `overall`, Cursor, Services and consumer `grok` product are excluded.
The latter covers grok.com, mobile apps, Grok in X, Imagine and Voice rather than
the API used by these accounts; a consumer-only outage must not mark the API down.
A missing/duplicate API product or missing/empty/malformed API component list
fails to unknown.

The public page bundles define `available` (Operational), `info` (Info),
`disruption` (Degraded), `outage` (Outage), and the display-only `empty` (No data).
The following mapping is scoped to xAI; unfamiliar strings and non-string values
never imply operational status:

| xAI value | Dashboard indicator |
| --- | --- |
| `available` | `operational` |
| `info` | `minor` |
| `disruption` | `major` |
| `outage` | `critical` |
| `maintenance` | `maintenance` (literal meaning; not observed in the live response or bundles) |
| `empty`, `no_data`, any other value | `unknown` |

`updated_at` must be timezone-aware, not future-dated and at most 20 minutes old
at collection, so repeatedly fetching a stale upstream payload cannot keep it
green. Snapshot `checked_at` remains the actual check time, including failures;
the reader independently expires that timestamp under the existing rules.
Known states have short API-scoped descriptions. `/api/status` contains no
incident titles. The same-origin `/api/incidents` returned `{"incidents":[]}`;
`/history` and its public bundles expose incident UI, but a nonempty incident
schema could not be verified. The page also references
`https://data.x.ai/status/incidents.json`; that additional host was neither
requested nor allowlisted. No separate incident feed is collected and incident
titles remain null until its populated schema and API scope can be verified.

Google only considers incidents without an `end`, whose `affected_products`
names mention Gemini or Vertex AI. `SERVICE_INFORMATION`, `SERVICE_DISRUPTION`
and `SERVICE_OUTAGE` become minor, major and critical. Its healthy description
is **No active Gemini/Vertex AI incidents reported**. This Cloud feed does not
prove consumer Gemini API/AI Studio or Antigravity availability. Antigravity
always stays unknown. Whether a different Google link/coverage is preferred
remains a product decision; no Cloud incident is projected as Antigravity health.

## Collection and sandbox

`personal-codex-router-provider-status.timer` runs the oneshot 30 seconds after
boot and every five minutes, with up to 30 seconds of jitter and 5 seconds of timer
accuracy. Each of six feeds is requested once per run, concurrently, with an
8-second socket timeout, a 2 MiB response limit, and a 45-second service limit.
Feed failures publish unknown independently; raw responses/errors are not
exposed. A failed/hung collector leaves the prior snapshot to expire.

The service uses the dedicated static system user `router-provider-status`,
created by the full installer with no home and a nologin shell, separating it
from the gateway and dashboard's `cliproxyapi` user, including access through gateway `/proc`
entries. systemd creates `RuntimeDirectory=personal-codex-router-provider-status`
(0755); `RuntimeDirectoryPreserve=yes` keeps the public snapshot between runs.
The file is atomically replaced with mode 0644 despite the service's 0077 umask.
The dashboard can read this `/run` path with its existing sandbox: strict
filesystem protection prevents writes but does not hide it. No dashboard unit
change is needed. A DynamicUser with runtime-directory preservation on systemd
259 instead places the directory under `/run/private` (0700), which prevents
dashboard reads; the static identity avoids that private-directory indirection.
The runtime snapshot disappears on reboot; the reader returns unknown until the
timer collects again.
`ProtectSystem=strict` leaves only this output directory writable, apart from
private temporary storage. Home, gateway credentials/config, disconnect
backups, routing state and usage watch directories are hidden. It has no
capabilities, new privileges, devices, namespaces or listening sockets, and
uses system-call filtering and kernel protections.

The collector requires Linux Landlock ABI 4+ (Linux 6.7+ with Landlock enabled).
It fails closed if unavailable. Landlock denies TCP binds and permits TCP
connections only to destination port 443; UDP/Unix DNS resolution remains
available. The HTTP client additionally accepts only the six fixed HTTPS feed
URLs, verifies certificates and disables environment proxies and redirects.
The unit permits IPv4/IPv6/Unix sockets for this purpose; it does not inherit
the dashboard's `IPAddressDeny=any`. No firewall or auth-directory changes are
needed. The xAI addition allowlists only `https://status.x.ai/api/status` on the
already linked status host. The service unit has no destination-host allowlist;
no systemd or Landlock change is required. A status-feed redirect or schema
change becomes unknown until reviewed.

## Overview contract

`/api/overview` adds a top-level `provider_status` map, keyed by the dashboard
provider ID. Each mapped ID always has:

```json
{
  "source": "OpenAI",
  "indicator": "operational",
  "description": "All systems operational",
  "url": "https://status.openai.com/",
  "checked_at": "2026-10-31T12:00:00Z",
  "incident": null
}
```

The indicator enum is `operational|minor|major|critical|maintenance|unknown`.
`incident` is an optional active incident title or null. `checked_at` is the
time the source was checked, including a failed attempt; it is null for sources
with no collector. Missing, corrupt, oversized, invalidly dated, future-dated
or more than 20-minute-old entries become `unknown` / `Status unavailable`,
retaining the canonical URL and valid last check timestamp. Stale incident
titles are removed. The reader validates each entry independently and ignores
snapshot-supplied source names and URLs. No dashboard request performs network
I/O for provider status.

## Deployment

A full `scripts/install-operations.py --apply` installs the collector, shared
backend reader, service and timer, reloads systemd, and **automatically enables
and starts the new timer**. The existing installer also restarts the dashboard
and configured preview service. Its restricted deployment modes ship the
reader dependency with `server.py`, but do not install/enable this collector;
use the full deployment for this feature. Until the timer produces a snapshot,
overview returns unknown entries. Verify the timer's first run and Landlock
support on the deployment host. This implementation task did not deploy,
restart services or run the installer with `--apply`.
