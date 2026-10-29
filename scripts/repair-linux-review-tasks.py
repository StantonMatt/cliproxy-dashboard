#!/usr/bin/env python3
"""Repair the two confirmed legacy-provider tasks after Linux Codex closes."""
import fcntl
import importlib.util
from pathlib import Path


TASKS = (
    '00000000-0000-4000-8000-000000000002',
    '00000000-0000-4000-8000-000000000003',
)


def main():
    home = Path.home() / '.codex'
    path = Path(__file__).with_name('repair-linux-thread-provider.py')
    spec = importlib.util.spec_from_file_location('provider_repair', path)
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    # Preflight both tasks before mutating either. The API also enforces ownership.
    for task in TASKS:
        repair.record(home, task)
        lock = home / 'thread-writer-locks' / (task + '.lock')
        if lock.exists():
            with lock.open('rb') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SystemExit('Close Codex on Linux first. No tasks changed.')
                fcntl.flock(handle, fcntl.LOCK_UN)
    for task in TASKS:
        repair.migrate(home, task, 'personal_codex_gateway')
    print('DONE. Both tasks verified. Reopen Codex on Linux with the existing sign-in.')


if __name__ == '__main__':
    main()
