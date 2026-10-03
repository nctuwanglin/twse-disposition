"""User-visible certainty and data gaps, rendered from controlled inputs."""
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 2)
        self.row = {'code': '9999', 'name': '測試長股名', 'exchange': 'TWSE',
                    'raw_criteria': '115年9月30日至115年10月2日連續三次'}

    def test_partial_radar_warning_remains_next_to_surviving_rows(self):
        with patch.dict(u.SOURCE_STATUS, {'tpex_warning': {'ok': False}}, clear=True):
            html = u.render_tab3([self.row], [], {}, {}, self.today)
        self.assertIn('上櫃注意累計', html)
        self.assertIn('來源暫時無法取得', html)
        self.assertIn('9999', html)

    def test_radar_never_presents_estimate_as_confirmed_disposition(self):
        html = u.render_tab3([self.row], [], {}, {}, self.today)
        self.assertIn('非正式處置公告', html)
        self.assertNotIn('已達處置條件・待公告', html)
        self.assertNotIn('達注意標準即進處置', html)

    def test_missing_quote_and_threshold_are_visible_without_expanding(self):
        html = u.render_notetrans_rows([self.row], {}, self.today)
        summary = html.split('<details')[0]
        self.assertIn('報價未取得', summary)
        self.assertIn('價格門檻未取得', summary)

    def test_unparseable_radar_does_not_claim_three_more_days(self):
        html = u.render_notetrans_rows([dict(self.row, raw_criteria='未知格式')], {}, self.today)
        self.assertIn('累計條件未確認', html)
        self.assertNotIn('差 3 日', html)

    def test_upcoming_explains_official_announcement_even_when_empty(self):
        html = u.render_tab2_upcoming_batches({}, {}, self.today)
        self.assertIn('正式公告', html)
        self.assertIn('不含雷達推估', html)

    def test_released_date_is_expiry_not_resume_date(self):
        stock = dict(self.row, period_end=date(2026, 10, 1))
        html = u.render_stock_row(stock, {}, self.today, 'pill-yellow', '今日恢復交易')
        self.assertIn('10/1 期滿', html)
        self.assertNotIn('10/1 解禁', html)

    def test_released_row_uses_available_quote(self):
        stock = dict(self.row, period_end=date(2026, 10, 1))
        html = u.render_tab3([], [], {date(2026, 10, 1): {'stocks': [stock]}}, {}, self.today,
                             stock_quotes={'9999': {'close': 88.5, 'change': 0}})
        self.assertIn('88.5', html)
        self.assertNotIn('報價未取得', html)

    def test_date_header_labels_market_date_without_promising_old_schedule(self):
        html = u.render_date_block(self.today)
        self.assertIn('資料日', html)
        self.assertNotIn('21:00', html)

    def test_next_session_price_is_not_called_tomorrow(self):
        thr = {'9999': {'latest_date': self.today, 'current_close': 10,
                        'clause1': {'ref_date': self.today, 'ref_close': 8, 'cum_pct': 25,
                                    'pct': 32, 'threshold': 10.56, 'diff_pct': 5.6,
                                    'triggered': False, 'next_session_threshold': 11}}}
        html = u.render_notetrans_rows([self.row], {}, self.today, nt_thresholds=thr)
        self.assertIn('下一交易日收盤', html)
        self.assertNotIn('明日收盤', html)
        self.assertNotIn('任一即可', html)

    def test_unparsed_counts_do_not_hide_available_price_threshold(self):
        row = dict(self.row, raw_criteria='無法解析的注意次數')
        thr = {'9999': {'latest_date': self.today, 'current_close': 10,
                        'clause1': {'ref_date': self.today, 'ref_close': 8, 'cum_pct': 25,
                                    'pct': 32, 'threshold': 10.56, 'diff_pct': 5.6,
                                    'triggered': False, 'next_session_threshold': 11}}}
        html = u.render_notetrans_rows([row], {}, self.today, nt_thresholds=thr)
        self.assertIn('累計條件未確認', html)
        self.assertIn('11.00', html)
        self.assertIn('注意股單一條件試算', html)
        self.assertNotIn('價格門檻未取得', html)

    def test_risk_details_do_not_promise_first_disposition_or_specific_measures(self):
        for count in ('二', '三', '五'):
            with self.subTest(count=count):
                analysis = u.analyze_criteria(f'115年9月30日至115年10月2日連續{count}次')
                html = u.render_risk_detail(analysis, self.today)
                self.assertNotIn('第一次處置', html)
                self.assertNotIn('2分撮合', html)
                self.assertIn('官方', html)
