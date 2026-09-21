#!/usr/bin/env bash
# 本機手動更新的標準入口：先同步遠端（GitHub Actions 可能已推新 commit），
# 跑測試，再執行更新腳本。直接跑 update_dashboard.py 容易與 Actions 產生分岔。
#
# 用法：scripts/run_local.sh [選項]
#   （無選項）      正常抓取並更新
#   --render-only   從 dispo.json 離線重繪頁面，不連任何 API。改 renderer／CSS
#                   後在本機預覽用；本機 Python 對證交所憑證鏈較嚴格，正常抓取
#                   會踩 SSL 問題，這條路完全不需要網路。
#   --allow-drop    確認過檔數驟降屬實時放行驟降守則
# （--force 已於 2026-09 移除：它會一次解除所有資料品質防線，詳見 R03）
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
