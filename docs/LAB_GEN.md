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

## Slice 3：general 桶出題、`gold_answers`（migration 0015）與輪詢 worker（Refs #7、#10）

建立在 coding agent 的 groundwork commit `28510e7`（`generate_prompts.py`）之上。

- **prompt**（`generate_prompts.py`，版本 `PROMPT_VERSION`）：每個呼叫一頁；只送那一頁**已提升的乾淨 evidence 文字**（每頁字元上限），
  以 `<evidence page="N">` 圍起來當 DATA；system prompt 固定在程式碼裡，明說資料裡的任何指令都要忽略。不送檔名、evidence／文件／專案／actor id、
  診斷、其他頁、key。模型只回 `{question, reference_answer, facts:[{statement, weight?}]}`（weight 省略時為 1，給了就必須 0 < w ≤ 10）；
  其他格式一律丟棄並計數，不修補。模型 pin 由 enqueue 時給（預定 `claude-haiku-4-5-20251001`），每次輸出 ≤ 500 token（slice 1/2 已強制）。
- **出幾題**：只做 general 桶；目標 = 素材規劃的 general 最低數 `n_target − 3·q_min`，依頁序輪流選乾淨文字頁（`select_pages`），一頁可被多次選到。
- **由程式推導**（`generate_general.py`）：Question（bucket general、tags `llm_generated` 與 `prompt:<版本>`、語言粗判、modality text、
  reasoning `value_lookup`、hash = 折疊空白與大小寫後的 sha256）；SemanticAnswer（calculation null、無 partial credit）；
  SourceAnswer（一個 evidence set = 那頁全部已提升的文字單元，無 distractor）；TextualAnswer 只在 fact 原文（折疊空白／大小寫）出現在該頁某單元時
  才有 `quote` check，否則為空（不捏造）。每題都經契約 + `validate_bundle`，不合格丟棄並計數；重複（hash 相同，含題庫已有題）丟棄並計數。
- **一個交易**：questions、facts、evidence_sets(+members)、`gold_answers`（semantic／textual／source 各一列）與 `llm_calls` 結算列都在
  `jobs.finish` 的 fenced hook 裡寫入；lease 遺失 → 只保留已付費的 `llm_calls` 列，題目與 gold 一列都不寫。寫入時 `lab.actor_id` 設為排程者
  （owner／maintainer），以通過 gold RLS。
- **benchmark 狀態**：`generating → validating`（不凍結）；轉換理由記錄寫入題數、呼叫數、invalid／duplicate／超量丟棄數，以及題數 < `q_min`
  時的 **shortfall**（不補題）；人工審查一律 `skipped`。
- **migration 0015 `gold_answers`**：PK (question_id, kind)、kind ∈ semantic／textual／source、payload = 驗證過的契約 jsonb（≤ 256 KiB）、
  composite FK 到同專案的 benchmark 與同 benchmark 的 question；append-only；凍結的 benchmark 拒絕 insert（`benchmark_child_guard`）；
  照 0008 開 RLS（forced）：只有 owner／maintainer／reviewer 讀得到、只有 owner／maintainer 能寫；participant／observer 看不到任何列。
- **worker**：`python -m lab_api.generate_worker_main [--once]`，沿用 parse worker 的迴圈（lease、heartbeat、停止、backoff、shutdown release），
  kind `document.generate`。設定只從環境變數讀：`LAB_GEN_CMD`、`LAB_GEN_PROTOCOL`、`LAB_GEN_BASE_URL`、`LAB_GEN_ALLOWED_HOST`、
  兩個價格（缺一個就 exit 2，不假設）；`LAB_GEN_API_KEY` 只交給子程序環境。

**限制（誠實列出）**：沒有人工審查；題目品質沒有量測；模型可能寫出不看該頁也答得出來的題（一般常識），目前偵測不到；source answer 是「整頁」
而不是證明必要的最小集合；部分呼叫失敗（`partial_calls`）時整批結果不採用，benchmark 停在 `generating`；`llm_calls` 沒有 prompt 版本欄位，
版本記在每題的 tag 與轉換理由裡；cross_page／chart_table／confusable 三個桶尚未出題；沒有 API 端點觸發生成；CI 從不呼叫真實模型。
