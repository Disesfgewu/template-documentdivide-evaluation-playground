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
| 資料庫或連線層失敗（含 persist 內；`acquire`、`fail_exhausted`、取消確認也受保護） | `abandoned(infrastructure_error)`：job 維持 `running`，lease 過期後重跑或由 `fail_exhausted` 收尾；只有連線層錯誤算數，`QueryCanceled`／`ProgramLimitExceeded` 是確定性錯誤（`persist_failed`／`internal_error`） |
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

## 執行 worker（`python -m lab_api.parse_worker_main`）

設定與 API 相同（`LAB_DATABASE_URL`、`LAB_ARTIFACT_ROOT`…）；解析函式庫要在 `--python` 指定的直譯器（預設目前這個）。`--once` 處理目前可認領的 job 後結束並印出各結果的計數；否則輪詢到收到 SIGTERM／SIGINT。

- 輪詢間隔 `--interval` 限 1..60 秒；併發 `--concurrency` 限 1..4（每個 job 一條連線）。
- 優雅停止：不再認領新 job；執行中的 job 有 `--grace`（預設 60）秒完成，仍在跑的留給 lease 到期（會被重跑並耗掉一次 attempt）。
- 退避：每個非終態結果（transient 重排、無法認領、任何 `abandoned`）都讓該 job 在記憶體內退避：`--backoff` 秒起、每次加倍、上限 5 分鐘，並有 ±50% jitter 避免多個 worker 同步重試；每輪之間至少相隔 0.25 秒，`--once` 另有 `--once-timeout`，所以失敗的認領不可能空轉。重啟會遺失退避（沒有 not-before 欄位），長時間沒再出現的 job 的記錄會被清掉。
- 寬限期真的會執行：`--grace` 秒後取消仍在跑的 parser（殺掉子程序）、job 留給 lease 到期、worker 結束；第二次 SIGTERM／SIGINT 跳過寬限。極端情況下執行緒不返回時以 `os._exit(1)` 結束。
- 記憶體：每個 parser 子程序的位址空間上限是 4 GiB（虛擬位址，實測最小需求在 1 到 2 GiB 之間，見 #105），併發 4 時最多 16 GiB 虛擬位址；實體記憶體需求遠小於此，但部署時要依機器設定 `--concurrency`。
- 公平性：認領順序是 `ORDER BY id`，不保證專案之間公平，一個專案的大量 job 可以排在其他專案前面。
- `QueryCanceled`（statement／lock timeout）是確定性錯誤：persist 內遇到就是 `failed(persist_failed)`，沒有有界重試。
- 資料庫連不上時只記固定代碼並在下一輪重試，不會崩潰。
- 尚未涵蓋：頁數上限、部署（systemd／容器）、多 worker 間的公平性、health endpoint、以及使用者取消 API（目前沒有 HTTP 端點可以送出取消）。

## 批次解析（PM-DECISION B，Refs #7）

預設的解析函式是 `lab_parser.batch_parse.parse_in_batches`：同一個 job 內依 `plan_batches`（預設每批 10 頁）把文件切成批次，只解析各批次的**核心頁**（overlap 脈絡頁是給後續 LLM 階段用的，所以頁面不會被解析兩次、evidence 不會重複），最多 2 個批次子程序同時執行；失敗的頁面依 `derive_retry_plan`／`execute_and_merge_retry` 逐頁重試，最多 `retry_rounds`（預設 2、上限 5）輪；結果合併成一個 `NormalizedBatch`（原始 0-based 實體頁碼、各頁 status／diagnostics），再由既有的 persist 存入，沒有新表。

- **失敗政策**：重試預算用完仍是 `failed` 的頁面 → `PartialParseFailure` → job `failed(pages_failed)`，**不發布任何部分結果**（好的頁面也不存）。`needs_review`（沒有可認定的幾何／空白頁）是誠實的不確定，不算失敗。
- **崩潰＝整個 job 重跑**：批次進度不持久；持久化、可續跑的批次（方案 A）要等 500／600 頁負載測試證明需要才做。
- **取消**：取消旗標傳給每個批次子程序（各自獨立的 process group），全部被殺後以 `cancelled` 結束。
- **記憶體**：每個子程序位址空間上限 4 GiB；一個 job 最多 2 個批次子程序，所以 `--concurrency N` 最多 N×2 個子程序（N=2 時最多 16 GiB 虛擬位址）。部署時請以此設定 `--concurrency`。
- 頁面子集由子程序內以 pypdf 建立子文件（保留旋轉與 CropBox），parser 頁碼與原始頁碼的對照寫在 manifest 的 `page_index_map`；合併後的 manifest 以實體頁碼 +1 為頁碼。
- 尚未涵蓋：批次層級的 `MaxRetries` 以外的退避、跨 job 的批次共享、overlap 脈絡頁的實際使用。

批次解析的成本界線與紀錄（#114 審查後補充）：
- **全部批次都以同一個暫時性代碼失敗**（`timeout`、`process_unavailable`、`inspection_io_failed`）→ 重新丟出該 `InspectionError`，消費者的處理與未分批時完全相同（重排至次數用完）；`unsupported_platform`、`invalid_input` 等系統性代碼立刻重新丟出，不逐頁重試。
- **第一輪失敗頁面超過 50%** 視為系統性問題：直接 `PartialParseFailure`，不做逐頁重試（避免 N 頁 × 重試輪數個子程序）。
- **job 層級期限**（預設 1800 秒）：超過後不再啟動新的子程序並以 `timeout`（暫時性）結束；每個子程序另有自己的 120 秒上限。2000 頁文件在最壞情況下仍是 200 個批次，期限就是它的上界。
- **合併後的 raw 與 manifest**：合併的 manifest 把 parser 頁碼定為實體頁碼 +1，但它引用的 raw 輸出保留各自子文件內的 `page_number`；每次執行自己的 manifest（有真實的 `page_index_map`）也保存在 artifacts 中。因此要重新 normalize 已存的 raw 輸出必須用該次執行的 manifest，不能用合併的那份。evidence id 取決於批次計畫與重試歷史（不同批次大小或被重試的頁面會有不同的 raw artifact，因此 id 不同），只在相同計畫與結果下穩定。

## 哪些頁面有幾何（Unstructured fast，Refs #7）

在 CI 以真實 unstructured 0.27.36 實測（e2e 的 `bbox_measurement` 與 `bbox_validation_against_pdfium`）：pdfminer 會把 `/Rotate` 套進它的 layout（layout 尺寸是旋轉後的 MediaBox），但**忽略 `/CropBox`**，座標相對於 MediaBox。因此：

- **有幾何**：`/Rotate` 為 0 或 180 的頁面，不論有沒有 CropBox。`lab_parser.frame_transform.display_transform` 由該頁的 MediaBox、CropBox 與旋轉算出旋轉後可見框的原點，座標減去原點再除以顯示尺寸；`DisplayFrame` 以 `layout_width/height` 與 `origin_x/y` 表示（原始 raw 輸出不改）。runner 另外確認算出的顯示尺寸等於 L0 量到的尺寸，否則該頁不給 frame。
- **PDFium 驗證**：5 個案例（一般、CropBox 偏移、較小 CropBox、180、180＋CropBox）的 evidence 聯集與 PDFium 實際渲染的墨跡框，左右差距約 1 pt、上下約 1–2 pt（容許 3／6 pt，因為 pdfminer 的行框含字型的上下緣）。
- **沒有幾何（保留文字）**：`/Rotate` 為 90 或 270。pdfminer 對側向文字逐字母拆開並打亂（量測：`"e","ilrahc","ovarbahpA"`），座標雖然正確，但框住的是亂序碎片，所以不認定。
- 限制：只測了單頁、一種字型與合成文字；多欄、表格、頁面內容超出 CropBox、混合旋轉的實際文件沒有量測。
