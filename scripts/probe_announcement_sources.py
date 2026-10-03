#!/usr/bin/env python3
"""
處置公告來源時效性探測（唯讀取外部 API，不影響 pipeline；預設只印出結果）。

背景（2026-09 調查）：回推 55 天歷史快照發現有 59 筆公告在當天沒被收錄、
隔一交易日才出現，且極度偏向證交所——TWSE 漏 52 筆、TPEx 只漏 7 筆；
反過來「當天就進『即將被處置』」的 TPEx 有 61 筆、TWSE 只有 2 筆。
這些公告的處置起始日幾乎都是次一交易日，等於使用者從未在生效前看到它們。

待釐清的問題：證交所公告到底幾點才取得到？以及現用的 openapi 端點（無日期
參數、窗口不可控）與 RWD 端點（支援 startDate/endDate 前瞻查詢）在同一時刻
的內容是否有差。本腳本在每次排程執行時把兩個來源的當下內容印進 run log，
累積 2–3 個交易日後即可定案 R01 的修法（每班都抓 / 換端點 / 調排程）。

使用 --output PATH 可另存單一 JSON 結果，供 workflow 上傳 artifact 跨天比對，
不再追加 data/probe_log.jsonl。結果包含實際執行時刻、參考日期、公告識別與
穩定內容雜湊。--reference-date YYYY-MM-DD 可固定跨午夜比對基準；未指定時
明確標示使用執行當下的台灣本地日期（非上一交易日）。

⚠ 這是暫時性的實證蒐集。確認公告時間窗之後就把 probe.yml 關掉，
（見 .github/workflows/probe.yml 的註解）。

用法：python scripts/probe_announcement_sources.py
      python scripts/probe_announcement_sources.py --reference-date 2026-10-02 --output .run/probe.json
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


def _summarize(rows, today_s):
    # Identity excludes the display name; content includes every normalized field.
    # Hash source content only, not observation time, reference date, or row order.
    identities = sorted([r["code"], r["ann"], r["ps"], r["pe"]] for r in rows)
    canonical_rows = sorted(rows, key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
    content = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "rows": len(rows),
        "forward": sum(1 for r in rows if r["ps"] > today_s),
        "max_ann": max((r["ann"] for r in rows), default=None),
        "event_identities": identities,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _reference_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError(value)
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("reference date must be YYYY-MM-DD")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write one JSON result (default: print only)")
    parser.add_argument("--reference-date", type=_reference_date,
                        help="YYYY-MM-DD; default: current Taiwan-local date")
    args = parser.parse_args(argv)
    now_tpe = datetime.now(TPE)
    now_utc = now_tpe.astimezone(timezone.utc)
    today = now_tpe.date()
    reference = args.reference_date or today
    reference_s = reference.isoformat()
    reference_source = "explicit" if args.reference_date else "taiwan_local_default"
    print("=" * 74)
    print(f"公告來源探測　台灣時間 {now_tpe:%Y-%m-%d %H:%M:%S}"
          f"（UTC {now_utc:%H:%M}）")
    print(f"參考日期 {reference_s} ({reference_source})")
    print("=" * 74)

    n_fwd = {}
    # 實際執行時刻才是有意義的座標：排程名目時間與實際執行常差數小時
    entry = {"at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "at_tpe": now_tpe.strftime("%Y-%m-%dT%H:%M:%S"),
             "tpe_date": today.isoformat(),
             "reference_date": reference_s,
             "reference_date_source": reference_source}
    try:
        rows = rows_openapi()
        n_fwd["openapi"] = show("openapi（現用・無日期參數）", rows, reference_s)
        entry["openapi"] = _summarize(rows, reference_s)
    except Exception as e:
        print(f"  [openapi] 失敗：{e}")
        entry["openapi"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        r, title = rows_rwd(reference, reference + timedelta(days=10))
        n_fwd["rwd"] = show("RWD（可前瞻）", r, reference_s, f"title={title}")
        entry["rwd"] = _summarize(r, reference_s)
    except Exception as e:
        print(f"  [RWD] 失敗：{e}")
        entry["rwd"] = {"error": f"{type(e).__name__}: {e}"}

    print("-" * 74)
    print(f"  結論指標：未生效公告筆數 → {n_fwd}")
    print("  （>0 代表此來源／此時刻已能提供『即將被處置』的前瞻資料）")
    print("=" * 74)
    result = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    print(result)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
