# 第二階段執行紀錄

基準：f5974be；使用者授權於同一隔離分支 codex/disposition-phase1 接續第二階段。
保持 main、正式資料與線上網站不動；不 push、不部署、不更改 GitHub 平台設定。

沿用已核准設計，修改既有資料生成與 workflow 流程，不新增外部服務。

- [x] 離線快照保留 sources、公告日期、比較基準；預覽與正式產物隔離。
- [x] 先在暫存區生成全部產物，驗證日期／統計／來源／hash；寫入失敗恢復舊產物。
- [x] 探測紀錄改 Actions artifact；更新推送不盲目 rebase 已生成資料；明確請求 Pages build 並驗證。
- [x] 最近成功檢查與來源狀態分開記錄；呈現實際 workflow 與上線狀態，未知不假稱成功。
- [x] 離線故障注入、workflow／部署腳本測試、獨立 review 與整體驗收。

基線：185 tests passed（第一階段）。

最終驗收：229 Python tests、8 JS health scenarios 全數通過；兩份 workflow YAML 可解析，`git diff --check` 通過。實際 10/2 快照離線預覽成功，正式 index / JSON / manifest / data 無 diff。獨立審查修正後通過（審查者另跑 37 個針對性 Python tests + 8 JS scenarios）。

審查追加修正：preview 遇正式 journal 拒絕而非自動復原；heartbeat 納入同一把更新鎖；完整 state 與當日 history 驗證；推送回覆遺失時只接受遠端等於預定 HEAD；非空 baseline 保留與重跑位元組一致性測試。

## 操作與語意

- 本機離線預覽：`python3 -B scripts/update_dashboard.py --render-only`，输出 `.preview/快照日期/index.html` 及配套 JSON、JS、manifest。預覽不查 GitHub、不允許部署驗證器接受。
- CI 正式更新入口：`python scripts/run_update.py`；market bundle 與 `update-status.json` 心跳分開。沒有市場變動仍會留下檢查時間 commit，訊息明示非市場更新。
- 主更新先測試、抓取、暫存、驗證，再寫入產物與 commit。遠端 main 前進時停止，需從最新 checkout 重跑，絕不把舊產物 rebase 到新版本。
- 探測預設只印出；`--output .run/probe.json` 供 artifact 留存 30 天。`--reference-date YYYY-MM-DD` 可固定跨午夜比較基準，預設明示台灣本地日期，非交易日推定。
- 頁面健康列區分資料日期、最近完成檢查、最新工作流程、最近成功流程（含部署驗證）。GitHub API 無法取得時顯示未知與 Actions 連結；不把檢查成功當成部署成功。
- 第一次 Phase 2 CI 的 run ID 起算，舊版 workflow 綠燈不算已驗證。成功流程時間是 workflow 更新時間，非精確部署完成時間。

## 邊界與恢復

- 個別檔案以 replace 寫入；多檔不是單一原子操作。完整 journal 可在錯誤當下或下次啟動回復舊檔。沒有承諾硬碟斷電 fsync 級耐久性。
- 發布到 Pages 的單位是驗證過的 Git commit；直接讀取工作目錄的其他程式應驗證 manifest，避免讀到短暫跨檔版本。
- 復原只允許既定產物與日期命名 history；備份缺失時停止，不自動刪 journal。保留 `.dashboard-transaction.json` 與 `.dashboard-backup-*` 供人工處理。
- 失敗更新不推送半成品。失敗紀錄保留於 Actions artifact；頁面透過公開 Actions API 看見失敗／執行中，API 限流時只顯示未知。
- 未變更 GitHub 排程時間、Pages 設定或外部服務。排程仍屬 best-effort，這次改善可觀測性與一致性，不能保證準時啟動。
- 本輪僅離線測試與 preview，尚未實跑 GitHub Actions／Pages API；正式上線後仍需一次端到端驗收。
