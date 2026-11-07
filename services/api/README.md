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
- 請求 Trace 關聯（`lab_api.trace_context.TraceContextMiddleware`，#6 API 側基礎增量）：
  - 信任邊界預設關閉（`LAB_TRUST_REMOTE_TRACEPARENT=false`）：公開或未受信任入口傳入的 `traceparent`、`tracestate`、`baggage`、`X-Request-ID` 一律不採信，每次請求皆以密碼學亂數重新產生 `request_id`（`req_<32hex>`）、`trace_id`（32 小寫 hex）與 `span_id`（16 小寫 hex），`ingress` 記為 `ignored_untrusted`（未帶 header 時為 `absent`）。
  - 明確啟用受信任入口（`LAB_TRUST_REMOTE_TRACEPARENT=true` 或 `create_app(..., trust_remote_traceparent=True)`）：僅接受單一、嚴格有界的 W3C version `00` `traceparent`（55 字元 ASCII 小寫 `00-<32hex>-<16hex>-<00|01>`，且 `trace_id`／`span_id` 非全零），延續 `trace_id` 與 `sampled`，將遠端 span 記為 `parent_span_id` 並產生新的本地 `span_id` 與 `request_id`（`ingress="accepted"`）。若重複、超長、非 ASCII、含空白、版本或旗標不支援、全零 ID，絕不拒絕合法 HTTP 請求，而是改開新 `trace_id`（`ingress="rejected"`），且絕不在回應或狀態中回顯外部輸入。
  - 無 payload 狀態與回應標頭：於子 `scope["state"]`（`request.state.trace_context`、`request_id`、`trace_id`、`span_id`、`parent_span_id`）及 `current_trace_context()` 提供僅含隨機或已驗證關聯 ID 的結構（不含 URL、query、headers、body 或錯誤文字），不變動呼叫端傳入的 `scope`／`scope["state"]`；所有 HTTP 回應（含 `/healthz`、`/readyz`、404/405 及未處理例外產生的 500 回應）皆附加本地產生的 `X-Request-ID` 與 `traceparent` 標頭，並保留 ASGI 例外重拋語意。
  - 範圍區隔：此模組為 FastAPI/ASGI 入口的純程序內關聯基礎原語，外部或上游 `traceparent` 僅為診斷關聯 metadata，絕不作為身分認證或授權依據。與 Go worker 側 `services/worker/internal/tracebridge`（PR #50）分屬不同服務邊界；跨服務實際串接、持久化 `trace_spans`／`log_events`／`audit_events` PostgreSQL 寫入、權限控管（#5）與完整 workflow／provider 整合仍在後續增量完成。
- 設定：`LAB_ENV`、`LAB_PROVIDER_MODE`、`LAB_DATABASE_URL`、`LAB_ARTIFACT_ROOT` 沒有預設值，缺少或不合法時啟動失敗。資料庫只接受 `postgresql://`（不以 SQLite 代替）。
- mock 與真 provider 分開：`mock` 是本機決定性假回應；`real` 目前**直接拒絕啟動**，因為尚無真 provider adapter（T84 待後續）。`prod` 禁止搭配 `mock`。
