# Gateway resilience work — 2026-09-25

**Current status (2026-11-20):** 8.0.79 is live ([upgrade record](gateway-upgrade-8.0.79-2026-11-19.md)); the 7.3.x versions and account availability below are September 25 observations. Current retry policy is `request-retry: 5`, `max-retry-interval: 45` seconds and `max-retry-credentials: 2`.

The dashboard now reports safe account availability states without exposing provider error text:

- `ready`: eligible for routing.
- `quota_exhausted`: the provider reported an explicit usage limit or a reported window reached zero; the reset time is shown when available.
- `rate_limited`: a temporary model or credential rate-limit cooldown.
- `provider_unavailable`: a temporary 502/503 or other provider cooldown.
- `auth_required`, `disabled`, and `unknown` remain separate states.

The overview also reports whether the pool is fully available, reduced (one account ready), or unavailable. The exhausted account is currently excluded by the gateway and the dashboard reports one ready account. No reset credit was consumed.

Validation:

- Linux router tests: 41 Python tests passed.
- Billing-date tests passed in both configured time zones.
- JavaScript syntax check passed.
- CLIProxyAPI retry tests passed for the installed 7.3.11 source and the current 7.3.17 source.
- The v7.3.17 release asset checksum was verified and its binary was run only for version inspection; the production gateway remains on 7.3.11.
- The 7.3.11 and 7.3.17 retry implementation/tests are unchanged in the relevant executor files, so upgrading solely for this incident is not justified yet.

The production gateway was not restarted or reconfigured. The dashboard restart left its gateway PID unchanged. No live completion was sent for the resilience tests, and no reset was used.
