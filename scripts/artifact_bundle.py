"""Validate and recoverably publish one dashboard bundle (local files, no network)."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from datetime import date

PUBLIC = ('index.html', 'dispo.json', 'build-manifest.json')
AUX = ('data/last_counts.json', 'data/perf_stats.json', 'data/stock_info.json')
JOURNAL = '.dashboard-transaction.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contract(snapshot):
    return {k: snapshot.get(k) for k in ('date', 'counts', 'sources', 'announcement_asof')}


def stamp_html(html, snapshot):
    value = json.dumps(contract(snapshot), ensure_ascii=False, sort_keys=True).replace('<', '\\u003c')
    tag = f'<script id="dashboard-contract" type="application/json">{value}</script>'
    html = re.sub(r'<script id="dashboard-contract" type="application/json">.*?</script>\s*', '', html, flags=re.S)
    if html.count('</head>') != 1:
        raise ValueError('HTML 必須有唯一 head 結尾')
    return html.replace('</head>', tag + '\n</head>')


def validate(root, *, allow_preview=False):
    root = Path(root)
    manifest = json.loads((root / 'build-manifest.json').read_text())
    snap = json.loads((root / 'dispo.json').read_text())
    if manifest['mode'] != 'live' and not allow_preview:
        raise ValueError('預覽產物不得發布')
    for rel in ('index.html', 'dispo.json'):
        if manifest['artifacts'].get(rel) != digest(root / rel):
            raise ValueError(f'產物 hash 不一致：{rel}')
    for rel, expected in manifest['artifacts'].items():
        if expected != digest(safe_path(root, rel)):
            raise ValueError(f'產物 hash 不一致：{rel}')
    for field, key in [('data_date', 'date'), ('sources', 'sources'), ('announcement_asof', 'announcement_asof')]:
        if manifest.get(field) != snap.get(key):
            raise ValueError(f'manifest / snapshot {field} 不一致')
    html = (root / 'index.html').read_text()
    tags = re.findall(r'<script id="dashboard-contract" type="application/json">(.*?)</script>', html, re.S)
    if len(tags) != 1 or json.loads(tags[0]) != contract(snap):
        raise ValueError('HTML / snapshot 資料契約不一致')
    if f"Updated {snap['date'].replace('-', '/')}</title>" not in html:
        raise ValueError('HTML 資料日期不一致')
    best = {}
    for e in snap['active']:
        if not e['period_start'] <= snap['date'] <= e['period_end']:
            raise ValueError('active 含無效日期事件')
        old = best.get(e['code'])
        if old is None or (e['period_end'], e.get('disp_count', 1)) > (old['period_end'], old.get('disp_count', 1)):
            best[e['code']] = e
    counts = {'active': len(best), 'twse': sum(e['exchange'] == 'TWSE' for e in best.values()),
              'tpex': sum(e['exchange'] == 'TPEx' for e in best.values()),
              'second': sum(e.get('disp_count', 1) >= 2 for e in best.values()),
              'notetrans': len(snap['notetrans'])}
    if snap['counts'] != counts:
        raise ValueError('快照統計與明細不一致')
    state = root / 'data/last_counts.json'
    if manifest['mode'] == 'live':
        if not state.is_file():
            raise ValueError('正式產物缺少 state')
        state = json.loads(state.read_text())
        expected_state = {'date': snap['date'], 'quote_date': snap['date'],
                          'announcement_asof': snap.get('announcement_asof'),
                          'total_active': counts['active'],
                          'prev_day': snap.get('comparison_baseline'),
                          **{k: counts[k] for k in ('twse', 'tpex', 'second', 'notetrans')}}
        if any(k not in state or state[k] != value for k, value in expected_state.items()):
            raise ValueError('state 與快照不一致')
        baseline = snap.get('comparison_baseline')
        if baseline and not baseline.get('date', '') < snap['date']:
            raise ValueError('比較基準日期必須早於資料日')
        history = root / 'data/history'
        if any(p.stem > snap['date'] for p in history.glob('*.json')):
            raise ValueError('資料日不得早於既存歷史快照')
        current = history / (snap['date'] + '.json')
        if date.fromisoformat(snap['date']).weekday() < 5 or current.exists():
            if not current.is_file() or json.loads(current.read_text()) != snap:
                raise ValueError('每日歷史快照與目前快照不一致')
    return manifest


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('交易紀錄包含不安全路徑')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('交易路徑不可指向工作區之外')
    return target


@contextlib.contextmanager
def locked(root):
    with (Path(root) / '.dashboard.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一個更新程序執行中，請稍後重試') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def recover(root):
    root = Path(root)
    journal = root / JOURNAL
    if not journal.exists():
        return
    record = json.loads(journal.read_text())
    name = record['backup']
    if not name.startswith('.dashboard-backup-') or Path(name).name != name:
        raise ValueError('無效備份目錄')
    backup = safe_path(root, name)
    for rel, existed in record['files'].items():
        if (rel not in PUBLIC + AUX + ('assets/health.js',)
                and not re.fullmatch(r'data/history/\d{4}-\d{2}-\d{2}\.json', rel)):
            raise ValueError('交易紀錄包含非產物檔案')
        if not isinstance(existed, bool):
            raise ValueError('無效交易檔案狀態')
    entries = [(safe_path(root, rel), safe_path(backup, rel), existed)
               for rel, existed in record['files'].items()]
    if any(existed and not original.is_file() for _, original, existed in entries):
        raise ValueError('備份不完整，保留交易紀錄供人工復原')
    for target, original, existed in entries:
        if existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        else:
            target.unlink(missing_ok=True)
    journal.unlink()
    shutil.rmtree(backup)


def publish(stage, root):
    """Caller validates first. Journal permits rollback on error or next-start recovery.

    Individual replacements are atomic, not the multi-file set. Only validated Git
    commits are deployed; direct file readers must validate the manifest.
    """
    stage, root = Path(stage), Path(root)
    recover(root)
    files = list(PUBLIC) + [p for p in AUX if (stage / p).exists()]
    if (stage / 'assets/health.js').exists():
        files.append('assets/health.js')
    files += [p.relative_to(stage).as_posix() for p in (stage / 'data/history').glob('*.json')]
    files = [p for p in files if not (root / p).exists() or digest(root / p) != digest(stage / p)]
    if not files:
        return
    backup = Path(tempfile.mkdtemp(prefix='.dashboard-backup-', dir=root))
    record = {'backup': backup.name, 'files': {}}
    try:
        for rel in files:
            target = safe_path(root, rel)
            record['files'][rel] = target.exists()
            if target.exists():
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
        journal = root / JOURNAL
        # No destination changes until a complete recovery record exists.
        tmp = root / (JOURNAL + '.tmp')
        tmp.write_text(json.dumps(record))
        os.replace(tmp, journal)
        for rel in files:
            target = safe_path(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / rel, target)
        journal.unlink()  # Commit point; an interruption before this restores old files.
    except BaseException:
        if (root / JOURNAL).exists():
            recover(root)
        raise
    finally:
        if backup.exists() and not (root / JOURNAL).exists():
            shutil.rmtree(backup)
