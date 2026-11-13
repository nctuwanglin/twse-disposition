"""
TPEx 處置公告：主來源與官方備援都失敗時，沿用上一份已驗證快照。

背景（2026-11-06）：TPEx 源站整晚回 HTTP 520，官方 OpenAPI 備援也被驗證拒絕，
兩班都中止 —— 上市資料明明正常，整個網站卻停更一個交易日。
沿用必須明示為過期資料，且不得無限期串接沿用。
"""
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


def entry(code, exchange, ps, pe, ann=None):
    return {'code': code, 'name': f'股{code}', 'exchange': exchange,
            'ann_date': ann or ps, 'period_start': ps, 'period_end': pe,
            'auction': '2分撮合', 'disp_count': 1, 'career_count': 1,
            'close': None, 'change_pct': None, 'vol_k': None}


class CarryForwardTests(unittest.TestCase):
    TARGET = date(2026, 11, 6)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.snapshot = {
            'date': '2026-11-05',
            'active': [entry('6620', 'TPEx', '2026-11-03', '2026-11-09'),
                       entry('2330', 'TWSE', '2026-11-03', '2026-11-09')],
            'upcoming': [entry('4542', 'TPEx', '2026-11-10', '2026-11-16', '2026-11-05')],
            'released': [entry('8227', 'TPEx', '2026-10-20', '2026-10-26')],
            'sources': {},
        }
        for p in [patch.object(u, 'REPO_ROOT', self.root),
                  patch.object(u, 'LAST_TRADE_DATE', self.TARGET),
                  patch.dict(u.SOURCE_STATUS, {}, clear=True),
                  patch.object(u.time, 'sleep')]:
            p.start()
            self.addCleanup(p.stop)

    def outage(self, backup_payload=None):
        """主來源 520；備援回 backup_payload（None＝備援也連不上）。"""
        (self.root / 'dispo.json').write_text(json.dumps(self.snapshot))

        def transport(req, timeout):
            if '/www/' in req.full_url or backup_payload is None:
                raise HTTPError(req.full_url, 520, 'origin error', {}, None)
            return io.BytesIO(json.dumps(backup_payload).encode())
        with patch.object(u.urllib.request, 'urlopen', side_effect=transport):
            return u.fetch_and_normalize_tpex('test')

    def test_both_fail_carries_forward_previous_tpex_rows(self):
        rows = self.outage()
        self.assertEqual(sorted(r['code'] for r in rows), ['4542', '6620', '8227'])
        self.assertTrue(all(r['exchange'] == 'TPEx' for r in rows))
        self.assertIsInstance(rows[0]['period_start'], date)

    def test_carried_rows_are_marked_degraded_not_fresh(self):
        self.outage()
        self.assertFalse(u.source_ok('tpex_disposal'))
        status = u.SOURCE_STATUS['tpex_disposal']
        self.assertEqual(status['carried_from'], '2026-11-05')
        self.assertIn('tpex_disposal', u.degraded_sources())

    def test_carry_forward_refuses_stale_snapshot(self):
        self.snapshot['date'] = '2026-10-20'   # 距目標日超過上限
        self.assertEqual(self.outage(), [])
        self.assertNotIn('carried_from', u.SOURCE_STATUS['tpex_disposal'])

    def test_carry_forward_does_not_chain_indefinitely(self):
        # 前一份快照本身就是沿用來的：以最初的資料日計算期限，不得一路串接
        self.snapshot['sources'] = {'tpex_disposal': {'ok': False, 'carried_from': '2026-10-20'}}
        self.assertEqual(self.outage(), [])
        self.assertNotIn('carried_from', u.SOURCE_STATUS['tpex_disposal'])

    def test_chained_carry_keeps_original_origin_date(self):
        self.snapshot['sources'] = {'tpex_disposal': {'ok': False, 'carried_from': '2026-11-04'}}
        self.outage()
        self.assertEqual(u.SOURCE_STATUS['tpex_disposal']['carried_from'], '2026-11-04')

    def test_no_snapshot_means_no_carry_forward(self):
        def transport(req, timeout):
            raise HTTPError(req.full_url, 520, 'origin error', {}, None)
        with patch.object(u.urllib.request, 'urlopen', side_effect=transport):
            self.assertEqual(u.fetch_and_normalize_tpex('test'), [])
        self.assertNotIn('carried_from', u.SOURCE_STATUS['tpex_disposal'])

    def test_valid_backup_still_preferred_over_carry_forward(self):
        backup = [{'Date': '1151106', 'SecuritiesCompanyCode': c, 'CompanyName': f'股{c}',
                   'DispositionPeriod': p, 'DisposalCondition': '約每2分鐘撮合一次'}
                  for c, p in (('6620', '1151103~1151109'), ('4542', '1151110~1151116'),
                               ('5314', '1151109~1151113'))]
        # 既有有效事件的公告日要與備援一致，否則連續性檢查會拒絕
        self.snapshot['active'][0]['ann_date'] = '2026-11-06'
        self.snapshot['upcoming'][0]['ann_date'] = '2026-11-06'
        rows = self.outage(backup)
        self.assertTrue(u.source_ok('tpex_disposal'))
        self.assertNotIn('carried_from', u.SOURCE_STATUS['tpex_disposal'])
        self.assertIn('5314', [r['code'] for r in rows])


class DisposalGateTests(unittest.TestCase):
    """主流程的處置源放行判斷。"""

    def setUp(self):
        p = patch.dict(u.SOURCE_STATUS, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_failed_source_without_carry_aborts(self):
        u.record_source('tpex_disposal', ok=False, error='520')
        with self.assertRaises(SystemExit):
            u._disposal_source_gate('TPEx', 'tpex_disposal', [])

    def test_carried_source_does_not_abort(self):
        u.record_source('tpex_disposal', ok=False, error='520')
        u.SOURCE_STATUS['tpex_disposal']['carried_from'] = '2026-11-05'
        u._disposal_source_gate('TPEx', 'tpex_disposal', [{'code': '6620'}])

    def test_twse_is_never_carried_forward(self):
        # 只有 TPEx 有沿用機制；TWSE 處置源失敗仍須中止
        u.record_source('twse_punish', ok=False, error='down')
        u.SOURCE_STATUS['twse_punish']['carried_from'] = '2026-11-05'
        with self.assertRaises(SystemExit):
            u._disposal_source_gate('TWSE', 'twse_punish', [])

    def test_healthy_empty_source_passes(self):
        u.record_source('tpex_disposal', ok=True, rows=0)
        u._disposal_source_gate('TPEx', 'tpex_disposal', [])


class CarryForwardNoticeTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(u.SOURCE_STATUS, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_notice_tells_users_which_date_is_reused(self):
        u.record_source('tpex_disposal', ok=False, error='520')
        u.SOURCE_STATUS['tpex_disposal']['carried_from'] = '2026-11-05'
        html = u.render_source_notice()
        self.assertIn('上櫃處置公告', html)
        self.assertIn('沿用 11/5', html)


class NonRegularRowValidationTests(unittest.TestCase):
    """
    驗證只應擋「會被採用」的列。可轉債（5 碼）、權證（6 碼）本來就會被過濾掉，
    它們有空白欄位不該讓整班中止；但代號空白的列仍要擋（無法判斷，且可能是
    欄位改名的格式變更）。
    """

    def setUp(self):
        p = patch.dict(u.SOURCE_STATUS, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def twse(self, code, period):
        return {'Code': code, 'Name': f'股{code}', 'Date': '1151106',
                'DispositionPeriod': period, 'DispositionMeasures': '約每2分鐘撮合一次'}

    def test_twse_warrant_with_blank_field_is_ignored(self):
        rows = u.normalize_twse_rows([self.twse('030001', ''),
                                      self.twse('2330', '1151109~1151113')])
        self.assertEqual([r['code'] for r in rows], ['2330'])

    def test_twse_regular_row_with_blank_field_still_aborts(self):
        with self.assertRaises(ValueError):
            u.normalize_twse_rows([self.twse('2330', '')])

    def test_blank_code_still_aborts(self):
        # 代號空白可能代表欄位改名 —— 不能當成「可略過的非一般股票」
        with self.assertRaises(ValueError):
            u.normalize_twse_rows([self.twse('', '1151109~1151113')])


if __name__ == '__main__':
    unittest.main()


class FullWidthPlaceholderTests(unittest.TestCase):
    """
    TPEx 當天沒有新處置公告時，會在最前面插一列「本日無處置資料」占位列——
    補滿全部欄位、公布日期為當天、其餘欄位空白（2026-11-13 實際抓到）。
    舊白名單只認得單欄占位列，這種格式被當成資料列 → 代號空白 → 整班中止，
    正是 2026-11-06 與 10-14 停更的根因。
    """
    FIELDS = ['編號', '公布日期', '證券代號', '證券名稱', '累計', '處置起訖時間',
              '處置原因', '處置措施', '處置內容', '收盤價', '本益比', ' ']
    PLACEHOLDER = ['1', '115/11/13', '', '                ', '', '', '', '',
                   '本日無處置資料', '', '', '']
    REAL = ['4', '115/11/09', '4542', '科嶠(../../mainboard/listed/company-detail.html?code=4542)',
            '1', '115/11/10~115/11/16', '連續3個營業日', '再次處置',
            '* 公布日期：115/11/09<br>約每2分鐘撮合一次', '225.00', '103.21', '']

    def test_full_width_placeholder_is_skipped_real_rows_kept(self):
        rows = u._tpex_rows_to_dicts(self.FIELDS, [self.PLACEHOLDER, self.REAL])
        self.assertEqual([r['證券代號'] for r in rows], ['4542'])

    def test_single_cell_placeholder_still_skipped(self):
        self.assertEqual(u._tpex_rows_to_dicts(self.FIELDS, [['本日無公布注意交易累計資訊']]), [])

    def test_unknown_short_row_still_rejected(self):
        with self.assertRaises(ValueError):
            u._tpex_rows_to_dicts(self.FIELDS, [['系統維護中']])

    def test_row_with_a_security_code_is_never_treated_as_placeholder(self):
        # 只要列裡有證券代號，就算某欄寫著「本日無…」也不能被當成占位列吞掉
        row = list(self.REAL)
        row[8] = '本日無處置資料'
        rows = u._tpex_rows_to_dicts(self.FIELDS, [row])
        self.assertEqual([r['證券代號'] for r in rows], ['4542'])

    def test_main_source_with_placeholder_completes(self):
        with patch.dict(u.SOURCE_STATUS, {}, clear=True):
            raw = {'tables': [{'fields': self.FIELDS, 'data': [self.PLACEHOLDER, self.REAL]}]}
            rows = u._tpex_table_rows(raw, 'tpex_disposal', ('證券代號', '公布日期', '處置起訖時間'),
                                      code_key='證券代號')
        self.assertEqual(len(rows), 1)

    def test_backup_dict_placeholder_is_skipped(self):
        backup = [{'Date': '1151113', 'SecuritiesCompanyCode': '', 'CompanyName': '',
                   'DispositionPeriod': '', 'DisposalCondition': '本日無處置資料'},
                  {'Date': '1151109', 'SecuritiesCompanyCode': '4542', 'CompanyName': '科嶠',
                   'DispositionPeriod': '1151110~1151116', 'DisposalCondition': '約每2分鐘撮合一次'}]
        kept = [r for r in backup if not u._is_tpex_placeholder(r.values())]
        self.assertEqual([r['SecuritiesCompanyCode'] for r in kept], ['4542'])
