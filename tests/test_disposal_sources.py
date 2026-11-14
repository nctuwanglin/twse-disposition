"""
處置公告來源的時效與查詢區間。

1. 上市改用 www.twse.com.tw RWD 端點。探測 10/15–11/13 共 86 次的結果：
   openapi 對公告日 D 的公告要到 D+1 早上 06–11 點才出現，兩班排程（實測約
   21:47、00:40）一律晚一天抓到；RWD 在 D 當天約 18:15 前就有。6 月已因同樣
   的延遲把報價與大盤換到 www.twse.com.tw，處置公告當時漏換。openapi 留作備援。
2. 兩市場都以日期區間查詢。只查「目前公告中」會讓期滿的處置立刻消失，
   近期出關清單與出關績效統計因此從 9 月起完全沒有新資料。
"""
import io
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_dashboard as u

RWD_FIELDS = ['編號', '公布日期', '證券代號', '證券名稱', '累計', '處置條件',
              '處置起迄時間', '處置措施', '處置內容', '備註']


def rwd_row(code, ann, period, measures='第一次處置'):
    return ['1', ann, code, f'股{code}', '1', '連續三次', period, measures,
            '１處置原因：連續三個營業日。\n約每二分鐘撮合一次', '']


class TwseRwdTests(unittest.TestCase):
    TARGET = date(2026, 11, 13)

    def setUp(self):
        self.urls = []
        for p in [patch.object(u, 'LAST_TRADE_DATE', self.TARGET),
                  patch.dict(u.SOURCE_STATUS, {}, clear=True),
                  patch.object(u.time, 'sleep')]:
            p.start()
            self.addCleanup(p.stop)

    def serve(self, rwd=None, rwd_error=None, openapi=None):
        def transport(req, timeout):
            self.urls.append(req.full_url)
            if 'rwd/zh/announcement/punish' in req.full_url:
                if rwd_error:
                    raise rwd_error
                return io.BytesIO(json.dumps(rwd).encode())
            if 'openapi.twse.com.tw' in req.full_url:
                return io.BytesIO(json.dumps(openapi or []).encode())
            raise AssertionError(req.full_url)
        return patch.object(u.urllib.request, 'urlopen', side_effect=transport)

    def test_rwd_rows_are_normalized_like_openapi(self):
        payload = {'stat': 'OK', 'fields': RWD_FIELDS,
                   'data': [rwd_row('2455', '115/11/13', '115/11/14～115/11/20'),
                            rwd_row('053040', '115/11/13', '115/11/14～115/11/20')]}
        with self.serve(rwd=payload):
            rows = u.normalize_twse_rows(u.fetch_twse_disposal_raw())
        self.assertEqual([r['code'] for r in rows], ['2455'])        # 權證被過濾
        self.assertEqual(rows[0]['ann_date'], date(2026, 11, 13))
        self.assertEqual(rows[0]['period_start'], date(2026, 11, 14))
        self.assertEqual(rows[0]['auction'], '2分撮合')
        self.assertEqual(u.SOURCE_STATUS['twse_punish']['endpoint'], u.TWSE_PUNISH_RWD)
        self.assertTrue(u.source_ok('twse_punish'))

    def test_starred_announcement_date_is_accepted(self):
        # RWD 少數列的公布日期前面帶「*」（探測期間 336 筆中有 11 筆）
        payload = {'stat': 'OK', 'fields': RWD_FIELDS,
                   'data': [rwd_row('2455', '*115/11/13', '115/11/14～115/11/20')]}
        with self.serve(rwd=payload):
            rows = u.normalize_twse_rows(u.fetch_twse_disposal_raw())
        self.assertEqual(rows[0]['ann_date'], date(2026, 11, 13))

    def test_query_window_covers_recent_releases(self):
        payload = {'stat': 'OK', 'fields': RWD_FIELDS, 'data': []}
        with self.serve(rwd=payload):
            u.fetch_twse_disposal_raw()
        url = self.urls[0]
        self.assertIn('startDate=20260914', url)   # 目標日往回 60 天
        self.assertIn('endDate=20261127', url)     # 往後 14 天

    def test_network_failure_falls_back_to_openapi(self):
        openapi = [{'Date': '1151112', 'Code': '2455', 'Name': '全新',
                    'DispositionPeriod': '115/11/13～115/11/19',
                    'DispositionMeasures': '第一次處置', 'Detail': '約每二分鐘撮合一次'}]
        err = HTTPError('x', 503, 'down', {}, None)
        with self.serve(rwd_error=err, openapi=openapi):
            rows = u.normalize_twse_rows(u.fetch_twse_disposal_raw())
        self.assertEqual([r['code'] for r in rows], ['2455'])
        status = u.SOURCE_STATUS['twse_punish']
        self.assertTrue(status['ok'])
        self.assertEqual(status['endpoint'], u.TWSE_PUNISH_API)
        self.assertIn('503', status['fallback_reason'])

    def test_non_ok_stat_falls_back(self):
        with self.serve(rwd={'stat': '查詢日期大於可查詢最大日期'}, openapi=[]):
            u.fetch_twse_disposal_raw()
        self.assertEqual(u.SOURCE_STATUS['twse_punish']['endpoint'], u.TWSE_PUNISH_API)

    def test_schema_change_falls_back(self):
        renamed = [f if f != '證券代號' else '有價證券代號' for f in RWD_FIELDS]
        with self.serve(rwd={'stat': 'OK', 'fields': renamed, 'data': []}, openapi=[]):
            u.fetch_twse_disposal_raw()
        self.assertIn('證券代號', u.SOURCE_STATUS['twse_punish']['fallback_reason'])

    def test_both_endpoints_failing_is_still_a_failure(self):
        def transport(req, timeout):
            raise HTTPError(req.full_url, 503, 'down', {}, None)
        with patch.object(u.urllib.request, 'urlopen', side_effect=transport):
            u.fetch_twse_disposal_raw()
        self.assertFalse(u.source_ok('twse_punish'))


class TpexWindowTests(unittest.TestCase):
    def test_tpex_query_uses_same_window(self):
        urls = []

        def transport(req, timeout):
            urls.append(req.full_url)
            return io.BytesIO(json.dumps({'tables': [{'fields': ['證券代號', '公布日期', '處置起訖時間'],
                                                      'data': []}]}).encode())
        with patch.object(u, 'LAST_TRADE_DATE', date(2026, 11, 13)), \
                patch.dict(u.SOURCE_STATUS, {}, clear=True), \
                patch.object(u.urllib.request, 'urlopen', side_effect=transport):
            u.fetch_and_normalize_tpex('test')
        self.assertIn('startDate=2026/09/14', urls[0])
        self.assertIn('endDate=2026/11/27', urls[0])


class ReleasedNowPopulatedTests(unittest.TestCase):
    """期滿的處置留在查詢結果裡，近期出關清單才會有資料。"""

    def test_recently_ended_disposition_is_released(self):
        target = date(2026, 11, 13)
        row = {'code': '2455', 'name': '全新', 'exchange': 'TWSE',
               'ann_date': date(2026, 10, 27), 'period_start': date(2026, 10, 28),
               'period_end': date(2026, 11, 3), 'auction': '2分撮合', 'disp_count': 1}
        _, _, released = u.group_into_batches([row], target)
        self.assertEqual([s['code'] for g in released.values() for s in g['stocks']], ['2455'])


if __name__ == '__main__':
    unittest.main()


class ReleaseGroupCollapseTests(unittest.TestCase):
    """出關清單有資料後（實測近 30 營業日約 96 檔、數十個日期群組），全部預設展開
    會讓手機版 Tab3 過長。比照 Tab1 批次只展開最近 3 個。"""

    def test_only_latest_three_release_groups_open(self):
        today = date(2026, 11, 13)
        released = {}
        for day in (3, 4, 5, 6, 7):
            pe = date(2026, 11, day)
            released[pe] = {'period_end': pe, 'stocks': [{
                'code': f'1{day:03d}', 'name': '股', 'exchange': 'TWSE',
                'ann_date': pe, 'period_start': pe, 'period_end': pe,
                'auction': '2分撮合', 'disp_count': 1}]}
        html = u.render_tab3([], [], released, {}, today)
        blocks = html.split('<details class="mb-0"')[1:]
        self.assertEqual(len(blocks), 5)
        self.assertEqual([b.startswith(' open') for b in blocks], [True, True, True, False, False])
        self.assertIn('11/7', blocks[0][:300])        # 最新的日期排最前面且展開
