#!/usr/bin/env python3
"""
處置公告來源時效性探測（唯讀，不寫任何檔案、不影響 pipeline）。

背景（2026-09 調查）：回推 55 天歷史快照發現有 59 筆公告在當天沒被收錄、
隔一交易日才出現，且極度偏向證交所——TWSE 漏 52 筆、TPEx 只漏 7 筆；
反過來「當天就進『即將被處置』」的 TPEx 有 61 筆、TWSE 只有 2 筆。
這些公告的處置起始日幾乎都是次一交易日，等於使用者從未在生效前看到它們。

待釐清的問題：證交所公告到底幾點才取得到？以及現用的 openapi 端點（無日期
參數、窗口不可控）與 RWD 端點（支援 startDate/endDate 前瞻查詢）在同一時刻
的內容是否有差。本腳本在每次排程執行時把兩個來源的當下內容印進 run log，
累積 2–3 個交易日後即可定案 R01 的修法（每班都抓 / 換端點 / 調排程）。

用法：python scripts/probe_announcement_sources.py
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.twse.com.tw/"}

OPENAPI = "https://openapi.twse.com.tw/v1/announcement/punish"
RWD     = "https://www.twse.com.tw/rwd/zh/announcement/punish"


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def roc(s):
    s = (s or "").replace("/", "").strip()
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}" if len(s) == 7 else s


def rows_openapi():
    out = []
    for r in get(OPENAPI):
        code = (r.get("Code") or "").strip()
        if len(code) != 4:                      # 只看一般股票，排除權證
            continue
        per = (r.get("DispositionPeriod") or "").split("～")
        out.append({
            "code": code, "name": r.get("Name", ""),
            "ann": roc(r.get("Date", "")),
            "ps": roc(per[0]) if len(per) == 2 else "?",
            "pe": roc(per[1]) if len(per) == 2 else "?",
        })
    return out


def rows_rwd(start, end):
    url = (f"{RWD}?response=json"
           f"&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}")
    d = get(url)
    out = []
    for r in (d.get("data") or []):
        code = str(r[2]).strip()
        if len(code) != 4:
            continue
        per = str(r[6]).split("～")
        out.append({
            "code": code, "name": str(r[3]),
            "ann": roc(str(r[1])),
            "ps": roc(per[0]) if len(per) == 2 else "?",
            "pe": roc(per[1]) if len(per) == 2 else "?",
        })
    return out, d.get("title", "")


def show(label, rows, today_s, extra=""):
    fwd = [r for r in rows if r["ps"] > today_s]
    print(f"  [{label}] 筆數={len(rows)}　未生效(ps>{today_s})={len(fwd)}　{extra}")
    for r in sorted(rows, key=lambda x: (x["ps"], x["code"])):
        mark = "★未生效" if r["ps"] > today_s else ""
        print(f"      {r['code']} {r['name'][:10]:<12} 公告 {r['ann']}  {r['ps']}~{r['pe']} {mark}")
    return len(fwd)


def main():
    now_tpe = datetime.now(TPE)
    today = now_tpe.date()
    print("=" * 74)
    print(f"公告來源探測　台灣時間 {now_tpe:%Y-%m-%d %H:%M:%S}"
          f"（UTC {datetime.now(timezone.utc):%H:%M}）")
    print("=" * 74)

    n_fwd = {}
    try:
        n_fwd["openapi"] = show("openapi（現用・無日期參數）",
                                rows_openapi(), today.isoformat())
    except Exception as e:
        print(f"  [openapi] 失敗：{e}")
    try:
        r, title = rows_rwd(today, today + timedelta(days=10))
        n_fwd["rwd"] = show("RWD（可前瞻）", r, today.isoformat(), f"title={title}")
    except Exception as e:
        print(f"  [RWD] 失敗：{e}")

    print("-" * 74)
    print(f"  結論指標：未生效公告筆數 → {n_fwd}")
    print("  （>0 代表此來源／此時刻已能提供『即將被處置』的前瞻資料）")
    print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
