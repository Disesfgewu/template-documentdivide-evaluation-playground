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

## 使用者取消 API（Refs #7）

`POST /api/v1/jobs/{job_id}/cancel`（`lab_api/jobs.py`、`lab_db.jobs.request_cancel`）。需要角色動作 `import_and_configure_parse`（owner、maintainer）；授權在寫入交易內、鎖住 job 列之後再做一次。

| 結果 | 狀況 |
|---|---|
| 200 `{job_id, status, changed}` | `queued`／`paused_budget`／`waiting_review` → `cancelled`（`changed:true`）；`running` 且 lease 有效 → `cancel_requested`（worker 的 heartbeat 最多 2 秒後看到並停止子程序、以 `acknowledge_cancel` 轉 `cancelled`）；`running` 但 lease 已過期（worker 已死）→ 同一交易內直接 `cancelled` |
| 200 `changed:false` | 已經 `cancelled`，或 `cancel_requested` 且還在寬限內（`CANCEL_ACK_GRACE_SECONDS`＝30 秒）：什麼都不寫。超過寬限仍是 `cancel_requested` 時，重複請求會把它轉成 `cancelled`（卡住的 job 的出路） |
| 401／404／403 | 未認證；非成員或不存在的 job（兩者都是 404，不洩漏存在與否）；成員但角色不足 |
| 409 | job 已 `succeeded` 或 `failed`，不能取消 |

- **冪等性靠狀態，不靠 `Idempotency-Key`**：這個端點**忽略** `Idempotency-Key` 標頭；重送同一請求安全，因為第二次會得到 `changed:false`（或 409，若 job 在兩次之間已完成）。
- **與完成的競態**：取消與 worker 的 `finish()` 都先鎖住 job 列，所以只有兩種結果：worker 先完成 → 取消看到已完成的 job 得 409，結果照常提交；取消先 → lease 被丟棄，worker 晚到的 `finish` 得 `LeaseLost`、結果寫入回滾。兩種順序都有真實 PG 測試（`tests/api_db/test_job_cancel_real_db.py`），但測試只釘住這兩種順序，不證明所有交錯。
- 每個有效的變更連同其 operation 與 audit 事件（`job.cancel_requested`）在同一交易提交；`changed:false` 不寫任何列。

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
- 尚未涵蓋：頁數上限、多 worker 間的公平性、health endpoint（容器探針只代表存活，見容器部署一節）。使用者取消 API 見下一節。

## 批次解析（PM-DECISION B，Refs #7）

預設的解析函式是 `lab_parser.batch_parse.parse_in_batches`：同一個 job 內依 `plan_batches`（預設每批 10 頁）把文件切成批次，只解析各批次的**核心頁**（overlap 脈絡頁是給後續 LLM 階段用的，所以頁面不會被解析兩次、evidence 不會重複），最多 2 個批次子程序同時執行；失敗的頁面依 `derive_retry_plan`／`execute_and_merge_retry` 逐頁重試，最多 `retry_rounds`（預設 2、上限 5）輪；結果合併成一個 `NormalizedBatch`（原始 0-based 實體頁碼、各頁 status／diagnostics），再由既有的 persist 存入，沒有新表。

- **失敗政策**：重試預算用完仍是 `failed` 的頁面 → `PartialParseFailure` → job `failed(pages_failed)`，**不發布任何部分結果**（好的頁面也不存）。`needs_review`（沒有可認定的幾何／空白頁）是誠實的不確定，不算失敗。
- **無聲丟字防護（Refs #7）**：unstructured 0.27.36 第一次用到時會下載 spaCy 模型 `en_core_web_sm`；唯讀根或沒有網路時下載失敗，只留一行 debug log 並**回傳零個元素**，job 本來會成功、頁面全是 `needs_review`。判斷分兩層：子程序只**回報**「這次執行沒有任何元素、但 pypdf 在某頁找到至少 20 個非空白字元」的頁面，runner 把它們標成 `needs_review` 並加診斷 `text_present_but_no_element`（資訊，不是錯誤，會存進 `parse_page_results`）；**整份文件**層級由 `parse_in_batches` 決定：合併後沒有任何元素（observation）且有頁面帶此診斷，才以 `text_extraction_empty` 失敗（transient：job 重排、次數有界，最後 `failed(text_extraction_empty)`，不發布空的成功結果）。只要文件任何地方有元素，可疑頁面維持 `needs_review` 加診斷，不致命；失敗代碼與文件大小或批次切法無關。**限制**：整份文件只有一頁／一批的 pypdf 與 pdfminer 不一致頁（浮水印、Type3、CropBox 外文字）在整份文件都沒有元素時仍會被當成總遺失而失敗；不涵蓋部分遺失、向量外框字（pypdf 抽不出）、少於 20 字元的頁面。
- **看不見的文字（G8，PM-DECISION，Refs #7）**：真實 fast 會把 render mode 3（隱形）的文字當一般元素並產生 evidence（#130 實測）。隔離子程序現在對每一頁掃內容串流（含它呼叫的 Form XObject，深度上限 4、每頁運算子上限 200,000），追蹤文字渲染模式（屬於圖形狀態：跨 `BT`／`ET` 保留、由 `Q` 還原）；只要有文字顯示運算子（`Tj`、`TJ`、`'`、`"`）在模式 3 或 7（只裁切）下執行，**整頁**就隔離：`needs_review`＋診斷 `text_not_visible`，沒有 evidence 區域、沒有 observation（原始輸出 artifact 仍保有完整元素文字供稽核）。內容串流解析失敗、XObject 缺漏或過深、運算子超出預算、`/Contents` 格式不正常（自我參照或巢狀陣列、非串流的項目、懸空參照）、`Tr` 運算元不是 0..7 的整數時同樣 fail closed（診斷 `content_unparseable`）。這是頁面層級的，不嘗試把個別元素對應到隱形片段。**限制**：白底白字、被其他內容蓋住、頁面外或被裁掉的文字、以及不由 `Tr` 控制的隱形文字**抓不到**（要靠之後的 PDFium 墨跡交叉檢查，選項 (b)）；有隱形文字層的 OCR 掃描 PDF（常見：影像加 `Tr 3` 文字）整頁都會是 `needs_review` 而沒有 evidence，這是 fail closed 的代價；Type3 字型的 glyph procedure 與註解外觀串流不掃。
- **渲染墨跡交叉檢查（G8 (b)，PM-DECISION，Refs #7）**：隔離子程序用 PDFium 以 72 dpi 灰階把每頁渲染一次（套用 `/Rotate` 與 `/CropBox`，與 L0 顯示座標同一個框），縮成 128×128 的有無墨跡網格（儲存格內有任一像素亮度 < 245 即有墨跡，`ink.json` 約 4 KB／頁）；頁面超過 4,000,000 像素或渲染失敗 → 該頁 `content_unparseable`（整頁隔離，同 (a)）。父程序只在 evidence 區域的框（向外取整）底下**至少有一個有墨跡的儲存格**時保留該 evidence；否則 evidence **與它的 observation** 都丟掉，頁面診斷加 `region_without_ink`（`needs_review`）。能抓到白底白字、頁面外與被裁掉而渲染不出來的文字。**抓不到**：畫在其他墨跡之上或之下的隱形文字（區域聯集框裡同時有可見的筆跡、被影像蓋住的文字）、以及顏色與背景只差一點（亮度 ≥ 245）的文字；檢查單位是元素的聯集框，不是單字。成本：每頁約 0.02–0.15 秒 CPU（純 Python 縮網格，無新相依套件）。
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

## 已存結果的已知限制（#107 審查的 P3，Refs #7）

- **大小上限在記錄已在記憶體之後才檢查**：`persist_parse_result` 對整筆紀錄（canonical JSON）的 64 MiB 上限，是在 normalizer 已經把整個結果建在記憶體之後才計算與檢查；它阻止的是寫入資料庫與 artifact 儲存，不是記憶體使用。真正的記憶體界線來自 normalizer 的輸入上限（16 MiB raw、200,000 元素、20,000 artifacts）與每批 10 頁。超過上限的結果在 worker 端以 `failed(persist_failed)` 結束（確定性，不重排，有測試）。
- **沒有「合成／真實」標記**：資料庫與 API 不知道一份結果是來自合成文件或真實文件，只有 manifest 的 parser 版本與 profile；誠實標示要靠證據（CI run、fixture 名稱）而不是結果欄位。
- **被隔離（quarantine）的批次不會被記錄**：normalizer 回傳 quarantine 的批次，其頁面在合併時視為失敗並重試；重試預算用完則整個 job 失敗且不存任何結果，所以資料庫裡沒有「失敗批次」的紀錄，只有 job 的轉換原因 `pages_failed:<頁數>`。
- 摘要的 `effective_profile`／`strategy` 在 manifest 缺欄位時回 `null` 而不是 500（防禦用；寫入路徑一定帶有該欄位）。

## 容器部署（Refs #7）

`deploy/worker.Dockerfile`：與 API／parser 映像相同的 digest 固定 Python 3.12 slim 基底，非 root（UID/GID 10001），只複製執行期需要的原始碼（`db/lab_db`、`contracts/lab_contracts`、`services/parser/lab_parser`、`services/api/lab_api`；沒有測試、私有 agentKit、開發工具或機密），`ENTRYPOINT python -m lab_api.parse_worker_main`。依賴全部釘在 `deploy/worker.requirements.lock`（CI e2e venv 的 `pip freeze`，Python 3.12.16，移除 CUDA 套件，torch／torchvision 走 CPU wheel 索引；`unstructured==0.27.36`）。映像約 2.5 GB（量測值）。

- **設定只用環境變數**（`LAB_DATABASE_URL`、`LAB_ARTIFACT_ROOT`、`LAB_AUTH_SECRET`…），映像裡沒有任何機密（CI 檢查映像歷史與內建環境）。`deploy/compose.dev.yml` 的 `parse-worker`（profile `worker`）只用與 `db` 服務相同的拋棄式開發憑證；真實部署要自己注入環境。
- **執行限制**：唯讀根檔案系統、`/tmp` 為 tmpfs、`cap_drop: ALL`、`no-new-privileges`、記憶體 6 GB、pids 200、2 CPU（每個 parser 子程序量到約 1.3 GB RSS，見 #123；併發 1 時最多 2 個子程序）。
- **健康檢查**：worker 每一輪（至少每個 `--interval` 秒，長時間解析中也一樣）改寫 `LAB_WORKER_HEARTBEAT_FILE`（預設 `/tmp/worker.heartbeat`）；探針 `python -m lab_api.parse_worker_main --check-heartbeat PATH --max-age 120` 不連資料庫，檔案缺失、壞掉、過期或來自未來都回 1。
- **探針只代表存活（liveness）**：它只檢查 worker 迴圈還在轉，不檢查資料庫連得上、也不檢查解析能力。資料庫斷線時 worker 仍會每一輪更新心跳（它只記錄固定代碼並重試），所以容器仍然是 healthy；要偵測資料庫或解析失敗，要看 job 狀態（`/api/v1/jobs/{id}`）與 log，而不是這個探針。
- **停止寬限**：compose 預設的 10 秒小於 worker 的 `--grace 60`（外加最多 10 秒取消並收掉子程序），`compose stop/down` 會在 job 中途被 SIGKILL 而沒有收尾（lease 到期後 job 會被重跑）；因此 `parse-worker` 設 `stop_grace_period: 90s`，CI 的停止步驟改用 compose 預設值並檢查這個設定。注意：CI 只停了閒置的 worker，**沒有**在 job 進行中停止的測試。
- **重要發現（已修）**：unstructured 0.27.36 第一次用到時會把 spaCy 模型 `en_core_web_sm` 從網路下載並安裝進 site-packages；在唯讀根或沒有網路時下載失敗，它只記一行 debug log（"PDF text extraction failed, skip text extraction"）然後**回傳零個元素**，job 仍然 `succeeded`、頁面是 `needs_review`（`requested_page_missing_or_blank_unconfirmed`）。映像因此在鎖檔中以雜湊釘住該模型，執行期不下載。這也是先前一次無法解釋的 e2e 偶發失敗（run 42702878315，第 1 頁沒有文字）的**合理假設**（每個 CI venv 第一次使用都會下載；暫時性的下載失敗會得到同樣的症狀），但沒有直接證據。
- **未涵蓋**：映像只在 x86_64 的 GitHub runner 上建置與測試；沒有多 worker、沒有 `--concurrency` 大於 1 的容器測試、沒有 orchestrator（systemd／Kubernetes）設定；映像沒有掃描漏洞；鎖檔以 pip freeze 產生而不是 `--require-hashes`（只有 spaCy 模型有雜湊）；無法保證 `packages.unstructured.io`／PyPI 之外的其他執行期下載不存在（目前只觀察到這一個）。

CI：手動 `workflow_dispatch` 的 `worker_image` 輸入執行 `worker-image` job：建置映像、以 compose 啟動、檢查非 root／唯讀／capability／健康狀態／映像沒有機密，並用 `deploy/worker_smoke.py` 在**執行中的容器內**以合成單頁 PDF 走完整條路徑（建立專案、上傳、排隊、由容器自己的 worker 以真實 unstructured 解析、讀回結果），最後優雅停止。
