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
TPEX_QUOTES      = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TPEX_STOCK_HIST  = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"  # 個股月資料

# 公司基本資料（自動補 stock_info 的 name/sector 用）
TWSE_COMPANY_API = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_API = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

# MOPS 產業別代碼（上市/上櫃共用編碼）
INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業",   "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合",     "20": "其他",     "21": "化學工業", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體",   "25": "電腦及週邊設備", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}

# 注意標準門檻（%）：(第一款 6日累積, 第二款 30日起迄)
# 依證交所/櫃買「注意交易資訊異常標準詳細數據」：TWSE 32/100、TPEx 30/100。
# 注意：法規另有「與大盤及同類股差幅」等相對條件，免費資料無法完整計算，
# 故此處門檻為「可能觸發的最低價位」（必要非充分條件）。
ATTENTION_PCT = {"TWSE": (32.0, 100.0), "TPEx": (30.0, 100.0)}

REPO_ROOT        = Path(__file__).parent.parent
HTML_PATH        = REPO_ROOT / "index.html"
STOCK_INFO_PATH  = REPO_ROOT / "data" / "stock_info.json"
LAST_COUNTS_PATH = REPO_ROOT / "data" / "last_counts.json"  # 資料源健康度狀態檔
PERF_STATS_PATH  = REPO_ROOT / "data" / "perf_stats.json"   # 出關股績效快取

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


def fetch_tpex_quotes():
    """
    TPEx 全上櫃收盤 {code: {close, change, change_pct, vol_k, date}}。
    date 為 AD yyyymmdd（openapi 回傳 ROC 7 碼）。
    """
    raw = safe_fetch_json(TPEX_QUOTES, default=[]) or []
    out = {}
    for r in raw:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        try:
            close_s = (r.get("Close") or "").replace(",", "").strip()
            close   = float(close_s) if close_s not in ("", "--", "---") else None
            chg_s   = (r.get("Change") or "").replace(",", "").strip()
            change  = float(chg_s) if chg_s not in ("", "--", "---") else 0.0
            vol_k   = int((r.get("TradingShares") or "0").replace(",", "")) // 1000
            d       = (r.get("Date") or "").strip()
            ad      = (str(int(d[:3]) + 1911) + d[3:]) if (len(d) == 7 and d.isdigit()) else ""
        except (ValueError, TypeError):
            continue
        prev = close - change if close is not None else None
        pct  = (change / prev * 100) if (prev and prev != 0) else None
        out[code] = {"close": close, "change": change, "change_pct": pct,
                     "vol_k": vol_k, "date": ad}
    return out


def fetch_tpex_stock_history(code, today):
    """
    TPEx 個股最近 2 個月日成交（www afterTrading/tradingStock）。
    回傳 [{date, close, vol_k}] 由舊到新，與 fetch_twse_stock_history 同型。
    欄位: [日期, 成交仟股, 成交仟元, 開, 高, 低, 收, 漲跌, 筆數]（量已是千股=張）
    """
    records = {}
    for months_back in range(2):
        y, m = today.year, today.month - months_back
        if m <= 0:
            m += 12
            y -= 1
        url = f"{TPEX_STOCK_HIST}?code={code}&date={y}/{m:02d}/01&response=json"
        data = safe_fetch_json(url, {"Referer": TPEX_REFERER_D}, default={})
        tables = data.get("tables") or [{}]
        for row in (tables[0].get("data") or []):
            try:
                d     = roc_to_date(row[0].replace("/", ""))
                close = float(row[6].replace(",", ""))
                vol_k = int(float(row[1].replace(",", "")))
                if close > 0:
                    records[d] = {"date": d, "close": close, "vol_k": vol_k}
            except (ValueError, IndexError):
                pass
    return sorted(records.values(), key=lambda x: x["date"])


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


# 最後實際交易日（main 由報價資料日填入）。連續達標是否中斷的正確判準：
# 最後達標日 >= 最後交易日 ⇒ 未有交易日空手而過，連續仍活著。
# 僅靠 next_weekday 會把颱風假等非週末休市誤判為中斷。
LAST_TRADE_DATE = None


def streak_is_alive(latest_end, today):
    if LAST_TRADE_DATE is not None:
        return latest_end >= LAST_TRADE_DATE
    return next_weekday(latest_end) > today

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

    streak_broken = not streak_is_alive(latest_end, today)
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


def calculate_attention_thresholds(history, pct6=32.0, pct30=100.0):
    """
    依注意交易資訊「異常標準詳細數據」計算觸發門檻（免費資料可算的絕對條件）。
    - 第一款: 最近 6 個營業日累積收盤漲幅 > pct6（TWSE 32% / TPEx 30%）
    - 第二款: 最近 30 個營業日起迄收盤漲幅 > pct30（兩市場皆 100%）
    - 量參考: 60 日平均量（量/週轉率各款的部分依據，集中度條件不在此計算）
    法規另有「與大盤及同類股差幅」相對條件無法以免費資料完整計算，
    故本門檻為「可能觸發的最低價位」（必要非充分）。
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
        thr6 = ref6["close"] * (1 + pct6 / 100)
        result["clause1"] = {
            "ref_date":  ref6["date"],
            "ref_close": ref6["close"],
            "cum_pct":   cum6,
            "pct":       pct6,
            "threshold": thr6,
            "diff_pct":  (thr6 - current_close) / current_close * 100,
            "triggered": current_close >= thr6,
        }

    ref30 = nth_before(30)
    if ref30:
        cum30 = (current_close - ref30["close"]) / ref30["close"] * 100
        thr30 = ref30["close"] * (1 + pct30 / 100)
        result["clause2"] = {
            "ref_date":  ref30["date"],
            "ref_close": ref30["close"],
            "cum_pct":   cum30,
            "pct":       pct30,
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
        p1 = c1.get("pct", 32.0)
        lines.append(
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">第一款</span>'
            f'<span class="text-slate-400">'
            f'6日累積 <span class="mono {"text-red-400" if c1["cum_pct"]>=p1 else ("text-amber-300" if c1["cum_pct"]>=p1*0.7 else "text-slate-400")}">'
            f'{c1["cum_pct"]:+.1f}%</span>'
            f'<span class="text-slate-600">（{ref_s} 起，門檻＞{p1:.0f}%）</span>'
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
        p2 = c2.get("pct", 100.0)
        lines.append(
            f'<div class="flex items-start gap-2 mb-0.5">'
            f'<span class="mono text-slate-600 shrink-0 w-10">第二款</span>'
            f'<span class="text-slate-400">'
            f'30日起迄 <span class="mono {"text-red-400" if c2["cum_pct"]>=p2 else ("text-amber-300" if c2["cum_pct"]>=p2*0.7 else "text-slate-400")}">'
            f'{c2["cum_pct"]:+.1f}%</span>'
            f'<span class="text-slate-600">（{ref_s} 起，門檻＞{p2:.0f}%）</span>'
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
# 前科追蹤（#13）：歷史庫觀測以來每檔的 distinct 處置期數
# ──────────────────────────────────────────────
CAREER_COUNTS = {}  # code -> 期數；main() 填入後供 render/_stock_entry 讀取


def load_career_counts(active_records):
    periods = defaultdict(set)
    hist_dir = REPO_ROOT / "data" / "history"
    if hist_dir.exists():
        for f in sorted(hist_dir.glob("*.json")):
            try:
                snap = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for s in snap.get("active", []):
                if s.get("code") and s.get("period_start"):
                    periods[s["code"]].add(s["period_start"])
    for s in active_records:
        periods[s["code"]].add(s["period_start"].isoformat())
    return {c: len(v) for c, v in periods.items()}


# ──────────────────────────────────────────────
# 出關股績效（#12）：處置期間報酬 / 出關後5日報酬，個股歷史只抓一次入快取
# ──────────────────────────────────────────────
def _idx_on_or_before(history, d):
    """history（由舊到新）中日期 ≤ d 的最後一筆 index；無則 -1。"""
    idx = -1
    for i, r in enumerate(history):
        if r["date"] <= d:
            idx = i
        else:
            break
    return idx


def update_perf_stats(released_groups, today):
    """
    對近期出關股計算：處置期間報酬（處置前一交易日收盤 → 處置末日收盤）與
    出關後 5 交易日報酬。結果存 data/perf_stats.json 累積；after5 尚無法計算
    （未滿 5 個交易日）者，之後的執行會重抓補算，補齊後永久跳過。
    """
    try:
        stats = (json.loads(PERF_STATS_PATH.read_text(encoding="utf-8"))
                 if PERF_STATS_PATH.exists() else {})
    except Exception:
        stats = {}

    todo = []
    for pe, grp in released_groups.items():
        for s in grp["stocks"]:
            key = f'{s["code"]}:{s["period_start"]}:{s["period_end"]}'
            e = stats.get(key)
            if e and e.get("after5_pct") is not None:
                continue
            todo.append((key, s))

    if todo:
        print(f"  出關股績效（{len(todo)} 檔待算/補算）...")
    for key, s in todo:
        fetch = (fetch_twse_stock_history if s["exchange"] == "TWSE"
                 else fetch_tpex_stock_history)
        hist = fetch(s["code"], today)
        if not hist:
            continue
        i_entry = _idx_on_or_before(hist, s["period_start"] - timedelta(days=1))
        i_exit  = _idx_on_or_before(hist, s["period_end"])
        if i_exit < 0:
            continue
        entry_c = hist[i_entry]["close"] if i_entry >= 0 else None
        exit_c  = hist[i_exit]["close"]
        during  = (exit_c / entry_c - 1) * 100 if entry_c else None
        i_a5    = i_exit + 5
        after5  = (hist[i_a5]["close"] / exit_c - 1) * 100 if i_a5 < len(hist) else None
        stats[key] = {
            "code": s["code"], "name": s["name"], "exchange": s["exchange"],
            "period_start": s["period_start"].isoformat(),
            "period_end":   s["period_end"].isoformat(),
            "entry_close": entry_c, "exit_close": exit_c,
            "during_pct": during, "after5_pct": after5,
        }

    if todo:
        PERF_STATS_PATH.write_text(
            json.dumps(stats, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
    return stats


def _agg(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return {"n": n, "avg": sum(xs) / n, "med": med,
            "win": sum(1 for x in xs if x > 0) / n * 100}


def perf_stats_summary(stats):
    """回傳 {'during': agg, 'after5': agg}（樣本不足回傳空欄位）。"""
    during = _agg([e["during_pct"] for e in stats.values()
                   if e.get("during_pct") is not None])
    after5 = _agg([e["after5_pct"] for e in stats.values()
                   if e.get("after5_pct") is not None])
    return {"during": during, "after5": after5}


def render_perf_stats_card(summary, sample_since="2026-06"):
    d, a = summary.get("during"), summary.get("after5")
    if not d or d["n"] < 3:
        return ""
    def line(label, g):
        if not g:
            return ""
        clr = "text-green-400" if g["avg"] > 0 else "text-red-400"
        return (f'<div class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[12px]">'
                f'<span class="text-slate-400 w-24">{label}</span>'
                f'<span>平均 <span class="mono {clr} font-semibold">{g["avg"]:+.1f}%</span></span>'
                f'<span>中位 <span class="mono text-slate-300">{g["med"]:+.1f}%</span></span>'
                f'<span>上漲比例 <span class="mono text-slate-300">{g["win"]:.0f}%</span></span>'
                f'<span class="text-slate-600 mono">n={g["n"]}</span>'
                f'</div>')
    return f"""    <div class="card mb-3">
      <div class="p-3 border-b border-slate-800">
        <div class="text-sm font-semibold">📊 處置績效統計</div>
        <div class="text-[11px] text-slate-400 mt-1">出關樣本自 {sample_since} 起累積 · 報酬以處置前一日收盤為基準</div>
      </div>
      <div class="p-3 flex flex-col gap-1.5">
        {line("處置期間", d)}
        {line("出關後5日", a)}
      </div>
    </div>"""


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
        "career_count": CAREER_COUNTS.get(s["code"], 1),
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
        print("  ✓ 寫入 dispo.json（週末，不寫入 data/history/）")
        return
    hist_dir = REPO_ROOT / "data" / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / f"{snap['date']}.json").write_text(text, encoding="utf-8")
    print(f"  ✓ 寫入 dispo.json + data/history/{snap['date']}.json")


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


def autofill_stock_info(stock_info, codes_needed):
    """
    對 stock_info 缺漏的代碼，自動從公司基本資料 API 補 name/sector（產業別）。
    tags 維持手動（sector_to_tags 會再從 sector 文字自動推導部分標籤）。
    有新增時就地更新 dict 並寫回 stock_info.json，回傳新增檔數。
    """
    missing = sorted(c for c in codes_needed
                     if c not in stock_info and is_regular_stock(c))
    if not missing:
        return 0

    maps = {}   # code -> (簡稱, 產業代碼, 交易所)
    for url, code_k, abbr_k, ind_k, ex in (
        (TWSE_COMPANY_API, "公司代號", "公司簡稱", "產業別", "TWSE"),
        (TPEX_COMPANY_API, "SecuritiesCompanyCode", "CompanyAbbreviation",
         "SecuritiesIndustryCode", "TPEx"),
    ):
        for r in (safe_fetch_json(url, default=[]) or []):
            c = (r.get(code_k) or "").strip()
            if c:
                maps.setdefault(c, ((r.get(abbr_k) or "").strip(),
                                    (r.get(ind_k) or "").strip(), ex))

    added = 0
    for c in missing:
        if c not in maps:
            continue
        name, ind, ex = maps[c]
        sector = INDUSTRY_NAMES.get(ind, "")
        stock_info[c] = {"name": name, "exchange": ex, "tags": "", "sector": sector}
        added += 1

    if added:
        STOCK_INFO_PATH.write_text(
            json.dumps(stock_info, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return added


def get_stock_meta(code, stock_info):
    meta   = stock_info.get(code, {})
    sector = meta.get("sector", "")
    manual = set(meta.get("tags", "").split())
    auto   = sector_to_tags(sector)
    tags   = " ".join(sorted(manual | auto))
    return {"tags": tags, "sector": sector}


def _quote_span(quote):
    """收盤/漲跌%/量 的小型 mono 標籤（站內慣例：綠漲紅跌）。"""
    if not quote or quote.get("close") is None:
        return ""
    close = quote["close"]
    chg   = quote.get("change") or 0
    pct   = quote.get("change_pct")
    vol_k = quote.get("vol_k") or 0
    if chg > 0:
        clr, arrow = "text-green-400", "▲"
    elif chg < 0:
        clr, arrow = "text-red-400", "▼"
    else:
        clr, arrow = "text-slate-400", "–"
    close_s = f"{close:,.2f}".rstrip("0").rstrip(".")
    pct_s   = f"{arrow}{abs(pct):.1f}%" if pct is not None else arrow
    vol_s   = f' <span class="text-slate-600">{vol_k:,}張</span>' if vol_k else ""
    return (f' <span class="mono {clr}" style="font-size:11px">'
            f'{close_s} {pct_s}</span>{vol_s}')


def render_stock_row(stock, stock_info, today, pill_class_override=None, pill_label_override=None,
                     quote=None):
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
    career = CAREER_COUNTS.get(stock["code"], 0)
    if career >= 2:
        name_html += (f' <span class="pill pill-gray ml-1" '
                      f'title="歷史庫觀測以來共 {career} 段處置期">前科{career}</span>')

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
        f'<div class="sector">{sector}{_quote_span(quote)}</div>'
        f'</div>'
        f'<div class="end-date-desktop text-right">'
        f'<span class="pill {pill_class}">{pill_label}</span>'
        f'{end_html}'
        f'</div>'
        f'</div>'
    )


def exchange_section(label, stocks, stock_info, today,
                     pill_class_override=None, pill_label_override=None, border=False,
                     stock_quotes=None):
    if not stocks:
        return ""
    sq = stock_quotes or {}
    border_cls = " border-t border-slate-800" if border else ""
    rows = "".join(
        render_stock_row(s, stock_info, today, pill_class_override, pill_label_override,
                         quote=sq.get(s["code"]))
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
def render_batch_block(batch, stock_info, today, is_latest=False, is_open=True,
                       stock_quotes=None):
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

    rows_html  = exchange_section("TWSE 上市", twse_stocks, stock_info, today,
                                  stock_quotes=stock_quotes)
    rows_html += exchange_section("TPEx 上櫃", tpex_stocks, stock_info, today,
                                  border=bool(twse_stocks), stock_quotes=stock_quotes)

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


def render_tab1_batches(active_groups, stock_info, today, stock_quotes=None):
    sorted_batches = sorted(active_groups.values(), key=lambda b: b["period_start"], reverse=True)
    blocks = [render_release_schedule(active_groups, today)]
    for i, batch in enumerate(sorted_batches):
        blocks.append(render_batch_block(batch, stock_info, today,
                                         is_latest=(i == 0), is_open=(i < 3),
                                         stock_quotes=stock_quotes))
    return "\n".join(blocks)


# ──────────────────────────────────────────────
# HTML 生成 — Tab 2
# ──────────────────────────────────────────────
def render_release_schedule(active_groups, today):
    """出關時間軸（#18）：現行有效管制（每檔取 period_end 最大）按出關日分組。"""
    best = {}
    for b in active_groups.values():
        for s in b["stocks"]:
            cur = best.get(s["code"])
            if cur is None or (s["period_end"], s.get("disp_count", 1)) > \
                              (cur["period_end"], cur.get("disp_count", 1)):
                best[s["code"]] = s
    by_end = defaultdict(list)
    for s in best.values():
        by_end[s["period_end"]].append(s)
    if not by_end:
        return ""
    rows = []
    for pe in sorted(by_end):
        stocks = sorted(by_end[pe], key=lambda s: s["code"])
        days   = (pe - today).days
        tag    = ('<span class="pill pill-red">今日</span>' if days == 0
                  else f'<span class="mono text-slate-500">D+{days}</span>')
        names  = "、".join(f'{s["name"]}({s["code"]})' for s in stocks[:8])
        if len(stocks) > 8:
            names += f" …等{len(stocks)}檔"
        rows.append(
            f'<div class="sched-row">'
            f'<span class="sched-date">{fmt_weekday(pe)}</span>'
            f'<span class="sched-tag">{tag}</span>'
            f'<span class="sched-count">{len(stocks)}檔</span>'
            f'<span class="sched-names">{names}</span>'
            f'</div>')
    return f"""    <div class="card mb-3">
      <div class="p-3 border-b border-slate-800">
        <div class="text-sm font-semibold">📅 出關排程</div>
        <div class="text-[11px] text-slate-400 mt-1">依現行有效管制（重疊處置取較長者）· 出關後 30 日內再犯直接升級二次處置</div>
      </div>
      <div>{"".join(rows)}</div>
    </div>"""


def render_tab2_content(latest_batch, stock_info, today, stock_quotes=None):
    ps     = latest_batch["period_start"]
    pe     = latest_batch["period_end"]
    stocks = latest_batch["stocks"]
    ann    = latest_batch["ann_date"]
    count  = len(stocks)

    twse_stocks = [s for s in stocks if s["exchange"] == "TWSE"]
    tpex_stocks = [s for s in stocks if s["exchange"] == "TPEx"]

    rows_html  = exchange_section(f"TWSE 上市（{len(twse_stocks)}檔）", twse_stocks, stock_info, today,
                                  stock_quotes=stock_quotes)
    rows_html += exchange_section(f"TPEx 上櫃（{len(tpex_stocks)}檔）", tpex_stocks, stock_info, today,
                                  border=bool(twse_stocks), stock_quotes=stock_quotes)

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
def _notetrans_urgency(r, today, thr):
    """注意累計股的危險度：(need, diff)。
    need = 還需連續達標日數（0=已達處置條件）；diff = 第一款門檻距離%（越小越近）。
    排序用此鍵，越前面代表越接近進處置（原「下一批雷達」的核心邏輯）。"""
    a = analyze_criteria(r.get("raw_criteria", ""))
    max_c = a["max_consecutive"] if a else 0
    streak_alive = bool(a) and streak_is_alive(a["latest_end"], today)
    need = 0 if max_c >= 3 else (3 - max_c if streak_alive else 3)
    c1 = (thr.get(r["code"]) or {}).get("clause1")
    diff = c1["diff_pct"] if c1 else None
    return need, (diff if diff is not None else 999.0), max_c, c1


def render_notetrans_rows(notetrans_list, stock_info, today, stock_quotes=None, nt_thresholds=None):
    """注意累計清單，依觸發距離（危險度）排序：越接近進處置越上面。
    每列 = 危險度狀態 + 連續進度條 + 明日觸發價（原雷達內容），底下可展開完整門檻明細。"""
    if not notetrans_list:
        return ""
    sq  = stock_quotes   or {}
    thr = nt_thresholds  or {}
    ranked = sorted(notetrans_list, key=lambda r: _notetrans_urgency(r, today, thr)[:2])

    rows = []
    for r in ranked:
        meta     = get_stock_meta(r["code"], stock_info)
        tags     = f' data-tags="{meta["tags"]}"' if meta["tags"] else ""
        sector   = meta["sector"] or r.get("exchange","")
        analysis = analyze_criteria(r.get("raw_criteria",""))
        quote    = sq.get(r["code"])
        need, _, max_c, c1 = _notetrans_urgency(r, today, thr)

        if max_c >= 3:
            sev_color = "bg-red-500"
            ticker_cl = "text-red-300 font-bold"
        else:
            sev_color = "bg-yellow-500"
            ticker_cl = "text-yellow-300 font-bold"

        # 危險度狀態（吸收原雷達的「差 N 日」判斷）
        if need == 0:
            status = '<span class="pill pill-red">已達處置條件・待公告</span>'
        elif need == 1:
            status = '<span class="pill pill-amber">差 1 日</span>'
        else:
            status = f'<span class="pill pill-gray">差 {need} 日</span>'

        prog = "".join(
            f'<span class="inline-block" style="width:18px;height:5px;border-radius:2px;'
            f'margin-right:2px;background:{"#eab308" if i < max_c else "#334155"}"></span>'
            for i in range(3))

        # 明日觸發價（第一款絕對條件）
        if c1 and not c1["triggered"]:
            tomo = (f'<div class="mt-0.5"><span class="text-slate-500 text-[11px]">明日收盤 ≥ '
                    f'<span class="mono text-slate-300">{c1["threshold"]:.2f}</span>'
                    f'（差 {c1["diff_pct"]:.1f}%）</span></div>')
        elif c1:
            tomo = ('<div class="mt-0.5"><span class="text-red-300 text-[11px]">'
                    '最新收盤已達第一款門檻</span></div>')
        else:
            tomo = ""

        # 展開明細：達標摘要 + 完整門檻條件（TWSE/TPEx 皆有 thr）
        t_data      = thr.get(r["code"])
        cond_date   = t_data["latest_date"] if t_data else today
        cond_html   = render_attention_conditions(t_data, cond_date) if t_data else ""
        detail_html = render_risk_detail(analysis, today, quote=quote, extra_html=cond_html)

        rows.append(
            f'<div class="table-row"{tags}>'
            f'<div class="flex items-center gap-2">'
            f'<span class="severity-bar {sev_color}" style="height:32px;"></span>'
            f'<span class="ticker {ticker_cl}">{r["code"]}</span>'
            f'</div>'
            f'<div>'
            f'<div class="text-sm font-semibold">{r["name"]}{_quote_span(quote)}</div>'
            f'<div class="sector">{sector}</div>'
            f'</div>'
            f'<div class="end-date-desktop text-right">'
            f'{status}'
            f'<div class="mt-1">{prog}<span class="text-[10px] text-slate-500 ml-1">連續 {max_c}/3</span></div>'
            f'{tomo}'
            f'</div>'
            + detail_html
            + '</div>'
        )
    return "".join(rows)


def render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today,
                stock_quotes=None, nt_thresholds=None, perf_html=""):
    sections = []
    sq  = stock_quotes  or {}
    thr = nt_thresholds or {}

    # ── Section 1: 注意累計 — 依觸發距離排序（原「下一批雷達」已併入此處）──
    # TWSE+TPEx 合併成單一排序清單，越上面越接近進處置；每列可展開門檻明細。
    all_notetrans = notetrans_twse + notetrans_tpex
    if all_notetrans:
        nt_rows = render_notetrans_rows(all_notetrans, stock_info, today, sq, thr)
        sections.append(f"""    <div class="card mb-3">
      <div class="p-3 border-b border-slate-800">
        <div class="text-sm font-semibold">⚡ 注意累計中 — 依觸發距離排序</div>
        <div class="text-[11px] text-slate-400 mt-1">連續 3 次（或 30 日累計 6 次）達注意標準即進處置，越上面越接近觸發。門檻為第一款絕對條件（必要非充分）。</div>
      </div>
      <div>{nt_rows}</div>
    </div>""")

    # ── Section 1.5: 處置績效統計（歷史庫樣本）──
    if perf_html:
        sections.append(perf_html)

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
def _delta_span(d):
    """KPI 昨日對比：增=amber（管制壓力升）、減=green，0 或無基準不顯示。"""
    if not d:
        return ""
    clr = "text-amber-400" if d > 0 else "text-green-400"
    return f'<span class="text-sm {clr} mono ml-1">({d:+d})</span>'


def render_stats(total_active, latest_count, second_count,
                 twse_count, tpex_count, latest_ann_date, deltas=None):
    ann_str = fmt_short(latest_ann_date) if latest_ann_date else "—"
    dl = deltas or {}
    return f"""  <div class="grid grid-cols-3 gap-3 mb-6">
    <div class="stat-block">
      <div class="text-[10px] text-slate-500 uppercase tracking-wider mono">處置中</div>
      <div class="flex items-baseline gap-2 mt-1">
        <span class="num-display text-3xl text-red-400">{total_active}</span>
        <span class="text-xs text-slate-400">檔</span>{_delta_span(dl.get("total_active"))}
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
        <span class="text-xs text-slate-400">檔</span>{_delta_span(dl.get("second"))}
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

    # 「今天」一律以報價 API 回傳的實際交易日為準，不用系統時鐘（無論 UTC 或
    # Asia/Taipei）。教訓（2026-07-14）：GitHub 排程延遲跨過午夜時，若用系統
    # 時鐘取「今天」，錢在市場尚未開盤前就已跨日，導致「今天」比最新報價日
    # 還新一天，誤判成「非交易日」而整批放棄更新——7/13 兩次排程因此都沒有
    # 寫入任何資料，卻仍回報 success。改用資料本身的日期，不受執行時間影響。
    print("  TWSE 個股報價...")
    stock_quotes = fetch_twse_stock_quotes()
    print("  TPEx 個股報價...")
    tpex_quotes = fetch_tpex_quotes()
    # 日期一致性（B6 教訓）：兩市場報價日不一致時棄用 TPEx，避免混入舊價
    twse_qdate = next((q["date"] for q in stock_quotes.values() if q.get("date")), "")
    tpex_qdate = next((q["date"] for q in tpex_quotes.values() if q.get("date")), "")
    if twse_qdate and tpex_qdate and twse_qdate != tpex_qdate:
        print(f"  WARNING: 報價日不一致 TWSE {twse_qdate} vs TPEx {tpex_qdate}，"
              f"棄用 TPEx 報價", file=sys.stderr)
    else:
        stock_quotes = {**stock_quotes, **tpex_quotes}

    if twse_qdate:
        today = date(int(twse_qdate[:4]), int(twse_qdate[4:6]), int(twse_qdate[6:8]))
    else:
        # 報價完全抓不到（總體性 API 故障）時退回系統時鐘，僅供繼續執行的
        # 最後手段；後面的處置源防呆通常會先中止。
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        print(f"  WARNING: 無法取得報價資料日，退回系統時鐘 {today}", file=sys.stderr)

    print(f"開始更新，資料日（今天）: {today}" + ("（--force）" if force else ""))

    global LAST_TRADE_DATE
    LAST_TRADE_DATE = today

    # 讀上次執行狀態（驟降保險 + KPI 昨日對比 + 新交易日守則共用）
    state = {}
    if LAST_COUNTS_PATH.exists():
        try:
            state = json.loads(LAST_COUNTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    # 新交易日守則：資料日沒有比上次成功處理的日期更新，代表還沒有新收盤資料
    # 可更新（同日重跑、假日/颱風停市時報價 API 只會重複回傳上個交易日）。
    # 用「資料日」而非系統時鐘比較，不受排程延遲跨日影響（見上方教訓）。
    if not force and state.get("date") and today.isoformat() <= state["date"]:
        print(f"  資料日 {today} 未晚於上次已處理日期 {state['date']}，"
              f"尚無新交易日資料，跳過更新（確要執行可用 --force）")
        return

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

    # 抓大盤 & 注意累計
    print("  大盤指數...")
    taiex = fetch_taiex()
    print("  TWSE 注意累計...")
    notetrans_twse = fetch_twse_notetrans()
    print("  TPEx 注意累計...")
    notetrans_tpex = fetch_tpex_warning()

    print("  注意累計歷史（計算觸發門檻，TWSE+TPEx）...")
    nt_thresholds = {}
    for r in notetrans_twse + notetrans_tpex:
        if r["exchange"] == "TWSE":
            hist = fetch_twse_stock_history(r["code"], today)
        else:
            hist = fetch_tpex_stock_history(r["code"], today)
        if hist:
            pct6, pct30 = ATTENTION_PCT[r["exchange"]]
            t = calculate_attention_thresholds(hist, pct6, pct30)
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
    prev_total = int(state.get("total_active", 0) or 0)
    if not force and prev_total >= 10 and total_active < prev_total * 0.5:
        sys.exit(f"ERROR: 處置中檔數驟降 {prev_total} → {total_active}"
                 f"（上次: {state.get('date','?')}），疑似資料源不完整，"
                 f"中止更新（確認正常可用 --force）")

    # KPI 昨日對比基準：同日重跑沿用原本的 prev_day，跨日則以上次執行為基準
    if state.get("date") == today.isoformat():
        baseline = state.get("prev_day")
    elif state:
        baseline = {k: state.get(k) for k in
                    ("date", "total_active", "twse", "tpex", "second", "notetrans")}
    else:
        baseline = None
    deltas = {}
    if baseline:
        for cur, key in ((total_active, "total_active"), (second_count, "second")):
            prev_v = baseline.get(key)
            if prev_v is not None:
                deltas[key] = cur - int(prev_v)

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
    needed = ({s["code"] for s in all_rows}
              | {r["code"] for r in notetrans_twse + notetrans_tpex})
    added = autofill_stock_info(stock_info, needed)
    if added:
        print(f"  ✓ stock_info.json 自動補 {added} 檔（name/sector，tags 留手動）")

    # 前科追蹤（#13）：渲染與快照前先聚合歷史庫
    active_records = [s for b in active_groups.values() for s in b["stocks"]]
    CAREER_COUNTS.update(load_career_counts(active_records))

    # 出關股績效（#12）
    perf_stats   = update_perf_stats(released_groups, today)
    perf_summary = perf_stats_summary(perf_stats)
    perf_html    = render_perf_stats_card(perf_summary)

    # 生成 HTML 片段
    context_html = render_context_banner(
        taiex, total_active, today_released,
        upcoming_count, upcoming_date,
        notetrans_twse, notetrans_tpex, today,
    )
    stats_html = render_stats(total_active, latest_count, second_count,
                              twse_count, tpex_count, latest_ann, deltas=deltas)
    tab1_html  = render_tab1_batches(active_groups, stock_info, today,
                                     stock_quotes=stock_quotes)
    tab2_html  = render_tab2_content(latest_batch, stock_info, today,
                                     stock_quotes=stock_quotes)
    tab3_html  = render_tab3(notetrans_twse, notetrans_tpex, released_groups, stock_info, today,
                             stock_quotes=stock_quotes, nt_thresholds=nt_thresholds,
                             perf_html=perf_html)
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
    # active 傳「全部處置紀錄」而非 all_active（後者按 code 去重，會丟失重疊
    # 處置中較新的那筆，如二次處置升級）；active_records 已於前面計算
    upcoming_stocks = [s for g in upcoming_groups.values() for s in g["stocks"]]
    snap = build_snapshot(
        today, taiex, active_records, upcoming_stocks,
        notetrans_twse, notetrans_tpex, nt_thresholds, stock_quotes,
        counts={"active": total_active, "twse": twse_count, "tpex": tpex_count,
                "second": second_count,
                "notetrans": len(notetrans_twse) + len(notetrans_tpex)},
    )
    snap["release_stats"] = perf_summary
    write_snapshot(snap)

    # 記錄本次檔數：驟降比對 + 下次的昨日對比基準（隨 commit 入庫）
    LAST_COUNTS_PATH.write_text(json.dumps({
        "date": today.isoformat(),
        "total_active": total_active,
        "twse": twse_count,
        "tpex": tpex_count,
        "second": second_count,
        "notetrans": len(notetrans_twse) + len(notetrans_tpex),
        "prev_day": baseline,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("更新完成！")


if __name__ == "__main__":
    main()
