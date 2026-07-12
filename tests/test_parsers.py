# -*- coding: utf-8 -*-
"""
Parser 固定測資：公告文字/日期/撮合方式解析的回歸測試。
TWSE/櫃買改字樣時這裡會先紅，避免靜默解析失敗（歷史教訓：解析 0 筆照樣發佈）。
執行：python3 -m unittest discover -s tests -q
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_dashboard import (          # noqa: E402
    roc_to_date, parse_period, analyze_criteria, parse_criteria,
    get_auction_type, get_disposition_count, calculate_attention_thresholds,
)


class TestDates(unittest.TestCase):
    def test_roc_7(self):
        self.assertEqual(roc_to_date("1150706"), date(2026, 7, 6))

    def test_roc_slash(self):
        self.assertEqual(roc_to_date("115/07/06"), date(2026, 7, 6))

    def test_parse_period(self):
        ps, pe = parse_period("1150706～1150717")
        self.assertEqual((ps, pe), (date(2026, 7, 6), date(2026, 7, 17)))

    def test_parse_period_ascii_tilde(self):
        ps, pe = parse_period("115/07/06~115/07/17")
        self.assertEqual((ps, pe), (date(2026, 7, 6), date(2026, 7, 17)))


class TestCriteria(unittest.TestCase):
    # 真實公告樣本（TWSE openapi / 櫃買，2026-07）
    SAMPLE_SIMPLE  = "115年7月8日至115年7月9日連續二次"
    SAMPLE_PADDED  = "115年07月08日至115年07月09日連續二次"
    SAMPLE_COMBO   = ("115年6月17日至115年6月18日連續二次"
                      "115年6月15日至115年6月18日累計四次")

    def test_simple(self):
        a = analyze_criteria(self.SAMPLE_SIMPLE)
        self.assertIsNotNone(a)
        self.assertEqual(a["max_consecutive"], 2)
        self.assertEqual(a["latest_end"], date(2026, 7, 9))

    def test_zero_padded_dates(self):
        a = analyze_criteria(self.SAMPLE_PADDED)
        self.assertIsNotNone(a)
        self.assertEqual(a["max_consecutive"], 2)

    def test_combo_consecutive_and_cumulative(self):
        a = analyze_criteria(self.SAMPLE_COMBO)
        self.assertEqual(len(a["entries"]), 2)
        self.assertEqual(a["max_consecutive"], 2)   # 累計不計入連續
        self.assertEqual(a["latest_end"], date(2026, 6, 18))

    def test_short_label(self):
        self.assertIn("連續二次", parse_criteria(self.SAMPLE_SIMPLE))

    def test_garbage_returns_none(self):
        self.assertIsNone(analyze_criteria("與本項無關的文字"))


class TestAuction(unittest.TestCase):
    def test_20min(self):
        self.assertEqual(get_auction_type("約每二十分鐘撮合一次"), "20分撮合")

    def test_5min(self):
        self.assertEqual(get_auction_type("約每五分鐘撮合一次"), "5分撮合")

    def test_disp_count_second(self):
        self.assertGreaterEqual(get_disposition_count("第二次處置"), 2)


class TestThresholds(unittest.TestCase):
    def _hist(self, closes):
        d0 = date(2026, 6, 1)
        out = []
        d = d0
        for c in closes:
            while d.weekday() >= 5:
                d = d.replace(day=d.day + 1)
            out.append({"date": d, "close": c, "vol_k": 100})
            d = d.fromordinal(d.toordinal() + 1)
        return out

    def test_clause1_pct_parameterized(self):
        # 7 天平盤 → 6 日累積 0%，TWSE 門檻 = ref*1.32
        hist = self._hist([100.0] * 10)
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        self.assertAlmostEqual(t["clause1"]["threshold"], 132.0)
        self.assertAlmostEqual(t["clause1"]["pct"], 32.0)
        self.assertFalse(t["clause1"]["triggered"])

    def test_clause1_triggered(self):
        hist = self._hist([100.0] * 9 + [135.0])
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        self.assertTrue(t["clause1"]["triggered"])

    def test_tpex_pct(self):
        hist = self._hist([100.0] * 10)
        t = calculate_attention_thresholds(hist, 30.0, 100.0)
        self.assertAlmostEqual(t["clause1"]["threshold"], 130.0)

    def test_insufficient_history(self):
        self.assertIsNone(calculate_attention_thresholds(self._hist([100.0] * 3)))


if __name__ == "__main__":
    unittest.main()
