#!/usr/bin/env python3
"""Select Go, provisioning a checksum-pinned official toolchain when needed."""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

VERSION = '1.26.10'
# https://go.dev/dl/?mode=json&include=all (go1.26.10.linux-amd64.tar.gz)
SHA256 = 'ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5'
URL = f'https://go.dev/dl/go{VERSION}.linux-amd64.tar.gz'
SOURCE = Path(__file__).resolve().parent


def version_tuple(text):
    match = re.search(r'\bgo(\d+)\.(\d+)(?:\.(\d+))?\b', text)
    if not match:
        raise RuntimeError('Unable to read Go version')
    return tuple(int(part or 0) for part in match.groups())


def go_version(binary):
    try:
        result = subprocess.run([str(binary), 'version'], capture_output=True, text=True,
                                check=True, timeout=15, env={**os.environ, 'GOTOOLCHAIN': 'local'})
        return version_tuple(result.stdout)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Unable to run selected Go toolchain') from None


def provision(cache):
    if os.geteuid() == 0:
        raise RuntimeError('Go download must run as the checkout owner, never root')
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / VERSION
    # Serialize downloads/extraction, publishing only a fully verified toolchain.
    with (cache / (VERSION + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = target / '.verified-sha256'
        binary = target / 'go/bin/go'
        if marker.is_file() and marker.read_text().strip() == SHA256 and binary.is_file():
            if go_version(binary) == version_tuple('go' + VERSION):
                return binary
        if target.exists():
            raise RuntimeError('Cached Go toolchain is incomplete; remove its version directory and retry')
        with tempfile.TemporaryDirectory(prefix='.go-download-', dir=cache) as directory:
            stage = Path(directory)
            archive = stage / 'go.tar.gz'
            print(f'Downloading Go {VERSION} from go.dev', file=sys.stderr, flush=True)
            digest = hashlib.sha256()
            try:
                with urlopen(URL, timeout=60) as response, archive.open('wb') as output:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
            except (OSError, TimeoutError):
                raise RuntimeError('Go download failed; check HTTPS access to go.dev and retry') from None
            if digest.hexdigest() != SHA256:
                raise RuntimeError('Go archive SHA-256 mismatch; refusing extraction')
            print(f'Verified Go {VERSION} SHA-256 {SHA256}', file=sys.stderr, flush=True)
            with tarfile.open(archive, 'r:gz') as bundle:
                bundle.extractall(stage, filter='data')
            archive.unlink()
            if go_version(stage / 'go/bin/go') != version_tuple('go' + VERSION):
                raise RuntimeError('Downloaded Go version does not match the pinned release')
            (stage / '.verified-sha256').write_text(SHA256 + '\n')
            stage.rename(target)
        return binary


def select_go():
    requirement = re.search(r'^go\s+(\S+)', (SOURCE / 'go.mod').read_text(), re.MULTILINE)
    minimum = version_tuple('go' + requirement[1])
    explicit = os.environ.get('GO')
    if explicit:
        # An explicit override is used as supplied; go build enforces go.mod's
        # minimum with automatic toolchain downloads disabled by build.sh.
        return explicit
    installed = shutil.which('go')
    if installed:
        if go_version(installed) >= minimum:
            return installed
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return str(provision(cache / 'personal-codex-router/go'))


def main():
    compiler = shlex.split(os.environ.get('CC', 'gcc'))
    if not compiler or not shutil.which(compiler[0]):
        raise RuntimeError('C compiler missing; install it with: sudo apt install gcc libc6-dev')
    print(select_go())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else 'Go toolchain setup failed; retry the build',
              file=sys.stderr)
        sys.exit(1)
