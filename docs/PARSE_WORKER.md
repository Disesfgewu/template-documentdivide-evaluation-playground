# document.parse worker（Python consumer）

Refs #7。實作：`services/api/lab_api/parse_worker.py`（`process_parse_job`）、`db/lab_db/jobs.py`（`finish(hook=)`、`fail_exhausted`、`acknowledge_cancel`）。

## 偏離 SPEC 12.2 的紀錄（ADR 摘要）

SPEC 12.2 以 Go 實作 agentKit workflow graph。這個 consumer 用 **Python**：parser、`lab_db.jobs` 的 lease/fencing 都在 Python，Go worker 仍是骨架，agentKit 是 private repo 在 public CI 無法取得。PM 已接受此偏離，限於 11/11 backend 範圍（SPEC 12.2 允許 Python PDF 服務）。agentKit graph、真實 LLM 呼叫仍屬 Go worker，維持 `blocked: no credentials`，不在此重做。

## persist 契約

`persist(conn, project_id, lease, batch)` 在 `jobs.finish(..., hook=...)` 的同一個 transaction 內執行：已通過 lease 檢查、job 列已鎖定且仍是 `running`、屬於該 lease。必須只透過 `conn` 寫入，並以 `(job_id, operation_id)` 為冪等鍵。例外會讓整個 transaction（包含 persist 的寫入）回滾，job 標為 `failed(persist_failed)`。沒有預設的 persist：沒有人存結果就不可能標 `succeeded`。

## 失敗與取消

| 情況 | 結果 |
|---|---|
| 不支援 profile／頁面選取、PDF 無效、normalizer quarantine、資源耗盡、artifact 不可讀、persist 失敗、其他例外 | `failed`（固定 reason code；其他例外為 `internal_error`） |
| timeout、process_unavailable、I/O | 還有 attempts 就回 `queued`，否則 `failed` |
| lease 遺失（被接手） | `abandoned`：不寫結果，由新持有者決定 |
| cancel_requested | 停止子程序，worker 以 `acknowledge_cancel` 轉為 `cancelled` |
| lease 過期且 attempts 用完（worker 崩潰） | 下次有人要求認領時由 `fail_exhausted` 轉 `failed(lease_exhausted)` |

取消延遲：heartbeat 在自己的連線上最多每 2 秒執行一次（與 lease ttl 無關），同時偵測取消與 lease 遺失；暫時性錯誤重試 3 次才放棄。`jobs` 的 CHECK 只允許 `running` 帶 lease，所以 cancel 請求一定會清掉 lease。

## 尚未涵蓋（已知）

- 沒有 not-before／backoff：transient 重排後呼叫端迴圈必須自己退避。
- 沒有併發上限、頁數上限與排程入口（沒有 CLI 或常駐程序會呼叫 consumer）。
- 隔離（quarantine）的批次不會被記錄；回應沒有「合成／真實」標記（資料庫無從得知）。

## 已存結果與讀取 API（migration 0012）

`lab_db.parse_results.persist_parse_result` 是上述契約中的 persist：把原始輸出存為 content-addressed artifact，寫入 `parse_results`（manifest、capabilities、不含各頁的 result、`record_sha256`）與每頁一列 `parse_page_results`（status、diagnostics、evidence、observations），全部 append-only。以 job 為冪等鍵；重放必須與整筆紀錄的雜湊相同，否則 `ParseResultConflict`。整筆紀錄超過 64 MiB 以 `ParseResultTooLarge` 拒絕且不寫入任何東西，資料庫另對各 JSON 欄位設上限（manifest／capabilities 1 MiB，result 與頁面 evidence／observations 16 MiB）。

讀取：`GET /api/v1/documents/{document_id}/parse-results/{job_id}`（摘要）與 `.../pages/{page_index}`（單頁）。權限動作為 `read_gold_and_generation_context`（owner、maintainer、reviewer）：organizer 的 parse 輸出是產生 evidence 的來源，SPEC 16.4 讓 participant 的解析與之分開，所以 participant 與 observer 一律 403（不論結果是否存在）；非成員、未知文件、未知結果、不在結果內的頁面為 404。
