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

## PDF 上傳與原檔下載（#67／#7 增量）

`POST /api/v1/documents`（亦有 `/documents`、`/api/v1/projects/{project_id}/documents`）、`GET /api/v1/documents/{id}`（`Accept: application/pdf` 取原檔）、`.../original`、`.../download`、`.../pages/{n}`。一律需要簽章的 Bearer actor token，project 角色由 PostgreSQL 判定；沒有在請求內自稱身分的方式。

- **先授權、再讀 body**：授權（`import_and_configure_parse`）通過後才取得 admission slot 並讀取請求內容；未授權的請求不佔 slot、不讀任何 body 位元組。
- **有界記憶體**：同時上傳數由 slot 控制（`app.state.upload_concurrency`，預設 2；下載 `download_concurrency`，預設 2），沒有 slot 時立刻回 503 `server_busy`（`retryable: true`），不排隊。最壞記憶體約為 `上傳並行數 × 2 × (max_pdf_bytes + 64 KiB)`：multipart body 保存一份，PDF part 再切出一份。`max_pdf_bytes` 預設 512 MiB。
- **讀 body 有期限**：已取得 slot 的上傳必須在 `upload_read_timeout_s`（預設 600 秒）內送完 body，否則回 408 `request_timeout`（`retryable: true`）並釋放 slot，避免少數慢速連線佔住全部 slot。
- **授權兩次**：讀 body 之前檢查一次；寫入交易內再檢查一次（慢速上傳期間若角色被撤銷，不會寫入）。
- **不阻塞 event loop、取消安全**：multipart 解析、sha256、PDF 檢查、SQL、artifact 讀寫都在 worker thread 執行（各用有上限的 `CapacityLimiter`）。請求被取消（斷線、逾時、關機）時，取消會等 worker 結束才繼續，因此 request 專用的連線與 store 不會在 worker 還在用時被關閉。不使用 `run_in_executor`（取消時它會立即返回、thread 仍在執行）。
- **嚴格 multipart**（`lab_api/multipart_strict.py`）：只接受固定格式（不接受 preamble、折行或重複 header、`base64`／`quoted-printable` 編碼）、最多 4 個 part、只允許一個 PDF part 與最多一個 `logical_id` 文字欄位；其他 part 一律拒絕而非忽略。PDF part 超過上限回 413，非 PDF 媒體型別回 415。
- **冪等重播**：帶 `Idempotency-Key` 時，同 key 同內容重送會重播原結果；成功回傳 200 `replayed: true`。被拒絕的請求（例如加密 PDF、頁面幾何錯誤）的**原始回應**與 operation 在同一交易中存入 `operation_results`（0009，append-only），重送時原樣回傳，只換上新請求的 `request_id`，不會重新執行 parser、也不受當下限制或程式版本影響。0009 之前就已失敗、沒有保存回應的 operation 回 409，並說明原回應未保留。同 key 不同內容回 409。
- **下載**：授權與 artifact 狀態、大小上限、checksum 都先在資料庫層檢查，才讀取檔案；讀檔受 `max_pdf_bytes` 限制並驗證 sha256。

尚未完成（本增量不宣稱 #67／#7 驗收）：上傳前的連線仍由 request dependency 建立（未驗證 actor 的連線洪流需由部署層限制）；`open`／`verify` 在未指定上限時仍可無上限讀取，需逐一評估呼叫端；project 層級的授權／RLS 完整行為、parse job 與批次、OCR。

`GET /api/v1/jobs/{id}`：工作的 kind、stage、status、attempt/max_attempts、retryable、row_version 及最近 50 筆 transition（新到舊，超過時 `transitions_truncated`；以 `lab_db.logs.redact` 遮蔽敏感資訊，保留原始 DB 稽核紀錄）。不回傳 lease owner、token 或到期時間。先查專案驗證權限，再以單一 statement snapshot 讀取狀態與 transitions，避免並行更新時讀到狀態與 transition 不一致。權限依 PM 在 Issue #6 決策限定 staff 角色，暫用 `read_gold_trace_and_lineage`（owner/maintainer/reviewer）；其他成員 403、非成員 404。
