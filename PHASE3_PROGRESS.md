# 第三階段執行紀錄

基準 5078835，沿用 codex/disposition-phase1 隔離 worktree。
規格：已核准的第 10–12 項（公告／推估區隔、就近缺值警告、360/390/430px 手機實測、回歸與完整流程驗收）。

- [x] 呈現語意與缺資料警告，先建立失敗案例。
- [x] 手機實測出關排程、長股名、缺價、明細、搜尋及空結果並修正。
- [x] 全套測試、獨立審查及交付。

Ruling: 屬既有畫面的限範圍修改，沿用已核准對話設計，不改框架、不新增雷達區塊；保留深藍底、原字型與三分頁，只改善訊息層級與換行。若理解有誤，代價是需調整局部文案／樣式，不影響資料算法。
Ruling: index.html 同時是 renderer 模板，本輪可修改靜態樣式／互動，但不重抓或覆寫正式市場 JSON；生成只輸出 preview。不得 push、merge、部署。
基線：229 Python tests 通過。測試瀏覽器阻擋瀏覽計數 API，避免驗收增加正式統計。

## 目前驗證

- Python 新增 11 個案例，涵蓋來源部分失敗、推估非公告、缺價／缺門檻、無法解析不能冒充倒數、正式待生效說明、期滿日語意、出關報價、資料日、下一交易日及審查兩項回歸。先失敗後通過；全套 240 通過，另 8 個 JS health 情境通過。
- 瀏覽器先重現 15 項失敗（360px 長股名溢出、各寬度排程欄位不齊、出關搜尋假空結果、篩選隱藏來源警告、badge 語意丟失），修改後三寬度全部通過。
- 加測產業／文字交集與收合批次搜尋；三寬度先重現搜尋結果留在收合批次，再修復自動展開，清空後回復原收合狀態。
- 真實 10/2 快照在 360、390、430、1280px 的三分頁均無整頁橫向溢出，390px 排程與雷達截圖已人工檢視。
- 本輪使用桌面 Chromium 的 viewport 模擬，非實體手機 Safari／觸控／VoiceOver 驗收；未執行正式資料更新或線上部署。

## 獨立審查與修正

- Final: fixed 注意次數解析與價格門檻可用性混淆 — `test_unparsed_counts_do_not_hide_available_price_threshold` RED→GREEN；沒有次數分析仍展示現有價格條件與展開內容。
- Final: fixed 明細推估冒稱第一次處置與固定措施 — `test_risk_details_do_not_promise_first_disposition_or_specific_measures` 二／三／五次分支 RED→GREEN；改為待官方確認，不再用下一平日當作預告公告日期。
- 瀏覽器 fixture 新增完整第一款／第二款／均量條件，三寬度展開均通過無溢位檢查。最終全套 240/240。
- Final: Ruling: 審查未重驗市場法規與算法，本輪僅降低呈現確定性、不新增處置規則 — 維持已核准範圍；代價是既有來源次數格式未解析時仍顯示「未確認」，不猜測。
- Final: Ruling: 審查未獨立操作瀏覽器或呼叫即時 API — 瀏覽器由主代理實測，正式來源與上線留後續授權；代價是尚不能聲稱實體手機與線上端到端已驗收。
- Final: Ruling: 不重生成正式市場 HTML — index.html 僅修改模板 CSS／篩選互動，市場 JSON 不動；下一次正式更新才會帶入新文案。代價是不可直接把目前 checkout 當成已完成部署的新產物集合。
- 無延後的 Minor findings。保留 worktree，不 push／merge／部署。

## 重跑方式

1. `python3 -B -m unittest discover -s tests -q` 與 `node tests/test_health.cjs`。
2. `python3 -B tests/make_ui_fixture.py` 在 `.preview/mobile-fixture` 產生隔離 fixture，經正式 renderer 與 bundle validator 輸出頁面。
3. 在此 worktree 執行 `python3 -m http.server 8941 --bind 127.0.0.1`，使用 Playwright browser_run_code_unsafe 的 filename 載入 `tests/mobile_ui.js`；回傳 `passed: true` 才算成功。
4. `python3 -B scripts/update_dashboard.py --render-only` 產生真實快照預覽 `.preview/2026-10-02/index.html`。本機伺服器只用於測試，非部署。
