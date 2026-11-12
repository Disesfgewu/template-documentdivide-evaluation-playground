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
- **預算（fail-closed）**：benchmark 剩餘 = 上限 − 已花費 − held（所有該 benchmark 的 `llm_calls`）。剩餘 ≤ 0 時 job 以
  `budget_exhausted` 失敗，**不啟動 lab-gen**。傳給 lab-gen 的 `budget.limit_micros` 就是剩餘額度。
- **`enqueue_generate_job`**（lab_db）：自己授權（`build_edit_freeze`，owner／maintainer）；benchmark 必須在 `planning` 且已釘住 parse；
  token 上限 > 500 或預算 ≤ 0 在寫入前就拒絕；每個 benchmark 同時只有一個 active job；同一交易把 benchmark 轉到 `generating`。
- **`process_generate_job`**（lab_api）：fenced lease + heartbeat（取消會殺掉子程式）→ 讀 job → 預算檢查 → 以受限子程序執行 lab-gen
  （環境只有 PATH 與 `LAB_GEN_API_KEY`）→ 嚴格驗證輸出（id 對應、request hash 重算比對、response hash、token ≤ 上限、費用與用量一致、
  總額不超過預算）→ 同一交易寫 `llm_calls` 與 job 終態。
- **記帳規則**：全部 `ok` → `succeeded`；有任何失敗 → `failed(partial_calls)` 但**所有已付費的列都保留**，且不把部分結果交給生成邏輯；
  子程序崩潰、逾時、輸出無效或被取消 → 整批每個呼叫記成 `outcome_unknown` 並 hold 最壞情況（可能已送出，絕不當作免費）；
  lab-gen 自己 `refused`（沒有任何呼叫）→ 不寫列、job `failed(lab_gen_refused)`。
- **`on_results` hook**：成功批次的回應文字在同一交易裡交給 slice 3 的題目寫入與驗證（本切片沒有）。
- **本切片沒有**：輪詢 worker 迴圈（slice 3 連同真正的 prompt 建構一起接）、題目生成與契約驗證、獨立驗題、demo 步驟、API 端點。
