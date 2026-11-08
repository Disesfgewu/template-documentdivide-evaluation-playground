# LAB web（React 骨架）

#3 的增量：Vite＋React＋TypeScript 骨架，只有一個「系統狀態」畫面，內容直接取自 LAB API 的 `/readyz`。

#13 的增量：文件上傳與授權下載操作員介面（`DocumentOperator`）。
- 認證與授權：由操作員於介面輸入 Bearer Token，Token 僅存於記憶體，絕不寫入 LocalStorage/SessionStorage/Cookie，亦不留存 Log。
- 檔案上傳：以 `multipart/form-data` 傳送檔案至 `/api/v1/projects/:project_id/documents`，驗證 UUID，嚴格核對回應欄位（UUID 格式、請求 project_id 一致性、SHA-256、正整數 revision/size_bytes/page_count），提供載入中、錯誤、權限不足（403）、認證失敗（401）與上傳成功狀態。
- 冪等性防護：每次上傳產生穩定 `Idempotency-Key`，在網路連線中斷或逾時（曖昧失敗）時重試保留相同 key，在輸入異動或確認成功後自動輪替新 key，並防範重複點擊提交。
- 檔案下載：以 `GET /api/v1/documents/:document_id/download` 並帶 `Accept: application/pdf` 取得檔案 Blob，嚴格驗證回應 Content-Type 為 `application/pdf` 且二進位資料以 `%PDF-` 簽章開頭（阻擋 200 HTML 偽裝下載），驗證 UUID，安全處理 Content-Disposition 百分比編碼（malformed percent-encoding 不崩潰並優雅降級），觸發下載後立即撤銷 Object URL，避免惡意 URL 導向。
- 後端整合閘門（PR #73）：後端端點依賴 `origin/issue-67-authenticated-pdf-integration`（PR #73，`services/api/lab_api/documents.py` 與 `auth.py`）。在 PR #73 合併入 main 前，現有 main 之 API 伺服器尚未包含此 API，呼叫將回傳 404。
- 未接入功能：登入發證流程、背景任務（Jobs）、題庫工作台、評分與比較檢視畫面等尚未實作（#7、#10–#13）。本介面僅為操作員介面基礎，不宣稱父任務完工。
- 產品 UI 去除技術細節：前端畫面上不顯示 PR 編號、分支路徑與內部 Issue 編號，簡明陳述後端服務可用性與未接入功能，技術整合閘門完整記錄於本 README。

- 狀態：檢查中、錯誤（連不到、非 JSON、狀態碼與內容不一致）、就緒、未就緒。沒有任何假資料；HTTP 200 卻回 `not_ready`，或 503 卻回 `ready`，都視為錯誤。
- provider 為 `mock` 時，畫面會標示「本機假資料，非真模型」。

```bash
cd web
npm ci            # 版本由 package-lock.json 鎖定；.npmrc 停用 install scripts
npm run dev       # /api/* 代理到 LAB_API_URL（預設 http://127.0.0.1:8000）
npm test && npm run build
```

`.npmrc` 設定 `legacy-peer-deps`：npm 10.9 解析 `@vitejs/plugin-react` 的 optional peer 時會出現 `Cannot read properties of null (reading 'edgesOut')`。因此 peer 依賴（`@testing-library/dom`）改為明確列在 devDependencies。
