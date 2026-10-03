"""CI entry point: market generation and operational heartbeat are separate."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import artifact_bundle
import update_dashboard as u


def record(root, *, outcome, now=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    path = root / 'update-status.json'
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    snap_path = root / 'dispo.json'
    snap = json.loads(snap_path.read_text()) if snap_path.exists() else {}
    sources = snap.get('sources', {}) if outcome == 'success' else dict(u.SOURCE_STATUS)
    bad = sorted(k for k, value in sources.items() if not value.get('ok'))
    if outcome == 'success' and bad:
        outcome = 'partial'
    market_hash = artifact_bundle.digest(snap_path) if snap_path.exists() else None
    status = dict(previous)
    if not status.get('pipeline_since_run_id') and os.environ.get('GITHUB_RUN_ID'):
        status['pipeline_since_run_id'] = os.environ['GITHUB_RUN_ID']
    status.update({'schema': 1, 'pipeline_version': 2,
                   'last_attempt': {'at': now, 'outcome': outcome, 'run_id': os.environ.get('GITHUB_RUN_ID')},
                   'data_date': snap.get('date'), 'sources': sources, 'degraded_sources': bad,
                   'market_hash': market_hash})
    if outcome != 'failure':
        status['last_successful_check_at'] = now
        if market_hash != previous.get('market_hash'):
            status['last_market_change_at'] = now
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    os.replace(tmp, path)
    return status


def run(argv=None):
    if '--render-only' in (argv or []):
        raise ValueError('CI 健康紀錄僅限 live 更新；預覽請直接使用 update_dashboard.py')
    # Rejected concurrent invocations must not write even a failure heartbeat.
    with artifact_bundle.locked(u.REPO_ROOT):
        try:
            u.main(argv, _locked=True)
        except BaseException:
            record(u.REPO_ROOT, outcome='failure')
            raise
        return record(u.REPO_ROOT, outcome='success')


if __name__ == '__main__':
    run(['update_dashboard.py', *sys.argv[1:]])
