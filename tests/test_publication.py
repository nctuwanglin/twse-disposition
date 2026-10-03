"""Publication failures and offline provenance; no real network or repo writes."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.repo = Path(__file__).resolve().parents[1]
        (self.root / 'data').mkdir()
        (self.root / 'index.html').write_bytes((self.repo / 'index.html').read_bytes())
        (self.root / 'data/stock_info.json').write_text('{}')
        for name, rel in [('REPO_ROOT', ''), ('HTML_PATH', 'index.html'),
                          ('STOCK_INFO_PATH', 'data/stock_info.json'),
                          ('PERF_STATS_PATH', 'data/perf_stats.json'),
                          ('LAST_COUNTS_PATH', 'data/last_counts.json')]:
            self.stack.enter_context(patch.object(u, name, self.root / rel))
        self.stack.enter_context(patch.dict(u.SOURCE_STATUS, {}, clear=True))
        self.stack.enter_context(patch.dict(u.CAREER_COUNTS, {}, clear=True))
        self.stack.enter_context(patch.object(u, 'LAST_TRADE_DATE', date(2026, 10, 2)))
        self.stack.enter_context(patch.object(u, 'fetch_json', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(u, 'autofill_stock_info', return_value=0))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def snap(self, sources=None):
        return {'schema': 1, 'date': '2026-10-02', 'source': 'live',
                'active': [], 'upcoming': [], 'released': [], 'notetrans': [],
                'quotes': {}, 'last_trade_date': '2026-10-02', 'prev_trading': '2026-10-01',
                'announcement_asof': '2026-10-01', 'taiex': None,
                'sources': sources or {'twse_notetrans': {'ok': True}, 'tpex_warning': {'ok': True}},
                'counts': {'active': 0, 'twse': 0, 'tpex': 0, 'second': 0, 'notetrans': 0}}

    def bundle(self, snap):
        path = self.root / 'fixture.json'
        path.write_text(json.dumps(snap))
        return u.bundle_from_snapshot(path)

    def test_snapshot_restores_failed_source_notice(self):
        bundle = self.bundle(self.snap({'tpex_quotes': {'ok': False, 'error': 'timeout'}}))
        out = u.render_and_publish(bundle)
        self.assertIn('部分更新', (out / 'index.html').read_text())
        manifest = json.loads((out / 'build-manifest.json').read_text())
        self.assertFalse(manifest['sources']['tpex_quotes']['ok'])
        self.assertEqual(manifest['announcement_asof'], '2026-10-01')

    def test_preview_refuses_pending_formal_recovery_without_writes(self):
        bundle = self.bundle(self.snap())
        before = (self.root / 'index.html').read_bytes()
        (self.root / '.dashboard-transaction.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, '復原'):
            u.render_and_publish(bundle)
        self.assertEqual((self.root / 'index.html').read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, '復原'):
            u.main(['update_dashboard.py', '--render-only'])

    def test_snapshot_nonempty_baseline_survives_render(self):
        snap = self.snap()
        snap['comparison_baseline'] = {'date': '2026-10-01', 'total_active': 2, 'twse': 2,
                                       'tpex': 0, 'second': 0, 'notetrans': 0}
        bundle = self.bundle(snap)
        self.assertEqual(bundle['comparison_baseline'], snap['comparison_baseline'])
        out = u.render_and_publish(bundle)
        self.assertIn('比較 2026-10-01', (out / 'index.html').read_text())

    def test_preview_is_not_deployable_and_health_is_offline(self):
        out = u.render_and_publish(self.bundle(self.snap()))
        with self.assertRaisesRegex(ValueError, '預覽'):
            u.artifact_bundle.validate(out)
        html = (out / 'index.html').read_text()
        self.assertIn('data-preview="true"', html)
        self.assertIn(u.artifact_bundle.digest(out / 'dispo.json'), html)
        self.assertTrue((out / 'assets/health.js').exists())
        self.assertLess(html.index('id="update-health"'), html.index('<!-- AUTO:CONTEXT_END -->'))

    def test_validator_rejects_rehashed_but_inconsistent_snapshot(self):
        out = u.render_and_publish(self.bundle(self.snap()))
        snap = json.loads((out / 'dispo.json').read_text())
        snap['counts']['active'] = 9
        (out / 'dispo.json').write_text(json.dumps(snap))
        manifest = json.loads((out / 'build-manifest.json').read_text())
        manifest['artifacts']['dispo.json'] = u.artifact_bundle.digest(out / 'dispo.json')
        (out / 'build-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, '資料契約'):
            u.artifact_bundle.validate(out, allow_preview=True)

    def test_validator_rejects_changed_health_script(self):
        out = u.render_and_publish(self.bundle(self.snap()))
        (out / 'assets/health.js').write_text('bad')
        with self.assertRaisesRegex(ValueError, 'hash'):
            u.artifact_bundle.validate(out, allow_preview=True)

    def test_healthy_empty_snapshot_does_not_inherit_old_failure(self):
        u.record_source('tpex_warning', ok=False, error='previous run')
        out = u.render_and_publish(self.bundle(self.snap()))
        self.assertNotIn('來源暫時無法取得', (out / 'index.html').read_text())

    def test_offline_preview_preserves_production_and_uses_snapshot_baseline(self):
        before = (self.root / 'index.html').read_bytes()
        (self.root / 'dispo.json').write_text('{"date":"2026-10-03"}')
        (self.root / 'data/last_counts.json').write_text('{"date":"2026-10-03","total_active":99}')
        out = u.render_and_publish(self.bundle(self.snap()))
        self.assertNotEqual(out, self.root)
        self.assertEqual((self.root / 'index.html').read_bytes(), before)
        self.assertEqual(json.loads((self.root / 'dispo.json').read_text())['date'], '2026-10-03')
        self.assertNotIn('比較 2026-10-03', (out / 'index.html').read_text())
        self.assertEqual(json.loads((out / 'dispo.json').read_text())['date'], '2026-10-02')

    def test_snapshot_write_failure_cannot_leave_new_html(self):
        bundle = self.bundle(self.snap())
        bundle['live'] = True
        before = (self.root / 'index.html').read_bytes()
        with patch.object(u, 'write_snapshot', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                u.render_and_publish(bundle)
        self.assertEqual((self.root / 'index.html').read_bytes(), before)
        self.assertFalse((self.root / 'data/last_counts.json').exists())

    def test_live_bundle_publishes_consistent_set(self):
        bundle = self.bundle(self.snap())
        bundle['live'] = True
        out = u.render_and_publish(bundle)
        self.assertEqual(out, self.root)
        manifest = u.artifact_bundle.validate(out)
        self.assertEqual(manifest['mode'], 'live')
        self.assertIn('data-preview="false"', (out / 'index.html').read_text())
        self.assertTrue((out / 'data/history/2026-10-02.json').exists())
        before = {p: (out / p).read_bytes() for p in manifest['artifacts']}
        u.render_and_publish(bundle)
        self.assertEqual(before, {p: (out / p).read_bytes() for p in manifest['artifacts']})

    def test_live_validator_requires_full_consistent_state_and_history(self):
        bundle = self.bundle(self.snap())
        bundle['live'] = True
        out = u.render_and_publish(bundle)
        state_path = out / 'data/last_counts.json'
        state = json.loads(state_path.read_text())
        for key, bad in [('twse', 999), ('announcement_asof', '2099-01-01'),
                         ('prev_day', {'date': '2099-01-01'})]:
            with self.subTest(key=key):
                state_path.write_text(json.dumps(dict(state, **{key: bad})))
                with self.assertRaises(ValueError):
                    u.artifact_bundle.validate(out)
        state_path.unlink()
        with self.assertRaises(ValueError):
            u.artifact_bundle.validate(out)
        state_path.write_text(json.dumps(state))
        (out / 'data/history/2026-10-02.json').write_text('{}')
        with self.assertRaises(ValueError):
            u.artifact_bundle.validate(out)

    def test_staging_failure_rolls_back_auxiliary_cache(self):
        bundle = self.bundle(self.snap())
        bundle['live'] = True
        def fill(info, codes):
            u.STOCK_INFO_PATH.write_text('{"changed":true}')
            return 1
        with patch.object(u, 'autofill_stock_info', side_effect=fill), \
                patch.object(u, 'write_build_manifest', side_effect=OSError('manifest failure')):
            with self.assertRaises(OSError):
                u.render_and_publish(bundle)
        self.assertEqual((self.root / 'data/stock_info.json').read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
