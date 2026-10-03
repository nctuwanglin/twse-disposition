"""Isolated end-to-end rendering: never writes repository data or uses network."""
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u


class PipelineTests(unittest.TestCase):
    def render(self, calendar=True, round_trip=False):
        template = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            (root / 'data').mkdir()
            (root / 'index.html').write_text(template)
            (root / 'data/stock_info.json').write_text('{}')
            paths = {'REPO_ROOT': root, 'HTML_PATH': root / 'index.html',
                     'STOCK_INFO_PATH': root / 'data/stock_info.json',
                     'PERF_STATS_PATH': root / 'data/perf_stats.json',
                     'LAST_COUNTS_PATH': root / 'data/last_counts.json'}
            for name, value in paths.items():
                stack.enter_context(patch.object(u, name, value))
            stack.enter_context(patch.object(u, 'LAST_TRADE_DATE', date(2026, 10, 2)))
            stack.enter_context(patch.dict(u.CAREER_COUNTS, {}, clear=True))
            stack.enter_context(patch.dict(u.SOURCE_STATUS, {}, clear=True))
            stack.enter_context(patch.object(u, 'fetch_json', side_effect=AssertionError('unexpected network')))
            stack.enter_context(patch.object(u, 'fetch_twse_stock_history', return_value=[]))
            stack.enter_context(patch.object(u, 'autofill_stock_info', return_value=0))
            stock = {'code': '2330', 'name': '測試', 'exchange': 'TWSE',
                     'ann_date': date(2026, 9, 11), 'period_start': date(2026, 9, 14),
                     'period_end': date(2026, 10, 1), 'auction': '2分', 'disp_count': 1}
            days = [date(2026, 8, 3) + timedelta(days=i) for i in range(61)
                    if (date(2026, 8, 3) + timedelta(days=i)).weekday() < 5]
            bundle = {'live': True, 'today': date(2026, 10, 2),
                      'state': {'date': '2026-09-25', 'total_active': 2, 'second': 0},
                      'stock_quotes': {}, 'taiex': None,
                      'all_rows': [stock, dict(stock, period_start=date(2026, 9, 28),
                                               period_end=date(2026, 10, 8)),
                                   dict(stock, code='2317'),
                                   dict(stock, code='2303', period_start=date(2026, 8, 24),
                                        period_end=date(2026, 8, 28))],
                      'ann_asof': date(2026, 9, 11), 'notetrans_twse': [],
                      'notetrans_tpex': [], 'nt_thresholds': {},
                      'trading_days': days if calendar else [], 'prev_trading': None,
                      'release_stats': None}
            with contextlib.redirect_stdout(io.StringIO()):
                u.render_and_publish(bundle)
            html = (root / 'index.html').read_text()
            snap = json.loads((root / 'dispo.json').read_text())
            if round_trip:
                with contextlib.redirect_stdout(io.StringIO()):
                    preview = u.render_and_publish(u.bundle_from_snapshot())
                html = (preview / 'index.html').read_text()
            manifest = json.loads((root / 'build-manifest.json').read_text())
            for rel, digest in manifest['artifacts'].items():
                self.assertEqual(digest, hashlib.sha256((root / rel).read_bytes()).hexdigest())
            return html, snap

    def test_real_pipeline_never_calls_still_active_stock_released(self):
        html, snap = self.render()
        tab3 = html.split('<!-- AUTO:TAB3_CONTENT_START -->')[1].split('<!-- AUTO:TAB3_CONTENT_END -->')[0]
        self.assertNotIn('data-code="2330"', tab3)
        self.assertIn('data-code="2317"', tab3)
        self.assertRegex(html, r'今日出關</span>\s*<span[^>]*>1 檔')
        self.assertEqual(snap['counts']['active'], 1)
        self.assertEqual(snap['prev_trading'], '2026-10-01')
        self.assertIn('比較 2026-09-25', html)
        self.assertIn('近 30 個交易日', tab3)

    def test_unknown_calendar_does_not_claim_zero_released(self):
        html, snap = self.render(calendar=False)
        self.assertRegex(html, r'今日出關</span>\s*<span[^>]*>未確認')
        self.assertIsNone(snap['prev_trading'])
        self.assertIn('交易日曆未確認', html)

    def test_live_to_offline_preserves_calendar_release_membership(self):
        html, snap = self.render(round_trip=True)
        tab3 = html.split('<!-- AUTO:TAB3_CONTENT_START -->')[1].split('<!-- AUTO:TAB3_CONTENT_END -->')[0]
        self.assertIn('data-code="2303"', tab3)
        self.assertIn('近 30 個交易日', tab3)


if __name__ == '__main__':
    unittest.main()
