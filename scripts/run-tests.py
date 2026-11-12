#!/usr/bin/env python3
"""Run every Python/Node test, continuing after failures. Invoke through heavy."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def checks(root=ROOT):
    yield 'Python unittest discovery', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], {}, 1800
    for path in sorted((root / 'tests').glob('*.cjs')):
        if path.name == 'test_billing_dates.cjs':
            for zone in ('America/Fortaleza', 'America/New_York'):
                yield path.name + ' TZ=' + zone, ['node', str(path)], {'TZ': zone}, 120
        else:
            yield path.name, ['node', str(path)], {}, 120
    for name in ('dashboard/static/app.js', 'scripts/router-preview.mjs'):
        yield name + ' syntax', ['node', '--check', str(root / name)], {}, 120


def run(root=ROOT):
    results = []
    for name, command, environment, timeout in checks(root):
        print('\nRUN ' + name, flush=True)
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, cwd=root, env={**os.environ, **environment}, start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Test-created servers/workers inherit this dedicated process
                # group. A timeout must not leave them running into tomorrow.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                print('Timed out after ' + str(timeout) + ' seconds.', flush=True)
                code = 124
        except OSError:
            print('Could not start check.', flush=True)
            code = 127
        results.append((name, code, time.monotonic() - started))
    print('\nTEST SUMMARY', flush=True)
    for name, code, elapsed in results:
        print(f'{"PASS" if code == 0 else "FAIL"} {name} (exit {code}, {elapsed:.1f}s)', flush=True)
    failed = sum(code != 0 for _, code, _ in results)
    print(f'{len(results) - failed} passed; {failed} failed; {len(results)} checks.', flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(run())
