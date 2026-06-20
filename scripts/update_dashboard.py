#!/usr/bin/env python3
"""
台股處置股儀表板自動更新腳本
每個交易日盤後 (21:00 台灣時間) 由 GitHub Actions 執行。

自動更新範圍：
  - Header 日期
  - 市場脈絡 Banner（大盤數字 + 處置統計）
  - Summary Stats（處置中總數、最新批次數、二次處置數）
  - Tab 1：處置中（各批次 details 區塊）
  - Tab 2：即將被處置（最新公告批次）
  - Tab 3：注意累計 + 近期出關名單

不更新：
  - Tab 3 的觸發門檻說明卡片（靜態內容）
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

# ──────────────────────────────────────────────
# 常數
# ──────────────────────────────────────────────
TWSE_PUNISH_API  = "https://openapi.twse.com.tw/v1/announcement/punish"
TWSE_NOTETRANS   = "https://openapi.twse.com.tw/v1/announcement/notetrans"
TWSE_MI_INDEX    = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
TPEX_DISPOSAL    = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
TPEX_WARNING     = "https://www.tpex.org.tw/www/zh-tw/bulletin/warning"
TPEX_REFERER_D   = "https://www.tpex.org.tw/zh-tw/announce/market/disposal.html"
TPEX_REFERER_W   = "https://www.tpex.org.tw/zh-tw/announce/market/warning.html"

REPO_ROOT        = Path(__file__).parent.parent
HTML_PATH        = REPO_ROOT / "index.html"
STOCK_INFO_PATH  = REPO_ROOT / "data" / "stock_info.json"

SVG_CHEV = (
    '<svg class="chev w-4 h-4 text-slate-400" fill="none" stroke="currentColor" '
    'viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" '
    'stroke-width="2" d="M19 9l-7 7-7-7"/></svg>'
)


# ──────────────────────────────────────────────
# HTTP 工具
# ──────────────────────────────────────────────
def fetch_json(url, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_fetch_json(url, extra_headers=None, default=None):
    try:
        return fetch_json(url, extra_headers)
    except Exception as e:
        print(f"  WARNING: 無法取得 {url}: {e}", file=sys.stderr)
        return default


# ──────────────────────────────────────────────
# 日期解析
# ──────────────────────────────────────────────
def roc_to_date(s):
    s = s.strip().replace("/", "")
    if len(s) == 7:
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])
    elif len(s) == 6:
        y, m, d = int(s[:2]) + 1911, int(s[2:4]), int(s[4:6])
    else:
        raise ValueError(f"Unknown ROC date: {s!r}")
    return date(y, m, d)


def parse_period(period_str):
    parts = re.split(r"[～~]", period_str.strip())
    return roc_to_date(parts[0]), roc_to_date(parts[1])


def fmt_short(d):
    return f"{d.month}/{d.day}"


# ──────────────────────────────────────────────
# 文字清理
# ──────────────────────────────────────────────
def clean_name(raw):
    """
    移除 HTML 標籤，以及 TPEx 名稱中內嵌的相對 URL。
    例：'尚茂(../../mainboard/listed/company-detail.html?code=8291)' → '尚茂'
    """
    s = re.sub(r"<[^>]+>", "", str(raw))
    # TPEx 名稱格式：股名(相對URL)
    s = re.sub(r"\([^)]*(?:\.\./|https?://)[^)]*\)", "", s)
    return s.strip()


def is_regular_stock(code):
    """只保留 4 位數股票代號（跳過可轉債、權證等 5+ 位）。"""
    return len(code) == 4


def get_auction_type(detail_text, measures_text=""):
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
    m = re.search(r"第([一二三四五六七八九十]+)次處置", measures_text)
    if m:
        d = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
        return d.get(m.group(1), 1)
    return 1


# ──────────────────────────────────────────────
# 資料抓取與正規化
# ──────────────────────────────────────────────
def normalize_twse_rows(raw_rows):
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
        detail   = row.get("Detail", "")
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


def fetch_and_normalize_tpex(referer):
    raw = safe_fetch_json(TPEX_DISPOSAL, {"Referer": referer}, default={"tables": [{"fields":[],"data":[]}]})
    table  = raw["tables"][0]
    fields = table["fields"]
    rows   = table["data"]
    result = []
    for row in rows:
        d = {f: str(row[i]) for i, f in enumerate(fields)}
        code = clean_name(d.get("證券代號", ""))
        if not code or not is_regular_stock(code):
            continue
        pub_date_str = d.get("公布日期", "")
        period_str   = d.get("處置起訖時間", "")
        measures     = d.get("處置內容", "")
        try:
            ann_date = roc_to_date(pub_date_str.replace("/", ""))
            period_start, period_end = parse_period(period_str)
        except Exception:
            continue
        result.append({
            "code": code,
            "name": clean_name(d.get("證券名稱", "")),
            "exchange": "TPEx",
            "ann_date": ann_date,
            "period_start": period_start,
            "period_end": period_end,
            "auction": get_auction_type(measures),
            "disp_count": get_disposition_count(measures),
        })
    return result


def fetch_taiex():
    """回傳最新一日加權指數資料，找不到時回傳 None。"""
    data = safe_fetch_json(TWSE_MI_INDEX, default=[])
    for row in data:
        if row.get("指數") == "發行量加權股價指數":
            return row
    return None


def parse_criteria(criteria_str):
    """
    將累計標準字串轉為簡短標籤。
    輸入：'115年6月17日至115年6月18日連續二次115年6月15日至115年6月18日連續四次'
    輸出：'連續二次 (6/17–6/18) + 連續四次 (6/15–6/18)'
    """
    pattern = r'\d+年(\d+)月(\d+)日至\d+年(\d+)月(\d+)日(連續[^\d\s]+次|累計[^\d\s]+次)'
    matches = re.findall(pattern, criteria_str)
    if matches:
        return " + ".join(f"{count} ({m1}/{d1}–{m2}/{d2})"
                          for m1, d1, m2, d2, count in matches)
    m = re.search(r'(連續.+?次|累計.+?次)', criteria_str)
    return m.group(1) if m else criteria_str[:30]


def fetch_twse_notetrans():
    """注意累計次數異常（TWSE，接近處置門檻）。只回傳 4 碼股票。"""
    data = safe_fetch_json(TWSE_NOTETRANS, default=[])
    return [
        {"code": r.get("Code",""), "name": clean_name(r.get("Name","")),
         "exchange": "TWSE",
         "criteria": parse_criteria(r.get("RecentlyMetAttentionSecuritiesCriteria",""))}
        for r in data if r.get("Code") and is_regular_stock(r.get("Code",""))
    ]


def fetch_tpex_warning():
    """TPEx 注意累計（接近處置門檻）。只回傳 4 碼股票。"""
    raw = safe_fetch_json(TPEX_WARNING, {"Referer": TPEX_REFERER_W},
                          default={"tables": [{"fields":[],"data":[]}]})
    table  = raw["tables"][0]
    fields = table["fields"]
    rows   = table["data"]
    result = []
    for row in rows:
        d = {f: str(row[i]) for i, f in enumerate(fields)}
        code = clean_name(d.get("證券代號", ""))
        name = clean_name(d.get("證券名稱", ""))
        if not code or not is_regular_stock(code):
            continue
        raw_criteria = d.get("近期達本公司「公布注意交易資訊」標準之情形", "")
        result.append({"code": code, "name": name, "exchange": "TPEx",
                        "criteria": parse_criteria(raw_criteria)})
    return result


# ──────────────────────────────────────────────
# 批次分組
# ──────────────────────────────────────────────
def group_into_batches(all_rows, today):
    """
    返回 (active_groups, upcoming_groups)。
    每個 group 的 key 是 period_start。
    同一股票同一 period 只保留最新公告那筆。
    """
    dedup = {}
    for row in all_rows:
        key = (row["code"], row["period_start"])
        if key not in dedup or row["ann_date"] > dedup[key]["ann_date"]:
            dedup[key] = row

    active   = [r for r in dedup.values() if r["period_start"] <= today <= r["period_end"]]
    upcoming = [r for r in dedup.values() if r["period_start"] > today]
    released = [r for r in dedup.values()
                if r["period_end"] < today and r["period_end"] >= today - timedelta(days=30)]

    def build(rows):
        groups = defaultdict(list)
        for r in rows:
            groups[r["period_start"]].append(r)
        result = {}
        for ps, stocks in groups.items():
            twse = sorted([s for s in stocks if s["exchange"] == "TWSE"], key=lambda x: x["code"])
            tpex = sorted([s for s in stocks if s["exchange"] == "TPEx"], key=lambda x: x["code"])
            ann_date  = max(s["ann_date"]    for s in stocks)
            period_end = max(s["period_end"] for s in stocks)
            result[ps] = {"period_start": ps, "period_end": period_end,
                          "ann_date": ann_date, "stocks": twse + tpex}
        return result

    released_groups = {}
    r_dedup = {}
    for r in released:
        key = (r["code"], r["period_end"])
        if key not in r_dedup or r["ann_date"] > r_dedup[key]["ann_date"]:
            r_dedup[key] = r
    rg = defaultdict(list)
    for r in r_dedup.values():
        rg[r["period_end"]].append(r)
    for pe, stocks in rg.items():
        twse = sorted([s for s in stocks if s["exchange"] == "TWSE"], key=lambda x: x["code"])
        tpex = sorted([s for s in stocks if s["exchange"] == "TPEx"], key=lambda x: x["code"])
        released_groups[pe] = {"period_end": pe, "stocks": twse + tpex}

    return build(active), build(upcoming), released_groups


# ──────────────────────────────────────────────
# HTML 生成 — 共用
# ──────────────────────────────────────────────
def load_stock_info():
    with open(STOCK_INFO_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_stock_meta(code, stock_info):
    meta = stock_info.get(code, {})
    return {"tags": meta.get("tags",""), "sector": meta.get("sector","")}


def render_stock_row(stock, stock_info, today, pill_class_override=None, pill_label_override=None):
    meta = get_stock_meta(stock["code"], stock_info)
    tags_attr = f' data-tags="{meta["tags"]}"' if meta["tags"] else ""
    sector    = meta["sector"] or stock.get("exchange","")

    is_second  = stock.get("disp_count", 1) >= 2
    is_yellow  = pill_class_override == "pill-yellow"

    if is_yellow:
        severity_color = "bg-yellow-500"
        ticker_class   = "text-yellow-300 font-bold"
    elif is_second:
        severity_color = "bg-red-500"
        ticker_class   = "text-amber-300 font-bold"
    else:
        severity_color = "bg-amber-500"
        ticker_class   = "text-amber-300 font-bold"

    pill_class = pill_class_override or ("pill-red" if is_second else "pill-amber")
    pill_label = pill_label_override or stock.get("auction", "5分撮合")

    name_html = stock["name"]
    if is_second and not is_yellow:
        nth = "3rd+" if stock.get("disp_count",1) >= 3 else "2nd"
        name_html += f' <span class="pill pill-red ml-1">{nth}</span>'

    end_html = ""
    if "period_end" in stock and not is_yellow:
        end_label = fmt_short(stock["period_end"])
        end_html = f'<div class="text-[10px] text-slate-500 mt-1 mono">~ {end_label}</div>'
    elif "period_end" in stock and is_yellow:
        end_label = fmt_short(stock["period_end"])
        end_html = f'<div class="text-[10px] text-slate-500 mt-1 mono">{end_label} 解禁</div>'

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
        f'<span class="pill {pill_class}">{pill_label}</span>'
        f'{end_html}'
        f'</div>'
        f'</div>'
    )


def exchange_section(label, stocks, stock_info, today,
                     pill_class_override=None, pill_label_override=None, border=False):
    if not stocks:
        return ""
    border_cls = " border-t border-slate-800" if border else ""
    rows = "".join(
        render_stock_row(s, stock_info, today, pill_class_override, pill_label_override)
        for s in stocks
    )
    return (
        f'<div class="text-[10px] tracking-widest text-slate-500 uppercase '
        f'px-3 pt-3 pb-1 mono{border_cls}">{label}</div>'
        f'<div>{rows}</div>'
    )


# ──────────────────────────────────────────────
# HTML 生成 — Context Banner
# ──────────────────────────────────────────────
def render_context_banner(taiex, total_active, today_released_count,
                          upcoming_count, upcoming_date, notetrans_twse, notetrans_tpex, today):
    # 大盤數字
    if taiex:
        close  = float(taiex["收盤指數"].replace(",",""))
        change = float(taiex["漲跌點數"].replace(",",""))
        pct    = float(taiex["漲跌百分比"])
        sign   = taiex.get("漲跌","+")
        color  = "text-green-400" if sign == "+" else "text-red-400"
        sign_s = "+" if sign == "+" else "-"
        taiex_html = (
            f'<span class="{color} font-bold mono">'
            f'{close:,.0f}</span>'
            f' <span class="{color}">{sign_s}{change:,.2f} ({sign_s}{pct:.2f}%)</span>'
        )
        taiex_date = roc_to_date(taiex["日期"])
        taiex_label = f"大盤 {fmt_short(taiex_date)}"
    else:
        taiex_html  = '<span class="text-slate-500">資料取得中</span>'
        taiex_label = "大盤"

    # 即將加入
    if upcoming_count and upcoming_date:
        upcoming_html = (
            f'<span class="text-amber-400 font-bold mono">{upcoming_count} 檔</span>'
            f'<span class="text-slate-500 ml-1">({fmt_short(upcoming_date)} 起)</span>'
        )
    else:
        upcoming_html = '<span class="text-slate-400">—</span>'

    # 注意累計
    nt_count = len(notetrans_twse) + len(notetrans_tpex)
    nt_detail = (
        f'TWSE {len(notetrans_twse)} 檔'
        + (f'・TPEx {len(notetrans_tpex)} 檔' if notetrans_tpex else '')
    )
    # 列出累計股票代號
    nt_codes = ", ".join(
        f'{r["code"]}{r["name"]}' for r in (notetrans_twse + notetrans_tpex)
    )

    update_str = today.strftime("%Y/%m/%d")

    return f"""  <div class="card mb-6 p-4 border-l-4" style="border-left-color: var(--amber);">
    <div class="flex items-start gap-3">
      <div class="text-amber-400 text-xl">⚡</div>
      <div class="flex-1">
        <div class="text-sm font-semibold mb-2 text-amber-200">市場概況</div>
        <div class="flex flex-wrap gap-x-6 gap-y-1 text-xs mb-2">
          <div><span class="text-slate-400">{taiex_label}</span> {taiex_html}</div>
          <div><span class="text-slate-400">處置中</span> <span class="text-red-400 font-bold mono">{total_active} 檔</span></div>
          <div><span class="text-slate-400">今日出關</span> <span class="text-yellow-400 font-bold mono">{today_released_count} 檔</span></div>
          <div><span class="text-slate-400">即將加入</span> {upcoming_html}</div>
        </div>
        <div class="text-[11px] text-slate-500">
          注意累計 {nt_detail}：<span class="text-slate-400">{nt_codes}</span>
          ｜ 自動更新 {update_str}
        </div>
      </div>
    </div>
  </div>"""


# ──────────────────────────────────────────────
# HTML 生成 — Tab 1
# ──────────────────────────────────────────────
def render_batch_block(batch, stock_info, today, is_latest=False, is_open=True):
    ps     = batch["period_start"]
    pe     = batch["period_end"]
    stocks = batch["stocks"]
    count  = len(stocks)
    ann    = batch["ann_date"]

    is_expiring = (pe == today)

    if is_latest:
        pill = '<span class="pill pill-amber">最新公告 🆕</span>'
    elif is_expiring:
        pill = '<span class="pill pill-red">今日出關 ⏰</span>'
    else:
        pill = '<span class="pill pill-gray">處置中</span>'

    period_label = f"處置期 {fmt_short(ps)} – {fmt_short(pe)}"
    ann_label    = f"公告 {fmt_short(ann)}"
    open_attr    = " open" if is_open else ""

    twse_stocks = [s for s in stocks if s["exchange"] == "TWSE"]
    tpex_stocks = [s for s in stocks if s["exchange"] == "TPEx"]

    rows_html  = exchange_section("TWSE 上市", twse_stocks, stock_info, today)
    rows_html += exchange_section("TPEx 上櫃", tpex_stocks, stock_info, today,
                                  border=bool(twse_stocks))

    return f"""    <details class="card mb-3"{open_attr}>
      <summary class="p-3 flex items-center justify-between border-b border-slate-800">
        <div class="flex items-center gap-2">
          {pill}
          <span class="text-sm font-semibold">{period_label}</span>
          <span class="text-[10px] text-slate-500 mono">{count} 檔</span>
          <span class="text-[10px] text-slate-500 mono">{ann_label}</span>
        </div>
        {SVG_CHEV}
      </summary>
      <div>{rows_html}</div>
    </details>"""


def render_tab1_batches(active_groups, stock_info, today):
    sorted_batches = sorted(active_groups.values(), key=lambda b: b["period_start"], reverse=True)
    blocks = []
    for i, batch in enumerate(sorted_batches):
        blocks.append(render_batch_block(batch, stock_info, today,
                                         is_latest=(i == 0), is_open=(i < 3)))
    return "\n".join(blocks)


# ──────────────────────────────────────────────
# HTML 生成 — Tab 2
# ──────────────────────────────────────────────
def render_tab2_content(latest_batch, stock_info, today):
    ps     = latest_batch["period_start"]
    pe     = latest_batch["period_end"]
    stocks = latest_batch["stocks"]
    ann    = latest_batch["ann_date"]
    count  = len(stocks)

    twse_stocks = [s for s in stocks if s["exchange"] == "TWSE"]
    tpex_stocks = [s for s in stocks if s["exchange"] == "TPEx"]

    rows_html  = exchange_section(f"TWSE 上市（{len(twse_stocks)}檔）", twse_stocks, stock_info, today)
    rows_html += exchange_section(f"TPEx 上櫃（{len(tpex_stocks)}檔）", tpex_stocks, stock_info, today,
                                  border=bool(twse_stocks))

    return f"""    <div class="card">
      <div class="p-3 border-b border-slate-800 flex items-center justify-between flex-wrap gap-2">
        <div class="flex items-center gap-2">
          <span class="pill pill-amber">{fmt_short(ann)} 公告</span>
          <span class="text-sm font-semibold">處置期 {fmt_short(ps)} – {fmt_short(pe)}</span>
        </div>
        <div class="text-[10px] text-slate-500 mono">{count} 檔</div>
      </div>
      <div>{rows_html}</div>
    </div>"""


# ──────────────────────────────────────────────
# HTML 生成 — Tab 3
# ──────────────────────────────────────────────
def render_notetrans_rows(notetrans_list, stock_info, today):
    if not notetrans_list:
        return ""
    rows = []
    for r in notetrans_list:
        meta    = get_stock_meta(r["code"], stock_info)
        tags    = f' data-tags="{meta["tags"]}"' if meta["tags"] else ""
        sector  = meta["sector"] or r.get("exchange","")
        # 簡化累計說明（只取第一行，避免過長）
        criteria = r.get("criteria","").split("115年")[1] if "115年" in r.get("criteria","") else r.get("criteria","")
        criteria = re.sub(r"\s+", " ", criteria.strip())[:50]
        rows.append(
            f'<div class="table-row"{tags}>'
            f'<div class="flex items-center gap-2">'
            f'<span class="severity-bar bg-yellow-500" style="height:32px;"></span>'
            f'<span class="ticker text-yellow-300 font-bold">{r["code"]}</span>'
            f'</div>'
            f'<div>'
            f'<div class="text-sm font-semibold">{r["name"]}</div>'
            f'<div class="sector">{sector}</div>'
            f'</div>'
            f'<div class="end-date-desktop text-right">'
            f'<span class="pill pill-yellow">注意累計</span>'
            f'<div class="text-[10px] text-slate-500 mt-1">{criteria}</div>'
            f'</div>'
            f'</div>'
        )
    return "".join(rows)


def render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today):
    sections = []

    # ── Section 1: 注意累計（接近處置門檻）──
    all_notetrans = notetrans_twse + notetrans_tpex
    if all_notetrans:
        twse_nt = [r for r in all_notetrans if r["exchange"] == "TWSE"]
        tpex_nt = [r for r in all_notetrans if r["exchange"] == "TPEx"]
        nt_rows  = ""
        if twse_nt:
            nt_rows += (
                '<div class="text-[10px] tracking-widest text-slate-500 uppercase '
                'px-3 pt-3 pb-1 mono">TWSE 注意累計</div>'
                f'<div>{render_notetrans_rows(twse_nt, stock_info, today)}</div>'
            )
        if tpex_nt:
            border = " border-t border-slate-800" if twse_nt else ""
            nt_rows += (
                f'<div class="text-[10px] tracking-widest text-slate-500 uppercase '
                f'px-3 pt-3 pb-1 mono{border}">TPEx 注意累計</div>'
                f'<div>{render_notetrans_rows(tpex_nt, stock_info, today)}</div>'
            )
        sections.append(f"""    <div class="card mb-3">
      <div class="p-3 border-b border-slate-800">
        <div class="text-sm font-semibold">注意累計中（接近處置門檻）</div>
        <div class="text-[11px] text-slate-400 mt-1">達連續 3 次或累計 6 次以上之股票</div>
      </div>
      <div>{nt_rows}</div>
    </div>""")

    # ── Section 2: 近期出關（按出關日期分組）──
    if released_groups:
        sorted_releases = sorted(released_groups.items(), key=lambda x: x[0], reverse=True)
        release_blocks  = []
        for pe, grp in sorted_releases:
            stocks = grp["stocks"]
            if not stocks:
                continue
            is_today = (pe == today)
            label_pill = "今日出關 ⏰" if is_today else f"{fmt_short(pe)} 出關"
            pill_cls   = "pill-red" if is_today else "pill-gray"
            count      = len(stocks)

            twse_s = [s for s in stocks if s["exchange"] == "TWSE"]
            tpex_s = [s for s in stocks if s["exchange"] == "TPEx"]

            rows_html  = exchange_section("TWSE 上市", twse_s, stock_info, today,
                                          "pill-yellow", label_pill)
            rows_html += exchange_section("TPEx 上櫃", tpex_s, stock_info, today,
                                          "pill-yellow", label_pill,
                                          border=bool(twse_s))

            release_blocks.append(f"""      <details class="mb-0" open>
        <summary class="px-3 py-2 flex items-center gap-2 border-b border-slate-800 cursor-pointer">
          <span class="pill {pill_cls}">{label_pill}</span>
          <span class="text-[11px] text-slate-400">{count} 檔 · 30 日內再犯即升級 20 分撮合</span>
        </summary>
        <div>{rows_html}</div>
      </details>""")

        sections.append(f"""    <div class="card">
      <div class="p-3 border-b border-slate-800">
        <div class="text-sm font-semibold">近期出關 — 30 日內再犯升級風險</div>
        <div class="text-[11px] text-slate-400 mt-1">出關後 30 個交易日內再觸發，直接升級為二次處置（20 分撮合）</div>
      </div>
{"".join(release_blocks)}
    </div>""")

    if not sections:
        return '    <div class="card p-4"><div class="text-slate-500 text-sm text-center">今日無注意資料</div></div>'

    return "\n".join(sections)


# ──────────────────────────────────────────────
# HTML 生成 — 其他
# ──────────────────────────────────────────────
def render_stats(total_active, latest_count, second_count,
                 twse_count, tpex_count, latest_ann_date):
    ann_str = fmt_short(latest_ann_date) if latest_ann_date else "—"
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
    return (
        f'      <div class="mono text-sm font-bold text-slate-200">{today.strftime("%Y.%m.%d")}</div>\n'
        f'      <div class="mono text-[10px] text-slate-500">資料截止 {fmt_short(today)}</div>\n'
        f'      <div class="mono text-[10px] text-slate-500" style="margin-top: 1px;">自動更新 盤後 21:00</div>'
    )


# ──────────────────────────────────────────────
# HTML 替換
# ──────────────────────────────────────────────
def replace_between(html, start_marker, end_marker, new_content):
    pattern = rf"({re.escape(start_marker)}).*?({re.escape(end_marker)})"
    result, count = re.subn(pattern, rf"\1\n{new_content}\n\2", html, count=1, flags=re.DOTALL)
    if count == 0:
        print(f"WARNING: marker not found: {start_marker!r}", file=sys.stderr)
    return result


def update_inline_counts(html, tab1_total, tab1_latest):
    html = re.sub(r'(data-count="1">)\d+ 檔(<)', rf'\g<1>{tab1_total} 檔\2', html)
    html = re.sub(r'(data-count="2">)\d+ 檔(<)', rf'\g<1>{tab1_latest} 檔\2', html)
    html = re.sub(r'(class="mono text-2xl font-bold text-red-400">)\d+(<)',
                  rf'\g<1>{tab1_total}\2', html)
    html = re.sub(r'(class="mono text-2xl font-bold text-amber-400">)\d+(<)',
                  rf'\g<1>{tab1_latest}\2', html)
    return html


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    from datetime import datetime
    today = date.today()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 開始更新，今天: {today}")

    # 抓處置股資料
    print("  TWSE 處置股...")
    twse_raw = safe_fetch_json(TWSE_PUNISH_API, default=[])
    print(f"  TPEx 處置股...")
    tpex_rows = fetch_and_normalize_tpex(TPEX_REFERER_D)
    all_rows  = normalize_twse_rows(twse_raw) + tpex_rows
    print(f"  合計 {len(all_rows)} 筆（去重前）")

    # 抓大盤 & 注意累計
    print("  大盤指數...")
    taiex = fetch_taiex()
    print("  TWSE 注意累計...")
    notetrans_twse = fetch_twse_notetrans()
    print("  TPEx 注意累計...")
    notetrans_tpex = fetch_tpex_warning()

    # 分組
    active_groups, upcoming_groups, released_groups = group_into_batches(all_rows, today)

    if not active_groups:
        print("WARNING: 今天沒有任何處置中的股票，跳過更新。")
        return

    # 統計
    all_active = []
    seen = set()
    for b in active_groups.values():
        for s in b["stocks"]:
            if s["code"] not in seen:
                seen.add(s["code"])
                all_active.append(s)

    total_active  = len(all_active)
    twse_count    = sum(1 for s in all_active if s["exchange"] == "TWSE")
    tpex_count    = sum(1 for s in all_active if s["exchange"] == "TPEx")
    second_count  = sum(1 for s in all_active if s["disp_count"] >= 2)

    # 今日出關
    today_released = sum(len(g["stocks"]) for pe, g in released_groups.items() if pe == today)

    # 最新批次（active + upcoming 中 period_start 最大）
    all_combined = {**active_groups, **upcoming_groups}
    latest_batch = max(all_combined.values(), key=lambda b: b["period_start"])
    latest_count = len(latest_batch["stocks"])
    latest_ann   = latest_batch["ann_date"]

    upcoming_date  = latest_batch["period_start"] if latest_batch["period_start"] > today else None
    upcoming_count = latest_count if upcoming_date else 0

    print(f"  處置中: {total_active} 檔 (TWSE:{twse_count} TPEx:{tpex_count})")
    print(f"  最新批次: {latest_count} 檔 / 今日出關: {today_released} 檔")
    print(f"  二次處置: {second_count} 檔 / 注意累計: TWSE {len(notetrans_twse)} TPEx {len(notetrans_tpex)}")

    # 讀 stock_info
    stock_info = load_stock_info()

    # 生成 HTML 片段
    context_html = render_context_banner(
        taiex, total_active, today_released,
        upcoming_count, upcoming_date,
        notetrans_twse, notetrans_tpex, today,
    )
    stats_html = render_stats(total_active, latest_count, second_count,
                              twse_count, tpex_count, latest_ann)
    tab1_html  = render_tab1_batches(active_groups, stock_info, today)
    tab2_html  = render_tab2_content(latest_batch, stock_info, today)
    tab3_html  = render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today)
    date_html  = render_date_block(today)

    # 讀 HTML
    html = HTML_PATH.read_text(encoding="utf-8")

    # 替換 marker 區段
    html = replace_between(html, "<!-- AUTO:DATE_START -->",          "<!-- AUTO:DATE_END -->",          date_html)
    html = replace_between(html, "<!-- AUTO:CONTEXT_START -->",       "<!-- AUTO:CONTEXT_END -->",       context_html)
    html = replace_between(html, "<!-- AUTO:STATS_START -->",         "<!-- AUTO:STATS_END -->",         stats_html)
    html = replace_between(html, "<!-- AUTO:TAB1_BATCHES_START -->",  "<!-- AUTO:TAB1_BATCHES_END -->",  tab1_html)
    html = replace_between(html, "<!-- AUTO:TAB2_CONTENT_START -->",  "<!-- AUTO:TAB2_CONTENT_END -->",  tab2_html)
    html = replace_between(html, "<!-- AUTO:TAB3_CONTENT_START -->",  "<!-- AUTO:TAB3_CONTENT_END -->",  tab3_html)

    # 更新 tab nav 數字
    html = update_inline_counts(html, total_active, latest_count)

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"  ✓ 寫入 {HTML_PATH}")
    print("更新完成！")


if __name__ == "__main__":
    main()
