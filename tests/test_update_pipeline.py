import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class RunStatusTests(unittest.TestCase):
    def test_rejected_concurrent_run_cannot_write_status(self):
        p = importlib.import_module('run_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(p.u, 'REPO_ROOT', root), p.artifact_bundle.locked(root):
                with self.assertRaises(RuntimeError):
                    p.run(['update_dashboard.py'])
            self.assertFalse((root / 'update-status.json').exists())

    def test_record_executes_inside_publication_lock(self):
        p = importlib.import_module('run_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def record(*args, **kwargs):
                with self.assertRaises(RuntimeError):
                    with p.artifact_bundle.locked(root):
                        pass
            with patch.object(p.u, 'REPO_ROOT', root), patch.object(p.u, 'main'), \
                    patch.object(p, 'record', side_effect=record):
                p.run(['update_dashboard.py'])

    def test_failed_run_preserves_last_successful_check(self):
        p = importlib.import_module('run_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'update-status.json').write_text(json.dumps({'last_successful_check_at': '2026-10-01T10:00:00Z'}))
            with patch.object(p.u, 'REPO_ROOT', root), patch.object(p.u, 'main', side_effect=RuntimeError('failed')):
                with self.assertRaises(RuntimeError):
                    p.run(['update_dashboard.py'])
            status = json.loads((root / 'update-status.json').read_text())
            self.assertEqual(status['last_successful_check_at'], '2026-10-01T10:00:00Z')
            self.assertEqual(status['last_attempt']['outcome'], 'failure')

    def test_no_market_change_updates_check_but_not_change_time(self):
        p = importlib.import_module('run_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dispo.json').write_text('{"date":"2026-10-02","sources":{}}')
            p.record(root, outcome='success', now='2026-10-02T10:00:00Z')
            p.record(root, outcome='success', now='2026-10-02T13:00:00Z')
            status = json.loads((root / 'update-status.json').read_text())
            self.assertEqual(status['last_successful_check_at'], '2026-10-02T13:00:00Z')
            self.assertEqual(status['last_market_change_at'], '2026-10-02T10:00:00Z')

    def test_partial_run_does_not_claim_complete_sources(self):
        p = importlib.import_module('run_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dispo.json').write_text('{"date":"2026-10-02","sources":{"tpex_quotes":{"ok":false}}}')
            p.record(root, outcome='success', now='2026-10-02T10:00:00Z')
            status = json.loads((root / 'update-status.json').read_text())
            self.assertEqual(status['last_attempt']['outcome'], 'partial')
            self.assertEqual(status['degraded_sources'], ['tpex_quotes'])


class DeploymentTests(unittest.TestCase):
    def test_matching_manifest_but_stale_file_is_rejected(self):
        d = importlib.import_module('verify_deployment')
        expected = {'index.html': b'new', 'dispo.json': b'{}', 'build-manifest.json': b'{}'}
        def fetch(url):
            return b'old' if 'index.html' in url else b'{}'
        self.assertFalse(d.check('https://example.test/', expected, fetch=fetch))

    def test_http_error_never_counts_as_success(self):
        d = importlib.import_module('verify_deployment')
        with patch.object(d, 'fetch_bytes', side_effect=OSError('503')):
            self.assertFalse(d.check('https://example.test/', {'index.html': b''}))

    def test_status_only_change_is_verified(self):
        d = importlib.import_module('verify_deployment')
        expected = {'index.html': b'unchanged', 'update-status.json': b'new check'}
        self.assertFalse(d.check('https://example.test/', expected,
                                 fetch=lambda url: b'unchanged' if 'index.html' in url else b'old check'))

    def test_poll_stops_after_bound_and_then_can_succeed(self):
        d = importlib.import_module('verify_deployment')
        with patch.object(d, 'check', side_effect=[False, True]):
            self.assertTrue(d.poll('url', {}, attempts=2, interval=0))
        with patch.object(d, 'check', return_value=False):
            self.assertFalse(d.poll('url', {}, attempts=2, interval=0))
