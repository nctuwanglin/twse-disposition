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
    apply_new_rule_period, prev_trading_day,
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

    # ── 處置新制（2026-08-10）：統一 2 分撮合 ──
    def test_2min_twse_wording(self):
        # TWSE 用國字「約每二分鐘」，第一次／第二次皆同
        self.assertEqual(
            get_auction_type("約每二分鐘撮合一次", "第一次處置", date(2026, 8, 17)), "2分撮合")
        self.assertEqual(
            get_auction_type("約每二分鐘撮合一次", "第二次處置", date(2026, 8, 17)), "2分撮合")

    def test_2min_tpex_wording(self):
        # TPEx 用阿拉伯數字「約每2分鐘」
        self.assertEqual(
            get_auction_type("約每2分鐘撮合一次", "", date(2026, 8, 17)), "2分撮合")

    def test_2min_not_confused_with_old(self):
        # "二分鐘"/"2分鐘" 不可誤匹配舊制的 "二十分鐘"/"20分鐘"
        self.assertEqual(get_auction_type("約每二十分鐘撮合一次"), "20分撮合")
        self.assertEqual(get_auction_type("約每20分鐘撮合一次"), "20分撮合")

    def test_old_announcement_crossing_new_rule_forced_2min(self):
        # 舊公告條文仍寫 5 分，但處置期跨過生效日 → 一律改 2 分
        self.assertEqual(
            get_auction_type("約每五分鐘撮合一次", "第一次處置", date(2026, 8, 11)), "2分撮合")

    def test_old_announcement_ended_before_new_rule_keeps_old(self):
        # 生效日前就結束者維持原撮合（歷史正確性）
        self.assertEqual(
            get_auction_type("約每二十分鐘撮合一次", "第二次處置", date(2026, 8, 7)), "20分撮合")


class TestNewRulePeriod(unittest.TestCase):
    """處置新制過渡換算（真實案例，2026-08-11 對照兩市場 API 驗證過）。"""

    def test_already_served_released_on_effective_date(self):
        # 1515 力山：7/31 起，8/6 即滿 5 日 → 8/10 解除，處置到 8/7 為止
        self.assertEqual(
            apply_new_rule_period(date(2026,7,30), date(2026,7,31), date(2026,8,13)),
            date(2026, 8, 7))

    def test_exactly_five_days_by_prev_day(self):
        # 3026 禾伸堂：8/3 起，8/7 剛好滿 5 日
        self.assertEqual(
            apply_new_rule_period(date(2026,8,3), date(2026,8,3), date(2026,8,14)),
            date(2026, 8, 7))

    def test_day_trading_variant_twelve_to_seven(self):
        # 8046：原 12 個營業日（涉當沖警示）→ 新制 7 日，8/3 起算至 8/11
        self.assertEqual(
            apply_new_rule_period(date(2026,8,3), date(2026,8,3), date(2026,8,18)),
            date(2026, 8, 11))

    def test_post_new_rule_announcement_untouched(self):
        # 生效日起公告者 API 已是新制，不得再動
        self.assertEqual(
            apply_new_rule_period(date(2026,8,10), date(2026,8,11), date(2026,8,17)),
            date(2026, 8, 17))

    def test_ended_before_new_rule_untouched(self):
        self.assertEqual(
            apply_new_rule_period(date(2026,7,20), date(2026,7,21), date(2026,8,3)),
            date(2026, 8, 3))

    def test_never_extends(self):
        # 新制只縮短不延長：原迄日早於換算結果時取原迄日
        self.assertEqual(
            apply_new_rule_period(date(2026,8,7), date(2026,8,10), date(2026,8,11)),
            date(2026, 8, 11))


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


class TestPrevTradingDay(unittest.TestCase):
    """
    「今日出關」的基準日。舊版拿 today 去比對 released_groups（其篩選條件是
    period_end < today），兩者互斥導致數字恆為 0；正解是比對前一交易日——
    處置迄日當天仍受管制，次一交易日才恢復正常交易。
    """

    def test_uses_last_processed_trading_day(self):
        # 週二，上次執行是週一 → 前一交易日 = 週一
        self.assertEqual(
            prev_trading_day(date(2026, 8, 11), {"date": "2026-08-10"}),
            date(2026, 8, 10))

    def test_skips_holiday_gap_via_baseline(self):
        # 中間隔了颱風停市／連假：以實際有資料的日子為準，不是單純減一天
        self.assertEqual(
            prev_trading_day(date(2026, 8, 11), {"date": "2026-08-06"}),
            date(2026, 8, 6))

    def test_falls_back_to_previous_weekday(self):
        # 沒有 baseline（首次執行）→ 退回前一平日；週一應回到上週五
        self.assertEqual(prev_trading_day(date(2026, 8, 10), None), date(2026, 8, 7))

    def test_ignores_stale_baseline_not_before_today(self):
        # 同日重跑時 baseline 可能等於今天，不可當成前一交易日
        self.assertEqual(
            prev_trading_day(date(2026, 8, 11), {"date": "2026-08-11"}),
            date(2026, 8, 10))

    def test_ignores_malformed_baseline(self):
        self.assertEqual(
            prev_trading_day(date(2026, 8, 11), {"date": "not-a-date"}),
            date(2026, 8, 10))


if __name__ == "__main__":
    unittest.main()
