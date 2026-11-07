# LAB API（FastAPI 骨架）

#3 的第一個增量：設定驗證、liveness/readiness，以及真 PostgreSQL＋pgvector 的就緒檢查。業務端點、migration、權限在後續 Issue（#4、#5）。

## 乾淨環境啟動

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r services/api/requirements.lock
docker compose -f deploy/compose.dev.yml up -d --wait     # PostgreSQL 17 + pgvector 0.8.6（digest 鎖定）

export LAB_ENV=dev LAB_PROVIDER_MODE=mock \
       LAB_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab \
       LAB_ARTIFACT_ROOT="$PWD/.lab-artifacts"
mkdir -p "$LAB_ARTIFACT_ROOT"
(cd services/api && python -m lab_api check-config)
(cd services/api && uvicorn --factory lab_api.main:create_app --port 8000)
curl -s localhost:8000/healthz; curl -s localhost:8000/readyz
```

變數說明見 `services/api/.env.example`（程式不讀 .env 檔）。

## 行為

- `/healthz`：只表示程序存活，不檢查依賴。
- `/readyz`：真的連資料庫並確認 `vector` extension 存在，並在 artifact 根目錄實際建立後刪除暫存檔。任一項失敗回 503 `not_ready`；檢查本身拋例外也算失敗。回應只帶例外類別名稱，不回傳連線字串或主機資訊。
- 設定：`LAB_ENV`、`LAB_PROVIDER_MODE`、`LAB_DATABASE_URL`、`LAB_ARTIFACT_ROOT` 沒有預設值，缺少或不合法時啟動失敗。資料庫只接受 `postgresql://`（不以 SQLite 代替）。
- mock 與真 provider 分開：`mock` 是本機決定性假回應；`real` 目前**直接拒絕啟動**，因為尚無真 provider adapter（T84 待後續）。`prod` 禁止搭配 `mock`。
- 結構化請求／錯誤日誌基礎（`lab_api.request_logging`）：依 SPEC §10.3、R18、R19、T69，透過 ASGI middleware（`RequestLoggingMiddleware`）為目前實際存在的 LAB API 路徑發出有界、僅含白名單欄位的結構化 JSON 紀錄（`RequestLogEvent`，預設輸出至 `lab_api.request_log` logger，亦可於 `create_app(..., log_sink=...)` 注入觀察器）。
  - **白名單欄位與分類**：每筆 HTTP 請求產生一筆固定欄位紀錄（`event_id`、`schema_version`、`occurred_at`、`observed_at`、`level`、`event_name`、`service`、`component`、`env`、`provider_mode`、`request_id`、`operation_name`、`http_method`、`route`、`status`、`http_status`、`duration_ms`、`error_code`、`error_type`、`telemetry_origin`）。路由僅記錄固定白名單樣板（`/healthz`、`/readyz` 或 `unmatched`）；HTTP method 僅接受標準白名單（`GET`、`HEAD`、`POST`、`PUT`、`DELETE`、`OPTIONS`、`PATCH`），其餘正規化為 `INVALID_METHOD`；`http_status` 嚴格驗證為 `100..599` 的整數（排除 `bool`）。
  - **零敏感資訊外洩與例外區分**：絕不讀取或記錄原始 URL path、動態路由參數、query string、request/response headers（如 `Authorization`、`Cookie`、`X-API-Key`、外部 `X-Request-ID`）、request/response body、client IP、設定密鑰／DB URL 或原始例外訊息／stack trace。未處理例外與 `BaseException`（如 `CancelledError`、`TimeoutError`、標準內建錯誤及自訂例外）僅映射為封閉白名單的 `error_code`／`error_type` 並原樣重拋，完整保留 FastAPI／ASGI 回應與錯誤語意；若同步 `log_sink` 拋出例外，僅遞增 `dropped_count` 而不影響請求處理。
  - **精確範圍與限制（非持久稽核或完整追蹤宣告）**：此模組僅為程序內（in-process）ASGI 請求／錯誤觀測基礎原語，**不宣稱**具備 PostgreSQL `log_events`／`audit_events` 持久化儲存、collector 緩衝／缺口偵測／重送去重、`/logs` 過濾與匯出（T68）、跨服務分散式 trace 傳遞、身份驗證與角色／跨專案權限控管（T70），亦不代表 #14 或父任務驗收完成。
