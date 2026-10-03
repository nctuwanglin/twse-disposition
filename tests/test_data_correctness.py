"""Phase 1 regressions. All network and output paths are isolated."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.status = patch.dict(u.SOURCE_STATUS, {}, clear=True)
        self.status.start()
        self.addCleanup(self.status.stop)

    def test_bad_twse_date_is_not_valid_empty(self):
        with self.assertRaises(ValueError):
            u.normalize_twse_rows([{'Code': '2330', 'Date': 'bad',
                                    'DispositionPeriod': 'bad'}])
        self.assertFalse(u.source_ok('twse_punish'))

    def test_missing_code_column_rejected(self):
        with self.assertRaises(ValueError):
            u.normalize_twse_rows([{'SecurityCode': '2330'}])

    def test_mixed_good_and_bad_rows_rejected(self):
        good = {'Code': '2330', 'Date': '1151001',
                'DispositionPeriod': '1151002～1151008'}
        with self.assertRaises(ValueError):
            u.normalize_twse_rows([good, dict(good, Date='broken')])

    def test_valid_empty_twse_is_healthy(self):
        self.assertEqual(u.normalize_twse_rows([]), [])
        self.assertTrue(u.source_ok('twse_punish'))

    def test_unknown_tpex_short_row_rejected(self):
        with self.assertRaises(ValueError):
            u._tpex_rows_to_dicts(['證券代號', '證券名稱'], [['2330']])

    def test_tpex_disposal_changed_columns_rejected(self):
        payload = {'tables': [{'fields': ['代碼'], 'data': [['2330']]}]}
        with patch.object(u, 'fetch_json', return_value=payload):
            with self.assertRaises(ValueError):
                u.fetch_and_normalize_tpex('test')
        self.assertFalse(u.source_ok('tpex_disposal'))

    def test_optional_attention_bad_schema_degrades(self):
        with patch.object(u, 'fetch_json', return_value=[{'SecurityCode': '2330'}]):
            self.assertEqual(u.fetch_twse_notetrans(), [])
        self.assertFalse(u.source_ok('twse_notetrans'))

    def test_optional_tpex_bad_schema_does_not_crash(self):
        with patch.object(u, 'fetch_json', return_value={'tables': []}):
            self.assertEqual(u.fetch_tpex_warning(), [])
        self.assertFalse(u.source_ok('tpex_warning'))

    def test_tpex_non_scalar_required_cells_rejected(self):
        fields = ['證券代號', '公布日期', '處置起訖時間']
        for value in ([], {}, None):
            with self.subTest(value=value), patch.object(u, 'fetch_json', return_value={
                'tables': [{'fields': fields, 'data': [[value, '1151001', '1151002～1151008']]}]
            }):
                with self.assertRaises(ValueError):
                    u.fetch_and_normalize_tpex('test')
                self.assertFalse(u.source_ok('tpex_disposal'))


class QuoteDateTests(unittest.TestCase):
    def gather(self, quotes, previous='2026-10-02'):
        def empty_punish(*args, **kwargs):
            u.record_source('twse_punish', ok=True)
            return []
        def empty_tpex(*args):
            u.record_source('tpex_disposal', ok=True)
            return []
        with patch.object(u, 'LAST_TRADE_DATE', None), patch.dict(u.SOURCE_STATUS, {}, clear=True), patch.multiple(
            u, fetch_twse_stock_quotes=lambda: quotes,
            fetch_tpex_quotes=lambda: {}, _load_state=lambda: {'date': previous},
            safe_fetch_json=empty_punish, fetch_and_normalize_tpex=empty_tpex,
            fetch_taiex=lambda: None, fetch_recent_trading_days=lambda *a, **k: [],
            fetch_twse_notetrans=lambda: [], fetch_tpex_warning=lambda: []
        ), contextlib.redirect_stdout(io.StringIO()):
            return u.gather_live()

    def test_older_quote_day_rejected(self):
        with self.assertRaisesRegex(SystemExit, '倒退'):
            self.gather({'2330': {'date': '20261001'}})

    def test_mixed_quote_dates_rejected(self):
        with self.assertRaisesRegex(SystemExit, '日期'):
            self.gather({'2330': {'date': '20261002'}, '2317': {'date': '20261001'}})

    def test_same_day_is_still_processed(self):
        self.assertEqual(self.gather({'2330': {'date': '20261002'}})['today'],
                         date(2026, 10, 2))


def event(code='2330', start=date(2026, 9, 14), end=date(2026, 10, 1)):
    return {'code': code, 'name': '測試', 'exchange': 'TWSE',
            'ann_date': start - timedelta(days=1), 'period_start': start,
            'period_end': end, 'disp_count': 1, 'auction': '2分'}


class ReleaseTests(unittest.TestCase):
    def test_active_overlap_not_released_but_event_retained(self):
        rows = [event(), event(start=date(2026, 9, 28), end=date(2026, 10, 8))]
        active, _, expired = u.group_into_batches(rows, date(2026, 10, 2))
        self.assertEqual(u.released_securities(expired, active), {})
        self.assertEqual(len(expired[date(2026, 10, 1)]['stocks']), 1)

    def test_only_latest_expiry_per_security(self):
        rows = [event(end=date(2026, 9, 18)),
                event(start=date(2026, 9, 21), end=date(2026, 10, 1))]
        active, _, expired = u.group_into_batches(rows, date(2026, 10, 2))
        shown = u.released_securities(expired, active)
        self.assertEqual(list(shown), [date(2026, 10, 1)])

    def test_adjacent_disposition_is_not_released(self):
        rows = [event(), event(start=date(2026, 10, 2), end=date(2026, 10, 8))]
        active, _, expired = u.group_into_batches(rows, date(2026, 10, 2))
        self.assertFalse(u.released_securities(expired, active))

    def test_thirty_sessions_includes_more_than_thirty_calendar_days(self):
        days = [date(2026, 8, 3) + timedelta(days=i) for i in range(61)
                if (date(2026, 8, 3) + timedelta(days=i)).weekday() < 5]
        _, _, expired = u.group_into_batches(
            [event(start=date(2026, 8, 17), end=date(2026, 8, 28))],
            date(2026, 10, 2), trading_days=days)
        self.assertIn(date(2026, 8, 28), expired)


class CalendarTests(unittest.TestCase):
    def test_unknown_calendar_does_not_guess_from_execution_date(self):
        self.assertIsNone(u.prev_trading_day(date(2026, 10, 1), {'date': '2026-09-25'}, []))

    def test_cross_year_fetches_explicit_months(self):
        def fetch(url, *a, **kw):
            if 'date=20260101' in url:
                return {'stat': 'OK', 'data': [['115/01/02', 'x']]}
            if 'date=20251201' in url:
                return {'stat': 'OK', 'data': [['114/12/31', 'x']]}
            if 'date=20251101' in url:
                return {'stat': 'OK', 'data': [['114/11/28', 'x']]}
            raise AssertionError(url)
        with patch.object(u, 'fetch_json', side_effect=fetch), patch.dict(u.SOURCE_STATUS, {}, clear=True):
            days = u.fetch_recent_trading_days(date(2026, 1, 2))
        self.assertEqual(days, [date(2025, 11, 28), date(2025, 12, 31), date(2026, 1, 2)])
        self.assertEqual(u.prev_trading_day(date(2026, 1, 2), None, days), date(2025, 12, 31))

    def test_partial_month_failure_does_not_create_false_calendar(self):
        def fetch(url, *a, **kw):
            if 'date=20260101' in url:
                return {'stat': 'OK', 'data': [['115/01/02', 'x']]}
            return {'stat': 'error', 'data': []}
        with patch.object(u, 'fetch_json', side_effect=fetch), patch.dict(u.SOURCE_STATUS, {}, clear=True):
            self.assertEqual(u.fetch_recent_trading_days(date(2026, 1, 2)), [])
            self.assertFalse(u.source_ok('trading_calendar'))

    def test_kpi_shows_actual_comparison_date(self):
        html = u.render_stats(10, 2, 1, 5, 5, None,
                              deltas={'total_active': 2}, baseline_date='2026-09-25')
        self.assertIn('2026-09-25', html)
        self.assertNotIn('較昨日', html)

    def test_calendar_must_reach_quote_date(self):
        def fetch(url, *args, **kwargs):
            for month, roc in [('20261001', '115/10/01'), ('20260901', '115/09/30'),
                               ('20260801', '115/08/31')]:
                if 'date=' + month in url:
                    return {'stat': 'OK', 'data': [[roc, 'x']]}
            raise AssertionError(url)
        with patch.object(u, 'fetch_json', side_effect=fetch), patch.dict(u.SOURCE_STATUS, {}, clear=True):
            self.assertEqual(u.fetch_recent_trading_days(date(2026, 10, 5)), [])
            self.assertFalse(u.source_ok('trading_calendar'))


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'perf.json'
        p = patch.object(u, 'PERF_STATS_PATH', self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_total_first_failure_remains_retryable_outside_window(self):
        e = event(start=date(2026, 9, 14), end=date(2026, 9, 18))
        groups = {e['period_end']: {'stocks': [e]}}
        with patch.object(u, 'fetch_twse_stock_history', return_value=[]):
            result = u.update_perf_stats(groups, date(2026, 9, 21))
        key = '2330:2026-09-14:2026-09-18'
        self.assertIn(key, result)
        self.assertIn(key, json.loads(self.path.read_text()))
        days = [date(2026, 9, 11) + timedelta(days=i) for i in range(15)
                if (date(2026, 9, 11) + timedelta(days=i)).weekday() < 5]
        hist = [{'date': d, 'close': 100 + i, 'vol_k': 100} for i, d in enumerate(days)]
        with patch.object(u, 'fetch_twse_stock_history', return_value=hist):
            result = u.update_perf_stats({}, date(2026, 11, 2))
        self.assertIsNotNone(result[key]['after5_pct'])

    def test_overlapping_disposition_excluded_from_after5_summary(self):
        first = event(start=date(2026, 9, 14), end=date(2026, 9, 18))
        second = event(start=date(2026, 9, 17), end=date(2026, 9, 25))
        days = [date(2026, 9, 11) + timedelta(days=i) for i in range(15)
                if (date(2026, 9, 11) + timedelta(days=i)).weekday() < 5]
        hist = [{'date': d, 'close': 100 + i, 'vol_k': 100} for i, d in enumerate(days)]
        with patch.object(u, 'fetch_twse_stock_history', return_value=hist):
            result = u.update_perf_stats({first['period_end']: {'stocks': [first]}},
                                        date(2026, 9, 25), disposition_rows=[first, second])
        summary = u.perf_stats_summary(result)
        self.assertIsNone(summary['after5'])
        self.assertEqual(summary['during']['n'], 1)

    def test_sixty_day_average_excludes_today_not_one_prior_day(self):
        hist = [{'date': date(2026, 7, 1) + timedelta(days=i),
                 'close': 100, 'vol_k': i + 1} for i in range(61)]
        hist[-1]['vol_k'] = 999999
        t = u.calculate_attention_thresholds(hist)
        self.assertEqual(t['vol_days'], 60)
        self.assertEqual(t['vol_avg'], 30.5)

    def seed_complete(self):
        key = '2330:2026-09-14:2026-09-18'
        e = {'code': '2330', 'name': '測試', 'exchange': 'TWSE',
             'period_start': '2026-09-14', 'period_end': '2026-09-18',
             'entry_close': 100, 'exit_close': 110, 'during_pct': 10,
             'after5_pct': 5, 'after5_date': '2026-09-25', 'after5_overlap': False}
        self.path.write_text(json.dumps({key: e}))
        return key, e

    def test_malformed_cached_event_does_not_abort_valid_event(self):
        key, e = self.seed_complete()
        self.path.write_text(json.dumps({key: e, 'bad': {'period_start': 'bad'}}))
        with patch.object(u, 'fetch_twse_stock_history', return_value=[]):
            result = u.update_perf_stats({}, date(2026, 10, 2))
        self.assertEqual(result[key]['after5_pct'], 5)

    def test_complete_cache_reconciles_new_overlap_without_pending(self):
        key, _ = self.seed_complete()
        with patch.object(u, 'fetch_twse_stock_history', side_effect=AssertionError('no prices needed')):
            result = u.update_perf_stats({}, date(2026, 10, 2), disposition_rows=[
                event(start=date(2026, 9, 21), end=date(2026, 9, 25))])
        self.assertTrue(result[key]['after5_overlap'])
        self.assertTrue(json.loads(self.path.read_text())[key]['after5_overlap'])

    def test_overlap_evidence_survives_disappearing_api_event(self):
        key, _ = self.seed_complete()
        with patch.object(u, 'fetch_twse_stock_history', return_value=[]):
            u.update_perf_stats({}, date(2026, 9, 25), disposition_rows=[
                event(start=date(2026, 9, 21), end=date(2026, 9, 25))])
            # No API evidence in next run; unrelated new pending event still processed.
            new = event(code='2317')
            result = u.update_perf_stats({new['period_end']: {'stocks': [new]}}, date(2026, 10, 2))
        self.assertTrue(result[key]['after5_overlap'])

    def test_legacy_uncertain_overlap_can_be_resolved_with_exact_day(self):
        key, e = self.seed_complete()
        del e['after5_date']
        self.path.write_text(json.dumps({key: e}))
        later = event(start=date(2026, 9, 30), end=date(2026, 10, 6))
        with patch.object(u, 'fetch_twse_stock_history', return_value=[]):
            out = u.update_perf_stats({}, date(2026, 10, 2), disposition_rows=[later])
        self.assertTrue(out[key]['after5_overlap'])
        days = [date(2026, 9, 11) + timedelta(days=i) for i in range(15)
                if (date(2026, 9, 11) + timedelta(days=i)).weekday() < 5]
        hist = [{'date': d, 'close': 100 + i, 'vol_k': 100} for i, d in enumerate(days)]
        with patch.object(u, 'fetch_twse_stock_history', return_value=hist):
            out = u.update_perf_stats({}, date(2026, 10, 5))
        self.assertFalse(out[key]['after5_overlap'])


if __name__ == '__main__':
    unittest.main()
