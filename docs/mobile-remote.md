# Phone access and router authentication

## Current setup — September 24

Phone Remote connects directly to the managed Linux Codex server. The user has confirmed it works with the Mac asleep and Linux GUI closed. Mac SSH and the separate **Codex — Shared Linux** desktop profile use that same managed server; keep the original standalone Linux GUI closed to avoid task ownership conflicts. The user startup unit is `codex-shared-server-start.service`, with user linger enabled. Read-only health uses `codex app-server daemon version`.

Project websites now have a separate private Tailscale workflow: [phone previews](private-previews.md). The phone's Tailscale connection is required for those websites; Codex Remote has its own connection. Neither requires Moonlight. Settings recovery is documented in [backups and recovery](backups-and-recovery.md).

The shared **[Personal Router dashboard](http://router:8409/)** also uses Tailscale. It supports refresh, notice dismissal, routing changes, automatic reset policy changes, and reset redemption on the phone under the existing device, Host, and Origin restrictions. Raw gateway management routes remain blocked by the private proxy.

## Historical September 22 setup

The Mac-mediated alternative and restart instructions below describe earlier setup stages, not the current everyday connection path.

Checked September 22, 2026 against the official Remote documentation and the Linux authentication repair described below.

The router API key is an inference credential. It is not a ChatGPT account that can be used to sign in to the ChatGPT mobile app or pair Codex Remote.

The documented mobile path is:

Phone signed into ChatGPT → Mac signed into the same ChatGPT account and workspace → existing SSH connection to user-Desktop → Linux model gateway and its two subscriptions.

The Mac retains its router API-key sign-in. The user switched the Linux app to a personal ChatGPT account to test direct phone access to the always-on Linux host. That exposed a gateway authentication conflict, now repaired and verified in a fresh process. After the existing-task repair below, the user confirmed both a successful desktop prompt and a successful direct phone Remote connection to Linux. This is user-confirmed behavior on the installed Linux build; the official documentation still describes Mac and Windows hosts.

## Current Linux authentication

Linux keeps `personal_codex_gateway`, `gpt-6-astra`, and `xhigh`, with its provider endpoint at `http://127.0.0.1:8317/v1`. The provider retains `requires_openai_auth = true` so the app server recognizes the personal ChatGPT account. Its `http_headers` now supplies `X-Api-Key` using the existing router key. The key is stored only in the private user configuration, not this project.

The original combination of ChatGPT profile authentication and the provider's explicit bearer credential returned HTTP 401 from the gateway. A temporary test with `requires_openai_auth = false` fixed inference but made `account/read` return no account. That flag change was reverted. Supplying the router key in `X-Api-Key` lets the gateway accept requests while retaining ChatGPT account recognition.

Validation using the Linux desktop app's bundled Codex executable:

- Fresh app-server `account/read`: `chatgpt`, plan `pro`, `requiresOpenaiAuth: true`.
- Fresh execution with saved defaults: `uname -s` returned `Linux`, followed by `LINUX_ROUTER_CHATGPT_OK`; exit 0, no HTTP 401.
- The personal authentication file was untouched by the repair. Gateway, dashboard, and Tailscale services remained active.
- No existing desktop process was restarted, and no phone pairing was attempted. Fully quit and reopen the Linux app to load the repaired provider settings, then retry the failed task.

Private backup and guarded rollback: `/home/user/.codex/gateway-integration/chatgpt-signin-repair-20260922T214113Z/rollback.py`. Rollback restores the original provider configuration only if no later edits have occurred; it does not change ChatGPT sign-in and would restore the prior authentication conflict. See `linux-chatgpt-auth-repair.json` for the evidence boundary.

### Existing-task provider repair

The user's subsequent retry failed in existing task `00000000-0000-4000-8000-000000000001`. Its saved provider was still `openai`. The built-in provider used the global gateway base URL but did not inherit the custom provider's key header. Changing the default provider does not migrate existing tasks.

After the user closed the Linux desktop app to release its writer lock, the supported app-server `thread/resume` API changed that task to `personal_codex_gateway`. A separate fresh process resumed the same task without a provider override and reported both its stored and runtime provider as `personal_codex_gateway`, retaining `gpt-6-astra`, `xhigh`, and ChatGPT account recognition. No user prompt was submitted by the repair. The user subsequently confirmed that the desktop retry worked and the phone connected successfully to the Linux desktop through Remote. Other historical tasks retaining `openai` may need the same supported provider migration.

The private `resumed-thread` subdirectory under the repair backup contains the original rollout, selected metadata, and verification results. The configuration rollback above does not undo this task-provider migration.

## Documented Mac-host alternative

For the documented Mac-host path, the Mac desktop app would need a real ChatGPT account sign-in matching the phone. Keep the model-provider routing to Personal Codex Router, apply the equivalent explicit router-key header if needed, and verify both account recognition and a fresh gateway task after changing sign-in. A separate bearer credential alone did not avoid the Linux conflict above. Existing Linux SSH authentication and its gateway routing can remain in place.

Account identity for device pairing and the accounts supplying model usage are separate. Signing the Mac into one personal account for Remote does not remove either account from the router.

## Pairing steps after that change

1. Sign in to ChatGPT on the phone and the Mac with the same personal account and workspace.
2. On the Mac, open Settings → Connections → Control this Mac → Set up or Add.
3. Scan its QR code with the phone and finish verification.
4. Open Remote on the phone and choose the Mac, then the Linux project available through its existing SSH connection.
5. Keep the Mac awake, online, and running the app; keep Tailscale and Linux online for SSH projects.

If an existing pairing remains, enabling Remote Control again may restore it. The documentation says signing out disables Remote Control but preserves device pairings.

The official documentation currently lists Mac and Windows desktop apps as mobile Remote hosts, and iOS and Android as clients. It does not establish direct Linux mobile-host support. The documented Linux-project path goes through the connected desktop app host. The always-on Linux machine alone does not keep this Mac-mediated phone connection available while the Mac sleeps.

Sources:
- https://learn.chatgpt.com/docs/remote
- https://learn.chatgpt.com/docs/remote-connections
- https://learn.chatgpt.com/docs/config-file/config-reference
