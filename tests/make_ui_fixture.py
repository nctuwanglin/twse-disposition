"""Generate offline UI edge cases without market APIs or production writes."""
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import update_dashboard as u


def build():
    root = REPO / '.preview/mobile-fixture'
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / 'index.html', root / 'index.html')
    (root / 'data').mkdir(exist_ok=True)
    (root / 'data/stock_info.json').write_text('{"9005":{"tags":"pcb"}}')
    snap = json.loads((REPO / 'dispo.json').read_text())
    # Fixed fixtures are independent of future repository data dates.
    snap.update(date='2026-10-02', last_trade_date='2026-10-02', prev_trading='2026-10-01',
                announcement_asof='2026-10-02', active=[], upcoming=[], released=[],
                notetrans=[], quotes={}, trading_days=['2026-10-01', '2026-10-02'],
                comparison_baseline=None, release_stats=None)
    entry = dict(code='9001', name='測試超長股票名稱ABCDEFGHIJKLMNOPQRSTUVWXYZ', exchange='TWSE',
                 ann_date='2026-10-01', period_start='2026-10-01', period_end='2026-10-02',
                 auction='2分撮合', disp_count=1, career_count=1)
    snap['active'] = [entry, dict(entry, code='9002', name='隔日到期測試', period_end='2026-10-05')]
    snap['upcoming'] = [dict(entry, code='9003', name='公告待生效測試', period_start='2026-10-05', period_end='2026-10-12')]
    snap['released'] = [dict(entry, code='9004', name='出關搜尋測試', period_start='2026-09-25', period_end='2026-10-01')]
    snap['notetrans'] = [dict(code='9005', name='雷達長股名ABCDEFGHIJKLMNOPQRSTUVWXYZ', exchange='TWSE',
                             raw_criteria='115年9月30日至115年10月2日連續三次')]
    snap['notetrans'].append(dict(code='9006', name='門檻資料完整測試', exchange='TWSE',
        raw_criteria='注意次數格式未確認', thresholds={
            'latest_date': '2026-10-02', 'current_close': 100,
            'clause1': {'ref_date': '2026-09-24', 'ref_close': 80, 'cum_pct': 25, 'pct': 32,
                       'threshold': 105.6, 'diff_pct': 5.6, 'triggered': False, 'next_session_threshold': 110},
            'clause2': {'ref_date': '2026-08-20', 'ref_close': 60, 'cum_pct': 66.67, 'pct': 100,
                       'threshold': 120, 'diff_pct': 20, 'triggered': False},
            'vol_avg': 12345, 'vol_days': 60}))
    snap['sources'] = {'twse_notetrans': {'ok': True}, 'tpex_warning': {'ok': False, 'error': 'fixture'}}
    snap['counts'] = dict(active=2, twse=2, tpex=0, second=0, notetrans=2)
    path = root / 'fixture.json'
    path.write_text(json.dumps(snap, ensure_ascii=False))
    with patch.object(u, 'fetch_json', side_effect=AssertionError('network forbidden')), u._output_root(root):
        return u.render_and_publish(u.bundle_from_snapshot(path))


if __name__ == '__main__':
    print(build())
