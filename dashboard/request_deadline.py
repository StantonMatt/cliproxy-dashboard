"""Account POST deadlines; mirrored and parity-tested in the proxy and browser."""
from contextlib import contextmanager
import time

ACCOUNT_MUTATION_SECONDS = 45
MANAGEMENT_CALL_SECONDS = 18
LOCK_WAIT_SECONDS = 10


class RequestDeadline:
    def __init__(self, seconds=ACCOUNT_MUTATION_SECONDS, clock=time.monotonic):
        self.clock = clock
        self.ends_at = clock() + seconds

    def timeout(self, maximum):
        remaining = self.ends_at - self.clock()
        if remaining <= 0:
            raise TimeoutError('Account request deadline exceeded')
        return min(maximum, remaining)

    def wrap(self, call, maximum=MANAGEMENT_CALL_SECONDS):
        def bounded(*args, **kwargs):
            kwargs['timeout'] = self.timeout(maximum)
            result = call(*args, **kwargs)
            self.timeout(maximum)
            return result
        return bounded

    @contextmanager
    def lock(self, lock):
        if not lock.acquire(timeout=self.timeout(LOCK_WAIT_SECONDS)):
            raise TimeoutError('Account request lock unavailable')
        try:
            self.timeout(LOCK_WAIT_SECONDS)
            yield
        finally:
            lock.release()
