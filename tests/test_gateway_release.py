import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from test_operations import ops, probe

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'scripts/check-gateway-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'gateway-release.json'
        self.now = 1800000000
        self.gateway = dict(online=True, checked_at=dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat(),
                            gateway_version='8.0.47', accounts=[])

    def response(self, payload, etag='"release-1"'):
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {'ETag': etag}
        return response

    def test_anonymous_bounded_request_ignores_drafts_prereleases_and_sorts_versions(self):
        payload = [dict(tag_name=tag, draft=draft, prerelease=pre) for tag, draft, pre in (
            ('v9.0.0', True, False), ('v8.1.0', False, True), ('v8.0.99-rc1', False, False),
            ('v8.0.9', False, False), ('v8.0.48', False, False), ('v8.0.47', False, False))]
        with patch.object(release, 'urlopen', return_value=self.response(payload)) as fetch:
            self.assertTrue(release.check(self.path, now=self.now))
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, release.API)
        self.assertEqual(fetch.call_args.kwargs, {'timeout': 10})
        self.assertNotIn('Authorization', request.headers)
        state = json.loads(self.path.read_text())
        self.assertEqual(state['latest_version'], '8.0.48')
        self.assertEqual(state['status'], 'ok')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def seed(self):
        release.save(self.path, dict(version=1, latest_version='8.0.48', etag='"cached"', checked_at='previous'))

    def test_rate_limits_retain_success_and_honor_backoff_without_retrying(self):
        for code in (403, 429):
            self.seed()
            error = HTTPError(release.API, code, 'PRIVATE RESPONSE',
                              {'Retry-After': '7200', 'X-RateLimit-Reset': str(self.now + 5400)}, None)
            with patch.object(release, 'urlopen', side_effect=error) as fetch:
                self.assertFalse(release.check(self.path, now=self.now))
                self.assertFalse(release.check(self.path, now=self.now + 100))
                self.assertEqual(fetch.call_count, 1)
            state = json.loads(self.path.read_text())
            self.assertEqual(state['retry_after'], self.now + 7200)
            self.assertEqual(state['latest_version'], '8.0.48')
            self.assertEqual(state['checked_at'], 'previous')
            self.assertEqual(state['error'], 'rate_limited')
            self.assertNotIn('PRIVATE', self.path.read_text())

    def test_etag_not_modified_refreshes_observation(self):
        self.seed()
        with patch.object(release, 'urlopen', side_effect=HTTPError(release.API, 304, '', {}, None)) as fetch:
            self.assertTrue(release.check(self.path, now=self.now))
        self.assertEqual(fetch.call_args.args[0].get_header('If-none-match'), '"cached"')
        self.assertEqual(json.loads(self.path.read_text())['latest_version'], '8.0.48')

    def test_timeout_invalid_payload_and_http_errors_preserve_previous_observation(self):
        for error in (TimeoutError('PRIVATE'), URLError('PRIVATE'), HTTPError(release.API, 500, 'PRIVATE', {}, None)):
            self.seed()
            with patch.object(release, 'urlopen', side_effect=error):
                self.assertFalse(release.check(self.path, now=self.now))
            self.assertEqual(json.loads(self.path.read_text())['latest_version'], '8.0.48')
            self.assertNotIn('PRIVATE', self.path.read_text())
        for payload in ({}, [], [dict(tag_name='v9.0.0', draft=False, prerelease=True)]):
            self.seed()
            with patch.object(release, 'urlopen', return_value=self.response(payload)):
                self.assertFalse(release.check(self.path, now=self.now))
            self.assertEqual(json.loads(self.path.read_text())['latest_version'], '8.0.48')

    def test_one_normal_notice_per_version_survives_failure_outage_restart_and_upgrade(self):
        self.seed()
        store = ops.NoticeStore(self.root / 'notices.json', self.root)
        store.update(self.gateway, self.now)
        notice, = store.view()['items']
        self.assertEqual(notice['message'], 'Gateway 8.0.48 is available (running 8.0.47)')
        self.assertEqual(notice['urgency'], 'normal')
        self.assertEqual(notice['kind'], 'gateway_release')
        identity = notice['id']
        self.path.write_text('{broken')
        store.update(dict(self.gateway, online=False, gateway_version=None), self.now + 5)
        self.assertEqual(store.view()['items'][0]['id'], identity)
        self.seed()
        store = ops.NoticeStore(self.root / 'notices.json', self.root)
        store.update(self.gateway, self.now + 10)
        self.assertEqual(store.view()['items'][0]['id'], identity)
        upgraded = dict(self.gateway, gateway_version='v8.0.48')
        store.update(upgraded, self.now + 15)
        self.assertFalse(store.view()['items'])
        # Keep version identities beyond the ordinary 90-day notice expiry.
        later = self.now + 100 * 86400
        upgraded['checked_at'] = dt.datetime.fromtimestamp(later, dt.timezone.utc).isoformat()
        store.update(upgraded, later)
        self.assertIn('gateway:release:8.0.48', store.rows)
        release.save(self.path, dict(latest_version='8.0.49'))
        store.update(upgraded, later)
        notice, = store.view()['items']
        self.assertIn('8.0.49', notice['message'])
        self.assertNotEqual(notice['id'], identity)

    def test_unknown_running_versions_do_not_invent_or_clear_release_notices(self):
        self.seed()
        for version in (None, 'dev', '8.0.48-rc1', '9' * 5000 + '.0.1'):
            gateway = dict(self.gateway, gateway_version=version)
            self.assertFalse(ops.candidates(gateway, self.now, release=json.loads(self.path.read_text())))

    def test_desktop_release_delivery_is_deduplicated_after_90_days(self):
        notice = dict(id='version-48', email='Gateway', kind='gateway_release', urgency='normal',
                      message='Gateway 8.0.48 is available (running 8.0.47)')
        response = lambda: io.StringIO(json.dumps(dict(notifications=dict(available=True, items=[notice]))))
        with patch.object(probe, 'STATE', self.root), patch.object(probe, 'urlopen', side_effect=lambda *a, **k: response()), \
                patch.object(probe.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000)), \
                patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as send:
            probe.deliver()
            ledger = json.loads((self.root / 'desktop-delivery.json').read_text())
            self.assertIn('gateway-release:version-48', ledger)
            ledger['gateway-release:version-48'] = '2000-01-01T00:00:00+00:00'
            (self.root / 'desktop-delivery.json').write_text(json.dumps(ledger))
            probe.deliver()
            self.assertEqual(send.call_count, 1)
            self.assertIn('gateway-release:version-48', json.loads((self.root / 'desktop-delivery.json').read_text()))


if __name__ == '__main__':
    unittest.main()
