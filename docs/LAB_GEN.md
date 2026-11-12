# lab-gen：題目生成的 LLM 呼叫階段（P-D slice 1，Refs #7）

PM-DECISION（Issue #7，2026-11-12）核准 G-lite：Go 子程式 `services/worker/agentflow/cmd/lab-gen` 用 agentKit 的
`model`、`graph`、`agentlog` 加我們自己的 agentflow（`ChatCall`、`NodeRunner`、`Ledger`、`Price`、`ValidateModelConfig`）
組出本專案自己的 graph；Python 負責資料、job 與驗證。本切片只有 Go 這一半，沒有 migration、沒有 Python。

## 介面

stdin 一份 JSON（`labgen.Input`），stdout 一份 JSON（`labgen.Output`），其他什麼都不印。key 只從環境變數
`LAB_GEN_API_KEY` 讀（由 agentKit 讀取），不在參數、stdout、stderr 或輸出裡。`LAB_GEN_AUDIT_DIR` 設了就寫 agentKit 的
hash chain 稽核檔；稽核檔含請求內容，只留在本機，不公開。輸入有未知欄位或不是合法 JSON：exit 2、stdout 空。

## 在任何網路呼叫之前就擋下的規則（`labgen.Validate`）

- `max_output_tokens` 必須 1..500（擁有者決定：每次呼叫最多 500 個輸出 token），超過就整份輸入被拒，一次呼叫都不會發出。
- 只接受 https、host 必須等於 `allowed_host` 且是公開 DNS 名稱（不是 IP、單一標籤、內部尾碼）；沒有 userinfo、query、fragment。
  loopback 只有測試用的 `Options.AllowLoopback` 能開，JSON 輸入無法開啟。不跟隨任何 redirect。
- 價格、幣別、價格版本都必須明確給（不假設）；`max_retries` 1..2、timeout、每分鐘請求數必須設（`ValidateModelConfig`）。
- 預算是 fail-closed：每次呼叫先用最壞情況（prompt 位元組數當輸入 token、輸出用滿上限）預留；放不進剩餘預算就不發出，
  之後的呼叫也全部跳過（`skipped_budget`、`skipped_after_stop`）。

## 結果與記帳

每個呼叫一個結果：`ok`、`failed`（provider 狀態碼）、`skipped_budget`、`skipped_after_stop`、`outcome_unknown`。
usage 已知才依設定價格結算；usage 未知時 `charged_micros = -1`，預留保留為 unknown，**不當成 0 元**。
輸出附 request 的 sha256、回應文字長度的 sha256 與帳本總計，不含 key 與 prompt。回應文字原樣交給呼叫端驗證。

## 這個切片沒有做的事

沒有 Python 呼叫端、沒有 `llm_calls` 表（migration 0014）、沒有題目生成與驗證、沒有 demo 步驟。CI 只用本機假 server 測，從不呼叫真實模型；
真實模型只在擁有者手動執行的 demo 步驟（slice 5）才會用到。測試涵蓋：上限、端點規則、費用結算與 key 不外洩、預算預留與 fail-closed、
未知 usage、provider 錯誤、redirect 不外送 key、輸入規則、JSON 無法開 loopback。

## Slice 2：`document.generate` job、`llm_calls` 帳本與預算（Refs #7）

- **migration 0014**：`generate_jobs`（一個 job 綁一個 benchmark、model pin、每次呼叫上限 ≤ 500 token、預算上限、幣別與價格版本）和
  `llm_calls`（每次呼叫一列：model、狀態、token、費用 micro、held、嘗試次數、request／response 的 sha256 與長度）。兩者 append-only；
  資料庫以 CHECK 保證不變式：已知用量才有已知費用；未知費用必須 `charged = -1` 且 `held > 0`（最壞情況）；沒跑的呼叫不計費。
  表內**沒有** key、prompt、回應文字欄位。
- **預算（先預留再啟動，fail-closed）**：啟動 lab-gen **之前**，同一個交易（每個 benchmark 一把 advisory lock）依序為每個呼叫 append 一列
  `reserved`（held = 最壞情況）；放不進剩餘預算的第一個呼叫與其後全部寫 `skipped_budget`，永遠不送出。lab-gen 回來後再 append 每個呼叫的
  結算列（ok／failed／outcome_unknown…）。帳本 append-only：一個呼叫的「目前狀態」是其 (job, attempt, call) 最新（seq 最大）的一列；
  剩餘 = 上限 − 已花費 − held（只算目前狀態）。worker 在啟動後被 SIGKILL，`reserved` 的 hold 仍在，重試（新 attempt）看到的是已減少的預算；
  兩個 worker 同時預留不會花同一份額度。代價：崩潰後那批的最壞情況額度不會自動釋放（保守，需人工對帳）。`budget.limit_micros` 傳給 lab-gen
  的是這次預留的總額。
- **每個 attempt 各自的列**：unique 是 (job, attempt, call) 且分「預留」「結算」兩種，重試是新 attempt，所以前一次 `skipped_budget`/`failed` 的列
  不會讓重試實際花錢的列被吞掉；只有「同一個 attempt 的重放」才是 no-op。
- **`enqueue_generate_job`**（lab_db）：自己授權（`build_edit_freeze`，owner／maintainer）；benchmark 必須在 `planning` 且已釘住 parse；
  token 上限 > 500 或預算 ≤ 0 在寫入前就拒絕；每個 benchmark 同時只有一個 active job；同一交易把 benchmark 轉到 `generating`。
- **`process_generate_job`**（lab_api）：fenced lease + heartbeat（取消會殺掉子程式）→ 讀 job → 預算檢查 → 以受限子程序執行 lab-gen
  （環境只有 PATH 與 `LAB_GEN_API_KEY`）→ 嚴格驗證輸出（id 對應、request hash 重算比對、response hash、token ≤ 上限、費用與用量一致、
  總額不超過預算）→ 同一交易寫 `llm_calls` 與 job 終態。
- **記帳規則**：全部 `ok` → `succeeded`；有任何失敗 → `failed(partial_calls)` 但**所有已付費的列都保留**，且不把部分結果交給生成邏輯；
  子程序崩潰、逾時、輸出無效或被取消 → 已預留的每個呼叫結算成 `outcome_unknown`，hold 維持預留額（可能已送出，絕不當作免費）；
  lab-gen 自己 `refused`（沒有任何呼叫）→ 釋放 hold（`skipped_after_stop`，held 0）、job `failed(lab_gen_refused)`。
  子程序在自己的 process group 執行，逾時、取消或輸出超過上限時整個 group 被殺；stdout 分塊讀取，不會整份進記憶體。
- **`on_results` hook**：成功批次的回應文字在同一交易裡交給 slice 3 的題目寫入與驗證（本切片沒有）。
- **本切片沒有**：輪詢 worker 迴圈（slice 3 連同真正的 prompt 建構一起接）、題目生成與契約驗證、獨立驗題、demo 步驟、API 端點。

## Slice 3：一般題生成（general bucket）、`gold_answers`（migration 0015）與輪詢 worker（Refs #7）

- **`build_calls`**（`lab_api/generate_questions.py`）：只讀 benchmark **釘住的 parse**（P-A）裡通過 G8 硬規則的頁、只取其文字證據單位；每題一個呼叫，
  頁面輪流（round-robin），同一頁重複時用程式產生的「第 k 個敘述」提示避免重複。目標數 = N − 3Q（其餘三個 bucket 各至少 Q）。
  **不送**檔名、document／project／actor id、diagnostics、其他頁、其他題的 gold、key。system prompt 固定在程式裡（版本
  `general-page-v1`，寫進稽核事件），證據用 `<evidence>` 框起來並明說裡面的指令一律忽略。
- **模型只寫三樣東西**（≤ 500 token）：題目、參考答案、1..3 個 fact。其餘契約欄位全由程式導出：language（粗略 zh／en）、tags `[]`、
  modality `text`、reasoning_type `value_lookup`、SourceAnswer（該頁被送出的文字單位，不含干擾項）、TextualAnswer `checks: []`、
  calculation `null`、每個 fact 平均權重、固定的評分敘述（無部分給分）。
- **驗證**：回覆必須恰好是預期 JSON（不是就丟，不修補）；每題以現有契約與 `validate_bundle` 驗證，不合格者丟棄並計數；`question_hash`
  （NFKC、折疊空白、case-fold 後的 sha256）重複者丟棄。合格者在同一交易寫入 `questions`、`facts`、`evidence_sets`／`members`、`gold_answers`
  （semantic、textual、source），並寫一筆只有計數的稽核事件（replies／written／unparseable／invalid／duplicate／total／target／shortfall）。
- **不足額就如實記錄**：寫不滿目標時 shortfall 記在稽核事件，benchmark 留在 `generating`，絕不湊數。只生成 general；cross_page、chart_table、
  confusable 在這個 parse 沒有足夠證據前不猜（D5）。partial 批次中已付費且合格的回覆仍會寫入（job 狀態仍是 `failed(partial_calls)`）。
- **migration 0015 `gold_answers`**：(question, kind) 唯一、append-only、凍結後不可寫（`benchmark_child_guard`）、payload ≤ 256 KiB、
  與 facts 同一套 RLS（owner／maintainer／reviewer 可讀，只有 owner／maintainer 可寫；participant／observer 看不到）。
- **輪詢 worker**：`python -m lab_api.generate_worker_main`，與 parse worker 同一個迴圈（lease、backoff、停止與關機釋放），只是 job 種類是
  `document.generate`。缺 `LAB_GEN_BIN`、`LAB_GEN_API_KEY`、`LAB_GEN_BASE_URL`、`LAB_GEN_ALLOWED_HOST`、兩個價格任何一項就印出
  `{"status":"blocked"}` 並 exit 3，絕不當作通過。
- **已知限制**：不靠頁面、靠常識就能回答的題目目前偵測不到（獨立驗題切片處理）；人工審核維持 `skipped`。
