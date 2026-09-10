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
    write_snapshot,
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


if __name__ == "__main__":
    unittest.main()
