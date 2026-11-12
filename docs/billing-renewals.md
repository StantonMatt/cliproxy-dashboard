# Billing renewal dates

The Overview account cards display a small billing date beside the allowance reset at the bottom of each card, using the same typography. The calendar-day countdown, suggested review date two days earlier, and source details appear on hover. The date becomes amber in the final seven days. The review date is a planning buffer, not a guarantee about charge timing. OpenAI advises canceling at least 24 hours before the next billing date: https://help.openai.com/en/articles/7232927.

Codex billing dates are detected from ChatGPT's `GET /backend-api/accounts/check/v4-2023-04-27`, through the gateway's management `/api-call` using the account's existing OAuth credential and the same headers as usage reads. The dashboard never calls ChatGPT directly. It selects the known ChatGPT account id when present in the response, otherwise `accounts.default`, and ignores unrelated response fields. An active subscription with a valid `renews_at` and no `cancels_at` displays **Renews** with source **ChatGPT billing**. Otherwise a valid `cancels_at`, then `expires_at`, supplies an **Expected renewal**, **Unconfirmed** estimate. Timestamps become calendar dates in the dashboard host's local time zone.

Date precedence is: a fresh or last-known live ChatGPT date; a date set by hand on the Codex card; then the legacy email-keyed confirmation and `chatgpt_subscription_active_until` estimate. The token claim is an entitlement end, not proof of a future charge. Legacy confirmations retain their existing period-matching rules. A manual Codex date is confirmed independently of that estimate. A successful ChatGPT lookup with no date removes the previous live date and allows these fallbacks.

Billing lookups run in the background independently of usage/quota refreshes. Successful results refresh about every six hours, or promptly after a plan change or once the known date has passed, with at least 30 minutes between attempts. Failures retry no sooner than 30 minutes; HTTP 429 waits at least one hour and honors a longer `Retry-After`. Errors keep the last successful date and never mark an account as needing sign-in. Dates never advance by assuming monthly recurrence. A past date is visibly marked **Check date**.

Last successful live results persist privately in `/var/lib/personal-codex-router/codex-billing.json`, written atomically with mode `0600`. Records use `codex:<auth_index>` and bind the filename and email, so another account reusing an index cannot inherit the date. Only the date, source, lookup time, identity binding and observed plan are stored, including successful no-date observations for scheduling across restarts. No raw billing response, subscription ids or credentials are saved.

When no live ChatGPT date is shown, the Codex card menu offers **Set renewal date…** with the existing save/clear dialog. Codex plan names remain automatic. The existing origin-checked, write-mode-only `POST /api/accounts/billing` now accepts Codex renewal dates, but rejects Codex plan changes. Manual dates use the same identity-bound `codex:<auth_index>` records as other providers in `/var/lib/personal-codex-router/billing.json`. Legacy email-keyed records remain readable. Historical shape:

```json
{"accounts":{"account@example.com":{"renewal_date":"2026-10-14","verified_at":"2026-09-23T12:00:00Z","source":"chatgpt_billing"}}}
```

Sources are `chatgpt_billing`, `account_metadata` (estimate), and `user_confirmed`. The browser receives only date, source, confirmation state, verification time, and editability. Billing adds no browser identifiers, identity token, access token, refresh token, or management key.

Changes to cancellation or auto-renewal are checked at the billing refresh cadence; confirm them in ChatGPT billing when needed. The link opens the browser's currently signed-in account, so check its email.

On September 23, 2026, ChatGPT Settings → Billing explicitly showed **auto-renews on Oct 14, 2026** for **account-a@example.com**, matched against the Account tab. The other account's metadata reported October 16; its billing confirmation is tracked separately in the private data file.

Validation: `python3 -m unittest discover -s tests -v`; `TZ=America/Fortaleza node tests/test_billing_dates.cjs`; `TZ=America/New_York node tests/test_billing_dates.cjs`; `node --check dashboard/static/app.js`.

Only the dashboard needs a restart for backend changes. The model gateway, routing preference controller, authentication, and Remote connections do not require changes.

## Deployment — September 23, 2026

Installed in the Linux source project and dashboard runtime. The friendly Mac URL serves the matching updated assets and reports October 14 as confirmed and October 16 as expected. All 12 Python tests passed on both hosts; the JavaScript calendar checks passed in Fortaleza and New York time zones. The gateway PID stayed 2258689, and both routing preference and usage timers remained active. Billing confirmations are owned by `cliproxyapi` with mode `0600`.

Previous runtime and source files are backed up under `/home/user/.local/state/personal-codex-router/backups/billing-renewals-20260923T121420Z`. The previous deployment had no `billing.js` or `billing.json`; preserve any later confirmation changes if rolling back. Restore the backed-up dashboard files and restart only the dashboard service for a rollback.

Final visual verification was unavailable because native Chrome stopped returning accessibility content/screenshots and browser automation reported an API-key authentication limitation. HTTP responses, asset hashes, and live billing data were verified instead. No commit or push was made.
