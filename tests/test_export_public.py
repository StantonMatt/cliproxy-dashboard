"""Offline tests for archive-only exports and fail-closed publication checks."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_public', ROOT / 'scripts/export-public.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def archive(self, entries):
        """Stand in for git archive, with no Git commits or installed services."""
        def run(command, *, stdout, **kwargs):
            self.assertEqual(command, ['git', '-C', str(ROOT), 'archive', '--format=tar', 'a' * 40])
            with tarfile.open(fileobj=stdout, mode='w') as archive:
                for name, content in entries.items():
                    entry = tarfile.TarInfo(name)
                    entry.mode = 0o755 if name.endswith('.sh') else 0o644
                    if content is None:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = '/outside'
                        archive.addfile(entry)
                    else:
                        entry.size = len(content)
                        archive.addfile(entry, io.BytesIO(content))
            return subprocess.CompletedProcess(command, 0)
        return run

    def export(self, entries, out=None, ref='HEAD'):
        out = out or self.root / 'out'
        with patch.object(exporter.subprocess, 'check_output', return_value='a' * 40) as resolve, \
                patch.object(exporter.subprocess, 'run', side_effect=self.archive(entries)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = exporter.export(ROOT, ref, out)
        self.assertEqual(resolve.call_args.args[0],
                         ['git', '-C', str(ROOT), 'rev-parse', '--verify', '--end-of-options', ref + '^{tree}'])
        return out, result

    def test_only_archive_members_are_written_and_no_history_is_created(self):
        out, result = self.export({'file.txt': b'committed', 'run.sh': b'#!/bin/sh\n'})
        self.assertFalse(result)
        self.assertEqual({p.name for p in out.iterdir()}, {'file.txt', 'run.sh'})
        self.assertEqual((out / 'file.txt').read_bytes(), b'committed')
        self.assertEqual((out / 'run.sh').stat().st_mode & 0o777, 0o755)

    def test_ref_is_resolved_and_pinned_before_archive(self):
        self.export({'file': b'ok'}, ref='release-tag')

    def test_personal_forms_and_regex_literals_keep_distinct_accounts(self):
        for source, target in exporter.REPLACEMENTS:
            with self.subTest(target=target):
                self.assertEqual(exporter.scrub(source), target)
                self.assertEqual(exporter.scrub(source.upper()), target if source != target else source.upper())
        first, second = exporter.REPLACEMENTS[:2]
        self.assertNotEqual(first[1], second[1])
        self.assertNotIn(first[1], ('a@example.com', 'one@example.com'))
        self.assertEqual(exporter.scrub('codex-' + first[0] + '-pro.json'), 'codex-' + first[1] + '-pro.json')
        for email, replacement in (first, second):
            for count in (1, 2):
                escaped = email.replace('.', '\\' * count + '.')
                self.assertEqual(exporter.scrub(escaped), replacement.replace('.', '\\' * count + '.'))

    def test_url_encoded_and_mixed_encoded_forms(self):
        for source, target in exporter.REPLACEMENTS:
            if source == target:
                continue
            for value in (quote(source, safe=''),
                          ''.join('%%%02x' % ord(c) for c in source),
                          ''.join('%%%02X' % ord(c) if i % 2 else c for i, c in enumerate(source))):
                self.assertEqual(exporter.decoded(exporter.scrub(value)), target)

    def test_private_identifiers_are_detected_case_insensitively_and_encoded(self):
        for source, target in exporter.REPLACEMENTS:
            if source == target:
                continue
            for value in (source.upper(), quote(source, safe=''), quote(quote(source, safe=''), safe='')):
                self.assertIn('personal-identifier', list(exporter.findings(value)))

    def test_generic_gmail_and_nonplaceholder_tailnet_addresses_are_detected(self):
        email = 'someone@' + 'gmail.com'
        address = '100.64.' + '0.99'
        for value in (email, quote(email, safe=''), email.replace('.', '\\.')):
            self.assertIn('gmail-address', list(exporter.findings(value)))
        for value in (address, address + '.', '100.127.' + '255.254'):
            self.assertIn('tailscale-address', list(exporter.findings(value)))
        for value in exporter.ALLOWED_IPS | {'100.128.0.1', '127.0.0.1', '100.63.255.255'}:
            self.assertFalse(list(exporter.findings(value)))

    def test_key_like_values_fail_without_being_printed(self):
        values = ['sk-' + 'x' * 24, 'sk-ant-' + 'x' * 32, 'eyJ' + 'a' * 24 + '.abc.def',
                  'ghp_' + 'a' * 36, 'AKIA' + 'A' * 16, 'api-keys: ["' + 'ab12' * 16 + '"]',
                  'secret-key: "' + 'cd34' * 16 + '"', 'Authorization: Bearer ' + 'e' * 40,
                  '-----BEGIN ' + 'PRIVATE KEY-----', 'api-keys: ["dummy-a", "' + 'ef56' * 16 + '"]',
                  '  - "' + '9a8b' * 16 + '"', '  - _' + 'Zq9' * 12, '  - -' + 'Zq9' * 12, 'api_key = "' + 'ab' * 20 + '"']
        for value in values:
            self.assertTrue(list(exporter.findings(value if value.startswith('  - ') else 'prefix' + value)))
        text = '\n'.join(values)
        (self.root / 'leaks.txt').write_text(text)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = exporter.main(['--check', str(self.root)])
        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue().count('leaks.txt:'), len(values))
        for value in values:
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(list(exporter.findings('TEST-SECRET-MUST-NOT-PRINT')))
        self.assertFalse(list(exporter.findings('  secret-key: "$ManagementKey"')))
        self.assertFalse(list(exporter.findings('  - "sandbox-' + 'k' * 32 + '"')))
        self.assertFalse(list(exporter.findings('-' * 59)))

    def test_scan_is_read_only_and_reports_original_file_line(self):
        path = self.root / 'file.txt'
        content = b'clean\n' + ('nobody@' + 'gmail.com').encode() + b'\n'
        path.write_bytes(content)
        before = path.stat()
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(exporter.scan(self.root))
        self.assertIn('file.txt:2: gmail-address', output.getvalue())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_binary_assets_are_preserved_and_embedded_ascii_is_scanned(self):
        font = b'\x00\xff\x01binary'
        out, result = self.export({'font.ttf': font})
        self.assertFalse(result)
        self.assertEqual((out / 'font.ttf').read_bytes(), font)
        (out / 'font.ttf').write_bytes(b'\xff\x00' + ('nobody@' + 'gmail.com').encode())
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(exporter.scan(out))

    def test_unicode_text_is_scrubbed_and_utf16_leaks_are_detected(self):
        source, target = exporter.REPLACEMENTS[0]
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32'):
            out, result = self.export({'unicode.txt': source.encode(encoding)}, out=self.root / encoding)
            self.assertFalse(result)
            self.assertEqual((out / 'unicode.txt').read_bytes().decode(encoding), target)
            (out / 'unicode.txt').write_bytes(('nobody@' + 'gmail.com').encode(encoding))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(exporter.scan(out))
        for encoding in ('utf-16-le', 'utf-16-be'):
            (self.root / 'bomless.txt').write_bytes(('nobody@' + 'gmail.com').encode(encoding))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(exporter.scan(self.root))

    def test_agent_instructions_are_excluded(self):
        out, result = self.export({name: b'private instructions' for name in (
            'AGENTS.md', 'nested/AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'CLAUDE.local.md', '.claude/settings.json',
            '.codex/config.toml', '.agents/skills/secret.md', '.git/config')})
        self.assertFalse(result)
        self.assertEqual(list(out.iterdir()), [])

    def test_github_files_are_excluded_and_gitignore_is_kept(self):
        out, result = self.export({'.github/workflows/x.yml': b'private workflow',
                                   '.gitignore': b'__pycache__/\n'})
        self.assertFalse(result)
        self.assertFalse((out / '.github').exists())
        self.assertEqual((out / '.gitignore').read_bytes(), b'__pycache__/\n')
        self.assertEqual(list(out.iterdir()), [out / '.gitignore'])

    def test_references_are_retained(self):
        out, result = self.export({'references/management-release.json': b'{}',
                                   'references/README.md': b'MIT attribution'})
        self.assertFalse(result)
        self.assertTrue((out / 'references/management-release.json').is_file())

    def test_nonempty_output_and_symlinks_are_refused_without_writes(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'keep').write_text('keep')
        with self.assertRaises(ValueError):
            self.export({}, out=out)
        self.assertEqual((out / 'keep').read_text(), 'keep')
        link = self.root / 'link'
        link.symlink_to(out, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.export({}, out=link)
        with self.assertRaises(ValueError):
            self.export({'link': None}, out=self.root / 'symlink-out')
        self.assertFalse((self.root / 'symlink-out').exists())

    def test_archive_traversal_and_collisions_are_refused(self):
        with self.assertRaises(ValueError):
            self.export({'../escape': b'bad'})
        self.assertFalse((self.root / 'escape').exists())
        source, target = exporter.REPLACEMENTS[0]
        if source != target:
            with self.assertRaises(ValueError):
                self.export({source: b'first', target: b'second'})

    def test_exporter_can_export_itself_without_carrying_private_map(self):
        script = 'scripts/export-public.py'
        out, result = self.export({script: (ROOT / script).read_bytes()})
        self.assertFalse(result)
        spec = importlib.util.spec_from_file_location('exported_exporter', out / script)
        module = importlib.util.module_from_spec(spec)
        # Keep this fixture archive-only: importing the exported script must not
        # add a pyc whose folded scanner constants look like private identifiers.
        with patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(module)
        self.assertTrue(all(source == target for source, target in module.REPLACEMENTS))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(module.scan(out))


if __name__ == '__main__':
    unittest.main()
