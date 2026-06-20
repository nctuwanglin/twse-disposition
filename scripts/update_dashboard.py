#!/usr/bin/env python3
"""
台股處置股儀表板自動更新腳本
每個交易日盤後 (21:00 台灣時間) 由 GitHub Actions 執行。

更新範圍：
  - Header 日期
  - Summary Stats（處置中總數、最新批次數、二次處置數）
  - Tab 1：處置中（各批次 details 區塊）
  - Tab 2：即將被處置（最新公告批次內容）

不更新：
  - Context Banner（市場脈絡敘述，需人工判讀）
  - Tab 3（未來有機會 / 注意股，需人工整理）
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime
from pathlib import Path
from collections import defaultdict

# ──────────────────────────────────────────────
# 常數
# ──────────────────────────────────────────────
TWSE_API = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_API = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
TPEX_REFERER = "https://www.tpex.org.tw/zh-tw/announce/market/disposal.html"

REPO_ROOT = Path(__file__).parent.parent
HTML_PATH = REPO_ROOT / "index.html"
STOCK_INFO_PATH = REPO_ROOT / "data" / "stock_info.json"

SVG_CHEV = '<svg class="chev w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>'


# ──────────────────────────────────────────────
# 資料抓取
# ──────────────────────────────────────────────
def fetch_json(url, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_twse_data():
    return fetch_json(TWSE_API)


def fetch_tpex_data():
    raw = fetch_json(TPEX_API, {"Referer": TPEX_REFERER})
    table = raw["tables"][0]
    fields = table["fields"]
    rows = table["data"]
    result = []
    for row in rows:
        result.append({f: str(row[i]) for i, f in enumerate(fields)})
    return result


# ──────────────────────────────────────────────
# 日期解析
# ──────────────────────────────────────────────
def roc_to_date(s):
    """
    '1150618' → date(2026,6,18)
    '115/06/18' → date(2026,6,18)
    """
    s = s.strip().replace("/", "")
    if len(s) == 7:
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])
    elif len(s) == 6:
        y, m, d = int(s[:2]) + 1911, int(s[2:4]), int(s[4:6])
    else:
        raise ValueError(f"Unknown ROC date format: {s!r}")
    return date(y, m, d)


def parse_period(period_str):
    """
    '115/06/18～115/07/02' or '115/06/18~115/07/02'
    → (date, date)
    """
    parts = re.split(r"[～~]", period_str.strip())
    return roc_to_date(parts[0]), roc_to_date(parts[1])


# ──────────────────────────────────────────────
# 資料正規化
# ──────────────────────────────────────────────
def clean_name(raw):
    """移除 HTML 連結標籤，只保留純文字股票名稱。"""
    return re.sub(r"<[^>]+>", "", str(raw)).strip()


def is_regular_stock(code):
    """只保留 4 位數股票代號（跳過可轉債、權證等 5+ 位）。"""
    return len(code) == 4


def get_auction_type(detail_text, measures_text):
    """
    從處置內容判斷撮合方式。
    優先從 detail_text 找關鍵字；fallback 到 measures_text。
    """
    if "五分鐘" in detail_text or "5分鐘" in detail_text:
        return "5分撮合"
    if "二十分鐘" in detail_text or "20分鐘" in detail_text:
        return "20分撮合"
    if "第一次處置" in measures_text:
        return "5分撮合"
    if "第二次處置" in measures_text:
        return "20分撮合"
    return "5分撮合"


def get_disposition_count(measures_text):
    """返回第幾次處置（整數），找不到回傳 1。"""
    m = re.search(r"第([一二三四五六七八九十]+)次處置", measures_text)
    if m:
        d = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        return d.get(m.group(1), 1)
    return 1


def normalize_twse_rows(raw_rows):
    """
    TWSE API 回傳的每一筆正規化成統一格式。
    同一股票可能有多筆（不同時間段的處置），全部保留。
    """
    result = []
    for row in raw_rows:
        code = row.get("Code", "").strip()
        if not code or not is_regular_stock(code):
            continue
        try:
            ann_date = roc_to_date(row["Date"])
            period_start, period_end = parse_period(row["DispositionPeriod"])
        except Exception:
            continue
        detail = row.get("Detail", "")
        measures = row.get("DispositionMeasures", "")
        result.append({
            "code": code,
            "name": clean_name(row.get("Name", "")),
            "exchange": "TWSE",
            "ann_date": ann_date,
            "period_start": period_start,
            "period_end": period_end,
            "auction": get_auction_type(detail, measures),
            "disp_count": get_disposition_count(measures),
        })
    return result


def normalize_tpex_rows(raw_rows):
    """
    TPEx API 回傳的每一筆正規化成統一格式。
    欄位: 編號, 公布日期, 證券代號, 證券名稱, 累計, 處置起訖時間, 處置原因, 處置內容, ...
    """
    result = []
    for row in raw_rows:
        code = clean_name(row.get("證券代號", ""))
        if not code or not is_regular_stock(code):
            continue
        pub_date_str = row.get("公布日期", "")
        period_str = row.get("處置起訖時間", "")
        measures = row.get("處置內容", "")
        try:
            ann_date = roc_to_date(pub_date_str.replace("/", ""))
            period_start, period_end = parse_period(period_str)
        except Exception:
            continue
        result.append({
            "code": code,
            "name": clean_name(row.get("證券名稱", "")),
            "exchange": "TPEx",
            "ann_date": ann_date,
            "period_start": period_start,
            "period_end": period_end,
            "auction": get_auction_type(measures, measures),
            "disp_count": get_disposition_count(measures),
        })
    return result


# ──────────────────────────────────────────────
# 批次分組
# ──────────────────────────────────────────────
def group_into_batches(all_rows, today):
    """
    將所有紀錄按「處置起始日期」分組，每個唯一的 period_start = 一個批次。
    回傳格式：
    {
      period_start: {
        "period_start": date,
        "period_end": date,
        "ann_date": date,      # 該批次最新的公告日
        "stocks": [row, ...]   # TWSE 優先，同交易所按代號排序
      }
    }
    只保留「今天在處置期內」的批次（start <= today <= end）。
    另外保留「今天還未開始」的批次（tomorrow batch）供 Tab 2 顯示。
    """
    # 先去重：同一股票同一 period，只保留最新公告的那筆
    dedup = {}
    for row in all_rows:
        key = (row["code"], row["period_start"])
        if key not in dedup or row["ann_date"] > dedup[key]["ann_date"]:
            dedup[key] = row

    active_rows = [r for r in dedup.values() if r["period_start"] <= today <= r["period_end"]]
    upcoming_rows = [r for r in dedup.values() if r["period_start"] > today]

    # 分組
    def build_groups(rows):
        groups = defaultdict(list)
        for r in rows:
            groups[r["period_start"]].append(r)
        result = {}
        for ps, stocks in groups.items():
            # TWSE 排前面，TPEx 在後；同交易所按代號排
            twse = sorted([s for s in stocks if s["exchange"] == "TWSE"], key=lambda x: x["code"])
            tpex = sorted([s for s in stocks if s["exchange"] == "TPEx"], key=lambda x: x["code"])
            ordered = twse + tpex
            # 批次的 ann_date 取該批次中最大的公告日（最近的）
            ann_date = max(s["ann_date"] for s in stocks)
            period_end = max(s["period_end"] for s in stocks)
            result[ps] = {
                "period_start": ps,
                "period_end": period_end,
                "ann_date": ann_date,
                "stocks": ordered,
            }
        return result

    active_groups = build_groups(active_rows)
    upcoming_groups = build_groups(upcoming_rows)
    return active_groups, upcoming_groups


# ──────────────────────────────────────────────
# HTML 生成
# ──────────────────────────────────────────────
def load_stock_info():
    with open(STOCK_INFO_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_stock_meta(code, stock_info):
    meta = stock_info.get(code, {})
    return {
        "tags": meta.get("tags", ""),
        "sector": meta.get("sector", ""),
        "exchange": meta.get("exchange", ""),  # override if available
    }


def fmt_date_short(d):
    """date → '6/18'"""
    return f"{d.month}/{d.day}"


def render_stock_row(stock, stock_info, today):
    meta = get_stock_meta(stock["code"], stock_info)
    tags_attr = f' data-tags="{meta["tags"]}"' if meta["tags"] else ""
    sector = meta["sector"] or stock["name"]

    is_second = stock["disp_count"] >= 2
    is_expiring_today = stock["period_end"] == today

    severity_color = "bg-red-500" if is_second else "bg-amber-500"
    pill_class = "pill-red" if is_second else "pill-amber"
    ticker_class = "text-amber-300 font-bold"

    name_html = stock["name"]
    if is_second:
        nth = "3rd+" if stock["disp_count"] >= 3 else "2nd"
        name_html += f' <span class="pill pill-red ml-1">{nth}</span>'

    end_label = fmt_date_short(stock["period_end"])

    return (
        f'<div class="table-row"{tags_attr}>'
        f'<div class="flex items-center gap-2">'
        f'<span class="severity-bar {severity_color}" style="height:32px;"></span>'
        f'<span class="ticker {ticker_class}">{stock["code"]}</span>'
        f'</div>'
        f'<div>'
        f'<div class="text-sm font-semibold">{name_html}</div>'
        f'<div class="sector">{sector}</div>'
        f'</div>'
        f'<div class="end-date-desktop text-right">'
        f'<span class="pill {pill_class}">{stock["auction"]}</span>'
        f'<div class="text-[10px] text-slate-500 mt-1 mono">~ {end_label}</div>'
        f'</div>'
        f'</div>'
    )


def render_batch_block(batch, stock_info, today, is_latest=False, is_open=True):
    ps = batch["period_start"]
    pe = batch["period_end"]
    stocks = batch["stocks"]
    count = len(stocks)
    ann_date = batch["ann_date"]

    # 是否今日出關（整批到期）
    is_expiring = pe == today

    # Summary pill
    if is_latest:
        summary_pill = '<span class="pill pill-amber">最新公告 🆕</span>'
    elif is_expiring:
        summary_pill = '<span class="pill pill-red">今日出關 ⏰</span>'
    else:
        summary_pill = '<span class="pill pill-gray">處置中</span>'

    period_label = f"處置期 {fmt_date_short(ps)} – {fmt_date_short(pe)}"
    ann_label = f"公告 {fmt_date_short(ann_date)}"

    open_attr = " open" if is_open else ""

    # Split by exchange
    twse_stocks = [s for s in stocks if s["exchange"] == "TWSE"]
    tpex_stocks = [s for s in stocks if s["exchange"] == "TPEx"]

    rows_html = ""
    if twse_stocks:
        rows_html += '<div class="text-[10px] tracking-widest text-slate-500 uppercase px-3 pt-3 pb-1 mono">TWSE 上市</div><div>'
        rows_html += "".join(render_stock_row(s, stock_info, today) for s in twse_stocks)
        rows_html += "</div>"
    if tpex_stocks:
        border = ' border-t border-slate-800' if twse_stocks else ''
        rows_html += f'<div class="text-[10px] tracking-widest text-slate-500 uppercase px-3 pt-3 pb-1 mono{border}">TPEx 上櫃</div><div>'
        rows_html += "".join(render_stock_row(s, stock_info, today) for s in tpex_stocks)
        rows_html += "</div>"

    return f"""    <details class="card mb-3"{open_attr}>
      <summary class="p-3 flex items-center justify-between border-b border-slate-800">
        <div class="flex items-center gap-2">
          {summary_pill}
          <span class="text-sm font-semibold">{period_label}</span>
          <span class="text-[10px] text-slate-500 mono">{count} 檔</span>
          <span class="text-[10px] text-slate-500 mono">{ann_label}</span>
        </div>
        {SVG_CHEV}
      </summary>
      <div>
        {rows_html}
      </div>
    </details>"""


def render_tab1_batches(active_groups, stock_info, today):
    """按 period_start 降冪排列（最新在最上面）。"""
    sorted_batches = sorted(active_groups.values(), key=lambda b: b["period_start"], reverse=True)
    blocks = []
    for i, batch in enumerate(sorted_batches):
        is_latest = (i == 0)
        is_open = i < 3  # 前三個展開
        blocks.append(render_batch_block(batch, stock_info, today, is_latest=is_latest, is_open=is_open))
    return "\n".join(blocks)


def render_tab2_content(latest_batch, stock_info, today):
    """Tab 2 顯示最新公告批次（period_start 最大的那批）。"""
    ps = latest_batch["period_start"]
    pe = latest_batch["period_end"]
    stocks = latest_batch["stocks"]
    ann_date = latest_batch["ann_date"]
    count = len(stocks)

    twse_stocks = [s for s in stocks if s["exchange"] == "TWSE"]
    tpex_stocks = [s for s in stocks if s["exchange"] == "TPEx"]

    def section(label, stocks_list):
        if not stocks_list:
            return ""
        n = len(stocks_list)
        rows = "".join(render_stock_row(s, stock_info, today) for s in stocks_list)
        return (
            f'<div class="text-[10px] tracking-widest text-slate-500 uppercase px-3 pt-3 pb-1 mono">'
            f'{label}（{n}檔）</div><div>{rows}</div>'
        )

    rows_html = section("TWSE 上市", twse_stocks) + section("TPEx 上櫃", tpex_stocks)
    ann_str = fmt_date_short(ann_date)
    period_str = f"{fmt_date_short(ps)} – {fmt_date_short(pe)}"

    return f"""    <div class="card">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between flex-wrap gap-2">
        <div class="flex items-center gap-2">
          <span class="pill pill-amber">{ann_str} 公告</span>
          <span class="text-sm font-semibold">處置期 {period_str}</span>
        </div>
        <div class="text-[10px] text-slate-500 mono">{count} 檔</div>
      </div>
      <div>
        {rows_html}
      </div>
    </div>"""


def render_stats(total_active, latest_count, second_count, twse_count, tpex_count, latest_ann_date):
    ann_str = fmt_date_short(latest_ann_date) if latest_ann_date else "—"
    return f"""  <div class="grid grid-cols-3 gap-3 mb-6">
    <div class="stat-block">
      <div class="text-[10px] text-slate-500 uppercase tracking-wider mono">處置中</div>
      <div class="flex items-baseline gap-2 mt-1">
        <span class="num-display text-3xl text-red-400">{total_active}</span>
        <span class="text-xs text-slate-400">檔</span>
      </div>
      <div class="text-[10px] text-slate-500 mt-1">上市 {twse_count} · 上櫃 {tpex_count}</div>
    </div>
    <div class="stat-block">
      <div class="text-[10px] text-slate-500 uppercase tracking-wider mono">最近一批</div>
      <div class="flex items-baseline gap-2 mt-1">
        <span class="num-display text-3xl text-amber-400">{latest_count}</span>
        <span class="text-xs text-slate-400">檔</span>
      </div>
      <div class="text-[10px] text-slate-500 mt-1">{ann_str} 公告</div>
    </div>
    <div class="stat-block">
      <div class="text-[10px] text-slate-500 uppercase tracking-wider mono">二次處置</div>
      <div class="flex items-baseline gap-2 mt-1">
        <span class="num-display text-3xl text-yellow-400">{second_count}</span>
        <span class="text-xs text-slate-400">檔</span>
      </div>
      <div class="text-[10px] text-slate-500 mt-1">含升級/延長</div>
    </div>
  </div>"""


def render_date_block(today):
    d = today.strftime("%Y.%m.%d")
    short = fmt_date_short(today)
    return f"""      <div class="mono text-sm font-bold text-slate-200">{d}</div>
      <div class="mono text-[10px] text-slate-500">資料截止 {short}</div>
      <div class="mono text-[10px] text-slate-500" style="margin-top: 1px;">自動更新 盤後 21:00</div>"""


# ──────────────────────────────────────────────
# HTML 替換（marker-based）
# ──────────────────────────────────────────────
def replace_between(html, start_marker, end_marker, new_content):
    pattern = rf"({re.escape(start_marker)}).*?({re.escape(end_marker)})"
    replacement = rf"\1\n{new_content}\n\2"
    result, count = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)
    if count == 0:
        print(f"WARNING: marker not found: {start_marker!r}", file=sys.stderr)
    return result


def update_inline_counts(html, tab1_total, tab1_latest, tab2_count):
    """更新 tab nav 及 section header 中的數字。"""
    # Tab nav count 1: 'data-count="1">xx 檔<'
    html = re.sub(
        r'(data-count="1">)\d+ 檔(<)',
        rf'\g<1>{tab1_total} 檔\2',
        html,
    )
    # Tab nav count 2
    html = re.sub(
        r'(data-count="2">)\d+ 檔(<)',
        rf'\g<1>{tab1_latest} 檔\2',
        html,
    )
    # Tab 1 section header big number
    html = re.sub(
        r'(class="mono text-2xl font-bold text-red-400">)\d+(<)',
        rf'\g<1>{tab1_total}\2',
        html,
    )
    # Tab 2 section header big number
    html = re.sub(
        r'(class="mono text-2xl font-bold text-amber-400">)\d+(<)',
        rf'\g<1>{tab2_count}\2',
        html,
    )
    # Tab 2 subtitle
    html = re.sub(
        r'即將被處置 <span[^>]*>\(最新公告[^<]*\)</span>',
        '即將被處置',
        html,
    )
    return html


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    today = date.today()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 開始更新，今天: {today}")

    # 抓資料
    print("  抓取 TWSE 處置股資料...")
    twse_raw = fetch_twse_data()
    print(f"  TWSE: {len(twse_raw)} 筆")

    print("  抓取 TPEx 處置股資料...")
    tpex_raw = fetch_tpex_data()
    print(f"  TPEx: {len(tpex_raw)} 筆")

    # 正規化
    twse_rows = normalize_twse_rows(twse_raw)
    tpex_rows = normalize_tpex_rows(tpex_raw)
    all_rows = twse_rows + tpex_rows

    # 分組
    active_groups, upcoming_groups = group_into_batches(all_rows, today)

    if not active_groups:
        print("WARNING: 今天沒有任何處置中的股票，跳過更新。")
        return

    # 統計
    all_active_stocks = []
    for b in active_groups.values():
        all_active_stocks.extend(b["stocks"])

    # 去重（同一股票可能在多個 batch 中）
    seen = set()
    unique_active = []
    for s in all_active_stocks:
        if s["code"] not in seen:
            seen.add(s["code"])
            unique_active.append(s)

    total_active = len(unique_active)
    twse_count = sum(1 for s in unique_active if s["exchange"] == "TWSE")
    tpex_count = sum(1 for s in unique_active if s["exchange"] == "TPEx")
    second_count = sum(1 for s in unique_active if s["disp_count"] >= 2)

    # 最新批次 = period_start 最大（含 active 和 upcoming 一天內的）
    all_groups_combined = {**active_groups, **upcoming_groups}
    latest_batch = max(all_groups_combined.values(), key=lambda b: b["period_start"])
    latest_count = len(latest_batch["stocks"])
    latest_ann_date = latest_batch["ann_date"]

    print(f"  處置中: {total_active} 檔 (TWSE:{twse_count} TPEx:{tpex_count})")
    print(f"  最新批次: {latest_count} 檔 (公告 {latest_ann_date})")
    print(f"  二次處置: {second_count} 檔")

    # 讀 stock_info
    stock_info = load_stock_info()

    # 生成 HTML 片段
    stats_html = render_stats(total_active, latest_count, second_count,
                              twse_count, tpex_count, latest_ann_date)
    tab1_html = render_tab1_batches(active_groups, stock_info, today)
    tab2_html = render_tab2_content(latest_batch, stock_info, today)
    date_html = render_date_block(today)

    # 讀入目前 HTML
    html = HTML_PATH.read_text(encoding="utf-8")

    # 替換 marker 區段
    html = replace_between(html, "<!-- AUTO:DATE_START -->", "<!-- AUTO:DATE_END -->", date_html)
    html = replace_between(html, "<!-- AUTO:STATS_START -->", "<!-- AUTO:STATS_END -->", stats_html)
    html = replace_between(html, "<!-- AUTO:TAB1_BATCHES_START -->", "<!-- AUTO:TAB1_BATCHES_END -->", tab1_html)
    html = replace_between(html, "<!-- AUTO:TAB2_CONTENT_START -->", "<!-- AUTO:TAB2_CONTENT_END -->", tab2_html)

    # 更新數字
    html = update_inline_counts(html, total_active, latest_count, latest_count)

    # 寫回
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"  ✓ 已寫入 {HTML_PATH}")
    print("更新完成！")


if __name__ == "__main__":
    main()
