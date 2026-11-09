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

## Method-service HTTP transport foundation (#11)

`lab_api.method_client.MethodClient` makes one real HTTP request per operation,
using the merged `lab_contracts.method` models. It implements capabilities,
prepare acknowledgement, synchronous/asynchronous query acknowledgement, one
job poll, and cancellation acknowledgement. It validates terminal query echo
IDs and prepare document/method manifests against the original request.

The configured origin is the sole operator allowlist entry: only an HTTP(S)
IP-literal origin is supported in this increment. DNS hostnames, URL credentials,
path prefixes, query/fragment configuration, redirects and environment proxies
are unsupported. The standard TLS certificate verifier remains enabled. Private
IP origins are allowed only because deployment explicitly chooses the method
service address; this client is not an arbitrary user URL fetcher. Future method
registration must authorize the configured origin before constructing it.

Every response has a whole-exchange timeout (including a slowly trickled body)
and at most 16 MiB of body bytes. Per-instance limits may only lower these
bounds. Only JSON with the exact expected status and contract is accepted;
duplicate keys, non-finite values, schema drift and wrong bindings fail with a
stable `MethodClientError.code` without remote body or URL diagnostics.

`query(request, deadline=...)`, `poll(job_id, request, deadline=...)` and
`cancel(job_id, request, deadline=...)` require an **absolute `time.monotonic()`
deadline**. The caller establishes it before first query dispatch (no later
than the query budget) and reuses it across queueing/polls/retries; each call
also respects the shorter configured transport timeout. A 202 acknowledgement
is not a completed operation or proof of cancellation. No automatic retry or
poll loop is performed. A durable scheduler must retain original job/request
binding, polling cadence and the overall deadline, and persist intent before
network dispatch. This transport does not implement those durable integrations.

Run the real local TCP service tests with
`PYTHONPATH=services/api:contracts python -m unittest tests.api.test_method_client`
or `scripts/check.sh --scope api`. Tests cover sync/async, prepare/cancel,
strict JSON, binding mismatches, redirects, 503 without retry, bounded chunked
bodies and a slow-trickle deadline. The HTTP test server is synthetic; this is
not a real method/provider acceptance or T30/T32 end-to-end result. #11's
PostgreSQL jobs, authorization, refreshable download grants, participant PDF
parsing, vector store and two independent example methods remain unfinished.


## PDF 上傳與原檔下載（#67／#7 增量）

`POST /api/v1/documents`（亦有 `/documents`、`/api/v1/projects/{project_id}/documents`）、`GET /api/v1/documents/{id}`（`Accept: application/pdf` 取原檔）、`.../original`、`.../download`、`.../pages/{n}`。一律需要簽章的 Bearer actor token，project 角色由 PostgreSQL 判定；沒有在請求內自稱身分的方式。

`GET /api/v1/benchmarks/{logical_id}/revisions/{revision}?project_id=…`：題庫 revision 狀態及公開摘要；各 quota bucket 題數只提供給具有 gold 讀取權限者。

`GET /api/v1/benchmarks/{logical_id}/revisions/{revision}/questions?project_id=…&limit=…&cursor=…`：公開問題集（依 ordinal 排序、cursor 分頁、limit 1–200）。每題只輸出 id、question_text、language，不含答案、facts、evidence sets 與 distractors。owner/maintainer/reviewer 可看草稿；participant 只看已凍結版本，草稿一律 404；observer 的 explicit grant 尚未接線，目前一律 403，即使有 grant 亦無法存取；非成員 404。

- **先授權、再讀 body**：授權（`import_and_configure_parse`）通過後才取得 admission slot 並讀取請求內容；未授權的請求不佔 slot、不讀任何 body 位元組。
- **有界記憶體**：同時上傳數由 slot 控制（`app.state.upload_concurrency`，預設 2；下載 `download_concurrency`，預設 2），沒有 slot 時立刻回 503 `server_busy`（`retryable: true`），不排隊。最壞記憶體約為 `上傳並行數 × 2 × (max_pdf_bytes + 64 KiB)`：multipart body 保存一份，PDF part 再切出一份。`max_pdf_bytes` 預設 512 MiB。
- **讀 body 有期限**：已取得 slot 的上傳必須在 `upload_read_timeout_s`（預設 600 秒）內送完 body，否則回 408 `request_timeout`（`retryable: true`）並釋放 slot，避免少數慢速連線佔住全部 slot。
- **授權兩次**：讀 body 之前檢查一次；寫入交易內再檢查一次（慢速上傳期間若角色被撤銷，不會寫入）。
- **不阻塞 event loop、取消安全**：multipart 解析、sha256、PDF 檢查、SQL、artifact 讀寫都在 worker thread 執行（各用有上限的 `CapacityLimiter`）。請求被取消（斷線、逾時、關機）時，取消會等 worker 結束才繼續，因此 request 專用的連線與 store 不會在 worker 還在用時被關閉。不使用 `run_in_executor`（取消時它會立即返回、thread 仍在執行）。
- **嚴格 multipart**（`lab_api/multipart_strict.py`）：只接受固定格式（不接受 preamble、折行或重複 header、`base64`／`quoted-printable` 編碼）、最多 4 個 part、只允許一個 PDF part 與最多一個 `logical_id` 文字欄位；其他 part 一律拒絕而非忽略。PDF part 超過上限回 413，非 PDF 媒體型別回 415。
- **冪等重播**：帶 `Idempotency-Key` 時，同 key 同內容重送會重播原結果；成功回傳 200 `replayed: true`。被拒絕的請求（例如加密 PDF、頁面幾何錯誤）的**原始回應**與 operation 在同一交易中存入 `operation_results`（0009，append-only），重送時原樣回傳，只換上新請求的 `request_id`，不會重新執行 parser、也不受當下限制或程式版本影響。0009 之前就已失敗、沒有保存回應的 operation 回 409，並說明原回應未保留。同 key 不同內容回 409。
- **下載**：授權與 artifact 狀態、大小上限、checksum 都先在資料庫層檢查，才讀取檔案；讀檔受 `max_pdf_bytes` 限制並驗證 sha256。
- **頁面預覽（Accept: image/png）**：`GET /api/v1/documents/{document_id}/pages/{page_index}` 預設回傳 JSON 幾何資訊；帶有 `Accept: image/png` 時，在獨立監督 subprocess 中以固定 150 DPI 渲染原檔 PDF（`lab_parser.render.render_region`，支援旋轉與 CropBox）。受 POSIX 資源上限（CPU/AS/FSIZE）與 hard timeout 控管。預設 2 個 preview 并行 slots（`preview_concurrency`，滿載回 503 `server_busy`），在讀取 PDF 前即授權與取 slot，取消安全保護。回應包含 `X-Document-Sha256`、`X-Page-Index`、`X-Image-Sha256`、`X-Image-Width`、`X-Image-Height`、`Cache-Control: no-store`。超出頁碼回 404、路徑不合法回 422、像素上限回 422、渲染器逾時/故障回 504/503。渲染頁面不宣稱為已驗證之證據。

尚未完成（本增量不宣稱 #67／#7 驗收）：上傳前的連線仍由 request dependency 建立（未驗證 actor 的連線洪流需由部署層限制）；`open`／`verify` 在未指定上限時仍可無上限讀取，需逐一評估呼叫端；project 層級的授權／RLS 完整行為、parse job 與批次、OCR。

`GET /api/v1/audit-events?project_id=…`：稽核事件查詢，可依 actor_id、resource_id、resource_type、action、outcome、since（含）/until（不含）過濾；新到舊、keyset cursor、limit 1–200。不認得的參數一律 400，避免拼錯的過濾條件悄悄放寬結果。存取暫用 `read_gold_trace_and_lineage`（owner/maintainer/reviewer），與 `GET /jobs/{id}` 相同，待 PM 決定；其他成員 403、非成員 404。
`GET /api/v1/runs/{id}`：run 狀態、benchmark revision、budget profile、finalized_at 與進度計數（題數、attempt、已接受作答/棄答、待處理、被拒及逾時提交）。只回計數，不含答案、payload 或分數。owner/maintainer/reviewer 可看專案內任何 run；participant 只能看自己擁有之 method revision 的 run，其餘 404；observer 僅限已發布彙總（尚未實作），故 403；非成員 404。

`GET /api/v1/runs/{id}/results`：某次 evaluation（預設最新，可用 `evaluation_id` 指定）的逐指標摘要（scored / not_applicable / pending / error 計數與已評分列的平均 raw_value）及逐題分數頁（ordinal、metric 排序，keyset cursor）。各指標獨立呈現，不產生加權總分（SPEC 9.1）；N/A 與待處理不以 0 計入平均。可用 `metric_name`、`quota_bucket` 過濾。僅 owner/maintainer/reviewer；participant 的可見分數取決於尚未定義的 feedback profile，自己的 run 為 403、他人的 run 為 404；observer 403。

Results summaries explicitly return `summary_scope="scored_rows_only"` and `provisional=true`, even when an evaluation status is completed. They are temporary statistics over stored scored rows, not the SPEC §16.5 final macro average: missing method-failure rows and the full applicable-question denominator are not yet accounted for. N/A is excluded and pending/error counts remain visible; participant feedback profiles and observer published aggregates remain separate unfinished gates. Cursors are canonical and bound to the evaluation and effective metric/bucket filters; all response score data shares one PostgreSQL statement snapshot.
`GET /api/v1/jobs/{id}`：工作的 kind、stage、status、attempt/max_attempts、retryable、row_version 及最近 50 筆 transition（新到舊，超過時 `transitions_truncated`；每筆 reason 於 SQL 讀取與 API 投影兩層設上限 2000 字元，超長原因採 fail-closed 回傳 `[omitted: reason exceeds maximum length]` 並標記 `reason_truncated: true`，避免截斷邊界 secret 洩漏；普通短 reason 以 `lab_db.logs.redact` 遮蔽敏感資訊，保留原始 DB 稽核紀錄）。不回傳 lease owner、token 或到期時間。先查專案驗證權限，再以單一 statement snapshot 讀取狀態與 transitions，避免並行更新時讀到狀態與 transition 不一致。權限依 PM 在 Issue #6 決策限定 staff 角色，暫用 `read_gold_trace_and_lineage`（owner/maintainer/reviewer）；其他成員 403、非成員 404。
## 血緣查詢（#5 增量）

`GET /api/v1/resources/{resource_id}/lineage`（亦有 `/resources/{id}/lineage`）回傳某個資源版本的上游或下游血緣，實際走訪由 `lab_db.lineage.walk` 完成。

- 參數（全部嚴格驗證，錯誤一律 400 `bad_request`，重複或未知的參數也拒絕）：`direction`（`upstream` 預設／`downstream`）、`depth`（1–10，預設 3）、`max_nodes`（1–1000，預設 1000）、`max_edges`（1–10000，預設 2000）、`limit`（每頁節點數 1–200，預設 100）、`cursor`。
- 權限：`read_gold_trace_and_lineage`，只有 owner、maintainer、reviewer 可讀；participant 與 observer 回 403（血緣可能暴露由 gold 衍生的資源）。資源不存在、或屬於呼叫者不是成員的 project，兩者都回完全相同的 404 `resource not found`，不洩漏存在與否。
- 回應：`nodes`（`id`、`resource_type`、`logical_id`、`revision`、`content_hash`、`depth`）、`edges`（`upstream_id`、`downstream_id`、`relation`、`operation_id`）、`next_cursor`、`truncated`（深度、節點、邊數或 frontier 上限之外還有未走訪的鄰居或未回傳的邊）、`frontier_count`（有界未走訪鄰居數，上限 1000）。
- 資源與分頁語意：資料庫讀取與回應均設強制上限（edge 與 frontier 查詢均帶 SQL `LIMIT`，避免高扇出與同端點多 operation 消耗大量記憶體）。分頁以節點（depth, id）為主序；每條已走訪 edge 列在其較遠端節點所在頁面。`truncated: true` 只在確有資料被省略時出現：深度、max_nodes、max_edges 或 max_frontier 上限之外仍有未走訪的鄰居，或有尚未回傳的 edge（包含指向已走訪節點的 edge）；剛好用完上限而沒有省略任何資料時為 `false`。`cursor` 採 HMAC 簽章並綁定該次查詢（資源、方向、depth、max_nodes、max_edges），竄改任何欄位（包含保留 q 但變更 depth 或 node id）一律 400；`limit` 可在翻頁間改變。
