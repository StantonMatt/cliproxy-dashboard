#!/usr/bin/env python3
"""Install/remove the current user's Plasma 6 tray applet, without restarting Plasma."""
import argparse
import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import sys

PLUGIN = 'org.kde.plasma.routerpersonal'
HERE = Path(__file__).resolve().parent


def spanish(environ=os.environ):
    # The same order as gettext: the first non-empty variable decides.
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        if environ.get(name):
            return environ[name].lower().startswith('es')
    return False


def say(es, en):
    return es if spanish() else en


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=30).stdout.strip()


def failure(error):
    detail = getattr(error, 'stderr', '') or str(error)
    # TimeoutExpired keeps captured stderr as bytes even with text=True.
    return (detail.decode('utf-8', errors='replace') if isinstance(detail, bytes) else detail).strip()


# Current Plasma embeds the tray containment; older Plasma 6 versions use
# SystrayContainmentId. Configure through its live scripting API in either case.
TRAY_SCRIPT = r'''
var count = 0;
var id = "org.kde.plasma.routerpersonal";
var remove = UNINSTALL;
function change(tray) {
    tray.currentConfigGroup = ["General"];
    function list(key) {
        var value = tray.readConfig(key, []);
        return Array.isArray(value) ? value : String(value).split(",").filter(Boolean);
    }
    ["extraItems", "shownItems", "hiddenItems"].forEach(function(key) {
        var items = list(key).filter(function(item) { return item !== id; });
        if (!remove && key !== "hiddenItems") items.push(id);
        tray.writeConfig(key, items);
    });
    tray.reloadConfig();
    count++;
}
panels().forEach(function(panel) {
    panel.widgets().forEach(function(widget) {
        if (widget.type !== "org.kde.plasma.systemtray") return;
        widget.currentConfigGroup = ["General"];
        var containmentId = widget.readConfig("SystrayContainmentId", 0);
        widget.currentConfigGroup = [];
        containmentId = widget.readConfig("SystrayContainmentId", containmentId);
        change(containmentId ? desktopById(containmentId) : widget);
    });
});
print("TRAYS=" + count);
'''


def update_trays(remove):
    """Return how many system trays were updated; raise if Plasma could not run the script."""
    result = run('qdbus6', 'org.kde.plasmashell', '/PlasmaShell', 'org.kde.PlasmaShell.evaluateScript',
                 TRAY_SCRIPT.replace('UNINSTALL', 'true' if remove else 'false'))
    count = next((line.split('=', 1)[1] for line in result.splitlines() if line.startswith('TRAYS=')), None)
    if 'Error' in result or count is None or not count.isdigit():
        raise RuntimeError(result or say('Plasma no respondió', 'Plasma did not answer'))
    return int(count)


def backup_config(config_home, state_home):
    config = config_home / 'plasma-org.kde.plasma.desktop-appletsrc'
    backup_dir = state_home / 'router-personal-tray/plasma-backup'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    if config.exists():
        backup = backup_dir / (config.name + '.' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        shutil.copy2(config, backup)
        print(say(f'Copia de configuración: {backup}', f'Configuration backup: {backup}'))


def install(data_home):
    for tool in ('kpackagetool6', 'qdbus6'):
        if not shutil.which(tool):
            raise RuntimeError(say(f'Falta {tool}; ejecuta este script en una sesión de Plasma 6.',
                                   f'{tool} is missing; run this script in a Plasma 6 session.'))
    package = data_home / 'plasma/plasmoids' / PLUGIN
    action = '--upgrade' if package.exists() else '--install'
    print(run('kpackagetool6', '--type', 'Plasma/Applet', action, str(HERE / 'package')))
    if not update_trays(False):
        raise RuntimeError(say('No se encontró una bandeja del sistema; añade una al panel.',
                               'No system tray was found; add one to a panel.'))
    print(say('Router personal instalado y visible en la bandeja.', 'Router personal installed and shown in the tray.'))


def uninstall(data_home, problems=None):
    """Remove what can be removed; a missing tray or tool never stops the other steps."""
    problems = list(problems or [])
    if shutil.which('qdbus6'):
        try:
            if not update_trays(True):
                print(say('No hay bandeja del sistema; no hay nada que quitar de ella.',
                          'No system tray found; nothing to remove from it.'))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
            problems.append(say('bandeja: ', 'tray: ') + failure(error))
    else:
        print(say('Falta qdbus6; se omite la bandeja.', 'qdbus6 is missing; skipping the tray.'))
    if (data_home / 'plasma/plasmoids' / PLUGIN).exists():
        try:
            if not shutil.which('kpackagetool6'):
                raise RuntimeError(say('falta kpackagetool6', 'kpackagetool6 is missing'))
            print(run('kpackagetool6', '--type', 'Plasma/Applet', '--remove', PLUGIN))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
            problems.append(say('paquete: ', 'package: ') + failure(error))
    if problems:
        raise RuntimeError(say('Desinstalación incompleta: ', 'Uninstall incomplete: ') + '; '.join(problems))
    print(say('Router personal desinstalado.', 'Router personal uninstalled.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    problems = []
    try:
        backup_config(config_home, state_home)
    except OSError as error:
        if not args.uninstall:
            raise
        problems.append(say('copia de configuración: ', 'configuration backup: ') + failure(error))
    if args.uninstall:
        uninstall(data_home, problems)
    else:
        install(data_home)


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(failure(error), file=sys.stderr)
        sys.exit(1)
