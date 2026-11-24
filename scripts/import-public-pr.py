#!/usr/bin/env python3
"""Replay public contributions onto private main without publishing anything.

Usage: python3 scripts/import-public-pr.py NUMBER [--dry-run]
       python3 scripts/import-public-pr.py NUMBER --branch NAME --worktree PATH
       python3 scripts/import-public-pr.py close NUMBER --commit SHA [--yes]

Imports preserve authors and messages, add Imported-from trailers, and check the
result with the trusted local exporter. Conflicts leave a worktree for manual
resolution; dry runs always remove their worktree, branch and temporary refs.
The close command previews by default and is never invoked by an import.
"""

import argparse
import ast
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPO = 'example-user/cliproxy-dashboard'
PUBLIC_URL = 'https://github.com/' + PUBLIC_REPO + '.git'
TIMEOUT = 120
SOURCE = re.compile(r'^Source commit: ([0-9a-f]{7,40})\s*$', re.M)
GENERIC_TARGETS = {'user', 'User', 'Example'}


class ImportFailure(Exception):
    pass


def run(args, repo=None, *, check=True, data=None, env=None):
    result = subprocess.run(args, cwd=repo, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=TIMEOUT)
    if check and result.returncode:
        # Never echo untrusted patch contents or authentication diagnostics.
        raise ImportFailure(f'{args[0]} {args[1]} failed (exit {result.returncode})')
    return result


def git(repo, *args, check=True, data=None, env=None):
    return run(['git', *map(str, args)], repo, check=check, data=data, env=env).stdout.decode('utf-8', 'replace').strip()


def exporter_module():
    spec = importlib.util.spec_from_file_location('trusted_exporter', ROOT / 'scripts/export-public.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def display(value):
    return '[redacted]' if list(exporter_module().findings(value)) else value.replace('\n', '?').replace('\r', '?')


def source_commit(repo, public_message):
    match = SOURCE.search(public_message)
    if match:
        result = run(['git', 'rev-parse', '--verify', '--end-of-options', match[1] + '^{commit}'], repo, check=False)
        if result.returncode == 0:
            return result.stdout.decode().strip()
    return None


def replacement_assignment(repo, ref):
    result = run(['git', 'show', f'{ref}:scripts/export-public.py'], repo, check=False)
    if result.returncode:
        return None
    try:
        tree = ast.parse(result.stdout)
        assignments = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            if any(isinstance(part, ast.Name) and part.id == 'REPLACEMENTS'
                   for target in targets for part in ast.walk(target)):
                assignments.append(ast.dump(node, include_attributes=False))
        return assignments
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise ImportFailure('Cannot validate exporter REPLACEMENTS; manual review required')


def changed_files(repo, before, after):
    # --no-renames ensures both sides of renames receive policy checks.
    output = run(['git', 'diff', '--name-only', '--no-renames', '-z', before, after], repo).stdout
    return [p.decode('utf-8', 'surrogateescape') for p in output.split(b'\0') if p]


def sensitive_changes(repo, commit):
    parent = commit + '^'
    paths = changed_files(repo, parent, commit)
    issues = [p for p in paths if '.github' in Path(p).parts]
    if 'scripts/export-public.py' in paths and replacement_assignment(repo, parent) != replacement_assignment(repo, commit):
        issues.append('scripts/export-public.py (REPLACEMENTS)')
    # Inspect blobs rather than trusting .gitattributes/numstat: a PR can mark
    # a binary file as text, but that must not bypass explicit confirmation.
    exporter = exporter_module()
    # Both sides, so deleting a binary or replacing it with text also needs confirmation.
    for path in paths:
        for side in (parent, commit):
            blob = run(['git', 'show', f'{side}:{path}'], repo, check=False)
            if blob.returncode == 0 and exporter.text_content(blob.stdout)[0] is None:
                issues.append(path + ' (binary)')
    return sorted(set(issues))


def placeholder_warnings(repo, before, after):
    exporter = exporter_module()
    # Ordinary words such as "user" appear in most code; only distinctive placeholders are worth a warning.
    # Whole tokens only, so "account-b" doesn't match "account-bottom".
    patterns = [re.compile(r'(?<![\w-])(?:' + exporter.replacement_pattern(target).pattern + r')(?![\w-])', re.I)
                for source, target in exporter.REPLACEMENTS
                if source != target and target not in GENERIC_TARGETS]
    warnings = []
    for path in changed_files(repo, before, after):
        patch = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-color', '--unified=0', before, after, '--', path)
        line = 0
        in_hunk = False
        for text in patch.splitlines():
            header = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', text)
            if header:
                line = int(header[1])
                in_hunk = True
            elif in_hunk and text.startswith('+'):
                found = next((m for p in patterns if (m := p.search(exporter.decoded(text[1:])))), None)
                if found:
                    warnings.append(f'{display(path)}:{line}: added public placeholder {display(found[0])}; review private value')
                line += 1
            elif text.startswith(' '):
                line += 1
    return warnings


def metadata(number, public_url):
    if public_url != PUBLIC_URL or not shutil.which('gh'):
        return {}
    try:
        result = run(['gh', 'pr', 'view', str(number), '--repo', PUBLIC_REPO,
                      '--json', 'title,author,url,baseRefName'], check=False)
    except (OSError, subprocess.TimeoutExpired):
        print('PR metadata unavailable; using public main and fetched Git authors.')
        return {}
    if result.returncode:
        print('PR metadata unavailable; using public main and fetched Git authors.')
        return {}
    return json.loads(result.stdout)


def import_pr(args):
    repo = Path(git(ROOT, 'rev-parse', '--show-toplevel'))
    main = git(repo, 'rev-parse', '--verify', 'origin/main^{commit}')
    info = metadata(args.number, args.public_url)
    if info:
        print(f'PR #{args.number}: {display(info.get("title", ""))} ({display(info.get("url", ""))})')
    base_name = info.get('baseRefName', 'main')
    if run(['git', 'check-ref-format', 'refs/heads/' + base_name], check=False).returncode:
        raise ImportFailure('Invalid public base branch')
    token = uuid.uuid4().hex
    namespace = f'refs/public-pr/{args.number}/{token}'
    head_ref, base_ref = namespace + '/head', namespace + '/base'
    scratch = Path.home() / '.cache/agent-scratch/personal-codex-router'
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='public-pr-', dir=scratch) if args.dry_run else None
    branch = 'public-pr-dry-' + token if args.dry_run else (args.branch or f'public-pr-{args.number}')
    worktree = Path(temporary.name) / 'worktree' if temporary else (args.worktree or Path.home() / f'.worktrees/personal-codex-router/public-pr-{args.number}').expanduser().absolute()
    created = False
    try:
        if run(['git', 'check-ref-format', '--branch', branch], repo, check=False).returncode or branch.startswith('-'):
            raise ImportFailure('Invalid branch name')
        git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', '--', args.public_url,
            f'+pull/{args.number}/head:{head_ref}', f'+refs/heads/{base_name}:{base_ref}')
        base = git(repo, 'merge-base', head_ref, base_ref)
        source = source_commit(repo, git(repo, 'log', '-1', '--format=%B', base))
        if source and run(['git', 'merge-base', '--is-ancestor', source, main], repo, check=False).returncode == 0:
            print(f'Public base {base[:7]}: private source {source[:7]}; private main is {git(repo, "rev-list", "--count", source + ".." + main)} commits ahead.')
        else:
            print('Warning: public base has no resolvable ancestor Source commit; private distance unknown.')
        commits = git(repo, 'rev-list', '--reverse', '--topo-order', base + '..' + head_ref).splitlines()
        if not commits:
            raise ImportFailure('PR has no commits to import')
        for commit in commits:
            if len(git(repo, 'rev-list', '--parents', '-n', '1', commit).split()) != 2:
                raise ImportFailure(f'{commit[:7]} is a merge/root commit; manual replay required')
            issues = sensitive_changes(repo, commit)
            if issues and not args.allow_sensitive_paths:
                raise ImportFailure('Sensitive changes require --allow-sensitive-paths:\n' + '\n'.join('  ' + display(p) for p in issues))
            if issues:
                print('Warning: sensitive changes allowed: ' + ', '.join(display(p) for p in issues))
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(repo, '-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '-b', branch, worktree, main)
        created = True
        print(f'Worktree: {worktree}' + (' (temporary dry run)' if args.dry_run else ''))
        for commit in commits:
            result = run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'rerere.enabled=false', 'cherry-pick', '--no-commit', commit], worktree, check=False)
            if result.returncode:
                raw = run(['git', 'diff', '--name-only', '--diff-filter=U', '-z'], worktree).stdout
                conflicts = [p.decode('utf-8', 'surrogateescape') for p in raw.split(b'\0') if p]
                if not conflicts:
                    conflicts = changed_files(repo, commit + '^', commit)
                print('Conflict: patch could not apply; scrubbed context is never guessed.', file=sys.stderr)
                for path in conflicts:
                    print('  ' + display(path), file=sys.stderr)
                    # Hunk markers give locations without printing private context.
                    target = worktree / path
                    if target.is_file() and not target.is_symlink():
                        for number, line in enumerate(target.read_text(errors='replace').splitlines(), 1):
                            if line.startswith('<<<<<<<'):
                                print(f'    conflict hunk at line {number}', file=sys.stderr)
                raise ImportFailure(f'Replay stopped at {commit[:7]}; resolve the index and commit manually, then replay remaining commits with Imported-from trailers')
            fields = git(repo, 'show', '-s', '--format=%an%x00%ae%x00%aI%x00%B', commit).split('\0', 3)
            name, email, date, message = fields
            # interpret-trailers joins an existing trailer block (Co-authored-by, Signed-off-by) instead of splitting it.
            message = git(worktree, 'interpret-trailers', '--trailer',
                          f'Imported-from: {PUBLIC_REPO}#{args.number} ({commit[:7]})', data=message.encode())
            env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date)
            git(worktree, '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '--no-gpg-sign', '-F', '-', data=message.encode(), env=env)
            print(f'Imported {commit[:7]}: {display(name)} <{display(email)}>')
        final = git(worktree, 'rev-parse', 'HEAD')
        files = changed_files(repo, main, final)
        print(f'Imported {len(commits)} commits; {len(files)} files changed:')
        for path in files:
            print('  ' + display(path))
        warnings = placeholder_warnings(repo, main, final)
        for warning in warnings:
            print('Warning: ' + warning)
        print(f'Placeholder warnings: {len(warnings)}')
        # Execute the trusted local exporter, never PR-supplied Python code.
        with tempfile.TemporaryDirectory(prefix='public-pr-export-', dir=scratch) as directory:
            out = Path(directory) / 'export'
            for mode in ('--out', '--check'):
                command = [sys.executable, str(ROOT / 'scripts/export-public.py'), mode, str(out)]
                if mode == '--out':
                    command += ['--ref', final]
                result = run(command, repo, check=False)
                print(result.stdout.decode('utf-8', 'replace').strip())
                if result.stderr:
                    print(result.stderr.decode('utf-8', 'replace').strip(), file=sys.stderr)
                if result.returncode:
                    raise ImportFailure('Exporter publication check failed; inspect the imported worktree')
        print('Exporter check passed.')
        print('Next: run focused tests, review the branch, push it and open a private PR.')
        return 0
    finally:
        if args.dry_run:
            if created:
                git(repo, 'worktree', 'remove', '--force', worktree)
                git(repo, 'branch', '-D', branch)
            temporary.cleanup()
            print('Dry run cleaned up: temporary worktree and branch removed.')
        elif created:
            print(f'Worktree retained: {worktree}')
        for ref in (head_ref, base_ref):
            git(repo, 'update-ref', '-d', ref)


def close_pr(args):
    message = args.message or ('This contribution has landed on private main and will appear in the next public sync. '
                               'The original contributor is kept as author, with credit in the sync commit.')
    if not args.yes:
        print(f'Preview only: comment on and close https://github.com/{PUBLIC_REPO}/pull/{args.number}')
        print('Verify private main contains commit ' + display(args.commit))
        print(display(message))
        print('Run again with --yes to verify main, post the comment and close the PR.')
        return 0
    if not re.fullmatch(r'[0-9a-fA-F]{7,40}', args.commit):
        raise ImportFailure('--commit must be a hexadecimal commit ID')
    # A disposable ref verifies current remote main rather than stale origin/main.
    ref = 'refs/public-pr/close-' + uuid.uuid4().hex
    try:
        git(ROOT, 'fetch', '--no-tags', '--no-write-fetch-head', 'origin', f'refs/heads/main:{ref}')
        commit = git(ROOT, 'rev-parse', '--verify', '--end-of-options', args.commit + '^{commit}')
        if run(['git', 'merge-base', '--is-ancestor', commit, ref], ROOT, check=False).returncode:
            raise ImportFailure('Commit is not on current private main; public PR was not changed')
        run(['gh', 'pr', 'close', str(args.number), '--repo', PUBLIC_REPO, '--comment', message], ROOT)
        print(f'Closed public PR #{args.number}.')
    finally:
        git(ROOT, 'update-ref', '-d', ref)
    return 0


def sync_message(args):
    """Write the public bot commit message; author strings never enter a shell."""
    final = git(ROOT, 'rev-parse', '--verify', '--end-of-options', args.source_sha + '^{commit}')
    previous = source_commit(ROOT, git(args.public_repo, 'log', '-1', '--format=%B'))
    if previous and run(['git', 'merge-base', '--is-ancestor', previous, final], ROOT, check=False).returncode == 0:
        revision = previous + '..' + final
    else:
        revision = final
        print('Previous source missing/unresolvable; crediting only the current source commit.')
    command = ['git', 'log', '--format=%an%x00%ae%x00', revision]
    if revision == final:
        command.insert(2, '-1')
    records = run(command, ROOT).stdout.decode().split('\0')
    owner = args.owner_login.casefold()
    excluded = {v.strip().casefold() for v in re.split(r'[,\s]+', os.environ.get('SHAREABLE_COPY_OWNER_EMAILS', '')) if v.strip()}
    authors = set()
    for index in range(0, len(records) - 1, 2):
        name, email = records[index].strip(), records[index + 1].strip()
        local, _, domain = email.casefold().rpartition('@')
        if (email.casefold() in excluded or (domain == 'users.noreply.github.com' and local.split('+')[-1] == owner)
                or '[bot]' in name.casefold() or '[bot]' in email.casefold()):
            continue
        # Prevent malformed author metadata from injecting additional trailers.
        if not name or any(c in name + email for c in '\r\n<>\x00') or not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
            raise ImportFailure('Invalid author metadata in source range')
        authors.add((name, email))
    message = f'Sync with the private repo ({datetime.now(timezone.utc):%Y-%m-%d})\n\nSource commit: {final[:7]}\n'
    if authors:
        message += '\n' + ''.join(f'Co-authored-by: {name} <{email}>\n' for name, email in sorted(authors))
    args.output.write_text(message)
    print(f'Sync message: {len(authors)} contributor trailers.')
    return 0


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('PR number must be positive')
    return number


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    if argv and argv[0] == 'close':
        parser.add_argument('command', choices=['close'])
        parser.add_argument('number', type=positive)
        parser.add_argument('--commit', required=True)
        parser.add_argument('--message')
        parser.add_argument('--yes', action='store_true')
        handler = close_pr
    elif argv and argv[0] == 'sync-message':
        parser.add_argument('command', choices=['sync-message'])
        parser.add_argument('--source-sha', required=True)
        parser.add_argument('--public-repo', required=True, type=Path)
        parser.add_argument('--owner-login', required=True)
        parser.add_argument('--output', required=True, type=Path)
        handler = sync_message
    else:
        parser.add_argument('number', type=positive)
        parser.add_argument('--branch')
        parser.add_argument('--worktree', type=Path)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--public-url', default=os.environ.get('PUBLIC_PR_URL', PUBLIC_URL), help='override public Git URL (also supports offline file URLs)')
        parser.add_argument('--allow-sensitive-paths', action='store_true', help='explicitly permit .github, replacement-map or binary changes')
        handler = import_pr
    args = parser.parse_args(argv)
    try:
        return handler(args)
    except (ImportFailure, OSError, subprocess.TimeoutExpired, ValueError) as error:
        # OSError/ValueError may carry untrusted paths or data; only our own
        # messages are safe to display verbatim (and contain no patch contents).
        print('Import failed: ' + (str(error) if isinstance(error, ImportFailure) else 'invalid data, filesystem operation or command timeout'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
