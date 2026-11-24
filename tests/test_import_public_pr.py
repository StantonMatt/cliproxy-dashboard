"""Offline import, cleanup, fail-closed patch and sync-attribution checks."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('import_public_pr', ROOT / 'scripts/import-public-pr.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def git(repo, *args, env=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE,
                                   text=True, env=env).strip()


class ImportPublicPRTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.private = self.root / 'private'
        self.public = self.root / 'public'
        for repo in (self.private, self.public):
            repo.mkdir()
            git(repo, 'init', '-b', 'main')
            git(repo, 'config', 'user.name', 'Owner')
            git(repo, 'config', 'user.email', '123+Owner@users.noreply.github.com')
            git(repo, 'config', 'commit.gpgsign', 'false')
        (self.private / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/export-public.py', self.private / 'scripts/export-public.py')
        self.exporter = importer.exporter_module()
        self.identifier, self.placeholder = self.exporter.REPLACEMENTS[0]
        (self.private / 'settings.txt').write_text('account=' + self.identifier + '\nunchanged\n')
        (self.private / 'clean.txt').write_text('before\n')
        git(self.private, 'add', '.')
        git(self.private, 'commit', '-m', 'Private base')
        self.base = git(self.private, 'rev-parse', 'HEAD')
        git(self.private, 'update-ref', 'refs/remotes/origin/main', self.base)
        # A real export provides scrubbed content, including a scrubbed exporter.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.exporter.export(self.private, 'HEAD', self.root / 'export'))
        shutil.copytree(self.root / 'export', self.public, dirs_exist_ok=True)
        git(self.public, 'add', '.')
        git(self.public, 'commit', '-m', 'Public sync', '-m', 'Source commit: ' + self.base[:7])
        git(self.public, 'checkout', '-b', 'contribution')
        self.worktree = self.root / 'imported'
        self.author = dict(os.environ, GIT_AUTHOR_NAME='Contributor', GIT_AUTHOR_EMAIL='contributor@example.org',
                           GIT_AUTHOR_DATE='2025-03-02T12:34:56+00:00')

    def contribute(self, path='clean.txt', content='after\n', message='Improve feature'):
        target = self.public / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        git(self.public, 'add', '.')
        git(self.public, 'commit', '-m', message, env=self.author)
        head = git(self.public, 'rev-parse', 'HEAD')
        git(self.public, 'update-ref', 'refs/pull/1/head', head)
        return head

    def invoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(importer, 'ROOT', self.private), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = importer.main(['1', '--public-url', self.public.as_uri(), '--worktree', str(self.worktree), *extra])
        self.addCleanup(self.cleanup_worktrees)
        return code, stdout.getvalue() + stderr.getvalue()

    def cleanup_worktrees(self):
        if self.worktree.exists():
            git(self.private, 'worktree', 'remove', '--force', str(self.worktree))

    def assert_no_refs(self):
        self.assertEqual(git(self.private, 'for-each-ref', 'refs/public-pr'), '')

    def test_clean_import_preserves_each_author_date_and_message(self):
        first = self.contribute(message='First change\n\nDetailed rationale.')
        second = self.contribute('other.txt', 'second\n', 'Second change')
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        commits = git(self.worktree, 'rev-list', '--reverse', self.base + '..HEAD').splitlines()
        self.assertEqual(len(commits), 2)
        for original, replayed in zip((first, second), commits):
            for fmt in ('%an', '%ae', '%aI'):
                self.assertEqual(git(self.public, 'show', '-s', '--format=' + fmt, original),
                                 git(self.worktree, 'show', '-s', '--format=' + fmt, replayed))
            original_message = git(self.public, 'show', '-s', '--format=%B', original)
            message = git(self.worktree, 'show', '-s', '--format=%B', replayed)
            self.assertTrue(message.startswith(original_message))
            self.assertIn(f'Imported-from: {importer.PUBLIC_REPO}#1 ({original[:7]})', message)
        self.assertEqual((self.worktree / 'settings.txt').read_text(), 'account=' + self.identifier + '\nunchanged\n')
        self.assertIn('Exporter check passed', output)
        self.assert_no_refs()
        self.assertEqual(git(self.private, 'rev-parse', 'HEAD'), self.base)

    def test_scrubbed_context_conflict_reports_file_and_retains_worktree(self):
        self.contribute('settings.txt', 'account=changed@example.org\nunchanged\n')
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('settings.txt', output)
        self.assertIn('conflict hunk at line', output)
        self.assertNotIn(self.identifier, output)
        self.assertTrue(self.worktree.exists())
        self.assert_no_refs()

    def test_added_placeholder_is_warned_with_line_number(self):
        self.contribute('new.txt', 'first\n' + self.placeholder + '\n')
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertIn('new.txt:2: added public placeholder', output)

    def test_ordinary_words_are_not_warned(self):
        self.contribute('new.txt', 'user = User()\nExample only\n<div class="account-bottom">\n')
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertNotIn('added public placeholder', output)

    def test_github_change_rejected_before_worktree_creation(self):
        self.contribute('.github/workflows/change.yml', 'name: untrusted\n')
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('--allow-sensitive-paths', output)
        self.assertIn('.github/workflows/change.yml', output)
        self.assertFalse(self.worktree.exists())
        self.assert_no_refs()

    def test_binary_change_requires_flag(self):
        self.contribute('image.bin', b'\x00\xffbinary')
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('image.bin (binary)', output)

    def test_binary_deletion_requires_flag(self):
        self.contribute('image.bin', b'\x00\xffbinary')
        (self.public / 'image.bin').unlink()
        git(self.public, 'commit', '-am', 'Remove image', env=self.author)
        self.assertIn('image.bin (binary)', importer.sensitive_changes(self.public, 'HEAD'))

    def test_existing_trailers_stay_in_one_block(self):
        self.contribute(message='Change\n\nCo-authored-by: Second <second@example.org>')
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        message = git(self.worktree, 'show', '-s', '--format=%B', 'HEAD')
        trailers = subprocess.run(['git', 'interpret-trailers', '--parse'], input=message, text=True,
                                  capture_output=True, check=True).stdout
        self.assertIn('Co-authored-by: Second <second@example.org>', trailers)
        self.assertIn('Imported-from: ', trailers)

    def test_binary_flag_cannot_be_bypassed_with_gitattributes(self):
        self.contribute('.gitattributes', '*.bin diff\n')
        self.contribute('image.bin', b'\x00\xffbinary')
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('image.bin (binary)', output)

    def test_added_line_beginning_with_plus_signs_is_still_scanned(self):
        self.contribute('new.txt', '++' + self.placeholder + '\n')
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertIn('new.txt:1: added public placeholder', output)

    def test_replacement_map_change_requires_flag(self):
        script = self.public / 'scripts/export-public.py'
        self.contribute('scripts/export-public.py', script.read_text().replace('REPLACEMENTS = (', "REPLACEMENTS = (('new', 'placeholder'),"))
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('REPLACEMENTS', output)

    def test_later_replacement_map_assignment_also_requires_flag(self):
        script = self.public / 'scripts/export-public.py'
        self.contribute('scripts/export-public.py', script.read_text() + "\nREPLACEMENTS += (('new', 'placeholder'),)\n")
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('REPLACEMENTS', output)

    def test_explicit_sensitive_flag_allows_binary_then_runs_exporter(self):
        self.contribute('image.bin', b'\x00\xffbinary')
        code, output = self.invoke('--allow-sensitive-paths')
        self.assertEqual(code, 0, output)
        self.assertIn('Exporter check passed', output)

    def test_dry_run_cleans_worktrees_branches_and_refs(self):
        self.contribute()
        before = git(self.private, 'worktree', 'list', '--porcelain')
        branches = git(self.private, 'branch', '--list')
        code, output = self.invoke('--dry-run')
        self.assertEqual(code, 0, output)
        self.assertEqual(git(self.private, 'worktree', 'list', '--porcelain'), before)
        self.assertEqual(git(self.private, 'branch', '--list'), branches)
        self.assertFalse(self.worktree.exists())
        self.assert_no_refs()

    def test_dry_run_conflict_also_cleans(self):
        self.contribute('settings.txt', 'account=conflict\n')
        before = git(self.private, 'worktree', 'list', '--porcelain')
        code, output = self.invoke('--dry-run')
        self.assertEqual(code, 1, output)
        self.assertEqual(git(self.private, 'worktree', 'list', '--porcelain'), before)
        self.assert_no_refs()

    def test_export_failure_does_not_report_success(self):
        self.contribute('new.txt', 'unknown@' + 'gmail.com\n')
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('Exporter publication check failed', output)
        self.assertIn('new.txt:1: gmail-address', output)
        self.assertNotIn('unknown@', output)

    def test_close_preview_makes_no_subprocess_or_gh_calls(self):
        with patch.object(importer.subprocess, 'run') as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(['close', '1', '--commit', 'abc1234']), 0)
        command.assert_not_called()

    def test_close_yes_rejects_commit_not_on_remote_main(self):
        self.contribute()
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        imported = git(self.worktree, 'rev-parse', 'HEAD')
        git(self.private, 'remote', 'add', 'origin', self.private.as_uri())
        stdout = io.StringIO()
        with patch.object(importer, 'ROOT', self.private), patch.object(importer, 'run', wraps=importer.run) as command, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            self.assertEqual(importer.main(['close', '1', '--commit', imported, '--yes']), 1)
        self.assertIn('not on current private main', stdout.getvalue())
        self.assertFalse(any(call.args[0][0] == 'gh' for call in command.call_args_list))
        self.assert_no_refs()

    def test_close_yes_can_resolve_a_new_remote_main_commit(self):
        remote = self.root / 'remote'
        git(self.root, 'clone', str(self.private), str(remote))
        git(remote, 'config', 'user.name', 'Owner')
        git(remote, 'config', 'user.email', 'owner@example.org')
        git(remote, 'commit', '--allow-empty', '-m', 'New merge on remote')
        final = git(remote, 'rev-parse', 'HEAD')
        git(self.private, 'remote', 'add', 'origin', remote.as_uri())
        original_run = importer.run
        def record(args, *positional, **kwargs):
            if args[0] == 'gh':
                return subprocess.CompletedProcess(args, 0, stdout=b'', stderr=b'')
            return original_run(args, *positional, **kwargs)
        with patch.object(importer, 'ROOT', self.private), patch.object(importer, 'run', side_effect=record), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(['close', '1', '--commit', final, '--yes']), 0)
        self.assert_no_refs()

    def test_close_yes_calls_gh_only_after_main_verification(self):
        git(self.private, 'remote', 'add', 'origin', self.private.as_uri())
        original_run = importer.run
        commands = []
        def record(args, *positional, **kwargs):
            commands.append(args)
            if args[0] == 'gh':
                return subprocess.CompletedProcess(args, 0, stdout=b'', stderr=b'')
            return original_run(args, *positional, **kwargs)
        with patch.object(importer, 'ROOT', self.private), patch.object(importer, 'run', side_effect=record), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(['close', '1', '--commit', self.base, '--yes']), 0)
        self.assertEqual(commands[-2][0:3], ['gh', 'pr', 'close'])
        self.assertIn('--comment', commands[-2])
        self.assert_no_refs()

    def test_sync_message_distinct_authors_owner_bots_and_safe_names(self):
        self.contribute()
        code, output = self.invoke()
        self.assertEqual(code, 0, output)
        # Additional private commits exercise deduplication and owner filtering.
        name = 'Name $(touch forbidden) `echo nope`'
        for author, email in [('Contributor', 'contributor@example.org'), ('Owner', '123+Owner@users.noreply.github.com'),
                              ('Robot[bot]', '123+robot[bot]@users.noreply.github.com'),
                              ('Other Owner', 'owner@example.org'), (name, 'safe@example.org')]:
            git(self.worktree, 'commit', '--allow-empty', '-m', 'Additional',
                env=dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email))
        final = git(self.worktree, 'rev-parse', 'HEAD')
        git(self.public, 'checkout', 'main')
        output = self.root / 'message'
        with patch.object(importer, 'ROOT', self.private), patch.dict(os.environ, SHAREABLE_COPY_OWNER_EMAILS='owner@example.org'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(['sync-message', '--source-sha', final, '--public-repo', str(self.public),
                                            '--owner-login', 'Owner', '--output', str(output)]), 0)
        text = output.read_text()
        self.assertEqual(text.count('Co-authored-by: Contributor <contributor@example.org>'), 1)
        self.assertIn(f'Co-authored-by: {name} <safe@example.org>', text)
        self.assertNotIn('Co-authored-by: Owner', text)
        self.assertNotIn('Co-authored-by: Other Owner', text)
        self.assertNotIn('Robot', text)

    def test_sync_message_falls_back_to_single_current_commit(self):
        self.contribute()
        final = git(self.private, 'rev-parse', 'HEAD')
        output = self.root / 'message'
        # PR HEAD has no Source commit trailer: fall back without crediting history.
        with patch.object(importer, 'ROOT', self.private), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(['sync-message', '--source-sha', final, '--public-repo', str(self.public),
                                            '--owner-login', 'Owner', '--output', str(output)]), 0)
        self.assertNotIn('Co-authored-by:', output.read_text())


if __name__ == '__main__':
    unittest.main()
