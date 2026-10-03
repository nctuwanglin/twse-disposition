const assert = require('node:assert/strict');
const {summarize} = require('../assets/health.js');
const status = {last_successful_check_at:'2026-10-02T10:00:00Z', data_date:'2026-10-02',
  market_hash:'abc', pipeline_since_run_id:'100', last_attempt:{outcome:'success'}, degraded_sources:[]};
const view = (s, runs) => summarize(s, runs, 'abc', new Date('2026-10-02T11:00:00Z')).join(' ');
assert.match(view(null, null), /尚無健康紀錄/);
assert.match(view(status, null), /執行狀態.*查不到/);
assert.match(view({...status, market_hash:'old'}, []), /不一致/);
assert.match(view({...status, last_attempt:{outcome:'partial'}, degraded_sources:['tpex_quotes']}, []), /部分來源/);
assert.match(view(status, [{id:101, status:'completed', conclusion:'failure'}]), /失敗/);
assert.match(view(status, [{id:101, status:'in_progress', conclusion:null}]), /執行中/);
assert.match(view(status, [{id:101, status:'completed', conclusion:'success', updated_at:'2026-10-02T10:01:00Z'}]), /最近成功流程.*上線驗證/);
assert.match(view(status, [{id:99, status:'completed', conclusion:'success', updated_at:'2026-10-01T10:00:00Z'}]), /尚無可確認的上線驗證/);
console.log('health: 8 scenarios passed');
