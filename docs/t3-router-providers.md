# T3 agents through the local gateway

T3 can run the official Codex, Claude Code and Antigravity agents against the
same CLIProxyAPI gateway. The agents retain their native installation and update
mechanisms. The gateway owns the upstream subscription sign-ins; each agent uses
a gateway client key to reach it.

This supplements [setup steps 5–9](../README.md#set-up-on-your-own-machine).
Keep existing provider instances and their homes when adding the gateway: those
homes contain login and session state. Configure providers on the environment
where the agents run. Loopback addresses below refer to that machine.

## Codex: preserve the instance and use an updateable CLI

An **Existing CLI** instance pointing inside a versioned T3-managed installation
can run successfully while leaving updates manual. Updating the T3 application
does not necessarily update that executable.

1. Inspect the existing instance's **Binary path**, **Home path** and launch
   arguments in **Settings → Providers**. Preserve its home, router configuration
   and existing environment variables.
2. Use an official Codex CLI installation. Follow the
   [official CLI installation guide](https://learn.chatgpt.com/docs/codex/cli).
   When using the standalone installer, retain the same Codex home so its package
   metadata and the instance's state use the intended location. If a valid native
   CLI is already installed, reuse it.
3. Point **Binary path** at its stable command, such as the absolute expansion of
   `~/.local/bin/codex`, rather than a file inside a particular release directory.
   Preserve the provider's existing ID and router settings from README step 7.
4. Check the CLI version and the update action T3 offers. A recognized standalone
   installation supports `codex update`; package-manager installations should
   use their owning package manager. Do not replace an installation with a
   wrapper just to make the version check pass.

This does not require another ChatGPT account or another T3 provider instance.
T3's managed **Connect with ChatGPT** setup is also supported by T3, but converting
an existing router instance into that setup is a separate authentication change.

## Claude Code

Keep its official CLI installation and the existing Claude config directory.
README step 9 lists the gateway environment variables, including the local
`ANTHROPIC_BASE_URL` and sensitive `ANTHROPIC_AUTH_TOKEN`. A native Claude Code
installation can be updated through T3 using `claude update`.

## Antigravity: managed agent, existing gateway account

First connect the Antigravity subscription account to CLIProxyAPI. Installing
the agent in T3 and signing the upstream account into the gateway are separate
operations.

### Give the agent explicit gateway model names

Read the models available to the gateway's Antigravity account. Choose a primary
model, a Flash model and a Flash Lite model supported by that account. The agent
may use the smaller models for auxiliary work.

Merge aliases into the existing gateway configuration under
`oauth.model-alias.antigravity`. Replace `PRO_ID`, `FLASH_ID` and `FLASH_LITE_ID`
below with actual model IDs from that account; they are placeholders, not model
names to send to the provider.

```yaml
oauth:
  model-alias:
    antigravity:
      - name: "PRO_ID"
        alias: "antigravity/PRO_ID"
        fork: true
      - name: "FLASH_ID"
        alias: "antigravity/FLASH_ID"
        fork: true
      - name: "FLASH_LITE_ID"
        alias: "antigravity/FLASH_LITE_ID"
        fork: true
```

Keep existing OAuth settings and aliases. `fork: true` keeps the original model
IDs available to other clients. The prefix avoids collisions with direct
provider models and prevents Antigravity's known-name normalization from
shortening the selected gateway IDs.

Persist changes using the gateway's supported management interface. If a private
file edit is necessary, preserve the config's inode, owner and permissions and
flush it to disk, as described in README step 6. Verify hot reload and check that
the aliases appear in an authenticated `GET /v1beta/models`. Do not restart an
active gateway just to add aliases.

### Configure the T3 instance

In **Settings → Providers**, enable Antigravity and install its official managed
runtime. Leave **Binary path** empty so T3 continues to own installation and
updates. Configure these environment variables, replacing the same model
placeholders as above:

| Variable | Value |
| --- | --- |
| `GOOGLE_GEMINI_BASE_URL` | `http://127.0.0.1:8317` |
| `AGY_LLM_GATEWAY_URL` | `http://127.0.0.1:8317` |
| `AGY_LLM_GATEWAY_WIRE_PROTOCOL` | `genai` |
| `AGY_LLM_GATEWAY_MODELS` | `antigravity/PRO_ID,antigravity/FLASH_ID,antigravity/FLASH_LITE_ID` |
| `AGY_LLM_GATEWAY_TIER_PRO` | `antigravity/PRO_ID` |
| `AGY_LLM_GATEWAY_TIER_FLASH` | `antigravity/FLASH_ID` |
| `AGY_LLM_GATEWAY_TIER_FLASH_LITE` | `antigravity/FLASH_LITE_ID` |
| `NO_PROXY` | `127.0.0.1,localhost,::1` |

Additional text models can be added to the explicit list after adding matching
aliases. This list does not automatically track future account model changes.
Exclude image-only models from the coding agent's list.

Choose **Gemini API key** as the sign-in method, enter the **gateway client key**
created in README step 6, and choose **Connect**. This is the local key accepted
by CLIProxyAPI, not its management key or an upstream Google credential. T3
stores this secret outside its settings file. The account already signed into
the gateway supplies the upstream access; this setup does not provision a paid
Google API key.

Use T3's supported settings UI for this instance. Do not hand-edit the generated
Antigravity ACP settings, which T3 can overwrite. The working setup uses the
standard `gemini-api-key` method; it does not require the CLI's experimental
gateway authentication method.

## Updates and connection labels

Apply compatible provider updates in **Settings → Providers**. Antigravity uses
T3's managed updater; Codex and Claude use the updater T3 recognizes for their
installation. Updating T3 itself and updating provider CLIs are different
operations. No extra cron job, timer or update watcher is required by this guide.
Updating a client does not update CLIProxyAPI or the dashboard.

T3's authentication subtitles describe different things. Codex can show the
plan of a saved ChatGPT login even when requests use a custom endpoint. Claude
reports **Custom API endpoint** for the router URL. Antigravity reports
**Gemini API key** for the selected local authentication method. None of these
labels identifies which pooled account handled a particular request. Use the
router's account cards and request history for that information.

## Verification and limits

After configuration, check that each instance is ready and that the intended
models are available. Send a short request through each agent and confirm its
provider/account in the gateway's request history. For Antigravity, also ask the
agent to read a temporary file containing a fresh test value and verify the
returned content. This exercises a tool turn as well as text generation.

The integration was checked with CLIProxyAPI 8.0.79, Codex 0.171.0, Claude Code
2.1.330, Antigravity 1.5.15 and T3 0.1.12-nightly.20261123.5151. Antigravity
completed both a real response and a file-read tool turn through the gateway.
These observations describe a configured Linux installation, not an end-to-end
clean-machine installer test or a guarantee for future releases. No account
identifiers, credentials, logs or host settings are included here.

The dashboard's optional T3 health probe checks the desktop user's default
`~/.t3/userdata/server-runtime.json`, the owner and name of its Linux `t3code`
process, and the loopback listener. It reports **T3 Code server** rather than
the standalone Codex daemon. An unrecognized process/runtime reports unknown;
without the T3 runtime file, the existing Codex daemon probe remains in use.
This is a local liveness check, not an inference or upstream status check.

Sources:

- [T3 provider installation and updates](https://github.com/pingdotgg/t3code/blob/main/docs/user/install.md#providers)
- [T3 Antigravity setup and managed runtime](https://github.com/pingdotgg/t3code/blob/main/docs/user/providers-antigravity.md)
- [Antigravity CLI settings](https://antigravity.google/docs/settings?tab=cli)
- [CLIProxyAPI 8.0.79 configuration example](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.79/config.example.yaml)
