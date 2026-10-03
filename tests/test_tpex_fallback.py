import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u


class TPExFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = {'date': '2026-10-02', 'active': [], 'upcoming': []}
        self.rows = [{'Date': '1151002', 'SecuritiesCompanyCode': '4174',
                      'CompanyName': '浩鼎', 'DispositionPeriod': '1151005~1151012',
                      'DispositionReasons': '連續3個營業日',
                      'DisposalCondition': '約每2分鐘撮合一次'}]
        for p in [patch.object(u, 'REPO_ROOT', self.root),
                  patch.object(u, 'LAST_TRADE_DATE', date(2026, 10, 2)),
                  patch.dict(u.SOURCE_STATUS, {}, clear=True),
                  patch.object(u.time, 'sleep')]:
            p.start()
            self.addCleanup(p.stop)

    def run_fallback(self, rows=None):
        (self.root / 'dispo.json').write_text(json.dumps(self.snapshot))
        payload = self.rows if rows is None else rows
        def transport(req, timeout):
            if '/www/' in req.full_url:
                raise HTTPError(req.full_url, 520, 'origin error', {}, None)
            self.assertIn('/openapi/v1/tpex_disposal_information', req.full_url)
            return io.BytesIO(json.dumps(payload).encode())
        with patch.object(u.urllib.request, 'urlopen', side_effect=transport):
            return u.fetch_and_normalize_tpex('test')

    def test_520_retries_before_recovery(self):
        for code in (520, 521, 522, 523, 524):
            with self.subTest(code=code):
                error = HTTPError('https://test', code, 'origin', {}, None)
                with patch.object(u.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'[]')]):
                    self.assertEqual(u.fetch_json('https://test'), [])

    def test_520_stops_after_three_attempts(self):
        error = HTTPError('https://test', 520, 'origin', {}, None)
        with patch.object(u.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(HTTPError):
                u.fetch_json('https://test')
        self.assertEqual(request.call_count, 3)

    def test_future_backup_is_not_success(self):
        self.rows[0]['Date'] = '1151005'
        self.assertEqual(self.run_fallback(), [])
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_missing_baseline_rejected(self):
        rows = [{'code': '4174', 'ann_date': date(2026, 10, 2)}]
        with self.assertRaises(FileNotFoundError):
            u._validate_tpex_backup(rows)

    def test_primary_success_never_fetches_backup(self):
        payload = {'tables': [{'fields': ['證券代號', '公布日期', '處置起訖時間'], 'data': []}]}
        with patch.object(u.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            self.assertEqual(u.fetch_and_normalize_tpex('test'), [])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(u.source_ok('tpex_disposal'))

    def test_fallback_normalizes_and_records_origin(self):
        result = self.run_fallback()
        self.assertEqual([r['code'] for r in result], ['4174'])
        self.assertEqual(result[0]['period_start'], date(2026, 10, 5))
        self.assertTrue(u.source_ok('tpex_disposal'))
        self.assertEqual(u.SOURCE_STATUS['tpex_disposal']['endpoint'], u.TPEX_DISPOSAL_BACKUP)
        self.assertIn('520', u.SOURCE_STATUS['tpex_disposal']['fallback_reason'])

    def test_empty_backup_is_not_success(self):
        self.assertEqual(self.run_fallback([]), [])
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_stale_backup_is_not_success(self):
        self.rows[0]['Date'] = '1151001'
        self.assertEqual(self.run_fallback(), [])
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_missing_existing_event_is_rejected(self):
        self.snapshot['active'] = [{'exchange': 'TPEx', 'code': '2221',
            'ann_date': '2026-09-30', 'period_start': '2026-10-01', 'period_end': '2026-10-12'}]
        self.assertEqual(self.run_fallback(), [])
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_malformed_backup_is_rejected(self):
        self.assertEqual(self.run_fallback([{'Date': '1151002'}]), [])
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_both_endpoints_fail(self):
        with patch.object(u.urllib.request, 'urlopen', side_effect=TimeoutError('down')):
            self.assertEqual(u.fetch_and_normalize_tpex('test'), [])
        self.assertFalse(u.source_ok('tpex_disposal'))
