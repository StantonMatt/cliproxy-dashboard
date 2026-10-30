# Windows setup

Paste this into Codex on the Windows machine: “Read `docs/windows-setup.md` in this repo and set up the gateway following it. Use gpt-6.1-sol for implementation and gpt-6-astra if advice is needed.”

The remaining instructions are for that agent. Set up stock **CLIProxyAPI 8.0.47** for three ChatGPT Pro accounts, all Codex, on this one Windows machine.

## Agent rules

- Never print, log, paste, or commit API keys, OAuth tokens, auth-file contents, or complete management responses. Read secrets into process memory only; report counts and pass/fail results. Do not enable shell tracing, transcripts, HTTP debugging, or request/body logging.
- Generate keys locally without echoing them. Keep configuration, credentials, logs, downloads, and backups outside the repo, in the private directory below.
- Bind only to `127.0.0.1`. Do not open firewall ports, enable network discovery, or configure Tailscale sharing.
- Preserve the user's existing Codex login and sessions. Do not run `codex login`, copy its auth files, restart its app/server, or change its default provider during gateway verification. Back up `%USERPROFILE%\.codex\config.toml` before editing it.
- Stop for every **HUMAN STEP:** and for an unexpected existing installation, conflicting setting, or failed check. Never bypass UAC, SmartScreen, Defender, or organizational policy.

This repo's routing settings and documentation carry over. Its dashboard, usage collectors, account controls, systemd units, installers, nftables rules, and Linux `.so` plugin do **not** constitute a Windows installation. Do not run those scripts. This setup needs neither Claude nor a Linux host. The computer must remain awake while clients use it; the scheduled task runs only while this Windows user is logged in.

Checked against the [v8.0.47 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.47), its Linux `--help`, [example configuration](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/config.example.yaml), and source. Windows execution, browser login, Task Scheduler, and client acceptance must be checked on the target machine; they were not run on Linux. Run PowerShell blocks in the same **non-administrator** session, in order.

## 1. Check the machine and download the release

Determine whether the user uses the native Windows CLI, the Windows Codex app, or both. Do not infer this from the word “Codex.” WSL has a separate Linux home/configuration; stop and clarify if it is the actual client. These commands target native Windows. If a CLI is missing, ask the human before installing it from the [official CLI instructions](https://developers.openai.com/codex/cli/); the app alone does not guarantee `codex` is on `PATH`.

```powershell
$ErrorActionPreference = 'Stop'
$Root = Join-Path $env:LOCALAPPDATA 'CLIProxyAPI'
$Version = '8.0.47'
$BinDir = Join-Path $Root "versions\$Version"
$DownloadDir = Join-Path $Root "downloads\$Version"
$AuthDir = Join-Path $Root 'auth'
$LogDir = Join-Path $Root 'logs'
$Config = Join-Path $Root 'config.yaml'
$CodexDir = Join-Path $env:USERPROFILE '.codex'
$TaskName = 'CLIProxyAPI-local'
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Get-Command codex -ErrorAction SilentlyContinue | Select-Object Name, Source
Get-AppxPackage | Where-Object { $_.Name -match 'Codex|ChatGPT' } | Select-Object Name
if ($env:CODEX_HOME -and $env:CODEX_HOME -ne $CodexDir) { throw 'Custom CODEX_HOME: clarify client configuration first.' }
if (Test-Path $Root) { throw 'Existing gateway directory: inspect its public metadata and ask before reusing it.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Scheduled task already exists.' }
if (Get-NetTCPConnection -State Listen -LocalPort 8317 -ErrorAction SilentlyContinue) { throw 'Port 8317 is occupied; identify its owner, do not stop it.' }
if ([Environment]::GetEnvironmentVariable('CODEX_GATEWAY_API_KEY', 'User') -or $env:CODEX_GATEWAY_API_KEY) { throw 'Gateway key variable already exists; ask before changing it.' }
if ($env:MANAGEMENT_PASSWORD -or $env:WRITABLE_PATH -or $env:MANAGEMENT_STATIC_PATH) { throw 'Existing CLIProxyAPI environment overrides: clarify first; do not display values.' }
$Arch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$AssetArch = switch ($Arch) { 'X64' { 'amd64' } 'Arm64' { 'aarch64' } default { throw 'Only Windows x64 and ARM64 releases are covered.' } }
$null = New-Item -ItemType Directory -Path $Root
& icacls.exe $Root /inheritance:r /grant:r "${Identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict gateway directory permissions.' }
$null = New-Item -ItemType Directory -Path $BinDir, $DownloadDir, $AuthDir, $LogDir
$Asset = "CLIProxyAPI_${Version}_windows_${AssetArch}.zip"
$Release = "https://github.com/router-for-me/CLIProxyAPI/releases/download/v$Version"
$Zip = Join-Path $DownloadDir $Asset
Invoke-WebRequest -UseBasicParsing "$Release/$Asset" -OutFile $Zip
Invoke-WebRequest -UseBasicParsing "$Release/checksums.txt" -OutFile (Join-Path $DownloadDir 'checksums.txt')
$Lines = @(Get-Content (Join-Path $DownloadDir 'checksums.txt') | Where-Object { $_ -match ("\s+\*?" + [regex]::Escape($Asset) + '$') })
if ($Lines.Count -ne 1) { throw 'Missing or ambiguous checksum.' }
$Expected = ($Lines[0] -split '\s+')[0]
if ((Get-FileHash $Zip -Algorithm SHA256).Hash -ne $Expected) { throw 'Checksum mismatch; do not extract or run.' }
Expand-Archive -LiteralPath $Zip -DestinationPath $BinDir
$Exe = Join-Path $BinDir 'cli-proxy-api.exe'
$SavedErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue' # Go help uses stderr; Windows PowerShell 5.1 wraps it as error records.
    $HelpText = (& $Exe --help 2>&1 | Out-String)
} finally { $ErrorActionPreference = $SavedErrorPreference }
if ($HelpText -notmatch 'Version: 8\.0\.47\b' -or $HelpText -notmatch '-codex-device-login' -or $HelpText -notmatch '-codex-login') { throw 'Unexpected binary version or flags.' }
'SHA256 verified; CLIProxyAPI 8.0.47 help verified.'
```

Expected: a matching SHA-256 and version 8.0.47. Upstream publishes `checksums.txt` alongside both Windows archives. Checksum matching checks the download against that release, not a publisher signature.

**HUMAN STEP:** If Windows asks for administrator approval or SmartScreen/Defender intervention, stop and show the human the executable path and official release link. Have them review the detection/download. Do not disable protection, add exclusions, or automatically unblock the executable. No administrator access is intended here.

## 2. Write the private configuration

Use separate random client and management keys. Management is enabled **only on loopback**, with a key required even locally; the web panel is disabled unless the human chooses step 8. This enables account-count and routing checks without opening raw auth files. Upstream also supports disabling management completely with an empty `management.secret-key`, but then the inventory checks and optional T3 usage hub are unavailable.

```powershell
function New-LocalKey {
    $Bytes = New-Object byte[] 32
    $Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Rng.GetBytes($Bytes) } finally { $Rng.Dispose() }
    return [BitConverter]::ToString($Bytes).Replace('-', '').ToLowerInvariant()
}
$ClientKey = New-LocalKey
$ManagementKey = New-LocalKey
[IO.File]::WriteAllText((Join-Path $Root 'client-key.txt'), $ClientKey)
[IO.File]::WriteAllText((Join-Path $Root 'management-key.txt'), $ManagementKey)
$AuthYaml = $AuthDir.Replace('\', '/')
$Yaml = @"
config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
management:
  allow-remote: false
  secret-key: "$ManagementKey"
  disable-control-panel: true
  disable-auto-update-panel: true
access:
  api-keys: ["$ClientKey"]
oauth:
  auth-dir: "$AuthYaml"
routing:
  strategy: "round-robin"
  session-affinity: true
  session-affinity-ttl: "1h"
  session-affinity-subagents: true
  retry:
    request-retry: 5
    max-retry-interval: 45
    max-retry-credentials: 2
requests:
  payload:
    filter:
      - models:
          - name: "*"
            protocol: "codex"
        params: ["service_tier"]
upstream:
  codex:
    response-steering: false
observability:
  logs:
    debug: false
    logging-to-file: true
    logs-max-total-size-mb: 50
    request-log: false
"@
[IO.File]::WriteAllText($Config, $Yaml)
$Yaml = $null
$ManagementKey = $null
$ClientKey = $null
if (!(Test-Path $Config) -or !(Test-Path (Join-Path $Root 'client-key.txt'))) { throw 'Configuration creation failed.' }
'Private configuration and two keys created; no secret output.'
```

Expected: files exist under the restricted directory; step 3 proves the YAML loads. Do not print the YAML. These are native v8 keys, matching the repo's [routing settings](claude-gateway-2026-10-15.md) and [retry policy](standard-management-setup.md). New sessions rotate across equal-priority accounts; a session and its subagents stay together for cache reuse, with failover when the account becomes unavailable. This does not pool subscriptions into one unlimited allowance or choose the soonest quota reset.

- `request-retry: 5`: allow five additional retry rounds for retryable errors, including Codex 429s.
- `max-retry-interval: 45`: cap cooldown waits between rounds at 45 seconds.
- `max-retry-credentials: 2`: try at most two different credentials **per round**, limiting fan-out; this is not a lifetime limit of two accounts.

Fast mode is stripped for **all three accounts** by the payload filter, including a client requesting priority. Remove that rule only after the human chooses to allow fast mode. There is no per-account toggle in this Windows setup. See step 9 for the plugin and WebSocket distinction.

## 3. Start it at logon and check the listener

Use Task Scheduler under the current user, without elevation. Set no execution time limit, retain operation on battery, and reject duplicate instances. The executable stays in its versioned directory; state stays outside it.

```powershell
$Action = New-ScheduledTaskAction -Execute $Exe -Argument ('-config "{0}"' -f $Config) -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$null = Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings
Start-ScheduledTask -TaskName $TaskName
$Listener = @()
for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    $Listener = @(Get-NetTCPConnection -State Listen -LocalPort 8317 -ErrorAction SilentlyContinue)
    if ($Listener.Count) { break }
    Start-Sleep -Seconds 1
}
if ($Listener.Count -ne 1 -or $Listener[0].LocalAddress -ne '127.0.0.1') { throw 'Expected only 127.0.0.1:8317.' }
$GatewayProcess = Get-Process -Id $Listener[0].OwningProcess
if ($GatewayProcess.Path -ne $Exe) { throw 'Listener is not the installed gateway.' }
"Gateway PID $($GatewayProcess.Id), listening only on 127.0.0.1:8317."

function Invoke-LocalGatewayJson {
    param([string]$Path, [string]$KeyFile = 'client-key.txt', [string]$Method = 'GET', [string]$Body)
    $Headers = @{ Authorization = 'Bearer ' + [IO.File]::ReadAllText((Join-Path $Root $KeyFile)).Trim() }
    $RequestArgs = @{ Uri = "http://127.0.0.1:8317$Path"; Headers = $Headers; Method = $Method; TimeoutSec = 300 }
    if ($Body) { $RequestArgs.Body = $Body; $RequestArgs.ContentType = 'application/json' }
    try { return Invoke-RestMethod @RequestArgs }
    catch {
        $Code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 'connection failure' }
        throw "Gateway request failed: $Code. Inspect locally without dumping headers or bodies."
    }
}
$Inventory = Invoke-LocalGatewayJson '/v0/management/auth-files' 'management-key.txt'
if (@($Inventory.files).Count -ne 0) { throw 'Fresh auth directory unexpectedly contains accounts.' }
'YAML loaded; authenticated management reachable; zero accounts as expected.'
```

Expected: one owned loopback listener and an empty inventory. Do not add `-local-model`: with this configuration it pins the model list to the catalogs embedded in the release, so later models never appear. v8.0.47 [already embeds `gpt-6.1-terra`](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/registry/models/codex_client_models.json#L513); without the flag, the gateway refreshes its catalogs from upstream. The application log rotates at `$env:LOCALAPPDATA\CLIProxyAPI\logs\main.log`, capped with the other logs at 50 MB. OAuth commands may print login status to their terminal; do not record that output. Do not dump log files into the agent chat. On startup failure, inspect Task Scheduler's last result and summarize only allowlisted error information.

## 4. Sign in the three accounts, one at a time

**HUMAN STEP:** Ask the human to identify the three Pro accounts and their login order. Before each invocation, tell them which account to select. They must finish browser authentication, switch away from any automatically selected previous account, and confirm completion **before the next login starts**. Do not automate passwords, MFA, or account selection.

Use device-code OAuth: it opens the browser without starting a callback listener. Start one login in a separate local console, without redirecting its output to a file or the agent transcript:

```powershell
$LoginProcess = Start-Process -FilePath $Exe -ArgumentList ('-config "{0}" -codex-device-login' -f $Config) -WorkingDirectory $Root -WindowStyle Normal -PassThru
```

**HUMAN STEP:** Have the human read the short code from that local console, enter it at `https://auth.openai.com/codex/device`, select the intended account, and confirm completion. Never request the code in chat. If device authorization is disabled for that account, have the human review its security settings; do not silently fall back to a callback server. After confirmation, run:

```powershell
$LoginProcess.Refresh()
if (!$LoginProcess.HasExited) { throw 'Login still running; wait for the human, do not start another.' }
$FilesOnDisk = @(Get-ChildItem -LiteralPath $AuthDir -File -Filter 'codex-*.json').Count
$Inventory = Invoke-LocalGatewayJson '/v0/management/auth-files' 'management-key.txt'
$CodexAccounts = @($Inventory.files | Where-Object { $_.provider -eq 'codex' -and !$_.disabled -and $_.source -eq 'file' })
"Codex files: $FilesOnDisk; loaded enabled Codex accounts: $($CodexAccounts.Count)."
```

Expected after successive logins: **1, 2, 3**, both on disk and loaded. Allow the watcher a few seconds and repeat the inventory check if needed. A repeated count means the same account may have been selected again; stop for the human. Do not open auth JSON to investigate. Final check:

```powershell
if ($FilesOnDisk -ne 3 -or $CodexAccounts.Count -ne 3) { throw 'Exactly three distinct Codex accounts are required.' }
if (@($CodexAccounts | Where-Object { $_.unavailable }).Count) { throw 'An account is unavailable; resolve before proceeding.' }
'Three Codex accounts loaded and available.'
```

Auth files land in **`$env:LOCALAPPDATA\CLIProxyAPI\auth`**, because the guide sets `oauth.auth-dir` explicitly. The upstream default is `~/.cli-proxy-api` (normally `%USERPROFILE%\.cli-proxy-api` on Windows), not Codex's own `.codex` login. Repeating login for the same identity replaces that gateway credential rather than adding capacity. Both `-codex-device-login` and `-codex-login` exist in 8.0.47. Do **not** use the latter here: its [OAuth callback server](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/auth/codex/oauth_server.go#L88) binds `:1455` (all interfaces), independently of `server.host`. Device login avoids that exception to the loopback rule.

## 5. Verify inference and account rotation

Run three small requests with different cache/session keys. They consume a small amount of normal Codex allowance. Do not reuse one conversation to test round-robin: affinity deliberately keeps it on one account.

```powershell
$Catalog = Invoke-LocalGatewayJson '/v1/models'
if ('gpt-6.1-sol' -notin @($Catalog.data.id)) { throw 'Expected model is not advertised; inspect model IDs only, then ask before substituting.' }
for ($Probe = 1; $Probe -le 3; $Probe++) {
    $Body = @{
        model = 'gpt-6.1-sol'
        instructions = 'Reply briefly.'
        input = @(@{ role = 'user'; content = @(@{ type = 'input_text'; text = 'Reply with exactly: gateway-ok' }) })
        stream = $false
        store = $false
        prompt_cache_key = [guid]::NewGuid().ToString()
    } | ConvertTo-Json -Depth 8 -Compress
    $Result = Invoke-LocalGatewayJson '/v1/responses' 'client-key.txt' 'POST' $Body
    $Text = @($Result.output | ForEach-Object { $_.content } | Where-Object { $_.type -eq 'output_text' } | ForEach-Object { $_.text }) -join ''
    if ($Result.status -ne 'completed' -or $Text.Trim() -ne 'gateway-ok') { throw 'Inference probe did not complete as expected; do not dump the response.' }
}
$Inventory = Invoke-LocalGatewayJson '/v0/management/auth-files' 'management-key.txt'
$Served = @($Inventory.files | Where-Object { $_.provider -eq 'codex' -and [long]$_.success -gt 0 }).Count
if ($Served -lt 2) { throw 'Fewer than two accounts served probes; investigate availability and affinity before changing clients.' }
"Three responses completed; $Served distinct Codex accounts have served successful requests."
```

Expected: all probes complete and at least two accounts have success counters (normally all three). The management inventory exposes these runtime counters; no auth-file contents or debug logging are needed. They reset with the gateway process. Without management, successful responses alone do not prove account distribution. Never use `/usage-queue` as a health check: reading it consumes records.

## 6. Connect Codex with a separate profile first

**HUMAN STEP:** Recommend a separate `gateway` profile initially. Confirm whether to keep it separate or eventually make the gateway the default for CLI/app. Do not edit a config used by ongoing sessions or restart them. If there are multiple client homes, establish which one the user intends before proceeding.

Current [Codex profile documentation](https://developers.openai.com/codex/config-advanced/) uses `%USERPROFILE%\.codex\gateway.config.toml` for `--profile gateway` (Codex 0.134.0 and newer), **not** `[profiles.gateway]` inside `config.toml`. Run `codex --version` and `codex --help`; if the installed version predates this behavior, ask before updating it. Back up the base file even though the profile leaves it unchanged:

```powershell
$CliVersionText = (& codex --version | Out-String)
if ($CliVersionText -notmatch '(\d+\.\d+\.\d+)') { throw 'Could not determine Codex version.' }
if ([version]$Matches[1] -lt [version]'0.134.0') { throw 'Ask before updating Codex; this guide requires the current profile format.' }
$CliHelpText = (& codex --help | Out-String)
if ($CliHelpText -notmatch '--profile') { throw 'Codex profile flag missing.' }
$null = New-Item -ItemType Directory -Force -Path $CodexDir
$BaseConfig = Join-Path $CodexDir 'config.toml'
$Backup = Join-Path $Root 'config.toml.before-gateway'
$GatewayProfile = Join-Path $CodexDir 'gateway.config.toml'
if ((Test-Path $GatewayProfile) -or (Test-Path $Backup)) { throw 'Profile or backup already exists; do not overwrite.' }
if (Test-Path $BaseConfig) { Copy-Item -LiteralPath $BaseConfig -Destination $Backup }
else { [IO.File]::WriteAllText((Join-Path $Root 'base-config-was-absent'), '') }
$ProviderToml = @'
model = "gpt-6.1-sol"
model_provider = "cliproxy"

[model_providers.cliproxy]
name = "CLIProxyAPI"
base_url = "http://127.0.0.1:8317/v1"
env_http_headers = { "X-Api-Key" = "CODEX_GATEWAY_API_KEY" }
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
'@
[IO.File]::WriteAllText($GatewayProfile, $ProviderToml)
[IO.File]::WriteAllText((Join-Path $Root 'gateway-profile-created'), '')
$env:CODEX_GATEWAY_API_KEY = [IO.File]::ReadAllText((Join-Path $Root 'client-key.txt')).Trim()
$CliVersionText.Trim()
codex exec --profile gateway 'Reply with exactly: gateway-ok'
if ($LASTEXITCODE -ne 0) { throw 'Codex profile test failed.' }
```

Run the test from a trusted project the user chooses. Expected: `gateway-ok`; start `codex --profile gateway`, check `/status` for provider `cliproxy`/model `gpt-6.1-sol`, and ask for one follow-up. Recheck management success counters to establish that gateway traffic increased; text output alone does not prove routing. For optional advice, `codex --profile gateway --model gpt-6-astra` uses the same provider; first verify that model appears in `/v1/models`.

The [official provider reference](https://developers.openai.com/codex/config-reference/) supports `base_url`, `env_http_headers`, `wire_api = "responses"`, and `requires_openai_auth`. Keep `requires_openai_auth = true`: Codex stays signed in to the human's ChatGPT account, so account features (account and plan recognition, usage display) keep working. With `false`, inference still works but Codex no longer recognizes the account. Codex then sends its ChatGPT token as the bearer, and the gateway authenticates the request by the client key in `X-Api-Key` instead. CLIProxyAPI 8.0.47 [accepts a valid `X-Api-Key` even when the bearer holds a different token](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/access/config_access/provider.go#L75-L103). `env_http_headers` reads the key from the environment, so no secret is written to TOML. Inference still rotates across all three gateway accounts; Codex's usage display shows only the signed-in account.

**HUMAN STEP:** Codex must already be signed in with ChatGPT (any one of the three accounts). If it is not, have the human sign in through Codex's normal login; never handle their credentials. A 401 from the gateway with this profile means `CODEX_GATEWAY_API_KEY` is missing from that process's environment.

For future PowerShell sessions, load `$env:CODEX_GATEWAY_API_KEY` from the private key file before `codex --profile gateway`. If the human approves default routing, **merge** the same top-level keys and provider table into `%USERPROFILE%\.codex\config.toml`, preserving unrelated settings and avoiding duplicate TOML keys/tables. Top-level keys must precede table headers. Do not replace the entire existing file with `$ProviderToml`.

The [Windows Codex app reads that same base config](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway#configure-the-windows-app); do not assume it selects the CLI's named profile. Leave the app unchanged during profile testing. After default routing is approved, make the credential available to new GUI processes without printing it:

```powershell
[Environment]::SetEnvironmentVariable('CODEX_GATEWAY_API_KEY', [IO.File]::ReadAllText((Join-Path $Root 'client-key.txt')).Trim(), 'User')
'Gateway key saved to the current user environment; no secret output.'
```

This stores the value in the current user's environment registry, not an encrypted vault. **HUMAN STEP:** Have the human finish active work and fully exit/reopen the Codex app. If it still inherits a stale environment, ask them to sign out of Windows and back in when safe. In a new app task, check the selected model/configuration, get `gateway-ok` plus a follow-up, and confirm increased gateway success counters. Expected: both CLI and app use the gateway; existing ChatGPT authentication remains intact. A different `CODEX_HOME`, WSL, managed policy, or another launcher can select different settings—verify the actual client rather than editing additional files speculatively.

## 7. Optional T3 Code

**HUMAN STEP:** Ask whether to install [T3 Code](https://t3.codes/). The human handles its installer, any UAC/security prompts, and sign-in. Do not assume it is wanted.

Follow [Theo's T3 Code + CLIProxyAPI guide](https://gist.github.com/t3dotgg/59a521d2ee8d70aaf90e280c175072af): in **Settings → Providers**, add/select Codex and choose **Use existing CLI**. **Continue with ChatGPT** uses a different login path. Use the native Windows `codex` executable whose configuration was verified above. If the installed T3 version exposes Codex launch arguments, set `--profile gateway`; otherwise use the already approved default configuration. Do not silently make the gateway the default just to enable T3. The T3 process needs `CODEX_GATEWAY_API_KEY` too. For a GUI launch, run the user-environment command from step 6 (it does not change the default provider), then have the human restart T3 with a fresh environment after finishing active work.

Expected: a fresh T3 conversation and follow-up work, and the gateway's successful-request counters increase. Confirm this on the actual T3 version; its UI and profile launch-argument support were not tested here.

The separate **Settings → Providers → Usage providers → Add hub** feature needs management access; it does not configure inference. Default: leave it off. If requested, **HUMAN STEP:** explain that granting T3 the gateway management key permits administrative operations, then obtain approval. Use hub URL `http://127.0.0.1:8317` (no `/v1`) and transfer `management-key.txt` through the local clipboard/UI without printing it; clear the clipboard afterward. Expect three accounts and quota windows, or diagnose unavailable data without dumping responses. This integration is source-backed but untested on Windows. The repo's [read-only usage hub](t3-usage-hub.md) uses a separately keyed Linux dashboard shim; that shim is **not** installed by this guide, so its token, ports, and deployment commands do not apply here.

## 8. Optional Management Center

**HUMAN STEP:** Ask whether to enable the official [Management Center](https://github.com/router-for-me/Cli-Proxy-API-Management-Center). It has administrative access to accounts and configuration, including actions that consume manual reset credits. Leave it disabled unless approved.

Back up the private config without displaying it. Under the **existing** `management` block, change only `disable-control-panel` to `false`; retain the management key and these settings (merge, do not replace the block):

```yaml
management:
  allow-remote: false
  disable-control-panel: false
  disable-auto-update-panel: true
```

Keep `server.host: "127.0.0.1"` and port `8317`. With `disable-auto-update-panel: true`, the panel downloads on first access if missing, then stays at that version. With separate human approval, `false` permits background GitHub update checks every three hours. The default `management.panel-github-repository` is `https://github.com/router-for-me/Cli-Proxy-API-Management-Center`; do not substitute another source. The panel release is independent of the gateway version; capabilities below were checked at [v1.26.7](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/releases/tag/v1.26.7), the latest panel on 2026-10-29.

Saving the config [hot-reloads these flags](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/api/server_reload.go#L178-L192); wait a few seconds, repeat step 3's listener/ownership check and confirm the authenticated inventory still contains three Codex accounts, then request `http://127.0.0.1:8317/management.html` without a key to trigger the first download. Do not restart the scheduled task for this change. If reload fails, stop and inspect safe error summaries; coordinate any necessary task restart with the human after active work finishes.

The [downloader](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/managementasset/updater.go#L245-L309) uses GitHub's latest `management.html` release asset and checks its SHA-256 against the asset digest **when supplied** (v1.26.7 supplies one). It can also fall back to `https://cpamc.router-for.me/` **without digest verification** when the first GitHub fetch fails. Before entering the key, compare `Get-FileHash (Join-Path $Root 'static\management.html') -Algorithm SHA256` with the `sha256:` asset digest from the official GitHub release API, `https://api.github.com/repos/router-for-me/Cli-Proxy-API-Management-Center/releases/latest`. Stop if the digest is missing or mismatched; do not authenticate an unverified panel.

**HUMAN STEP:** Open `http://127.0.0.1:8317/management.html`. Use server URL `http://127.0.0.1:8317` if prompted and the **management key**, not the client key. Transfer `management-key.txt` through the local clipboard/UI without printing it; clear the clipboard afterward and leave “Remember password” off. The HTML login page itself needs no key; its management API calls do, even on loopback. Never put the key in the URL.

The checked panel can list/enable/disable credentials, [refresh Codex quota windows and reset times, show manual reset credits and their expiry, and redeem an available credit](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/v1.26.7/src/features/quota/providers/codex/data.ts#L418-L548) with **Reset quota**. **HUMAN STEP:** Let the human choose the account and explicitly confirm each redemption; it consumes one credit. If redemption succeeded but clearing the gateway cooldown failed, use that account's **Clear cooldown** action; do not redeem another credit. Clearing a local cooldown alone does not restore upstream allowance.

It also displays and [edits routing strategy (`round-robin`, `weighted-round-robin`, `fill-first`) and session-affinity settings](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/v1.26.7/src/features/config/components/sections/SectionNetwork.tsx#L118-L165) in its configuration editor. Retain this guide's routing unless the human approves a change. It does not pool subscriptions, create reset credits, or automatically select the account whose quota resets soonest.

The dashboard also shows recent gateway request successes/failures and a traffic chart from credential counters; these need no `usage-statistics-enabled` change. They reset with the gateway process. This panel does not provide a durable token/cost history or the accounts' complete ChatGPT usage. Leave request/body logging and the separate usage queue off.

To turn the panel off, set `management.disable-control-panel: true` again and retain `disable-auto-update-panel: true`. After reload, expect `/management.html` to return 404 while the authenticated inventory still works. Sign out of the panel and close its tab. The downloaded HTML remains on disk; disabling the panel does not disable the management API. Panel login, quota/reset actions, charts, and routing edits still require Windows acceptance; they were source-checked, not executed here.

## 9. Fast mode and WebSockets

Go's standard `plugin` package does not support Windows, but CLIProxyAPI **does not use that mechanism**: v8.0.47 has a [Windows DLL loader](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.47/internal/pluginhost/loader_windows.go) calling `LoadDLL` and `cliproxy_plugin_init` through its C/JSON ABI. Its Windows release builds enable CGO. This repo's [fast-mode plugin build](../plugins/fast-mode-policy/build.sh) explicitly targets Linux amd64 with `-buildmode=c-shared`; the resulting `.so` cannot load on Windows. No Windows DLL build of this plugin is provided or validated.

The sample's `requests.payload.filter` removes `service_tier` on Codex requests globally. It provides a simpler all-accounts policy, not the Linux dashboard's per-account control. Keep `supports_websockets = false` and `upstream.codex.response-steering: false` for this setup. Theo's gist enables client WebSockets, but upstream Codex WebSockets also depend on credential flags; setting the client flag alone is not proof of the upstream transport. See [the policy](fast-mode-policy.md) and [WebSocket findings](codex-websockets.md) before changing this. Duplex follow-ups can bypass policy processing. Effective outbound tier enforcement on Windows remains an acceptance check, not a measured result of this document's Linux config-load test.

## 10. Upgrade, stop, or roll back

Before upgrading, wait for this gateway's tasks to finish and ask the human to close affected client sessions. Download the chosen release into a **new version directory**, verify its published checksum as in step 1, and read that version's config changes. Back up the private config; leave `auth` and both keys in place. Never replace a running executable or restore old OAuth tokens as part of a binary downgrade.

Record the existing scheduled task action in memory (`$OldAction = (Get-ScheduledTask -TaskName $TaskName).Actions`). Stop the task, point its action at the newly verified absolute executable path with the same config argument/working directory, then start it. Repeat the listener, three-account, inference, rotation, and fresh-client checks. To undo a failed upgrade, stop the task, restore `$OldAction` with `Set-ScheduledTask -TaskName $TaskName -Action $OldAction`, restore only the matching config backup if necessary, and start it again. Keep the prior version until acceptance passes. Do not use a guessed “latest” version.

For complete setup rollback, use the following in PowerShell. It restores the pre-setup Codex config (or its prior absence), removes only this guide's profile/task/environment variable, and stops only this gateway. Coordinate with the human if the base config changed again after the backup; do not overwrite later work. Private credentials and installed versions remain for recovery.

```powershell
$Root = Join-Path $env:LOCALAPPDATA 'CLIProxyAPI'
$CodexDir = Join-Path $env:USERPROFILE '.codex'
$TaskName = 'CLIProxyAPI-local'
$BaseConfig = Join-Path $CodexDir 'config.toml'
$Backup = Join-Path $Root 'config.toml.before-gateway'
if (Test-Path $Backup) { Copy-Item -LiteralPath $Backup -Destination $BaseConfig -Force }
elseif (Test-Path (Join-Path $Root 'base-config-was-absent')) {
    if (Test-Path $BaseConfig) { Remove-Item -LiteralPath $BaseConfig }
}
if ((Test-Path (Join-Path $Root 'gateway-profile-created')) -and (Test-Path (Join-Path $CodexDir 'gateway.config.toml'))) {
    Remove-Item -LiteralPath (Join-Path $CodexDir 'gateway.config.toml')
}
[Environment]::SetEnvironmentVariable('CODEX_GATEWAY_API_KEY', $null, 'User')
Remove-Item Env:CODEX_GATEWAY_API_KEY -ErrorAction SilentlyContinue
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
$Owned = @(Get-CimInstance Win32_Process -Filter "Name = 'cli-proxy-api.exe'" | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith((Join-Path $Root 'versions\'), [StringComparison]::OrdinalIgnoreCase) -and
    $_.CommandLine.Contains((Join-Path $Root 'config.yaml'))
})
$Owned | ForEach-Object { Stop-Process -Id $_.ProcessId }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task still exists.' }
$Remaining = @(Get-NetTCPConnection -State Listen -LocalPort 8317 -ErrorAction SilentlyContinue)
if ($Remaining.Count) { throw 'Port 8317 still has a listener; identify its owner without stopping unrelated processes.' }
'Gateway task removed, owned processes stopped, Codex configuration restored.'
```

Expected: no guide-owned task/process/listener, and the original Codex configuration restored. **HUMAN STEP:** Restart only the affected client when their work is safe; check it uses its original provider. Deleting the retained auth directory, keys, or backups requires a separate human decision.

## 11. Troubleshooting

| Symptom | Agent action and expected check |
| --- | --- |
| 401 from the gateway | Check the client environment variable exists without printing it and matches the private client-key file in memory. A management request needs the separate management key. Expect `/v1/models` and the inventory checks to pass. |
| Upstream 401 / invalidated login | Identify the affected account using inventory metadata privately; repeat only its gateway OAuth login with a **HUMAN STEP:**. Never log out the user's regular Codex client. Expect the account available and a successful fresh request. |
| 429 | Check inventory `unavailable`/quota/cooldown metadata and whether all three accounts are enabled. v8.0.47 automatically treats Codex `turn_rate_limit_exceeded` as transient capacity and can retry early WebSocket 429s on another credential; no extra retry key is needed. The retry settings cannot create allowance. Wait for reset or let another eligible account serve a fresh session. Do not disable cooling or repeatedly hammer the endpoint. |
| Only one account used | A single conversation is sticky by design. Repeat the three distinct-session probes, check all three entries are enabled/available and have equal priority, and count successful accounts. Repeated logins to one identity do not add accounts. |
| Port 8317 occupied | Run `Get-NetTCPConnection -State Listen -LocalPort 8317 -ErrorAction SilentlyContinue` and inspect its owning process metadata. Do not kill it or change every URL silently. Ask the human about the conflict. Device login does not need callback port 1455. |
| Task stops or does not start | Inspect `Get-ScheduledTaskInfo -TaskName 'CLIProxyAPI-local'`, the configured absolute action path/working directory, and safe startup error summaries. Expect the listener check to pass after a logon or explicit task start. It does not run through logout or sleep. |
| App/T3 reports missing key | A terminal's environment is not automatically the desktop application's environment. Check variable presence in the actual launcher, confirm the intended Codex home/profile, then use the human restart/sign-out handoff in step 6. |
| SmartScreen / Defender / unsigned executable | **HUMAN STEP:** pause for review of the official asset and matching checksum. Preserve protections. If blocked, stop setup and report the detection; do not invent an upstream signing guarantee. |

Finish by reporting installed version, private state directory, scheduled task name, owned PID/loopback port, account count, probe/client results, and any checks still unverified. Never attach secrets or raw logs.
