import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, script):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / script)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


renewal = module('preview_renewal', 'renew-preview-cert.py')
installer = module('preview_installer', 'install-operations.py')


class PreviewTLSOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'previews.json'
        self.config.write_text(json.dumps({'dns':'router.tail-example.ts.net'}))
        self.cert, self.key = self.root / 'test.crt', self.root / 'test.key'
        self.generate()
        self.calls = []

    def generate(self):
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=router.tail-example.ts.net','-keyout',str(self.key),'-out',str(self.cert)],
                       check=True,capture_output=True,timeout=10)

    def fake_command(self, *args):
        self.calls.append(args)
        if args[0] == '/usr/bin/tailscale':
            shutil.copyfile(self.cert, args[2].split('=',1)[1])
            shutil.copyfile(self.key, args[3].split('=',1)[1])
            return subprocess.CompletedProcess(args,0,b'',b'')
        if args[0] == '/usr/bin/systemctl':
            return subprocess.CompletedProcess(args,0,b'',b'')
        return subprocess.run(args,check=True,capture_output=True,timeout=5)

    def run_renewal(self, command=None):
        with patch.object(renewal,'CONFIG',self.config), patch.object(renewal,'STATE',self.root/'state'), \
                patch.object(renewal.os,'geteuid',return_value=0), \
                patch.object(renewal,'command',side_effect=command or self.fake_command), \
                contextlib.redirect_stdout(io.StringIO()):
            old = os.umask(0o077)
            try:
                renewal.renew()
            finally:
                os.umask(old)

    def restarts(self):
        return [c for c in self.calls if c[0]=='/usr/bin/systemctl']

    def test_renewal_publishes_private_pair_and_only_restarts_on_change(self):
        self.run_renewal()
        state = self.root/'state'
        current = state/'current'
        self.assertTrue(current.is_symlink())
        for path in (state,current.resolve(),current/'cert.pem',current/'key.pem'):
            self.assertEqual(path.stat().st_mode & 0o077,0)
        self.assertEqual((current/'cert.pem').read_bytes(),self.cert.read_bytes())
        self.assertEqual(len(self.restarts()),1)
        self.run_renewal()
        self.assertEqual(len(self.restarts()),1)
        old = current.resolve()
        self.generate();self.run_renewal()
        self.assertEqual(len(self.restarts()),2)
        self.assertNotEqual(current.resolve(),old)
        self.assertFalse(old.exists())

    def test_failed_restart_is_retried_and_invalid_pair_never_published(self):
        def failed(*args):
            if args[0]=='/usr/bin/systemctl':
                raise subprocess.CalledProcessError(1,args)
            return self.fake_command(*args)
        with self.assertRaises(subprocess.CalledProcessError):self.run_renewal(failed)
        self.assertFalse((self.root/'state/applied').exists())
        self.run_renewal()
        self.assertTrue((self.root/'state/applied').exists())
        current=(self.root/'state/current').resolve()
        self.key.write_text('invalid')
        with self.assertRaises(Exception):self.run_renewal()
        self.assertEqual((self.root/'state/current').resolve(),current)
        self.assertEqual(len(self.restarts()),1)

    def test_wrong_hostname_never_publishes_certificate(self):
        self.config.write_text(json.dumps({'dns':'wrong.tail-example.ts.net'}))
        with self.assertRaises((RuntimeError,subprocess.CalledProcessError)):self.run_renewal()
        self.assertFalse((self.root/'state/current').exists())
        self.assertEqual(self.restarts(),[])

    def test_units_ship_credentials_and_weekly_renewal(self):
        unit=(ROOT/'scripts/personal-codex-router-preview.service').read_text()
        for expected in ('DynamicUser=true','ProtectSystem=strict','ProtectHome=true',
                         'LoadCredential=preview.key:','LoadCredential=preview.crt:',
                         'IPAddressDeny=any','@DEVICE_ALLOW@'):
            self.assertIn(expected,unit)
        cert_unit=(ROOT/'scripts/personal-codex-router-preview-cert.service').read_text()
        self.assertIn('User=root',cert_unit)
        self.assertIn('StateDirectoryMode=0700',cert_unit)
        timer=(ROOT/'scripts/personal-codex-router-preview-cert.timer').read_text()
        self.assertIn('OnCalendar=weekly',timer)
        self.assertIn('Persistent=true',timer)
        names={dest.name for _,dest,_ in installer.preview_files()}
        self.assertEqual(names,{'router-preview.mjs','router-preview.py','renew-preview-cert.py','router-preview',
                               'personal-codex-router-preview.service','personal-codex-router-preview-cert.service',
                               'personal-codex-router-preview-cert.timer'})
        full={dest.name for _,dest,_ in installer.install_files()}
        self.assertTrue(names <= full)

    def test_preview_only_apply_preserves_registrations_aliases_and_firewall(self):
        config={'bind':'100.64.0.10','dns':'old.tail-example.ts.net','base_port':8400,
                'aliases':['router','router.tail-example.ts.net'],'address':'router',
                'devices':[{'name':'Mac','ips':['100.64.0.11']}],
                'previews':[{'name':'website-pagar','listen':8407,'target':4427,'host':'localhost:4427','expires_at':123},
                            {'name':'standing-demo','listen':8408,'target':4428,'host':'demo.localhost',
                             'expires_at':None,'permanent':True}]}
        self.config.write_text(json.dumps(config))
        backups=self.root/'backups';backups.mkdir()
        (backups/'status.json').write_text(json.dumps({'snapshot':'fixture'}))
        calls=[]
        def command(*args, **kwargs):
            calls.append(args)
            if args[0]=='tailscale':
                return json.dumps({'Self':{'DNSName':'router.tail-example.ts.net.','TailscaleIPs':[config['bind']]}})
            return 'active'
        original_site=installer.SITE_SETTINGS
        original_bind=installer.PREVIEW_BIND
        self.addCleanup(lambda:installer.configure_site(original_site))
        self.addCleanup(setattr,installer,'PREVIEW_BIND',original_bind)
        with patch.object(installer,'CONFIG',self.config),patch.object(installer,'PREVIEW_BACKUPS',backups), \
                patch.object(installer.os,'geteuid',return_value=0),patch.object(installer,'command',side_effect=command), \
                patch.object(installer,'install_file') as install,patch.object(installer,'pre_apply_backup') as backup, \
                contextlib.redirect_stdout(io.StringIO()):
            installer.install_previews(True)
        updated=json.loads(self.config.read_text())
        self.assertEqual(updated,{**config,'tls_hostname':'router.tail-example.ts.net'})
        self.assertEqual(install.call_count,len(installer.preview_files()))
        self.assertTrue(all(c[0] in ('tailscale','systemctl') for c in calls))
        self.assertTrue(all('dashboard' not in str(c) and 'nft' not in str(c) for c in calls))
        self.assertEqual(backup.call_args.kwargs,{'account_access_only':True})
        self.assertIn((self.config,self.config,0o644),backup.call_args.args[1])
        self.assertIn(('systemctl','start','personal-codex-router-preview-cert.service'),calls)
        with patch.object(installer,'CONFIG',self.config),patch.object(installer.os,'fchown'):
            installer.write_previews()
        self.assertEqual(json.loads(self.config.read_text())['previews'],config['previews'])


if __name__ == '__main__':unittest.main()
