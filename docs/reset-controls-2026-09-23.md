# Earned reset controls

**Current status (2026-10-27):** This records the September 23–24 revisions. Expiry redemption uses the final five minutes, without a remaining-allowance threshold; see [current behavior](../README.md#earned-reset-credits). Credit balances, expiry dates and enabled toggles below are dated observations, not current balances.

Implemented manual redemption and optional expiry redemption in the existing private dashboard. User authorized both features, including automatic use about a minute before expiry.

Live read-only provider checks reported two available reset credits for each existing account. The earliest expiries are 2026-10-05T04:18:37Z (account-a@example.com) and 2026-10-05T04:20:33Z (account-b@example.com). Remaining credits expire October 22. These are credit-expiry timestamps, distinct from subscription renewal dates and usage-window resets.

The backend forwards only three fixed provider paths through the existing authenticated gateway: usage, reset-credit details, and reset-credit consumption. No credentials are sent to the browser or moved between desktop logins. The new routes retain host, origin, and custom-header checks, validate account identity against connected gateway accounts, and require explicit confirmation for a manual credit. Provider errors are sanitized.

Automatic redemption is enabled for both existing accounts via their individual dashboard toggles, verified by the checked controls and success status. New accounts default off. A ten-second local deadline check triggers fresh provider reads only when a known credit is in its final minute. Redemption requires supported available credit, future expiry, provider applicability, and at least one fresh core window with 10% or less remaining. Unknown expiry and ineligible usage are skipped. Timing depends on Linux and network availability.

Settings and attempts persist in `/var/lib/personal-codex-router/reset-controls.json` with mode 0600. Identity is a deterministic UUID per account email/credit ID. A lock serializes manual and automatic redemption and policy changes. Identity is saved and fsynced before the provider write; retries reuse it. A failed write or ambiguous provider result never selects a second credit. Outcomes appear in the expanded account panel. Gateway quotas are refreshed following manual redemption; normal polling refreshes them after automatic redemption.

Tests: 19 focused Python cases passed on Mac and Linux; JS syntax passed. Coverage includes identity preservation across restart, manual/auto race, provider eligibility, expiry boundaries, unknown expiry, stale reads, corrupt state, failed persistence, and same-origin/manual-confirmation enforcement. Live reset balance reads, setting updates, and rendered UI passed. Actual redemption remains untested deliberately: no credits were spent in validation.

Only the dashboard service was restarted; model gateway and shared Codex daemon were not restarted. Production pre-change dashboard backups: `/home/user/.codex/gateway-integration/reset-dashboard-20260923T230417Z/installed/`. Earlier source backups are under the first `reset-dashboard-*` directory created during this change. No commits or pushes.

Rollback: first turn off both automatic checkboxes, then restore the backed-up dashboard files and restart only `personal-codex-router-dashboard`. Preserve the reset-controls state file and redemption identities even after rollback; never remove its attempt history to retry an uncertain redemption.

References:
- https://learn.chatgpt.com/docs/app-server#8-earned-rate-limit-resets-chatgpt
- https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt
- https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/rate_limit_resets.rs


## Modal and earliest-first revision

Replaced disclosure/select controls with a compact Resets button and a native dialog, following the user's screenshot reference. The earliest credit exposes Use reset -> Confirm on the same control, plus Cancel. Later credits are listed without a redeem action. Escape, close, focus restoration, persistent automatic toggle, and inline outcome feedback are supported. The backend freshly sorts by expiry (unknown expiry last, ID tie-breaker) and returns earliest_required for an attempted later credit.

Corrected the manual eligibility behavior: the previous local 10% threshold disabled a manual action even though the provider's own UI allowed initiating it. Confirmed manual requests now go to the provider for the final eligibility decision; automatic redemption retains its conservative threshold and applicability checks. No real redemption was performed to validate this revision.

Validation: 21 Python tests passed locally and on Linux, including manual eligibility delegation and rejecting later credits after changed server ordering. JS syntax passed. Live browser verified modal opening, enabled Use reset at 87% remaining, inline Confirm, Cancel, closing, and the disabled later-credit action. Automatic settings remain enabled for both accounts. Backup: `/home/user/.codex/gateway-integration/reset-modal-20260924T002408Z`.


## September 24: expiry redemption regardless of allowance

Removed the local remaining-allowance threshold and applicability-count gate from automatic expiry redemption at the user's request. This supersedes the conservative automatic policy described above. Both manual and automatic requests now let the provider decide whether it can apply a reset. Expiry automation still requires an enabled account, a fresh supported available credit, earliest-expiry ordering, and a known deadline within the final minute. It never buys credits or switches to a later credit on rejection.

Removed the extra usage lookup from redemption checks; fresh credit details remain mandatory. Updated modal explanation and README. All 23 Python tests passed on Mac and Linux, including the user's 97%-remaining scenario and provider nothing_to_reset outcome. No credits spent. Both automatic settings remain enabled. Only the dashboard service restarted. Backup: `/home/user/.codex/gateway-integration/expiry-policy-20260924T140050Z`.


## September 24: five-minute window and cleaner modal

Automatic redemption now starts in the final five minutes, superseding the one-minute window above. The ten-second background loop retries failures or uncertain results while the credit remains unexpired and automation is enabled. Request duration can lengthen retry intervals. All retries retain the same durable redemption ID; successful attempts cannot spend the credit twice. Only the earliest cached credit is considered, and fresh provider ordering is checked before redemption.

Removed the explanatory paragraph below the switch and the last credit row's bottom border, leaving one divider above automatic settings. Added tests for the exact five-minute boundary, repeated failure with the same ID, stopping at expiry, opt-out, and success. No real reset is used in verification.

Verification: all 26 tests passed on Mac and Linux; live health returned 200. Both automatic settings remain enabled, with zero recorded redemption attempts. Browser verified five-minute wording, removal of the paragraph and redundant divider; empty status space is hidden until needed. Backup: `/home/user/.codex/gateway-integration/five-minute-20260924T140812Z`. Only the dashboard service restarted; gateway remained active.
