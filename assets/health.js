(function (root) {
  'use strict';
  const time = value => value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString('zh-TW', {timeZone: 'Asia/Taipei', hour12: false}) : '未知';
  function summarize(status, runs, marketHash, now = new Date()) {
    const lines = [];
    if (!status) lines.push('尚無健康紀錄；資料日期不代表最近檢查時間。');
    else if (status.market_hash !== marketHash) lines.push('健康紀錄與目前資料不一致，可能仍在部署中。');
    else {
      lines.push(`資料日期：${status.data_date || '未知'}；最近完成檢查：${time(status.last_successful_check_at)}（台灣時間）`);
      if (status.last_attempt?.outcome === 'partial') lines.push(`部分來源異常：${(status.degraded_sources || []).join('、')}`);
      if (status.last_attempt?.outcome === 'failure') lines.push('最近資料更新失敗。');
      if (now - new Date(status.last_successful_check_at) > 48 * 3600000)
        lines.push('超過 48 小時未確認更新；可能包含休市日，請查看執行紀錄。');
    }
    if (!Array.isArray(runs)) lines.push('GitHub 執行狀態目前查不到，請查看 Actions 紀錄。');
    else {
      const latest = runs[0];
      if (latest && latest.status !== 'completed') lines.push('最近流程：執行中或排隊中。');
      else if (latest && latest.conclusion !== 'success') lines.push(`最近流程：失敗或未完成（${latest.conclusion || '未知'}）。`);
      const success = status?.pipeline_since_run_id && runs.find(r =>
        r.status === 'completed' && r.conclusion === 'success' &&
        BigInt(r.id) >= BigInt(status.pipeline_since_run_id));
      lines.push(success ? `最近成功流程（含上線驗證）：${time(success.updated_at)}（台灣時間；非精確部署時刻）`
        : '尚無可確認的上線驗證；資料檢查成功不等於已上線。');
    }
    return lines;
  }
  async function json(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (_) { return null; }
    finally { clearTimeout(timer); }
  }
  async function boot() {
    const panel = document.getElementById('update-health');
    if (!panel || panel.dataset.preview === 'true') return;
    const [status, response] = await Promise.all([
      json('update-status.json'),
      json('https://api.github.com/repos/nctuwanglin/twse-disposition/actions/workflows/update.yml/runs?branch=main&per_page=20')
    ]);
    const output = panel.querySelector('[data-health-output]');
    if (output) output.textContent = summarize(status, response?.workflow_runs, panel.dataset.marketHash).join(' ｜ ');
  }
  if (typeof module !== 'undefined') module.exports = {summarize};
  if (typeof document !== 'undefined') boot().catch(() => {});
})(typeof globalThis !== 'undefined' ? globalThis : this);
