#!/usr/bin/env python3
"""
從 git 歷史回填 data/history/ 每日快照（一次性工具，保留供重現）。

原理：index.html 每個交易日由自動更新 commit 一版，遍歷這些 commit、
parse 出 Tab1（處置中）名單，產生與 update_dashboard.py 同 schema 的快照，
標記 source="backfill"（無報價/大盤/注意股欄位，統計程式須容忍缺欄）。

規則：
  - 只處理含 AUTO:TAB1 marker 的版本（自動生成格式）；手寫舊版跳過
  - 快照日期以 <title> 的 Updated 日期為準（= 資料交易日），週末跳過
  - 同日多個 commit 由較新者覆蓋；已存在 source="live" 的日期不覆蓋
  - 自我驗證：解析出的檔數須等於頁面 tab 上的 data-count，不符即列警告
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).parent.parent
HIST = REPO / "data" / "history"

# 日期來源優先用 AUTO:DATE 區塊（每次自動更新必寫入正確日期）；
# <title> 在 2026/07/02 之前有卡住不更新的歷史（見 commit 152b0fa），僅作 fallback。
DATEBLK_RE = re.compile(r'AUTO:DATE_START -->[\s\S]{0,200}?>(\d{4})\.(\d{2})\.(\d{2})<')
TITLE_RE   = re.compile(r'Updated (\d{4})/(\d{2})/(\d{2})</title>')
TAB1_RE    = re.compile(r'<!-- AUTO:TAB1_BATCHES_START -->([\s\S]*?)<!-- AUTO:TAB1_BATCHES_END -->')
COUNT_RE   = re.compile(r'data-count="1"[^>]*>(\d+) 檔')
TOKEN_RE   = re.compile(
    r'處置期\s*(?P<bs_m>\d{1,2})/(?P<bs_d>\d{1,2})\s*[–\-]\s*(?P<be_m>\d{1,2})/(?P<be_d>\d{1,2})'
    r'|>(?P<ex>TWSE 上市|TPEx 上櫃)<'
    r'|<span class="ticker[^"]*">(?P<code>\d{4,6}[A-Z]?)</span>'
)
NAME_RE    = re.compile(r'<div class="text-sm font-semibold[^"]*">([^<]+)')
DISPCT_RE  = re.compile(r'pill pill-red[^>]*">(2nd|3rd\+?)')
AUCTION_RE = re.compile(r'pill pill-\w+[^>]*">([^<]*(?:撮合|管制)[^<]*)<')
END_RE     = re.compile(r'~\s*(\d{1,2})/(\d{1,2})')


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=REPO).stdout


def mk_date(year, m, d, after=None):
    """月份小於 after 的月份時視為跨年（12月→1月）。"""
    y = year + 1 if (after and m < after.month - 6) else year
    return date(y, m, d)


def parse_tab1(html, year):
    m = TAB1_RE.search(html)
    if not m:
        return None
    seg = m.group(1)
    toks = list(TOKEN_RE.finditer(seg))
    batch_start = batch_end = None
    exchange = None
    rows = []
    for i, t in enumerate(toks):
        if t.group("bs_m"):
            batch_start = date(year, int(t.group("bs_m")), int(t.group("bs_d")))
            batch_end   = mk_date(year, int(t.group("be_m")), int(t.group("be_d")),
                                  after=batch_start)
        elif t.group("ex"):
            exchange = "TWSE" if t.group("ex").startswith("TWSE") else "TPEx"
        elif t.group("code"):
            end = toks[i + 1].start() if i + 1 < len(toks) else len(seg)
            chunk = seg[t.end():end]
            nm = NAME_RE.search(chunk)
            dc = DISPCT_RE.search(chunk)
            au = AUCTION_RE.search(chunk)
            en = END_RE.search(chunk)
            period_end = (mk_date(year, int(en.group(1)), int(en.group(2)),
                                  after=batch_start) if en else batch_end)
            rows.append({
                "code": t.group("code"),
                "name": nm.group(1).strip() if nm else "",
                "exchange": exchange,
                "ann_date": None,
                "period_start": batch_start.isoformat() if batch_start else None,
                "period_end": period_end.isoformat() if period_end else None,
                "auction": au.group(1) if au else None,
                "disp_count": 3 if (dc and dc.group(1).startswith("3")) else
                              2 if dc else 1,
                "close": None, "change_pct": None, "vol_k": None,
            })
    return rows


def main():
    HIST.mkdir(parents=True, exist_ok=True)
    shas = git("log", "--reverse", "--format=%H", "--", "index.html").split()
    print(f"index.html 共 {len(shas)} 個 commit")
    written, skipped, warned = set(), 0, 0

    for sha in shas:
        html = git("show", f"{sha}:index.html")
        tm = DATEBLK_RE.search(html) or TITLE_RE.search(html)
        if not tm:
            skipped += 1
            continue
        d = date(int(tm.group(1)), int(tm.group(2)), int(tm.group(3)))
        if d.weekday() >= 5:
            skipped += 1
            continue
        rows = parse_tab1(html, d.year)
        if not rows:
            print(f"  {d} ({sha[:7]}): 無 AUTO 格式或解析 0 列，跳過")
            skipped += 1
            continue

        # 自我驗證：與頁面 tab 顯示的檔數比對（tab 計唯一代碼）
        uniq = len({r["code"] for r in rows})
        cm = COUNT_RE.search(html)
        if cm and int(cm.group(1)) != uniq:
            print(f"  ⚠️ {d} ({sha[:7]}): 解析 {uniq} 檔 ≠ 頁面 {cm.group(1)} 檔")
            warned += 1

        out = HIST / f"{d.isoformat()}.json"
        if out.exists():
            try:
                if json.loads(out.read_text(encoding="utf-8")).get("source") == "live":
                    continue  # live 快照優先，不覆蓋
            except Exception:
                pass

        key = lambda s: (s["exchange"] or "", s["code"])
        snap = {
            "schema": 1,
            "date": d.isoformat(),
            "source": "backfill",
            "taiex": None,
            "counts": {
                "active": uniq,
                "twse": len({r["code"] for r in rows if r["exchange"] == "TWSE"}),
                "tpex": len({r["code"] for r in rows if r["exchange"] == "TPEx"}),
                "second": len({r["code"] for r in rows if r["disp_count"] >= 2}),
                "notetrans": None,
            },
            "active": sorted(rows, key=key),
            "upcoming": [],
            "notetrans": [],
        }
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        written.add(d.isoformat())

    print(f"完成：寫入 {len(written)} 天、跳過 {skipped} 個 commit、警告 {warned} 筆")


if __name__ == "__main__":
    main()
