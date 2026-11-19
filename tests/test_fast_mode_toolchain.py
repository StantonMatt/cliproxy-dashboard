import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('toolchain', ROOT / 'plugins/fast-mode-policy/toolchain.py')
toolchain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolchain)


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.network = self.enterContext(patch.object(toolchain, 'urlopen', side_effect=AssertionError('no network')))
        self.enterContext(patch.object(toolchain.os, 'geteuid', return_value=1000))

    def test_explicit_go_takes_precedence_without_a_version_probe(self):
        with patch.dict(os.environ, {'GO': '/selected/go'}), \
                patch.object(toolchain, 'go_version', return_value=(1, 26, 0)) as version, \
                patch.object(toolchain.shutil, 'which') as which, \
                patch.object(toolchain, 'provision') as provision:
            self.assertEqual(toolchain.select_go(), '/selected/go')
            version.assert_not_called()
            which.assert_not_called()
            provision.assert_not_called()

    def test_sufficient_path_go_needs_no_download(self):
        with patch.object(toolchain.shutil, 'which', return_value='/bin/go'), \
                patch.object(toolchain, 'go_version', return_value=(1, 27, 0)), \
                patch.object(toolchain, 'provision') as provision:
            self.assertEqual(toolchain.select_go(), '/bin/go')
            provision.assert_not_called()

    def test_old_or_missing_path_go_provisions_in_user_cache(self):
        for installed in ('/bin/go', None):
            with self.subTest(installed=installed), \
                    patch.dict(os.environ, {'XDG_CACHE_HOME': '/cache'}), \
                    patch.object(toolchain.shutil, 'which', return_value=installed), \
                    patch.object(toolchain, 'go_version', return_value=(1, 25, 9)), \
                    patch.object(toolchain, 'provision', return_value=Path('/pinned/go')) as provision:
                self.assertEqual(toolchain.select_go(), '/pinned/go')
                provision.assert_called_once_with(Path('/cache/personal-codex-router/go'))
        with patch.object(toolchain.shutil, 'which', return_value=None), \
                patch.object(toolchain.Path, 'home', return_value=Path('/user')), \
                patch.object(toolchain, 'provision', return_value=Path('/pinned/go')) as provision:
            toolchain.select_go()
            provision.assert_called_once_with(Path('/user/.cache/personal-codex-router/go'))

    def test_checksum_mismatch_is_refused_before_extraction_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(toolchain, 'urlopen', return_value=io.BytesIO(b'bad archive')), \
                patch.object(toolchain.tarfile, 'open') as extract:
            cache = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                toolchain.provision(cache)
            extract.assert_not_called()
            self.assertEqual([p.name for p in cache.iterdir()], [toolchain.VERSION + '.lock'])

    def test_download_is_verified_then_cached_toolchain_is_reused(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
            info = tarfile.TarInfo('go/bin/go')
            info.mode, info.size = 0o755, 4
            bundle.addfile(info, io.BytesIO(b'fake'))
        data = archive.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(toolchain, 'SHA256', digest), \
                patch.object(toolchain, 'go_version', return_value=(1, 26, 10)), \
                patch.object(toolchain, 'urlopen', return_value=io.BytesIO(data)) as download:
            cache = Path(directory)
            binary = toolchain.provision(cache)
            self.assertEqual(binary, cache / toolchain.VERSION / 'go/bin/go')
            self.assertEqual(binary.read_bytes(), b'fake')
            self.assertEqual((cache / toolchain.VERSION / '.verified-sha256').read_text().strip(), digest)
            self.assertFalse((cache / toolchain.VERSION / 'go.tar.gz').exists())
            self.assertEqual(toolchain.provision(cache), binary)
            download.assert_called_once_with(toolchain.URL, timeout=60)

    def test_download_failure_is_clear_and_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(toolchain, 'urlopen', side_effect=OSError('blocked')):
            with self.assertRaisesRegex(RuntimeError, 'Go download failed'):
                toolchain.provision(Path(directory))
            self.assertFalse((Path(directory) / toolchain.VERSION).exists())

    def test_root_never_downloads(self):
        with patch.object(toolchain.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'never root'):
                toolchain.provision(Path('/must-not-create'))
        self.network.assert_not_called()

    def test_unverified_existing_cache_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / toolchain.VERSION).mkdir()
            with self.assertRaisesRegex(RuntimeError, 'Cached Go toolchain is incomplete'):
                toolchain.provision(cache)
        self.network.assert_not_called()

    def test_missing_compiler_has_one_actionable_error_before_toolchain_selection(self):
        with patch.object(toolchain.shutil, 'which', return_value=None), \
                patch.object(toolchain, 'select_go') as select:
            with self.assertRaisesRegex(RuntimeError, 'sudo apt install gcc libc6-dev'):
                toolchain.main()
            select.assert_not_called()

    def test_go_version_disables_automatic_toolchain_downloads(self):
        with patch.object(toolchain.subprocess, 'run') as run:
            run.return_value.stdout = 'go version go1.26.10 linux/amd64'
            self.assertEqual(toolchain.go_version('/go'), (1, 26, 10))
            self.assertEqual(run.call_args.kwargs['env']['GOTOOLCHAIN'], 'local')


if __name__ == '__main__':
    unittest.main()
