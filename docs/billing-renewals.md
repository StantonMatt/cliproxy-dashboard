# Billing renewal dates

The Overview account cards display a small billing date beside the allowance reset at the bottom of each card, using the same typography. The calendar-day countdown, suggested review date two days earlier, and source details appear on hover. The date becomes amber in the final seven days. The review date is a planning buffer, not a guarantee about charge timing. OpenAI advises canceling at least 24 hours before the next billing date: https://help.openai.com/en/articles/7232927.

The existing authenticated account metadata reports `chatgpt_subscription_active_until`. This is an entitlement end, not proof of a future charge or auto-renewal. Without confirmation the card therefore says **Expected renewal** and **Unconfirmed**.

Confirmations live privately outside the project in `/var/lib/personal-codex-router/billing.json`, readable by the dashboard service. Shape:

```json
{"accounts":{"account@example.com":{"renewal_date":"2026-10-14","verified_at":"2026-09-23T12:00:00Z","source":"chatgpt_billing"}}}
```

Accepted sources are `chatgpt_billing` (billing page checked) and `user_confirmed`. The browser receives only date, source, confirmation state, and verification time. It receives no identity token, access token, refresh token, or management key. No new external API calls or writable browser endpoints are added.

Dates update when account metadata changes. A different entitlement period is displayed as expected until its billing date is checked. Dates are never advanced by assuming monthly recurrence. A past date is visibly marked **Check date**. Changes to cancellation or auto-renewal are not continuously verified; confirm those in ChatGPT billing. The link opens the browser's currently signed-in account, so check its email.

On September 23, 2026, ChatGPT Settings → Billing explicitly showed **auto-renews on Oct 14, 2026** for **account-a@example.com**, matched against the Account tab. The other account's metadata reported October 16; its billing confirmation is tracked separately in the private data file.

Validation: `python3 -m unittest discover -s tests -v`; `TZ=America/Fortaleza node tests/test_billing_dates.cjs`; `TZ=America/New_York node tests/test_billing_dates.cjs`; `node --check dashboard/static/app.js`.

Only the dashboard needs a restart for backend changes. The model gateway, routing preference controller, authentication, and Remote connections do not require changes.

## Deployment — September 23, 2026

Installed in the Linux source project and dashboard runtime. The friendly Mac URL serves the matching updated assets and reports October 14 as confirmed and October 16 as expected. All 12 Python tests passed on both hosts; the JavaScript calendar checks passed in Fortaleza and New York time zones. The gateway PID stayed 2258689, and both routing preference and usage timers remained active. Billing confirmations are owned by `cliproxyapi` with mode `0600`.

Previous runtime and source files are backed up under `/home/user/.local/state/personal-codex-router/backups/billing-renewals-20260923T121420Z`. The previous deployment had no `billing.js` or `billing.json`; preserve any later confirmation changes if rolling back. Restore the backed-up dashboard files and restart only the dashboard service for a rollback.

Final visual verification was unavailable because native Chrome stopped returning accessibility content/screenshots and browser automation reported an API-key authentication limitation. HTTP responses, asset hashes, and live billing data were verified instead. No commit or push was made.
