#!/usr/bin/env python3
"""
台股處置股儀表板自動更新腳本
每個交易日盤後由 GitHub Actions 執行（排程 17:37 台灣時間，20:17 補跑一次；
GitHub scheduled workflows 為 best-effort，實際起跑可能再延遲數小時）。

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
import csv
import io
import re
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

# ──────────────────────────────────────────────
# 常數
# ──────────────────────────────────────────────
TWSE_PUNISH_API  = "https://openapi.twse.com.tw/v1/announcement/punish"
TWSE_NOTETRANS   = "https://openapi.twse.com.tw/v1/announcement/notetrans"
TWSE_MI_INDEX    = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json"
TWSE_STOCK_DAY   = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
TWSE_STOCK_AVG   = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_AVG_ALL?response=json"
TWSE_STOCK_HIST  = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"  # 個股月資料
TPEX_DISPOSAL    = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
TPEX_WARNING     = "https://www.tpex.org.tw/www/zh-tw/bulletin/warning"
TPEX_REFERER_D   = "https://www.tpex.org.tw/zh-tw/announce/market/disposal.html"
TPEX_REFERER_W   = "https://www.tpex.org.tw/zh-tw/announce/market/warning.html"

REPO_ROOT        = Path(__file__).parent.parent
HTML_PATH        = REPO_ROOT / "index.html"
STOCK_INFO_PATH  = REPO_ROOT / "data" / "stock_info.json"
LAST_COUNTS_PATH = REPO_ROOT / "data" / "last_counts.json"  # 資料源健康度狀態檔

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


def fetch_text(url, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "text/csv,application/json,*/*",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")


def safe_fetch_text(url, extra_headers=None, default=""):
    try:
        return fetch_text(url, extra_headers)
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
    data = safe_fetch_json(TWSE_MI_INDEX, default={})
    rows = data.get("data", [])
    if not rows:
        return None
    last = rows[-1]
    try:
        close  = float(last[4].replace(",", ""))
        change = float(last[5].replace(",", ""))
        prev   = close - change
        pct    = (change / prev * 100) if prev != 0 else 0.0
        roc_date = last[0].replace("/", "")  # '115/06/23' → '1150623'
        return {
            "收盤指數":   f"{close:.2f}",
            "漲跌點數":   f"{abs(change):.2f}",
            "漲跌百分比": f"{abs(pct):.2f}",
            "漲跌":       "+" if change >= 0 else "-",
            "日期":       roc_date,
        }
    except (ValueError, IndexError):
        return None


def fetch_twse_stock_quotes():
    """
    回傳 TWSE 全股日資料 dict：{code: {close, change, change_pct, vol_k, monthly_avg}}.
    - vol_k: 成交量（千股 = 張）
    - monthly_avg: 月均價（STOCK_DAY_AVG_ALL）
    TPEx 股票不在此資料中，呼叫後查不到會回傳 None。
    """
    # STOCK_DAY_ALL 已由 TWSE 改為 CSV 格式（欄位：日期,證券代號,證券名稱,
    # 成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數）
    resp_date, day_quotes = _fetch_twse_stock_day_csv()

    avg_resp = safe_fetch_json(TWSE_STOCK_AVG, default={})
    avg_map = {}
    for row in avg_resp.get("data", []):
        if len(row) >= 4 and row[0] and row[3]:
            avg_map[row[0]] = row[3]

    result = {}
    for code, (close, change, vol_k) in day_quotes.items():
        try:
            prev   = close - change if close is not None else None
            pct    = (change / prev * 100) if (prev and prev != 0) else None
            mavg_s = avg_map.get(code, "")
            mavg   = float(mavg_s.replace(",", "")) if mavg_s else None
        except (ValueError, TypeError):
            continue
        result[code] = {
            "close": close, "change": change, "change_pct": pct,
            "vol_k": vol_k, "monthly_avg": mavg,
            "date": resp_date,
        }
    return result


def _fetch_twse_stock_day_csv():
    """
    抓取 STOCK_DAY_ALL（TWSE 全股當日收盤）CSV，回傳
    (resp_date_yyyymmdd, {code: (close, change, vol_k)})。
    CSV 欄位索引：0=日期(民國) 1=代號 2=名稱 3=成交股數 8=收盤價 9=漲跌價差
    """
    text = safe_fetch_text(TWSE_STOCK_DAY, default="")
    resp_date = ""
    quotes = {}
    if not text:
        return resp_date, quotes
    for r in csv.reader(io.StringIO(text)):
        # 僅接受資料列：第一欄為 7 碼民國日期（跳過標題與雜訊列）
        if len(r) < 11 or not (r[0].isdigit() and len(r[0]) == 7):
            continue
        if not resp_date:
            resp_date = str(int(r[0][:3]) + 1911) + r[0][3:]  # 民國→西元 yyyymmdd
        code = r[1].strip()
        if not code:
            continue
        try:
            close  = float(r[8].replace(",", "")) if r[8].strip() not in ("", "--", "---") else None
            change = float(r[9].replace(",", "")) if r[9].strip() not in ("", "--", "---") else 0.0
            vol_k  = int(r[3].replace(",", "")) // 1000 if r[3].strip() else 0
        except (ValueError, TypeError):
            continue
        quotes[code] = (close, change, vol_k)
    return resp_date, quotes


_CN_INT = {"一":1,"二":2,"三":3,"四":4,"五":5,
           "六":6,"七":7,"八":8,"九":9,"十":10,"十一":11,"十二":12}
_DOW_ZH = ["一","二","三","四","五","六","日"]

def cn_to_int(s):
    return _CN_INT.get(s, 0)

def next_weekday(d):
    """返回 d 之後的下一個交易日（僅跳週末，不排除假日）。"""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt

def fmt_weekday(d):
    return f"{d.month}/{d.day}（{_DOW_ZH[d.weekday()]}）"


def analyze_criteria(raw):
    """
    解析原始累計標準字串，回傳結構化資料。
    例：'115年6月17日至115年6月18日連續二次115年6月15日至115年6月18日連續四次'
    回傳：{entries, max_consecutive, latest_end}
    """
    pattern = r'(\d+)年(\d+)月(\d+)日至(\d+)年(\d+)月(\d+)日(連續|累計)([一二三四五六七八九十]+)次'
    matches = re.findall(pattern, raw)
    if not matches:
        return None
    entries = []
    for y1, m1, d1, y2, m2, d2, kind, cn in matches:
        try:
            start = date(int(y1)+1911, int(m1), int(d1))
            end   = date(int(y2)+1911, int(m2), int(d2))
            cnt   = cn_to_int(cn)
            entries.append({"start": start, "end": end, "kind": kind, "count": cnt, "cn": cn})
        except Exception:
            pass
    if not entries:
        return None
    max_consec = max((e["count"] for e in entries if e["kind"] == "連續"), default=0)
    latest_end = max(e["end"] for e in entries)
    return {"entries": entries, "max_consecutive": max_consec, "latest_end": latest_end}


def render_risk_detail(analysis, today, quote=None, extra_html=""):
    """
    生成處置門檻進度詳情，放在 .table-row 內 grid-column: 1/-1 的欄位。
    quote: dict {close, change, change_pct, vol_k, monthly_avg} 或 None
    extra_html: 在進度條後追加的額外內容（如觸發條件區塊）
    """
    if not analysis:
        return ""

    max_c       = analysis["max_consecutive"]
    latest_end  = analysis["latest_end"]
    next_imm    = next_weekday(latest_end)

    streak_broken = next_imm <= today
    risk_date     = next_weekday(today) if next_imm <= today else next_imm

    # ── 達標摘要 ──
    parts = []
    for e in analysis["entries"]:
        parts.append(
            f'<span class="mono text-yellow-200">{e["kind"]}{e["cn"]}次</span>'
            f'<span class="text-slate-500"> ({fmt_short(e["start"])}–{fmt_short(e["end"])})</span>'
        )
    summary_html = (
        f'<div class="flex flex-wrap gap-x-3 gap-y-0.5 mb-1.5">'
        f'<span class="text-slate-500">最近達標：</span>'
        + "、".join(parts)
        + f'<span class="text-slate-500 ml-2">最後達標日：</span>'
        f'<span class="mono text-slate-300">{fmt_weekday(latest_end)}</span>'
        f'</div>'
    )

    # ── 最後交易日數值（收盤、漲跌、量、偏離月均）──
    if quote and quote.get("close") is not None:
        close = quote["close"]
        chg   = quote.get("change", 0) or 0
        pct   = quote.get("change_pct")
        vol_k = quote.get("vol_k", 0)
        mavg  = quote.get("monthly_avg")

        sign  = "+" if chg >= 0 else ""
        clr   = "text-green-400" if chg >= 0 else "text-red-400"
        pct_s = f" ({sign}{pct:.1f}%)" if pct is not None else ""
        vol_s = f"{vol_k:,} 張" if vol_k else "—"

        # 偏離月均價
        dev_s = ""
        if mavg and mavg > 0:
            dev = (close - mavg) / mavg * 100
            dev_clr = "text-red-400" if dev > 20 else ("text-amber-400" if dev > 10 else "text-slate-300")
            dev_s = f'<span class="text-slate-500 ml-2">偏離月均</span><span class="mono {dev_clr} ml-0.5">{"+" if dev>=0 else ""}{dev:.1f}%</span>'

        quote_html = (
            f'<div class="flex flex-wrap gap-x-4 gap-y-0.5 mb-1.5 border-l-2 border-slate-700 pl-2">'
            f'<span class="text-slate-500">收盤</span>'
            f'<span class="mono {clr} font-semibold">{close:.2f}</span>'
            f'<span class="mono {clr}">{sign}{chg:.2f}{pct_s}</span>'
            f'<span class="text-slate-500 ml-2">量</span>'
            f'<span class="mono text-slate-200">{vol_s}</span>'
            + dev_s
            + f'<span class="text-slate-600 ml-1">（{fmt_weekday(latest_end)} 收盤）</span>'
            f'</div>'
        )
    else:
        quote_html = ""

    # ── 風險預警 ──
    streak_note = "（需維持連續）" if streak_broken and max_c < 3 else ""

    if max_c >= 5:
        warn_html = (
            f'<div class="flex items-start gap-1.5">'
            f'<span class="text-red-400 shrink-0 font-bold">！</span>'
            f'<span class="text-red-300">已達連續{max_c}日 ≥ 門檻，隨時可能收到盤後處置公告</span>'
            f'</div>'
        )
    elif max_c >= 3:
        warn_html = (
            f'<div class="flex items-start gap-1.5">'
            f'<span class="text-red-400 shrink-0 font-bold">！</span>'
            f'<span class="text-red-300">已達連續{max_c}日門檻，'
            f'<span class="mono font-bold">{fmt_weekday(risk_date)}</span> 盤後可能收到處置公告'
            f'（第一次：5分撮合）</span>'
            f'</div>'
        )
    elif max_c == 2:
        warn_html = (
            f'<div class="flex items-start gap-1.5">'
            f'<span class="text-amber-400 shrink-0">⚠</span>'
            f'<span class="text-amber-200">'
            f'<span class="mono font-bold">{fmt_weekday(risk_date)}</span> '
            f'若再達注意標準{streak_note} → 觸發 <span class="font-semibold text-white">連續3日門檻</span>'
            f' → <span class="text-red-300">第一次處置（5分撮合）</span></span>'
            f'</div>'
        )
    elif max_c == 1:
        risk_date2 = next_weekday(risk_date)
        warn_html = (
            f'<div class="flex items-start gap-1.5">'
            f'<span class="text-yellow-500 shrink-0">●</span>'
            f'<span class="text-slate-300">'
            f'需 <span class="mono">{fmt_weekday(risk_date)}</span> +'
            f' <span class="mono">{fmt_weekday(risk_date2)}</span> 連續達標，'
            f'才觸發連續3日門檻</span>'
            f'</div>'
        )
    else:
        warn_html = ""

    # ── 進度條（連續3日門檻）──
    filled    = min(max_c, 3)
    empty     = max(0, 3 - filled)
    exceeded  = max_c >= 3
    bar_color = "bg-red-500" if exceeded else "bg-yellow-500"
    bar = ("".join(f'<span class="inline-block w-5 h-1.5 rounded-sm {bar_color} mr-0.5"></span>'
                   for _ in range(filled))
           + "".join(f'<span class="inline-block w-5 h-1.5 rounded-sm bg-slate-700 mr-0.5"></span>'
                     for _ in range(empty)))
    status_txt = "已達門檻" if exceeded else f"{max_c}/3"
    bar_html = (
        f'<div class="flex items-center gap-2 mt-1.5">'
        f'<div class="flex items-center">{bar}</div>'
        f'<span class="text-slate-500">{status_txt} 連續3日門檻</span>'
        f'</div>'
    )

    return (
        f'<div style="grid-column:1/-1" '
        f'class="mt-1 pt-2 border-t border-slate-800/50 text-[11px] leading-5 pb-1">'
        + summary_html + quote_html + warn_html + bar_html + extra_html
        + '</div>'
    )


def parse_criteria(raw):
    """
    供 fetch 函數使用的短標籤版，仍保留向後相容。
    """
    a = analyze_criteria(raw)
    if a:
        parts = [f"{e['kind']}{e['cn']}次 ({fmt_short(e['start'])}–{fmt_short(e['end'])})"
                 for e in a["entries"]]
        return " + ".join(parts)
    m = re.search(r'(連續.+?次|累計.+?次)', raw)
    return m.group(1) if m else raw[:30]


def fetch_twse_notetrans():
    """注意累計次數異常（TWSE，接近處置門檻）。只回傳 4 碼股票。"""
    data = safe_fetch_json(TWSE_NOTETRANS, default=[])
    return [
        {"code": r.get("Code",""), "name": clean_name(r.get("Name","")),
         "exchange": "TWSE",
         "criteria":     parse_criteria(r.get("RecentlyMetAttentionSecuritiesCriteria","")),
         "raw_criteria": r.get("RecentlyMetAttentionSecuritiesCriteria","")}
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
        rc = d.get("近期達本公司「公布注意交易資訊」標準之情形", "")
        result.append({"code": code, "name": name, "exchange": "TPEx",
                        "criteria": parse_criteria(rc), "raw_criteria": rc})
    return result


def fetch_twse_stock_history(code, today):
    """
    抓取個股最近 2 個月日成交資料（TWSE 舊版 exchangeReport/STOCK_DAY API）。
    回傳 [{date, close, vol_k}] 由舊到新排列，已去重。
    Fields: [日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數, 註記]
    """
    records = {}
    for months_back in range(2):
        y, m = today.year, today.month - months_back
        if m <= 0:
            m += 12
            y -= 1
        url = f"{TWSE_STOCK_HIST}?response=json&date={y}{m:02d}01&stockNo={code}"
        data = safe_fetch_json(url, default={})
        if not isinstance(data, dict) or data.get("stat") != "OK":
            continue
        for row in data.get("data", []):
            try:
                d      = roc_to_date(row[0].replace("/", ""))
                close  = float(row[6].replace(",", ""))
                vol_k  = int(row[1].replace(",", "")) // 1000
                if close > 0:
                    records[d] = {"date": d, "close": close, "vol_k": vol_k}
            except (ValueError, IndexError):
                pass
    return sorted(records.values(), key=lambda x: x["date"])


def calculate_attention_thresholds(history):
    """
    依 TWSE 注意交易資訊標準計算觸發門檻（使用免費資料可計算的條款）。
    - 第一款: 最近 6 個營業日收盤累積漲幅 ≥ 20%
    - 第二款: 最近 30 個營業日收盤累積漲幅 ≥ 30%
    - 量參考: 60 日平均量（第三/六款部分依據，集中度條件不在此計算）
    回傳 dict 或 None（資料不足時）。
    """
    if len(history) < 7:
        return None

    latest       = history[-1]
    current_close = latest["close"]

    def nth_before(n):
        idx = len(history) - 1 - n
        return history[idx] if idx >= 0 else None

    result = {
        "current_close": current_close,
        "latest_date":   latest["date"],
        "history_days":  len(history),
    }

    ref6 = nth_before(6)
    if ref6:
        cum6 = (current_close - ref6["close"]) / ref6["close"] * 100
        thr6 = ref6["close"] * 1.20
        result["clause1"] = {
            "ref_date":  ref6["date"],
            "ref_close": ref6["close"],
            "cum_pct":   cum6,
            "threshold": thr6,
            "diff_pct":  (thr6 - current_close) / current_close * 100,
            "triggered": current_close >= thr6,
        }

    ref30 = nth_before(30)
    if ref30:
        cum30 = (current_close - ref30["close"]) / ref30["close"] * 100
        thr30 = ref30["close"] * 1.30
        result["clause2"] = {
            "ref_date":  ref30["date"],
            "ref_close": ref30["close"],
            "cum_pct":   cum30,
            "threshold": thr30,
            "diff_pct":  (thr30 - current_close) / current_close * 100,
            "triggered": current_close >= thr30,
        }

    # 量：60 日均量（不含今日，作為第三/六款量條件參考）
    past = history[-min(60, len(history)):-1]
    if len(past) >= 10:
        avg_vol = sum(r["vol_k"] for r in past) / len(past)
        result["vol_avg"] = avg_vol
        result["vol_days"] = len(past)

    return result


def render_attention_conditions(thresholds, trade_date):
    """
    生成注意股觸發條件 HTML。不含外層 grid-column wrapper。
    trade_date: 資料所屬的交易日（用於標題顯示）。
    """
    if not thresholds:
        return ""

    lines = []
    current = thresholds["current_close"]

    lines.append(
        f'<div class="text-[10px] text-slate-500 uppercase tracking-wider mt-2.5 mb-1 mono '
        f'border-t border-slate-700/50 pt-2">'
        f'注意股觸發條件 {fmt_weekday(trade_date)}（任一即可）</div>'
    )

    def condition_row(label, cum_pct, cum_threshold_pct, thr_price, diff_pct, triggered):
        cum_s   = f"{cum_pct:+.1f}%"
        cum_clr = "text-red-400" if cum_pct >= cum_threshold_pct else (
                  "text-amber-300" if cum_pct >= cum_threshold_pct * 0.7 else "text-slate-400")
        if triggered:
            status_s = '<span class="text-red-300 font-semibold">今日已達標</span>'
        elif diff_pct <= 0:
            # threshold below current (stock can fall and still trigger)
            gap_s    = f"{abs(diff_pct):.1f}%"
            status_s = (
                f'收盤需 ≥ <span class="mono text-amber-300 font-semibold">{thr_price:.2f}</span>'
                f'<span class="text-green-600">（跌 {gap_s} 仍觸發）</span>'
            )
        elif diff_pct <= 5:
            status_s = (
                f'收盤需 ≥ <span class="mono text-amber-300 font-semibold">{thr_price:.2f}</span>'
                f'<span class="text-slate-500">（還差 {diff_pct:.1f}%）</span>'
            )
        else:
            status_s = (
                f'收盤需 ≥ <span class="mono text-slate-300">{thr_price:.2f}</span>'
                f'<span class="text-slate-600">（還差 {diff_pct:.1f}%）</span>'
            )
        return (
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">{label}</span>'
            f'<span class="text-slate-400">'
            f'累積 <span class="mono {cum_clr}">{cum_s}</span>'
            f'<span class="text-slate-600">（門檻≥{cum_threshold_pct:.0f}%）</span>'
            f' → {status_s}'
            f'</span></div>'
        )

    c1 = thresholds.get("clause1")
    if c1:
        ref_s = fmt_short(c1["ref_date"])
        lines.append(
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">第一款</span>'
            f'<span class="text-slate-400">'
            f'6日累積 <span class="mono {"text-red-400" if c1["cum_pct"]>=20 else ("text-amber-300" if c1["cum_pct"]>=14 else "text-slate-400")}">'
            f'{c1["cum_pct"]:+.1f}%</span>'
            f'<span class="text-slate-600">（{ref_s} 起，門檻≥20%）</span>'
            f' → '
            + (
                '<span class="text-red-300 font-semibold">今日已達標</span>'
                if c1["triggered"] else
                (
                    f'收盤需 ≥ <span class="mono {"text-amber-300" if c1["diff_pct"]<=5 else "text-slate-300"} font-semibold">{c1["threshold"]:.2f}</span>'
                    + (f'<span class="text-green-600">（跌 {abs(c1["diff_pct"]):.1f}% 仍觸發）</span>'
                       if c1["diff_pct"] <= 0 else
                       f'<span class="text-slate-500">（還差 {c1["diff_pct"]:.1f}%）</span>')
                )
            )
            + f'</span></div>'
        )

    c2 = thresholds.get("clause2")
    if c2:
        ref_s = fmt_short(c2["ref_date"])
        lines.append(
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">第二款</span>'
            f'<span class="text-slate-400">'
            f'30日累積 <span class="mono {"text-red-400" if c2["cum_pct"]>=30 else ("text-amber-300" if c2["cum_pct"]>=21 else "text-slate-400")}">'
            f'{c2["cum_pct"]:+.1f}%</span>'
            f'<span class="text-slate-600">（{ref_s} 起，門檻≥30%）</span>'
            f' → '
            + (
                '<span class="text-red-300 font-semibold">今日已達標</span>'
                if c2["triggered"] else
                (
                    f'收盤需 ≥ <span class="mono {"text-amber-300" if c2["diff_pct"]<=5 else "text-slate-300"} font-semibold">{c2["threshold"]:.2f}</span>'
                    + (f'<span class="text-green-600">（跌 {abs(c2["diff_pct"]):.1f}% 仍觸發）</span>'
                       if c2["diff_pct"] <= 0 else
                       f'<span class="text-slate-500">（還差 {c2["diff_pct"]:.1f}%）</span>')
                )
            )
            + f'</span></div>'
        )

    vol_avg = thresholds.get("vol_avg")
    if vol_avg is not None:
        days = thresholds.get("vol_days", 0)
        vol3x = vol_avg * 3
        lines.append(
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">量條件</span>'
            f'<span class="text-slate-400">'
            f'{days}日均量 <span class="mono text-slate-300">{vol_avg:,.0f} 張</span>'
            f' → 量需 > <span class="mono text-slate-300">{vol3x:,.0f} 張</span>'
            f'<span class="text-slate-600 text-[10px]">（×3，另需集中度條件）</span>'
            f'</span></div>'
        )

    return "".join(lines)


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
# 結構化輸出：dispo.json（隨 Pages 發佈）+ data/history/ 每日快照
# 內容為確定性（不含執行時間戳），同日重跑產生相同位元組 → 補跑不產生空 diff。
# ──────────────────────────────────────────────
def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _stock_entry(s, stock_quotes):
    q = (stock_quotes or {}).get(s["code"]) or {}
    return {
        "code": s["code"], "name": s["name"], "exchange": s["exchange"],
        "ann_date": s["ann_date"].isoformat(),
        "period_start": s["period_start"].isoformat(),
        "period_end": s["period_end"].isoformat(),
        "auction": s["auction"], "disp_count": s["disp_count"],
        # TPEx 個股不在 TWSE 全股報價內，close 等欄位為 null（報價補齊屬第三階段）
        "close": q.get("close"), "change_pct": q.get("change_pct"),
        "vol_k": q.get("vol_k"),
    }


def build_snapshot(today, taiex, active_stocks, upcoming_stocks,
                   notetrans_twse, notetrans_tpex, nt_thresholds, stock_quotes,
                   counts, source="live"):
    def nt_entry(r):
        e = {"code": r["code"], "name": r["name"], "exchange": r["exchange"],
             "criteria": r["criteria"], "raw_criteria": r["raw_criteria"]}
        t = (nt_thresholds or {}).get(r["code"])
        if t:
            e["thresholds"] = _jsonable(t)
        return e

    # active 為「處置紀錄」列表：同一代碼可能有多筆重疊處置（如先 5分撮合、
    # 期間內再犯升級 20分撮合）。counts.active 計唯一代碼；下游取現行有效
    # 管制時應選 period_end 最大（並列時 disp_count 最大）的那筆。
    key = lambda s: (s["exchange"], s["code"], s["period_start"], s["period_end"])
    return {
        "schema": 1,
        "date": today.isoformat(),
        "source": source,
        "taiex": taiex,
        "counts": counts,
        "active":    [_stock_entry(s, stock_quotes) for s in sorted(active_stocks, key=key)],
        "upcoming":  [_stock_entry(s, stock_quotes) for s in sorted(upcoming_stocks, key=key)],
        "notetrans": [nt_entry(r) for r in notetrans_twse + notetrans_tpex],
    }


def write_snapshot(snap):
    text = json.dumps(snap, ensure_ascii=False, indent=1) + "\n"
    (REPO_ROOT / "dispo.json").write_text(text, encoding="utf-8")
    # 歷史庫只收交易日快照；週末手動執行時資料與週五相同，不重複入庫
    if date.fromisoformat(snap["date"]).weekday() >= 5:
        return
    hist_dir = REPO_ROOT / "data" / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / f"{snap['date']}.json").write_text(text, encoding="utf-8")


# ──────────────────────────────────────────────
# HTML 生成 — 共用
# ──────────────────────────────────────────────
_SECTOR_TAG_RULES = [
    # IC 設計
    ("IC設計",       "icdesign"),
    ("類比",         "icdesign"),
    # IC 製造 / 晶圓 / 面板 / 記憶體
    ("矽晶圓",       "icmanufacturing"),
    ("晶圓",         "icmanufacturing"),
    ("面板",         "icmanufacturing"),
    ("功率半導體",   "icmanufacturing"),
    ("記憶體",       "icmanufacturing"),
    ("DRAM",         "icmanufacturing"),
    ("NOR",          "icmanufacturing"),
    ("磊晶",         "icmanufacturing"),
    ("GaAs",         "icmanufacturing"),
    ("分離式",       "icmanufacturing"),
    ("整流器",       "icmanufacturing"),
    # 封裝測試
    ("封測",         "packaging"),
    ("封裝",         "packaging"),
    ("導線架",       "packaging"),
    # PCB / 基板
    ("PCB",          "pcb"),
    ("銅箔",         "pcb"),
    ("CCL",          "pcb"),
    ("基板",         "pcb"),
    # 被動元件
    ("MLCC",         "passive"),
    ("被動元件",     "passive"),
    ("電阻",         "passive"),
    ("電容",         "passive"),
    ("陶瓷",         "passive"),
    ("電感",         "passive"),
    # 光電 / 光學
    ("光通訊",       "optical"),
    ("光學",         "optical"),
    ("光電",         "optical"),
    # 衛星
    ("衛星",         "satellite"),
    # 電動車 / 電池 / 電源
    ("EV",           "power"),
    ("電源",         "power"),
    ("電池",         "power"),
    ("儲能",         "power"),
]


def sector_to_tags(sector: str) -> set:
    """根據 sector 文字自動推導 tag 集合。"""
    return {tag for kw, tag in _SECTOR_TAG_RULES if kw in sector}


def load_stock_info():
    with open(STOCK_INFO_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_stock_meta(code, stock_info):
    meta   = stock_info.get(code, {})
    sector = meta.get("sector", "")
    manual = set(meta.get("tags", "").split())
    auto   = sector_to_tags(sector)
    tags   = " ".join(sorted(manual | auto))
    return {"tags": tags, "sector": sector}


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
          {f'<div><span class="text-slate-400">即將加入</span> {upcoming_html}</div>' if upcoming_count else ''}
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
def render_notetrans_rows(notetrans_list, stock_info, today, stock_quotes=None, nt_thresholds=None):
    if not notetrans_list:
        return ""
    sq  = stock_quotes   or {}
    thr = nt_thresholds  or {}
    rows = []
    for r in notetrans_list:
        meta     = get_stock_meta(r["code"], stock_info)
        tags     = f' data-tags="{meta["tags"]}"' if meta["tags"] else ""
        sector   = meta["sector"] or r.get("exchange","")
        analysis = analyze_criteria(r.get("raw_criteria",""))
        max_c    = analysis["max_consecutive"] if analysis else 0
        quote    = sq.get(r["code"])

        if max_c >= 3:
            sev_color = "bg-red-500"
            ticker_cl = "text-red-300 font-bold"
        else:
            sev_color = "bg-yellow-500"
            ticker_cl = "text-yellow-300 font-bold"

        if max_c >= 3:
            pill_extra = f'<span class="pill pill-red ml-1">超門檻</span>'
        elif max_c == 2:
            pill_extra = f'<span class="pill pill-amber ml-1">差1日</span>'
        else:
            pill_extra = ''

        # 觸發條件（僅 TWSE 有歷史 API）
        t_data    = thr.get(r["code"])
        # 用最新資料日期作為標題日期；若資料是今日則顯示今日
        cond_date = t_data["latest_date"] if t_data else today
        cond_html = render_attention_conditions(t_data, cond_date) if t_data else ""
        detail_html = render_risk_detail(analysis, today, quote=quote, extra_html=cond_html)

        rows.append(
            f'<div class="table-row"{tags}>'
            f'<div class="flex items-center gap-2">'
            f'<span class="severity-bar {sev_color}" style="height:32px;"></span>'
            f'<span class="ticker {ticker_cl}">{r["code"]}</span>'
            f'</div>'
            f'<div>'
            f'<div class="text-sm font-semibold">{r["name"]}</div>'
            f'<div class="sector">{sector}</div>'
            f'</div>'
            f'<div class="end-date-desktop text-right">'
            f'<span class="pill pill-yellow">注意累計</span>{pill_extra}'
            f'</div>'
            + detail_html
            + '</div>'
        )
    return "".join(rows)


def render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today,
                stock_quotes=None, nt_thresholds=None):
    sections = []
    sq  = stock_quotes  or {}
    thr = nt_thresholds or {}

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
                f'<div>{render_notetrans_rows(twse_nt, stock_info, today, sq, thr)}</div>'
            )
        if tpex_nt:
            border = " border-t border-slate-800" if twse_nt else ""
            nt_rows += (
                f'<div class="text-[10px] tracking-widest text-slate-500 uppercase '
                f'px-3 pt-3 pb-1 mono{border}">TPEx 注意累計</div>'
                f'<div>{render_notetrans_rows(tpex_nt, stock_info, today, sq)}</div>'
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


def update_inline_counts(html, tab1_total, tab1_latest, tab3_nt=0):
    html = re.sub(r'(data-count="1">)[^<]+(<)', rf'\g<1>{tab1_total} 檔\2', html)
    html = re.sub(r'(data-count="2">)[^<]+(<)', rf'\g<1>{tab1_latest} 檔\2', html)
    html = re.sub(r'(data-count="3">)[^<]+(<)', rf'\g<1>{tab3_nt} 注意累計\2', html)
    html = re.sub(r'(class="mono text-2xl font-bold text-red-400">)[^<]+(<)',
                  rf'\g<1>{tab1_total}\2', html)
    html = re.sub(r'(class="mono text-2xl font-bold text-amber-400">)[^<]+(<)',
                  rf'\g<1>{tab1_latest}\2', html)
    html = re.sub(r'(class="mono text-2xl font-bold text-yellow-400">)[^<]+(<)',
                  rf'\g<1>{tab3_nt}\2', html)
    return html


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    force = "--force" in sys.argv
    # GitHub Actions runner 是 UTC；排程延遲可能跨日，一律以台灣時區取「今天」
    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    today  = now_tw.date()
    print(f"[{now_tw:%Y-%m-%d %H:%M:%S}] 開始更新，今天: {today}" + ("（--force）" if force else ""))

    # 抓處置股資料
    print("  TWSE 處置股...")
    twse_raw  = safe_fetch_json(TWSE_PUNISH_API, default=[])
    twse_rows = normalize_twse_rows(twse_raw)
    print(f"  TPEx 處置股...")
    tpex_rows = fetch_and_normalize_tpex(TPEX_REFERER_D)

    # 資料源防呆：任一處置源為空幾乎必是 API 故障（正常時兩市場都有處置股）。
    # 缺一源仍往下走會發佈「只剩半個市場」的錯誤頁面，寧可中止讓 workflow 亮紅燈。
    if not force:
        if not twse_rows:
            sys.exit("ERROR: TWSE 處置股資料為空，疑似 API 故障，中止更新（確認正常可用 --force）")
        if not tpex_rows:
            sys.exit("ERROR: TPEx 處置股資料為空，疑似 API 故障，中止更新（確認正常可用 --force）")

    all_rows = twse_rows + tpex_rows
    print(f"  合計 {len(all_rows)} 筆（去重前）")

    # 抓大盤 & 注意累計 & 個股報價
    print("  大盤指數...")
    taiex = fetch_taiex()
    print("  TWSE 注意累計...")
    notetrans_twse = fetch_twse_notetrans()
    print("  TPEx 注意累計...")
    notetrans_tpex = fetch_tpex_warning()
    print("  TWSE 個股報價...")
    stock_quotes = fetch_twse_stock_quotes()
    print("  TWSE 注意累計歷史（計算觸發門檻）...")
    nt_thresholds = {}
    for r in notetrans_twse:
        hist = fetch_twse_stock_history(r["code"], today)
        if hist:
            t = calculate_attention_thresholds(hist)
            if t:
                nt_thresholds[r["code"]] = t

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

    # 檔數驟降保險：處置批次出關是漸進的（單日約 10 檔級別），單日驟降過半
    # 幾乎必是資料源回傳不完整。與上次成功執行的檔數比對，異常即中止。
    if LAST_COUNTS_PATH.exists() and not force:
        try:
            prev = json.loads(LAST_COUNTS_PATH.read_text(encoding="utf-8"))
            prev_total = int(prev.get("total_active", 0))
        except Exception:
            prev_total = 0
        if prev_total >= 10 and total_active < prev_total * 0.5:
            sys.exit(f"ERROR: 處置中檔數驟降 {prev_total} → {total_active}"
                     f"（上次: {prev.get('date','?')}），疑似資料源不完整，"
                     f"中止更新（確認正常可用 --force）")

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
    tab3_html  = render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today,
                             stock_quotes=stock_quotes, nt_thresholds=nt_thresholds)
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

    # 更新 <title> 日期
    html = re.sub(r'(Updated )\d{4}/\d{2}/\d{2}(</title>)',
                  rf'\g<1>{today.strftime("%Y/%m/%d")}\2', html)

    # 更新 tab nav 數字
    tab3_nt = len(notetrans_twse) + len(notetrans_tpex)
    html = update_inline_counts(html, total_active, latest_count, tab3_nt)

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"  ✓ 寫入 {HTML_PATH}")

    # 結構化輸出：dispo.json（供績效儀表板等下游讀取）+ 每日歷史快照
    # 傳「全部處置紀錄」而非 all_active（後者按 code 去重，會丟失重疊處置中
    # 較新的那筆，如二次處置升級）
    active_records  = [s for b in active_groups.values() for s in b["stocks"]]
    upcoming_stocks = [s for g in upcoming_groups.values() for s in g["stocks"]]
    snap = build_snapshot(
        today, taiex, active_records, upcoming_stocks,
        notetrans_twse, notetrans_tpex, nt_thresholds, stock_quotes,
        counts={"active": total_active, "twse": twse_count, "tpex": tpex_count,
                "second": second_count,
                "notetrans": len(notetrans_twse) + len(notetrans_tpex)},
    )
    write_snapshot(snap)
    print(f"  ✓ 寫入 dispo.json + data/history/{snap['date']}.json")

    # 記錄本次檔數，供下次執行做驟降比對（隨 commit 入庫）
    LAST_COUNTS_PATH.write_text(json.dumps({
        "date": today.isoformat(),
        "total_active": total_active,
        "twse": twse_count,
        "tpex": tpex_count,
        "notetrans": len(notetrans_twse) + len(notetrans_tpex),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("更新完成！")


if __name__ == "__main__":
    main()
