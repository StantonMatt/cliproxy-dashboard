"""Slow-drip POST bodies must not extend the absolute request deadline."""
import socket
import sys
import threading
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import server as app


class BodyDeadlineTests(TestCase):
    def test_all_body_reading_posts_close_slow_drip_connections(self):
        paths = ('/api/claude/accounts/routing', '/api/accounts/signin/start',
                 '/api/accounts/signin/cancel', '/api/accounts/routing', '/api/fast-mode',
                 '/api/claude/resets/consume', '/api/claude/resets/policy',
                 '/api/routing', '/api/claude-routing', '/api/routing-all', '/api/routing-link',
                  '/api/resets/policy', '/api/resets/consume')
        original = app.RequestDeadline
        for path in paths:
            with self.subTest(path=path):
                local, client = socket.socketpair()
                handler = object.__new__(app.Handler)
                handler.path = path
                handler.connection = local
                handler.rfile = local.makefile('rb')
                handler.headers = {'Host': '127.0.0.1:8320', 'X-Router-Request': '1',
                                   'Content-Type': 'application/json', 'Content-Length': '40'}
                handler.server = Mock()
                handler.send = Mock()
                done, stopped = threading.Event(), threading.Event()
                errors = []
                def request():
                    try: handler.do_POST()
                    except Exception as exc: errors.append(exc)
                    finally: done.set()
                def drip():
                    for _ in range(40):
                        if stopped.wait(.025): return
                        try: client.sendall(b' ')
                        except OSError: return
                workers = [threading.Thread(target=fn) for fn in (request, drip)]
                with patch.object(app, 'RequestDeadline', side_effect=lambda: original(seconds=.12)):
                    started = time.monotonic()
                    try:
                        for worker in workers: worker.start()
                        self.assertTrue(done.wait(.5), 'slow drip extended the absolute deadline')
                        self.assertLess(time.monotonic()-started, .5)
                        self.assertTrue(handler.close_connection)
                        client.settimeout(.1)
                        self.assertEqual(client.recv(1), b'')
                        self.assertEqual(handler.server.state.mock_calls, [])
                    finally:
                        stopped.set()
                        local.shutdown(socket.SHUT_RDWR)
                        client.close()
                        for worker in workers: worker.join(1)
                        handler.rfile.close()
                        local.close()
                self.assertEqual(errors, [])
