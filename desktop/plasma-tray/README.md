# Router personal · Plasma 6

A read-only tray widget for the local account router. Left click opens the
dashboard (`http://127.0.0.1:8320` by default) in the default browser, as a
normal window; hover shows its health. The context menu can open the dashboard,
copy its address or switch the language. Middle click does nothing. It polls
`/api/health` every 30 seconds with a five-second timeout and never sends
notifications or changes router settings.

From the desktop user's Plasma 6 session, without sudo:

```sh
python3 desktop/plasma-tray/install.py
```

The installer uses `kpackagetool6` and Plasma's live scripting API to enable the
applet and keep it visible in the system tray. It preserves other tray items.
It backs up `~/.config/plasma-org.kde.plasma.desktop-appletsrc` before changes to
`~/.local/state/router-personal-tray/plasma-backup/`. Plasma is never
restarted. Run the same command after updating the package; Plasma may keep the previous QML in memory until the next login. If an upgrade
keeps the previous behaviour, log out and back in when convenient; the installer
never does this automatically. A new installation loads immediately. It adds no
window rules, KWin scripts, launchers or browser profiles.

To remove it:

```sh
python3 desktop/plasma-tray/install.py --uninstall
```

Uninstall continues when no system tray or Plasma session is left: it removes
the applet package and reports anything it could not clean up.

## Settings

**Configure Router personal…** in the context menu sets the dashboard address,
for a dashboard started with another `--port`. Only `http` or `https` addresses
on this computer (`localhost`, `router.localhost` or `127.0.0.1`) at the root path are
accepted; until the field holds one, the widget keeps using
`http://127.0.0.1:8320` and the page says so. Changing it checks the new address
immediately.

The widget speaks Spanish when the system language is Spanish and English
otherwise. **Switch to English** / **Cambiar a español** in the context menu
changes it and saves the choice in the widget's configuration. The installer's
messages follow `LC_ALL`, `LC_MESSAGES` or `LANG` the same way.

## Status

The icon follows Plasma's text colour with the approved 16/22 px symbolic mark.
Orange means attention, red means no response, and a dim mark means checking.
The applet always reports Plasma's `Active` status and the installer pins it as
shown, so it stays next to the clock. It does not use `NeedsAttention`, which
would make Plasma pulse the icon; the badge colour alone signals a problem.

The shared state mapping and both languages are in
`package/contents/code/status.js`; verify with:

```sh
node tests/test_plasma_tray.cjs
node --check desktop/plasma-tray/package/contents/code/status.js
python3 -m unittest tests.test_plasma_tray_launcher
```

The widget ignores the API's overall `ok` and local-app `codex` health fields.
Weekly-limited accounts are informational when another account of the provider
works. Gateway startup with unknown status and empty accounts is treated as
checking for up to three minutes. Two consecutive failed dashboard reads mean
no response. Unknown status lasting over three minutes means attention.

`/api/health` provides account status but neither reset times nor which
allowance window is exhausted, so the tooltip shows only how many accounts are
at their limit.
