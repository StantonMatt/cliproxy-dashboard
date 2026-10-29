#!/usr/bin/env python3
"""Export a committed Git tree without history or private host identifiers.

Usage: python3 scripts/export-public.py --out /path/to/empty/export [--ref HEAD]
       python3 scripts/export-public.py --check /path/to/export

No Git repository is initialized, and no network or installed gateway is used.
Review the exported documentation before publishing: dated operational evidence
and subscription details are retained. A failed scan leaves the output available
for inspection; remove it or select a new empty directory before retrying.
"""

import argparse
import codecs
from collections import Counter
import ipaddress
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote, unquote


# Longest/more specific forms precede their components. Distinct account labels
# avoid the a@example.com and one@example.com identities already used by tests.
# In the exported script these become identity pairs, which the scanner ignores:
# the public copy never needs to carry the private replacement inputs forward.
REPLACEMENTS = (
    ('account-a@example.com', 'account-a@example.com'),
    ('account-b@example.com', 'account-b@example.com'),
    ('example-index-a', 'example-index-a'),
    ('example-index-b', 'example-index-b'),
    ('00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000001'),
    ('00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000002'),
    ('00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000003'),
    ('account-a', 'account-a'),
    ('account-b', 'account-b'),
    ('tailnet-example.ts.net', 'tailnet-example.ts.net'),
    ('100.64.0.10', '100.64.0.10'),
    ('100.64.0.11', '100.64.0.11'),
    ('100.64.0.12', '100.64.0.12'),
    ('/home/user', '/home/user'),
    ('/Users/user', '/Users/user'),
    ('user', 'user'),
    ('macbook', 'macbook'),
    ('Example User', 'Example User'),
    ('example-user', 'example-user'),
    ('User', 'User'),
    ('Example', 'Example'),
)

ALLOWED_IPS = {'100.64.0.10', '100.64.0.11', '100.64.0.12', '100.100.100.100'}
TAILSCALE_NET = ipaddress.ip_network(('100.64.0.' + '0/10'))
IP_PATTERN = re.compile(r'(?<!\d)100\.\d{1,3}\.\d{1,3}\.\d{1,3}(?!\d)')
GMAIL_PATTERN = re.compile(r'[\w.!#$%&\x27*+/=?^`{|}~-]+@' + r'gmail\.com\b', re.I)
# Require credential-length payloads, rather than flagging prose about prefixes.
# Obvious fixture values (dummy-, sandbox-, test-, example-) are not credentials.
FIXTURE = r'(?:dummy|sandbox|test|example|fake)[-_]'
SECRET_PATTERNS = (
    ('key-like', re.compile(r'sk-(?:ant-)?[A-Za-z0-9_-]{16,}')),
    ('jwt-like', re.compile(r'eyJ[A-Za-z0-9_-]{12,}(?:\.[A-Za-z0-9_-]+){0,2}')),
    ('github-token-like', re.compile(r'ghp_[A-Za-z0-9]{20,}')),
    ('aws-key-like', re.compile(r'AKIA[A-Z0-9]{16}')),
    ('private-key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('bearer-token', re.compile(r'Bearer\s+(?!' + FIXTURE + r')[A-Za-z0-9._~+/-]{24,}=*', re.I)),
    # Populated credential fields, e.g. a generated gateway key in access.api-keys.
    ('credential-field', re.compile(
        r'(?:api[-_]keys?|secret[-_]key|management[-_]key|client[-_]secret|password|access_token|refresh_token|id_token)'
        r'["\x27]?\s*(?::\s*\[?\s*["\x27]?|=\s*\[?\s*["\x27])(?!' + FIXTURE + r')[A-Za-z0-9_+/=.-]{24,}', re.I)),
    # Later entries of an inline list: api-keys: ["first", "second"].
    ('credential-field', re.compile(
        r'api[-_]keys?["\x27]?\s*[:=]\s*\[[^\]]*,\s*["\x27]?(?!' + FIXTURE + r')[A-Za-z0-9_+/=.-]{24,}', re.I)),
    # YAML block-list entries (api-keys:\n  - "key") arrive one line at a time.
    ('credential-list-item', re.compile(
        r'^\s*-\s*["\x27]?(?!' + FIXTURE + r')(?![-_=]+["\x27]?\s*(?:#.*)?$)[A-Za-z0-9_+/=-]{32,}["\x27]?\s*(?:#.*)?$', re.I)),
)


def decoded(text):
    """Normalize percent encoding (including nested encoding) and regex dots."""
    while True:
        new = unquote(text)
        if new == text:
            return re.sub(r'\\+\.', '.', text)
        text = new


def replacement_pattern(source):
    # Accept raw or percent-encoded characters independently. Regex literals
    # escape email/domain dots in several JS fixtures; preserve those escapes.
    parts = []
    for char in source:
        encoded = f'%{ord(char):02x}'
        literal = re.escape(char)
        if char == '.':
            literal = r'\\*\.'
        parts.append(f'(?:{literal}|{encoded})')
    return re.compile(''.join(parts), re.I)


def scrub(text):
    for source, target in REPLACEMENTS:
        if source == target:
            continue

        def replace(match):
            value = match.group()
            if '%' in value:
                # Preserve URL semantics without decoding unrelated file text.
                return quote(target, safe='')
            escapes = re.search(r'(\\+)\.', value)
            return target.replace('.', escapes.group(1) + '.') if escapes else target

        text = replacement_pattern(source).sub(replace, text)
    return text


def instruction_file(path):
    parts = PurePosixPath(path).parts
    return (any(part.lower() in {'.claude', '.codex', '.agents', '.git'} for part in parts)
            or parts[-1].lower() in {'agents.md', 'agents.override.md', 'claude.md', 'claude.local.md'})


def text_content(data):
    """Recognize Unicode text; keep binary assets byte-for-byte intact."""
    encoding = 'utf-8'
    for bom, candidate in ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
                           (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'),
                           (codecs.BOM_UTF8, 'utf-8-sig')):
        if data.startswith(bom):
            encoding = candidate
            break
    else:
        if b'\x00' in data:
            return None, None
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        return None, None


def findings(text):
    value = decoded(text)
    lowered = value.lower()
    if any(source.lower() in lowered for source, target in REPLACEMENTS if source != target):
        yield 'personal-identifier'
    if GMAIL_PATTERN.search(value):
        yield 'gmail-address'
    for match in IP_PATTERN.finditer(value):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if address in TAILSCALE_NET and str(address) not in ALLOWED_IPS:
            yield 'tailscale-address'
            break
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(value):
            yield label


def scan(directory):
    """Report locations and categories only, never the matching private value."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('check directory must be a real directory')
    counts = Counter()
    files = 0
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory).as_posix()
        # A malicious name can itself contain a credential: redact names too.
        name_issues = list(findings(relative))
        display = '[redacted path]' if name_issues else relative
        for label in name_issues:
            print(f'{display}:1: {label} in path', file=sys.stderr)
            counts[label] += 1
        if path.is_symlink():
            print(f'{display}:1: symlink is not allowed', file=sys.stderr)
            counts['symlink'] += 1
            continue
        if not path.is_file():
            continue
        files += 1
        # Scan binary bytes as well, so embedded ASCII identifiers do not hide
        # behind UTF-8 decode failures. Latin-1 maps every byte to a character.
        data = path.read_bytes()
        content, _ = text_content(data)
        contents = [content] if content is not None else [data.decode('latin-1')]
        if content is None and b'\x00' in data:
            # BOM-less UTF-16 text would otherwise hide behind interleaved NULs.
            contents += [data.decode(e, errors='ignore') for e in ('utf-16-le', 'utf-16-be')]
        for content in contents:
            for number, line in enumerate(content.splitlines(), 1):
                for label in findings(line):
                    print(f'{display}:{number}: {label}', file=sys.stderr)
                    counts[label] += 1
    print(f'Scan: {files} files; {sum(counts.values())} findings'
          + (f' ({", ".join(f"{k}={v}" for k, v in sorted(counts.items()))})' if counts else ''))
    return counts


def export(repo, ref, out):
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise ValueError('output must not exist or must be an empty real directory')
    # Resolve before creating output, and pin to a tree so refs cannot change
    # between validation and archive. --end-of-options prevents option injection.
    tree = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', '--verify', '--end-of-options', ref + '^{tree}'],
        stderr=subprocess.PIPE, text=True).strip()
    changed = 0
    excluded = 0
    # Spool outside /tmp: archived font assets and future imports may be large.
    scratch = Path.home() / '.cache/agent-scratch/personal-codex-router'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=scratch) as archive:
        subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', tree],
                       stdout=archive, stderr=subprocess.PIPE, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode='r:') as entries:
            # Validate everything before writing anything; never follow archived
            # symlinks or unpack submodules/special files into the output.
            members = entries.getmembers()
            for entry in members:
                name = PurePosixPath(entry.name)
                if name.is_absolute() or '..' in name.parts or '\\' in entry.name:
                    raise ValueError('unsafe archive path')
                if not (entry.isfile() or entry.isdir()):
                    raise ValueError('archive contains unsupported symlink or special file')
            out.mkdir(parents=True, exist_ok=True)
            for entry in members:
                if instruction_file(entry.name):
                    excluded += int(entry.isfile())
                    continue
                target = out / scrub(entry.name)
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError('scrubbed archive paths collide')
                data = entries.extractfile(entry).read()
                content, encoding = text_content(data)
                if content is not None:
                    updated = scrub(content).encode(encoding)
                    changed += updated != data
                    data = updated
                target.write_bytes(data)
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)
    print(f'Export: committed tree {tree}; {changed} scrubbed files; '
          f'{excluded} agent instruction files excluded')
    return scan(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--out', type=Path, help='new or empty export directory')
    mode.add_argument('--check', type=Path, help='scan an existing directory without modifying it')
    parser.add_argument('--ref', default='HEAD', help='committed ref to export (default: HEAD)')
    args = parser.parse_args(argv)
    try:
        if args.check is not None:
            if args.ref != 'HEAD':
                parser.error('--ref is only valid with --out')
            counts = scan(args.check)
        else:
            repo = Path(__file__).resolve().parents[1]
            counts = export(repo, args.ref, args.out.resolve() if not args.out.is_symlink() else args.out)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError):
        # Subprocess output or arbitrary filenames may contain private values.
        print('Export/check failed: invalid path, ref, archive or filesystem operation.', file=sys.stderr)
        return 2
    return 1 if counts else 0


if __name__ == '__main__':
    sys.exit(main())
