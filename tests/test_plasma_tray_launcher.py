"""Focused installer and package checks, without touching the desktop."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'desktop/plasma-tray/package'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('installer', 'desktop/plasma-tray/install.py')


class FakeDesktop:
    """Every KDE command is faked; nothing reaches Plasma, KWin or kpackagetool6."""

    def __init__(self, trays='1', tray_error=None, tools=('kpackagetool6', 'qdbus6')):
        self.calls, self.trays, self.tray_error, self.tools = [], trays, tray_error, tools

    def run(self, *args):
        self.calls.append(args)
        if args[0] == 'qdbus6' and 'evaluateScript' in args[3]:
            if self.tray_error:
                raise self.tray_error
            return 'TRAYS=' + self.trays
        return ''

    def which(self, tool):
        return '/usr/bin/' + tool if tool in self.tools else None

    def __enter__(self):
        self.patches = [patch.object(installer, 'run', side_effect=self.run),
                        patch.object(installer.shutil, 'which', side_effect=self.which)]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *error):
        for item in self.patches:
            item.stop()

    def commands(self):
        return [args[0] + ('' if args[0] != 'kpackagetool6' else ' ' + args[3]) for args in self.calls]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='router tray ')
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / 'data'
        (self.data / 'plasma/plasmoids' / installer.PLUGIN).mkdir(parents=True)

    def test_install_adds_the_applet_without_any_window_rule_or_script(self):
        with FakeDesktop() as desktop:
            installer.install(self.data)
        self.assertEqual(desktop.commands(), ['kpackagetool6 --upgrade', 'qdbus6'])
        self.assertFalse(any('KWin' in ' '.join(args) or args[0].startswith(('kreadconfig', 'kwriteconfig'))
                             for args in desktop.calls))

    def test_install_without_a_tray_fails(self):
        with FakeDesktop(trays='0'), self.assertRaises(RuntimeError):
            installer.install(self.data)

    def test_uninstall_without_a_tray_still_removes_the_package(self):
        with FakeDesktop(trays='0') as desktop:
            installer.uninstall(self.data)
        self.assertEqual(desktop.commands(), ['qdbus6', 'kpackagetool6 --remove'])

    def test_uninstall_continues_and_reports_backup_failure_with_other_problems(self):
        for tray_error in (None, RuntimeError('No such service')):
            with self.subTest(tray_error=tray_error), FakeDesktop(tray_error=tray_error) as desktop:
                with patch.object(installer.sys, 'argv', ['install.py', '--uninstall']), \
                     patch.dict(installer.os.environ, {'XDG_DATA_HOME': str(self.data), 'LC_ALL': 'en_US.UTF-8'}), \
                     patch.object(installer, 'backup_config', side_effect=PermissionError('State directory is read-only')):
                    with self.assertRaises(RuntimeError) as raised:
                        installer.main()
                self.assertIn('Uninstall incomplete: configuration backup: State directory is read-only',
                              str(raised.exception))
                if tray_error:
                    self.assertIn('tray: No such service', str(raised.exception))
                self.assertEqual(desktop.commands(), ['qdbus6', 'kpackagetool6 --remove'])

    def test_uninstall_continues_when_plasma_cannot_update_the_tray(self):
        error = subprocess.CalledProcessError(1, 'qdbus6', stderr='No such service')
        with FakeDesktop(tray_error=error) as desktop:
            with self.assertRaisesRegex(RuntimeError, 'No such service'):
                installer.uninstall(self.data)
        self.assertIn('kpackagetool6 --remove', desktop.commands(), 'the package is removed before reporting')

    def test_uninstall_continues_when_tray_timeout_stderr_is_bytes(self):
        error = subprocess.TimeoutExpired('qdbus6', 30, stderr=b' Tray timed out: \xff \n')
        with FakeDesktop(tray_error=error) as desktop:
            with self.assertRaises(RuntimeError) as raised:
                installer.uninstall(self.data)
        self.assertIn('Tray timed out: \ufffd', str(raised.exception))
        self.assertNotIn("b'", str(raised.exception))
        self.assertIn('kpackagetool6 --remove', desktop.commands())

    def test_package_timeout_stderr_is_decoded_before_reporting(self):
        with FakeDesktop() as desktop:
            original_run = desktop.run
            def run(*args):
                if args[0] == 'kpackagetool6':
                    raise subprocess.TimeoutExpired('kpackagetool6', 30, stderr=b' Package timed out \n')
                return original_run(*args)
            with patch.object(installer, 'run', side_effect=run), self.assertRaisesRegex(RuntimeError, 'Package timed out'):
                installer.uninstall(self.data)

    def test_uninstall_without_plasma_tools_skips_them(self):
        with FakeDesktop(tools=('kpackagetool6',)) as desktop:
            installer.uninstall(self.data)
        self.assertEqual(desktop.commands(), ['kpackagetool6 --remove'])

    def test_uninstall_of_a_removed_package_is_quiet(self):
        (self.data / 'plasma/plasmoids' / installer.PLUGIN).rmdir()
        with FakeDesktop() as desktop:
            installer.uninstall(self.data)
        self.assertEqual(desktop.commands(), ['qdbus6'])

    def test_messages_follow_the_system_language(self):
        self.assertTrue(installer.spanish({'LANG': 'es_CL.UTF-8'}))
        self.assertTrue(installer.spanish({'LC_ALL': 'es_ES.UTF-8', 'LANG': 'en_US.UTF-8'}))
        self.assertFalse(installer.spanish({'LC_MESSAGES': 'en_US.UTF-8', 'LANG': 'es_CL.UTF-8'}))
        self.assertFalse(installer.spanish({'LANG': 'de_DE.UTF-8'}))
        self.assertFalse(installer.spanish({}))


class PackageTests(unittest.TestCase):
    def test_dashboard_opens_as_a_normal_browser_window(self):
        qml = (PACKAGE / 'contents/ui/main.qml').read_text()
        self.assertIn('Qt.openUrlExternally(dashboardUrl)', qml)
        for removed in ('open_dashboard.py', 'activate.js', 'Plasma5Support', 'KWin', 'skipTaskbar'):
            self.assertNotIn(removed, qml)
        self.assertFalse((PACKAGE / 'contents/code/open_dashboard.py').exists())
        self.assertFalse((PACKAGE / 'contents/code/activate.js').exists())

    def test_settings_hold_the_address_and_language(self):
        schema = (PACKAGE / 'contents/config/main.xml').read_text()
        self.assertIn('<entry name="dashboardUrl" type="String">\n      <default>http://127.0.0.1:8320</default>', schema)
        self.assertIn('<entry name="language" type="String">', schema)
        self.assertIn('source: "configGeneral.qml"', (PACKAGE / 'contents/config/config.qml').read_text())
        page = (PACKAGE / 'contents/ui/configGeneral.qml').read_text()
        self.assertIn('property alias cfg_dashboardUrl: urlField.text', page)
        self.assertIn('RouterStatus.normalizeUrl(urlField.text) === null', page)
        qml = (PACKAGE / 'contents/ui/main.qml').read_text()
        self.assertIn('RouterStatus.dashboardUrl(Plasmoid.configuration.dashboardUrl)', qml)
        self.assertIn('Plasmoid.configuration.language = RouterStatus.otherLanguage(root.language)', qml)
        self.assertIn('Plasmoid.hasConfigurationInterface: true', qml)


if __name__ == '__main__':
    unittest.main()
