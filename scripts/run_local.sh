#!/usr/bin/env bash
# 本機手動更新的標準入口：先同步遠端（GitHub Actions 可能已推新 commit），
# 跑測試，再執行更新腳本。直接跑 update_dashboard.py 容易與 Actions 產生分岔。
# 用法：scripts/run_local.sh [--force]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== git pull --rebase =="
git pull --rebase

echo "== parser 測試 =="
python3 -m unittest discover -s tests -q

echo "== 更新腳本 =="
python3 scripts/update_dashboard.py "$@"

echo "== 工作區狀態（如有變更請自行 commit/push）=="
git status -sb
