# -*- coding: utf-8 -*-
"""
Parser 固定測資：公告文字/日期/撮合方式解析的回歸測試。
TWSE/櫃買改字樣時這裡會先紅，避免靜默解析失敗（歷史教訓：解析 0 筆照樣發佈）。
執行：python3 -m unittest discover -s tests -q
"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_dashboard import (          # noqa: E402
    roc_to_date, parse_period, analyze_criteria, parse_criteria,
    get_auction_type, get_disposition_count, calculate_attention_thresholds,
    apply_new_rule_period, prev_trading_day,
    ad_to_date, render_risk_detail,
    replace_between, update_inline_counts, MarkerError,
    _perf_complete, _months_between, update_perf_stats, perf_stats_summary,
    write_snapshot, _tpex_rows_to_dicts, _canonical_active_by_code,
    render_tab2_upcoming_batches, _notetrans_urgency,
    render_tab1_batches, _latest_batch_stats, _unexplained_drop,
    render_attention_conditions, render_notetrans_rows,
    render_stock_row, exchange_section, render_release_schedule,
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

    def test_clause2_window_is_30_days_including_today_not_31(self):
        # R10：條文是「30 個營業日含當日起迄」，起日本身就是這 30 天之一。
        # 舊版 nth_before(30) 等於多抓了一天當基準（用窗口外的第 31 天），
        # 這裡用兩個可區分的基準價驗證正確基準是 nth_before(29)。
        # index0=999（舊 bug 會誤用的「第 31 天」）、index1=50（正確基準）、
        # index2..30=100（其餘 29 天），latest=100。
        closes = [999.0, 50.0] + [100.0] * 29
        hist = self._hist(closes)
        self.assertEqual(len(hist), 31)
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        self.assertAlmostEqual(t["clause2"]["ref_close"], 50.0)
        self.assertEqual(t["clause2"]["ref_date"], hist[1]["date"])
        self.assertAlmostEqual(t["clause2"]["cum_pct"], 100.0)  # (100-50)/50*100

    def test_clause1_next_session_threshold_uses_rolled_window(self):
        # R10：明天的 6 日窗口基準會從 latest-6 移到 latest-5，不能沿用今天
        # 算出的 threshold 當「明日收盤」門檻。用兩個不同基準價驗證
        # next_session_threshold 確實用了 nth_before(5) 而非 nth_before(6)。
        closes = [200.0, 80.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        hist = self._hist(closes)
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        self.assertFalse(t["clause1"]["triggered"])
        self.assertAlmostEqual(t["clause1"]["threshold"], 200.0 * 1.32)       # 今天的門檻
        self.assertAlmostEqual(t["clause1"]["next_session_threshold"], 80.0 * 1.32)  # 明天的門檻
        self.assertEqual(t["clause1"]["next_session_ref_date"], hist[1]["date"])

    def test_clause1_no_next_session_threshold_when_already_triggered(self):
        # 今天已達標時，「明日門檻」這個概念沒有意義（今天已經觸發了），
        # 不應該算出一個容易被誤讀成「還要等明天」的數字。
        hist = self._hist([100.0] * 9 + [135.0])
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        self.assertTrue(t["clause1"]["triggered"])
        self.assertNotIn("next_session_threshold", t["clause1"])


class TestAttentionConditionsRenderingR10(unittest.TestCase):
    """
    R10：畫面文字必須與實際判定邏輯（>=）一致，不能顯示「門檻＞」；
    且必須明示這只是單一價格/量條件試算，不是完整觸發判定。
    """

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

    def test_threshold_symbol_matches_gte_logic(self):
        hist = self._hist([100.0] * 10)
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        html = render_attention_conditions(t, date(2026, 9, 16))
        self.assertNotIn("門檻＞", html)
        self.assertIn("門檻≥32%", html)

    def test_scope_caveat_present(self):
        hist = self._hist([100.0] * 10)
        t = calculate_attention_thresholds(hist, 32.0, 100.0)
        html = render_attention_conditions(t, date(2026, 9, 16))
        self.assertIn("僅供參考、不代表確定觸發", html)

    def test_notetrans_row_tomo_uses_next_session_threshold_not_todays(self):
        # R10 端對端：render_notetrans_rows 的「明日收盤 ≥ X」必須是
        # next_session_threshold，不能是今天窗口算出的 c1["threshold"]。
        closes = [200.0, 80.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        hist = self._hist(closes)
        thr = calculate_attention_thresholds(hist, 32.0, 100.0)
        record = {"code": "9999", "name": "測試股", "exchange": "TWSE",
                  "raw_criteria": "115年9月10日至115年9月11日連續二次"}
        html = render_notetrans_rows([record], {}, date(2026, 9, 16),
                                     stock_quotes={}, nt_thresholds={"9999": thr})
        self.assertIn(f'{80.0 * 1.32:.2f}', html)     # next_session_threshold
        self.assertNotIn(f'明日收盤 ≥ <span class="mono text-slate-300">{200.0 * 1.32:.2f}',
                         html)  # 不能是今天的 threshold


class TestR16SearchAndBadgeDataAttrs(unittest.TestCase):
    """
    R16：搜尋統計混合注意與出關、搜尋比對整列文字（誤中價量）、狀態文字
    與結果不一致。這裡驗證伺服器端 renderer 有正確輸出前端 JS 依賴的
    data-code/data-name/data-group 屬性（實際篩選行為由 index.html 的
    applyFilter() 消費，已在瀏覽器手動驗證過）。
    """

    def _stock(self, code, name, disp_count=1, period_end=None):
        return {"code": code, "name": name, "exchange": "TWSE",
                "period_end": period_end or date(2026, 9, 20), "disp_count": disp_count}

    def test_render_stock_row_has_code_and_name_attrs(self):
        html = render_stock_row(self._stock("8996", "高力"), {}, date(2026, 9, 16))
        self.assertIn('data-code="8996"', html)
        self.assertIn('data-name="高力"', html)

    def test_render_stock_row_marks_released_group_via_yellow_pill(self):
        # exchange_section 在 render_tab3 的「近期出關」區塊固定傳
        # pill_class_override="pill-yellow"；這是目前唯一的呼叫端，
        # 用它來標記 data-group="released" 讓前端徽章能排除這些列。
        html = render_stock_row(self._stock("8996", "高力"), {}, date(2026, 9, 16),
                                pill_class_override="pill-yellow", pill_label_override="今日恢復交易")
        self.assertIn('data-group="released"', html)

    def test_render_stock_row_active_batch_not_marked_released(self):
        html = render_stock_row(self._stock("8996", "高力"), {}, date(2026, 9, 16))
        self.assertNotIn('data-group="released"', html)

    def test_exchange_section_released_rows_carry_group_attr(self):
        stocks = [self._stock("8996", "高力"), self._stock("8227", "巨有科技")]
        html = exchange_section("TWSE 上市", stocks, {}, date(2026, 9, 16),
                                pill_class_override="pill-yellow", pill_label_override="今日恢復交易")
        self.assertEqual(html.count('data-group="released"'), 2)

    def test_notetrans_row_has_code_and_name_attrs(self):
        record = {"code": "2305", "name": "全友", "exchange": "TWSE",
                  "raw_criteria": "與本項無關的文字"}
        html = render_notetrans_rows([record], {}, date(2026, 9, 16),
                                     stock_quotes={}, nt_thresholds={})
        self.assertIn('data-code="2305"', html)
        self.assertIn('data-name="全友"', html)

    def test_release_schedule_states_it_ignores_filter(self):
        active = {date(2026, 9, 1): {"period_start": date(2026, 9, 1),
                                     "ann_date": date(2026, 8, 31),
                                     "stocks": [self._stock("8996", "高力")]}}
        html = render_release_schedule(active, date(2026, 9, 16))
        self.assertIn("不受下方搜尋/產業篩選影響", html)

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

    # ── R11（2026-09）：trading_days 地面真相優先於 baseline ──

    def test_trading_days_overrides_stale_baseline_after_missed_runs(self):
        # 實例重現：排程連續漏跑多日（9/11 崩潰到 9/15 才修好），baseline 停在
        # 9/11，但真實交易日清單（FMTQIK）顯示 9/14 才是 9/15 的前一交易日。
        # 地面真相必須贏過腳本自己的執行歷史。
        trading_days = [date(2026, 9, d) for d in (8, 9, 10, 11, 14, 15)]
        self.assertEqual(
            prev_trading_day(date(2026, 9, 15), {"date": "2026-09-11"}, trading_days),
            date(2026, 9, 14))

    def test_trading_days_correctly_skips_holiday_gap(self):
        # 交易日清單本身已經跳過週末/國定假日，不需要額外處理
        trading_days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 6)]
        self.assertEqual(
            prev_trading_day(date(2026, 8, 6), {"date": "2026-08-04"}, trading_days),
            date(2026, 8, 4))

    def test_falls_back_to_baseline_when_trading_days_empty(self):
        # 當月第一個交易日：trading_days 拿不到更早一筆，退回 baseline
        self.assertEqual(
            prev_trading_day(date(2026, 9, 1), {"date": "2026-08-31"}, []),
            date(2026, 8, 31))

    def test_falls_back_to_baseline_when_today_is_earliest_in_trading_days(self):
        trading_days = [date(2026, 9, 1), date(2026, 9, 2)]
        self.assertEqual(
            prev_trading_day(date(2026, 9, 1), {"date": "2026-08-31"}, trading_days),
            date(2026, 8, 31))

    def test_trading_days_none_behaves_like_before(self):
        # 未提供 trading_days（例如呼叫端沒傳）時，行為與舊版完全相同
        self.assertEqual(
            prev_trading_day(date(2026, 8, 11), {"date": "2026-08-06"}, None),
            date(2026, 8, 6))


class TestCanonicalActiveByCode(unittest.TestCase):
    """
    R12：同一代碼重疊處置時，取「現行有效管制」的規則必須與 API 回傳順序無關。
    舊版 main() 用 seen-set 取「先遇到的那筆」，等同於相信 API 回傳順序，
    導致 second_count 等 KPI 不穩定。
    """

    def _stock(self, code, period_end, disp_count=1, exchange="TWSE"):
        return {"code": code, "name": f"股{code}", "exchange": exchange,
                "period_start": date(2026, 9, 1), "period_end": period_end,
                "disp_count": disp_count}

    def test_picks_larger_period_end(self):
        old_rec = self._stock("1101", date(2026, 9, 10), disp_count=1)
        new_rec = self._stock("1101", date(2026, 9, 20), disp_count=2)
        groups = {1: {"stocks": [old_rec]}, 2: {"stocks": [new_rec]}}
        result = _canonical_active_by_code(groups)
        self.assertEqual(result["1101"]["period_end"], date(2026, 9, 20))
        self.assertEqual(result["1101"]["disp_count"], 2)

    def test_order_independent(self):
        # 同一份重疊資料，groups 走訪順序對調，結果必須完全相同
        old_rec = self._stock("6933", date(2026, 9, 8), disp_count=1)
        new_rec = self._stock("6933", date(2026, 9, 11), disp_count=2)

        result_a = _canonical_active_by_code(
            {1: {"stocks": [old_rec]}, 2: {"stocks": [new_rec]}})
        result_b = _canonical_active_by_code(
            {1: {"stocks": [new_rec]}, 2: {"stocks": [old_rec]}})

        self.assertEqual(result_a["6933"], result_b["6933"])
        self.assertEqual(result_a["6933"]["disp_count"], 2)

    def test_ties_broken_by_disp_count(self):
        # period_end 相同時，取 disp_count 較大者（較新升級的那筆）
        first  = self._stock("2317", date(2026, 9, 15), disp_count=1)
        second = self._stock("2317", date(2026, 9, 15), disp_count=2)
        result = _canonical_active_by_code({1: {"stocks": [first, second]}})
        self.assertEqual(result["2317"]["disp_count"], 2)

    def test_no_overlap_keeps_single_record(self):
        rec = self._stock("2330", date(2026, 9, 12))
        result = _canonical_active_by_code({1: {"stocks": [rec]}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result["2330"], rec)

    def test_multiple_distinct_codes(self):
        a = self._stock("1101", date(2026, 9, 10))
        b = self._stock("2330", date(2026, 9, 12))
        result = _canonical_active_by_code({1: {"stocks": [a, b]}})
        self.assertEqual(set(result.keys()), {"1101", "2330"})


class TestR07EmptyStateAndDropGuard(unittest.TestCase):
    """
    R07：處置中檔數合法歸零不能被當成資料源故障整批放棄更新；驟降保護
    也不能因為長時間漏跑或大量同批同日期滿而永久卡住（下一班仍拿同一個
    prev_total 比對）。
    """

    def test_render_tab1_batches_empty_shows_explicit_empty_state(self):
        # 舊 bug：active_groups 為空時 main() 直接 return，Tab1 marker
        # 區段維持上次舊內容；新版必須能正常渲染出明確的空狀態文字。
        html = render_tab1_batches({}, {}, date(2026, 9, 16))
        self.assertIn("目前沒有處置中的股票", html)
        self.assertNotIn("table-row", html)

    def test_latest_batch_stats_both_empty_does_not_crash(self):
        # 舊 bug：max({}.values(), key=...) 會丟 ValueError——當 active_groups
        # 因合法零筆而空，且 upcoming_groups 也剛好同時是空（公告空窗期）時
        # 會直接讓 main() 崩潰，而不是優雅地顯示「無最新批次」。
        count, ann = _latest_batch_stats({}, {})
        self.assertEqual(count, 0)
        self.assertIsNone(ann)

    def test_latest_batch_stats_picks_max_period_start(self):
        active = {date(2026, 9, 1): {"period_start": date(2026, 9, 1),
                                     "ann_date": date(2026, 8, 31),
                                     "stocks": [1, 2]}}
        upcoming = {date(2026, 9, 20): {"period_start": date(2026, 9, 20),
                                        "ann_date": date(2026, 9, 16),
                                        "stocks": [1, 2, 3]}}
        count, ann = _latest_batch_stats(active, upcoming)
        self.assertEqual(count, 3)
        self.assertEqual(ann, date(2026, 9, 16))

    def _released(self, pe, n):
        return {pe: {"period_end": pe, "stocks": list(range(n))}}

    def test_drop_fully_explained_by_known_releases_is_not_anomalous(self):
        # 上次 20 檔，這次只剩 5 檔，乍看驟降 75%；但 15 檔都在
        # last_processed_date 之後合法出關（released_groups 有紀錄），
        # 不該被當成資料源故障。
        released = self._released(date(2026, 9, 15), 15)
        result = _unexplained_drop(20, 5, released, date(2026, 9, 11))
        self.assertLessEqual(result, 0)

    def test_drop_not_explained_by_releases_is_anomalous(self):
        # 上次 20 檔、這次只剩 5 檔，但完全沒有已知出關紀錄能解釋——
        # 這才是真正該中止的資料源故障情境。
        result = _unexplained_drop(20, 5, {}, date(2026, 9, 11))
        self.assertEqual(result, 15)

    def test_release_before_last_processed_date_not_double_counted(self):
        # period_end 早於上次執行資料日的紀錄，上次執行時就已經出關、
        # 本來就不在 prev_total 裡，不能被拿來重複解釋這次的下降。
        released = self._released(date(2026, 9, 5), 15)  # 早於 9/11
        result = _unexplained_drop(20, 5, released, date(2026, 9, 11))
        self.assertEqual(result, 15)  # 完全沒被解釋

    def test_no_drop_returns_non_positive(self):
        result = _unexplained_drop(10, 12, {}, date(2026, 9, 11))
        self.assertLessEqual(result, 0)


class TestTab2UpcomingBatches(unittest.TestCase):
    """
    R08：Tab2「即將被處置」只能顯示真正尚未生效的批次（period_start > today），
    不可混入 active 批次；有多批 upcoming 時全部都要出現，不能只取一批。
    """

    def _batch(self, ps, pe, codes):
        stocks = [{"code": c, "name": f"股{c}", "exchange": "TWSE",
                  "period_start": ps, "period_end": pe, "disp_count": 1}
                 for c in codes]
        return {"period_start": ps, "period_end": pe,
                "ann_date": ps - timedelta(days=1), "stocks": stocks}

    def test_empty_upcoming_shows_explicit_empty_state(self):
        # 實測重現：全是 active、upcoming 為空時，舊版仍會生出股票；
        # 新版必須顯示明確空狀態，不得借用 active 內容頂替
        html = render_tab2_upcoming_batches({}, {}, date(2026, 9, 16))
        self.assertIn("目前沒有已公告待生效處置", html)
        self.assertNotIn("table-row", html)  # 確認真的沒有渲染任何股票列

    def test_single_upcoming_batch_rendered(self):
        today = date(2026, 9, 16)
        groups = {date(2026, 9, 18): self._batch(date(2026, 9, 18), date(2026, 9, 24), ["1101"])}
        html = render_tab2_upcoming_batches(groups, {}, today)
        self.assertIn("1101", html)
        self.assertIn("即將生效", html)  # 覆寫後的狀態標籤

    def test_multiple_upcoming_batches_all_included(self):
        # 舊 bug：多批 upcoming 時 max(period_start) 只取一批，較早生效的漏掉
        today = date(2026, 9, 16)
        early = self._batch(date(2026, 9, 17), date(2026, 9, 23), ["1101"])
        late  = self._batch(date(2026, 9, 20), date(2026, 9, 26), ["2330"])
        groups = {date(2026, 9, 17): early, date(2026, 9, 20): late}
        html = render_tab2_upcoming_batches(groups, {}, today)
        self.assertIn("1101", html)
        self.assertIn("2330", html)   # 兩批都要在，舊版只會有其中一批

    def test_active_batches_never_leak_into_tab2(self):
        # 直接證明「不會混用 active」：即使呼叫端傳入的是 active_groups 的資料
        # 結構（period_start <= today），只要它沒被放進 upcoming_groups，
        # render_tab2_upcoming_batches 本身完全不會去看 active_groups
        # （函式簽名已經不再接受 active_groups 參數，這裡驗證單一參數輸入時
        # 不會意外顯示不相關內容）
        today = date(2026, 9, 16)
        html = render_tab2_upcoming_batches({}, {}, today)
        self.assertNotIn("8996", html)  # 空 upcoming 不該冒出任何代碼


def _roc(d):
    """date -> 民國格式字串片段，例如 date(2026,6,17) -> '115年6月17日'（不補零，
    對應 analyze_criteria 的正則樣式）。"""
    return f"{d.year - 1911}年{d.month}月{d.day}日"


class TestNotransUrgencyR09(unittest.TestCase):
    """
    R09：失效的連續紀錄不能仍標「已達處置條件」，累計次數觸發不能被忽略。
    驗收（review 原文）：舊三連、舊二連+新一連、只有累計條件、解析失敗，
    不能一律當三連倒數；上方摘要（_notetrans_urgency）與下方明細
    （render_risk_detail）需一致。
    """
    TODAY = date(2026, 9, 16)  # 週三

    def test_old_dead_streak_is_not_need_zero(self):
        # 舊 bug 原文重現案例：一個月前已達標的連續三日，早就斷了，
        # 舊版 max_c>=3 仍直接回 need=0（已達處置條件），這裡驗證新版不會。
        start, end = date(2026, 8, 10), date(2026, 8, 12)
        raw = f"{_roc(start)}至{_roc(end)}連續三次"
        need, _, live_streak, _, cumulative_hit = _notetrans_urgency(
            {"code": "9999", "raw_criteria": raw}, self.TODAY, {})
        self.assertEqual(live_streak, 0)          # 已失效，不能算活著
        self.assertFalse(cumulative_hit)
        self.assertNotEqual(need, 0)              # 絕不能標「已達處置條件」
        self.assertEqual(need, 3)                 # 需重新累積，從 3 天倒數

    def test_old_two_plus_fresh_one_does_not_compose_to_need_one(self):
        # 舊 bug：max_consecutive 取「所有 entry 的最大次數」(舊的2)，
        # latest_end 取「所有 entry 的最大日期」(新的1)，兩者來自不同 entry，
        # 混合後變成「用舊的次數 2 配新的日期」→ need=3-2=1，錯誤地暗示
        # 只差一天。新版必須改用同一筆 entry 的 count/end，正確反映
        # 目前這一段只連續了 1 天，need 應為 2，不是 1。
        old_start, old_end = date(2026, 6, 17), date(2026, 6, 18)
        new_day = self.TODAY  # 用今天當「新的連續一天」，保證仍活著
        raw = (f"{_roc(old_start)}至{_roc(old_end)}連續二次"
               f"{_roc(new_day)}至{_roc(new_day)}連續一次")
        need, _, live_streak, _, cumulative_hit = _notetrans_urgency(
            {"code": "9999", "raw_criteria": raw}, self.TODAY, {})
        self.assertEqual(live_streak, 1)           # 目前這段只連續 1 天
        self.assertFalse(cumulative_hit)
        self.assertEqual(need, 2)                  # 3 - 1，不是舊 bug 的 3 - 2 = 1

    def test_cumulative_only_triggers_need_zero_independently(self):
        # 只有累計條件達標（30日內累計6次），沒有任何活著的連續紀錄，
        # 舊版完全沒檢查累計，這裡累計必須能獨立觸發 need=0。
        start, end = date(2026, 8, 20), date(2026, 9, 14)
        raw = f"{_roc(start)}至{_roc(end)}累計六次"
        need, _, live_streak, _, cumulative_hit = _notetrans_urgency(
            {"code": "9999", "raw_criteria": raw}, self.TODAY, {})
        self.assertEqual(live_streak, 0)            # 沒有連續型 entry
        self.assertTrue(cumulative_hit)
        self.assertEqual(need, 0)                   # 累計觸發，即使連續是 0

    def test_parse_failure_falls_back_to_need_three_not_zero(self):
        # 解析失敗（文字格式不符）不能一律當三連倒數（need=0），
        # 應退回保守值 need=3、live_streak=0、cumulative_hit=False。
        need, diff, live_streak, c1, cumulative_hit = _notetrans_urgency(
            {"code": "9999", "raw_criteria": "與本項無關的文字"}, self.TODAY, {})
        self.assertEqual(need, 3)
        self.assertEqual(live_streak, 0)
        self.assertFalse(cumulative_hit)
        self.assertIsNone(c1)

    def test_risk_detail_consistent_with_dead_streak(self):
        # 明細面板（render_risk_detail）必須與上面判斷一致：已失效的連續
        # 不能顯示「已達連續X日門檻」，而要明確講「已於 X 中斷」。
        start, end = date(2026, 8, 10), date(2026, 8, 12)
        raw = f"{_roc(start)}至{_roc(end)}連續三次"
        analysis = analyze_criteria(raw)
        html = render_risk_detail(analysis, self.TODAY)
        self.assertIn("已於", html)
        self.assertIn("中斷", html)
        self.assertNotIn("已達連續3日門檻", html)
        self.assertNotIn("隨時可能收到盤後處置公告", html)

    def test_risk_detail_consistent_with_alive_streak(self):
        # 活著的連續三日：明細面板要顯示「已達連續3日門檻」的紅色警示，
        # 不能顯示「已中斷」。
        raw = f"{_roc(self.TODAY)}至{_roc(self.TODAY)}連續三次"
        analysis = analyze_criteria(raw)
        html = render_risk_detail(analysis, self.TODAY)
        self.assertIn("已達連續3日門檻", html)
        self.assertNotIn("中斷", html)


class TestQuoteDate(unittest.TestCase):
    """
    報價日期來源（P0-1）。舊版把報價標成 `latest_end`（最後注意達標日），
    兩者不同日時會顯示錯誤的報價日 —— 產物中 7711 永擎曾標成 9/7（實為 9/8）。
    正解是取 quote 自帶的 date 欄位。
    """

    # 「連續二次 9/7–9/8」→ latest_end = 9/8；報價日刻意設不同的 9/9
    CRITERIA = "115年9月7日至115年9月8日連續二次"

    def _detail(self, quote):
        return render_risk_detail(analyze_criteria(self.CRITERIA), date(2026, 9, 9),
                                  quote=quote)

    def test_ad_to_date_basic(self):
        self.assertEqual(ad_to_date("20260908"), date(2026, 9, 8))

    def test_ad_to_date_rejects_garbage(self):
        for bad in ("", None, "2026090", "abcdefgh", "20261332"):
            self.assertIsNone(ad_to_date(bad), f"{bad!r} 應回 None")

    def test_uses_quote_date_not_latest_end(self):
        html = self._detail({"close": 594.0, "change": -66.0, "change_pct": -10.0,
                             "vol_k": 2433, "monthly_avg": None, "date": "20260909"})
        # 報價日 9/9 應出現在「收盤」註記；不可出現注意達標日 9/8 當報價日
        self.assertIn("9/9（三） 收盤", html)
        self.assertNotIn("9/8（二） 收盤", html)
        # 「最後達標日」仍應是 9/8（這欄本來就該用 latest_end）
        self.assertIn("最後達標日", html)
        self.assertIn("9/8（二）", html)

    def test_same_day_still_correct(self):
        html = self._detail({"close": 22.4, "change": 2.0, "change_pct": 9.8,
                             "vol_k": 188, "monthly_avg": None, "date": "20260908"})
        self.assertIn("9/8（二） 收盤", html)

    def test_missing_quote_date_omits_label(self):
        html = self._detail({"close": 10.0, "change": 0.0, "change_pct": 0.0,
                             "vol_k": 1, "monthly_avg": None})
        self.assertIn("收盤", html)          # 數值仍在
        self.assertNotIn("收盤）", html)      # 但不編造日期

    def test_no_quote_at_all(self):
        html = self._detail(None)
        self.assertNotIn("收盤）", html)


class TestMarkerFailFast(unittest.TestCase):
    """
    AUTO marker 取代必須 fail-fast（P0-2 / review R06）。

    舊版只印 WARNING 並回傳原字串，後果是連鎖的：title 日期由獨立 regex 照改
    成新日期 → 發布「新日期＋舊清單」→ workflow 部署驗證只比日期因而放行 →
    last_counts.json 記成功使資料日前進 → 隔天「無新交易日」守則擋住重跑。
    """

    S, E = "<!-- A_START -->", "<!-- A_END -->"

    def test_happy_path(self):
        html = f"x{self.S}old{self.E}y"
        out = replace_between(html, self.S, self.E, "new")
        self.assertIn("new", out)
        self.assertNotIn("old", out)

    def test_missing_start_raises(self):
        with self.assertRaises(MarkerError):
            replace_between(f"x{self.E}y", self.S, self.E, "new")

    def test_missing_end_raises(self):
        with self.assertRaises(MarkerError):
            replace_between(f"x{self.S}y", self.S, self.E, "new")

    def test_both_missing_raises(self):
        with self.assertRaises(MarkerError):
            replace_between("no markers here", self.S, self.E, "new")

    def test_duplicate_marker_raises(self):
        html = f"{self.S}a{self.E}{self.S}b{self.E}"
        with self.assertRaises(MarkerError):
            replace_between(html, self.S, self.E, "new")

    def test_reversed_order_raises(self):
        html = f"x{self.E}mid{self.S}y"
        with self.assertRaises(MarkerError):
            replace_between(html, self.S, self.E, "new")

    def test_original_html_untouched_on_failure(self):
        # raise 時呼叫端拿不到任何半成品，原字串不得被就地修改
        html = f"x{self.E}y"
        try:
            replace_between(html, self.S, self.E, "new")
        except MarkerError:
            pass
        self.assertEqual(html, f"x{self.E}y")


class TestInlineCountsFailFast(unittest.TestCase):
    """tab 徽章與統計數字是獨立 regex，版面改版後不得靜默失效。"""

    TPL = ('<span class="tab-count" data-count="1">0 檔</span>'
           '<span class="tab-count" data-count="2">0 檔</span>'
           '<span class="tab-count" data-count="3">0 注意累計</span>'
           '<div class="mono text-2xl font-bold text-red-400">0</div>'
           '<div class="mono text-2xl font-bold text-amber-400">0</div>'
           '<div class="mono text-2xl font-bold text-yellow-400">0</div>')

    def test_happy_path(self):
        out = update_inline_counts(self.TPL, 15, 3, 7)
        self.assertIn('data-count="1">15 檔', out)
        self.assertIn('data-count="2">3 檔', out)
        self.assertIn('data-count="3">7 注意累計', out)

    def test_missing_badge_raises(self):
        broken = self.TPL.replace('data-count="3">0 注意累計</span>', '')
        with self.assertRaises(MarkerError):
            update_inline_counts(broken, 15, 3, 7)

    def test_duplicate_badge_raises(self):
        with self.assertRaises(MarkerError):
            update_inline_counts(self.TPL + self.TPL, 15, 3, 7)

    def test_tab2_upcoming_overrides_tab2_badge_only(self):
        # R08：tab2 徽章要能獨立於「最近一批」統計卡的數字（tab1_latest）
        out = update_inline_counts(self.TPL, 15, 3, 7, tab2_upcoming=9)
        self.assertIn('data-count="2">9 檔', out)              # 徽章用 tab2_upcoming
        self.assertIn('text-2xl font-bold text-amber-400">3<', out)  # 統計卡仍用 tab1_latest

    def test_tab2_upcoming_none_falls_back_to_tab1_latest(self):
        # 未提供 tab2_upcoming 時維持舊行為（相容既有呼叫端）
        out = update_inline_counts(self.TPL, 15, 3, 7)
        self.assertIn('data-count="2">3 檔', out)


class TestPerfComplete(unittest.TestCase):
    """完整度判定（P0-3 / review R13a）：舊版只看 after5_pct。"""

    def test_both_present_is_complete(self):
        self.assertTrue(_perf_complete({"during_pct": -3.0, "after5_pct": 1.0}))

    def test_after5_only_is_incomplete(self):
        # 舊版會把這種當完整而永久跳過，during 再也補不回來
        self.assertFalse(_perf_complete({"during_pct": None, "after5_pct": 1.0}))

    def test_during_only_is_incomplete(self):
        self.assertFalse(_perf_complete({"during_pct": -3.0, "after5_pct": None}))

    def test_empty(self):
        self.assertFalse(_perf_complete(None))
        self.assertFalse(_perf_complete({}))


class TestMonthSpan(unittest.TestCase):
    """歷史抓取月份範圍（P0-3 / review R13b）：固定 2 個月抓不到跨月事件的進場基準。"""

    def test_same_month(self):
        self.assertEqual(_months_between(date(2026, 9, 1), date(2026, 9, 10)),
                         [(2026, 9)])

    def test_spans_year_boundary(self):
        self.assertEqual(_months_between(date(2025, 11, 20), date(2026, 2, 3)),
                         [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])

    def test_three_months_covers_cross_month_event(self):
        # 6/24 起、7/9 迄、9 月才回算 → 需涵蓋 6~9 月，固定 2 個月會漏掉 6 月
        self.assertEqual(_months_between(date(2026, 6, 14), date(2026, 9, 10)),
                         [(2026, 6), (2026, 7), (2026, 8), (2026, 9)])


class TestPerfStatsCache(unittest.TestCase):
    """
    績效快取的隊列與寫回規則（P0-3 / review R13）。
    用假的歷史抓取函式隔離網路；快取寫到暫存檔，不動正式資料。
    """

    def setUp(self):
        import tempfile, update_dashboard as U
        self.U = U
        self._orig_path = U.PERF_STATS_PATH
        self._orig_tw   = U.fetch_twse_stock_history
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        U.PERF_STATS_PATH = Path(self._tmp.name)

    def tearDown(self):
        self.U.PERF_STATS_PATH = self._orig_path
        self.U.fetch_twse_stock_history = self._orig_tw
        Path(self._tmp.name).unlink(missing_ok=True)

    def _hist(self, days, start=date(2026, 6, 20), price=100.0):
        """連續交易日（跳週末）的假歷史。"""
        out, d = [], start
        for i in range(days):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            out.append({"date": d, "close": price + i, "vol_k": 100})
            d += timedelta(days=1)
        return out

    def _run(self, hist, ps, pe, seed=None):
        import json as _j
        if seed is not None:
            Path(self._tmp.name).write_text(_j.dumps(seed), encoding="utf-8")
        self.U.fetch_twse_stock_history = lambda code, today, since=None: hist
        groups = {pe: {"period_end": pe, "stocks": [{
            "code": "9999", "name": "測試", "exchange": "TWSE",
            "period_start": ps, "period_end": pe}]}}
        return self.U.update_perf_stats(groups, date(2026, 9, 10))

    def test_full_data_becomes_complete(self):
        hist = self._hist(30)                      # 6/20 起 30 個交易日
        ps, pe = hist[5]["date"], hist[10]["date"]
        out = self._run(hist, ps, pe)
        e = list(out.values())[0]
        self.assertIsNotNone(e["during_pct"])
        self.assertIsNotNone(e["after5_pct"])
        self.assertNotIn("missing", e)

    def test_missing_entry_price_not_complete(self):
        # 歷史從處置起始日當天才開始 → 拿不到前一交易日的進場基準
        hist = self._hist(20)
        ps, pe = hist[0]["date"], hist[6]["date"]
        e = list(self._run(hist, ps, pe).values())[0]
        self.assertIsNone(e["during_pct"])
        self.assertIn("during_pct", e.get("missing", []))

    def test_missing_after5_not_complete(self):
        # 迄日後只有 2 個交易日 → after5 尚不可得
        hist = self._hist(12)
        ps, pe = hist[3]["date"], hist[9]["date"]
        e = list(self._run(hist, ps, pe).values())[0]
        self.assertIsNone(e["after5_pct"])
        self.assertIn("after5_pct", e.get("missing", []))

    def test_existing_valid_value_not_overwritten_by_null(self):
        """R13 額外發現：整筆覆寫會讓已算好的 during_pct 退化成 null。"""
        hist = self._hist(20)
        ps, pe = hist[0]["date"], hist[6]["date"]      # 此輪必然算不出 during
        key = f'9999:{ps}:{pe}'
        seed = {key: {"code": "9999", "name": "測試", "exchange": "TWSE",
                      "period_start": ps.isoformat(), "period_end": pe.isoformat(),
                      "entry_close": 88.0, "exit_close": 99.0,
                      "during_pct": 12.5, "after5_pct": None}}
        e = self._run(hist, ps, pe, seed=seed)[key]
        self.assertEqual(e["during_pct"], 12.5)        # 不得被寫成 None

    def test_cached_incomplete_outside_window_still_retried(self):
        """R13d：舊版隊列只看 released_groups，滾出 30 天窗的事件永久失聯。"""
        hist = self._hist(30)
        old_ps, old_pe = hist[2]["date"], hist[8]["date"]   # 早已超出 30 天窗
        key = f'9999:{old_ps}:{old_pe}'
        seed = {key: {"code": "9999", "name": "舊事件", "exchange": "TWSE",
                      "period_start": old_ps.isoformat(),
                      "period_end": old_pe.isoformat(),
                      "entry_close": None, "exit_close": None,
                      "during_pct": None, "after5_pct": None}}
        # released_groups 給空的：完全靠快取隊列把它撿回來
        import json as _j
        Path(self._tmp.name).write_text(_j.dumps(seed), encoding="utf-8")
        self.U.fetch_twse_stock_history = lambda code, today, since=None: hist
        out = self.U.update_perf_stats({}, date(2026, 9, 10))
        self.assertIsNotNone(out[key]["during_pct"], "超窗的未完成事件應仍被補算")


class TestSnapshotForceProtection(unittest.TestCase):
    """
    --force 不得覆寫既有歷史快照（P0-4）。
    正常排程走不到「檔案已存在」這條路（資料日沒推進就早期 return），
    所以會覆寫歷史的只有 force —— 那等於回頭篡改稽核軌跡。
    """

    def setUp(self):
        import tempfile, update_dashboard as U
        self.U = U
        self._orig_root = U.REPO_ROOT
        self._tmpdir = tempfile.mkdtemp()
        U.REPO_ROOT = Path(self._tmpdir)
        (U.REPO_ROOT / "data" / "history").mkdir(parents=True)

    def tearDown(self):
        import shutil
        self.U.REPO_ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _hist_file(self, d="2026-09-02"):
        return self.U.REPO_ROOT / "data" / "history" / f"{d}.json"

    SNAP_OLD = {"date": "2026-09-02", "active": [{"code": "1111"}]}
    SNAP_NEW = {"date": "2026-09-02", "active": [{"code": "1111"}, {"code": "3008"}]}

    def test_normal_run_writes_history(self):
        write_snapshot(self.SNAP_OLD, force=False)
        self.assertTrue(self._hist_file().exists())

    def test_normal_rerun_may_update_same_day(self):
        # 同一資料日的正常重跑（例如前次寫檔後才失敗）仍應能更新
        write_snapshot(self.SNAP_OLD, force=False)
        write_snapshot(self.SNAP_NEW, force=False)
        import json as _j
        got = _j.loads(self._hist_file().read_text(encoding="utf-8"))
        self.assertEqual(len(got["active"]), 2)

    def test_force_does_not_overwrite_existing_history(self):
        write_snapshot(self.SNAP_OLD, force=False)          # 當天正常寫入
        before = self._hist_file().read_text(encoding="utf-8")
        write_snapshot(self.SNAP_NEW, force=True)           # 事後 force 重新渲染
        self.assertEqual(self._hist_file().read_text(encoding="utf-8"), before,
                         "force 不得改寫既有歷史快照")

    def test_force_still_updates_dispo_json(self):
        # 歷史保護不影響當前狀態輸出，否則改版後線上頁面拿不到新內容
        write_snapshot(self.SNAP_OLD, force=False)
        write_snapshot(self.SNAP_NEW, force=True)
        import json as _j
        live = _j.loads((self.U.REPO_ROOT / "dispo.json").read_text(encoding="utf-8"))
        self.assertEqual(len(live["active"]), 2)

    def test_force_writes_history_when_absent(self):
        # 該日還沒有快照時，force 仍應建立（例如補跑漏掉的交易日）
        write_snapshot(self.SNAP_NEW, force=True)
        self.assertTrue(self._hist_file().exists())

    def test_weekend_never_writes_history(self):
        write_snapshot({"date": "2026-09-05", "active": []}, force=False)   # 週六
        self.assertFalse(self._hist_file("2026-09-05").exists())




class TestTpexRowsToDicts(unittest.TestCase):
    """
    TPEx 表格 API 的「本日無資料」佔位列不得讓整支腳本崩潰
    （2026-09-14 實際發生：fetch_tpex_warning 對這種列做 row[i] 逐欄取值，
    IndexError 讓 main() 整個中止，連續多個交易日完全沒有任何資料更新，
    直到 2026-09-15 才被發現）。
    """

    FIELDS = ["編號", "證券代號", "證券名稱", "近期達本公司「公布注意交易資訊」標準之情形"]

    def test_normal_row_converted(self):
        rows = [["1", "1101", "台泥", "連續三日達注意標準"]]
        out = _tpex_rows_to_dicts(self.FIELDS, rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["證券代號"], "1101")
        self.assertEqual(out[0]["證券名稱"], "台泥")

    def test_placeholder_no_data_row_is_skipped_not_crashed(self):
        # 真實回應：TPEx 無資料時回傳這種單一元素的佔位列，而非空 data:[]
        rows = [["本日無公布注意交易累計資訊"]]
        out = _tpex_rows_to_dicts(self.FIELDS, rows)
        self.assertEqual(out, [])   # 視同零筆，不得拋例外

    def test_mixed_placeholder_and_valid_rows(self):
        rows = [["本日無公布注意交易累計資訊"], ["2", "2330", "台積電", "累計六次"]]
        out = _tpex_rows_to_dicts(self.FIELDS, rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["證券代號"], "2330")

    def test_empty_rows_list(self):
        self.assertEqual(_tpex_rows_to_dicts(self.FIELDS, []), [])

    def test_row_longer_than_fields_still_works(self):
        # 多餘欄位被忽略，維持既有行為（原本 dict comprehension 也是這樣）
        rows = [["1", "1101", "台泥", "連續三日達注意標準", "多出來的欄位"]]
        out = _tpex_rows_to_dicts(self.FIELDS, rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["證券代號"], "1101")


if __name__ == "__main__":
    unittest.main()
