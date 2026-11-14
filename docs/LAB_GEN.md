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
  `general-page-v4`，寫進稽核事件），證據用 `<evidence>` 框起來並明說裡面的指令一律忽略。
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
- **失敗的 job 仍保留已付費且合格的題目**：partial 批次（job 是 `failed(partial_calls)`）裡合格的回覆已經寫入並維持，job 的失敗狀態只代表「不是每個呼叫都成功」，不會回收已寫的題目；shortfall 照實記在稽核事件。
- **來源只列模型實際看到的單位**：gold 的 SourceAnswer 只含 prompt 裡真的送出的證據單位（頁面文字在 6000 字元處截斷，之後的單位不列）；單位若不在資料庫就跳過並計入 invalid，不會丟例外讓整個 finish 回滾。
- **生成後轉 `validating`**：至少寫入一題時 benchmark 由 `generating` 轉 `validating`（轉換原因帶題數、目標與 shortfall）；從不 frozen，人工審核維持 skipped。
- **已知限制**：不靠頁面、靠常識就能回答的題目目前偵測不到（獨立驗題切片處理）；人工審核維持 `skipped`。

## 啟動與觀察生成的 API（Refs #7）

- `POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/generate`，body `{"model", "budget_cap_micros", "max_output_tokens"?, "currency"?, "pricing_revision"?}`：
  只有 owner／maintainer；benchmark 必須在 `planning` 且已釘住 parse；`max_output_tokens` 預設且最高 500；預算必須為正；同一 benchmark 同時只有一個
  active job（409）；未知欄位或型別錯誤 400；不是成員 404、其他角色 403。回 202 與 job id。API 本身不呼叫模型，呼叫由 `generate_worker_main` 做。
- `GET .../generate`：owner／maintainer／reviewer 可讀（`read_gold_and_generation_context`）。回 job 狀態、帳本（各狀態呼叫數、spent、held、cap、remaining）、
  題數對一般題目標與 shortfall、最後一次生成的稽核計數、`human_review: skipped` 與限制說明。**不回任何題目文字、答案或 gold**。

## Slice 4：自動驗題（migration 0016，Refs #7）

- **誠實標籤**：驗證者與生成者是**同一個 provider、同一個模型**（獨立的呼叫、獨立的 prompt），所以不是強意義的「獨立」；
  `needs_outside_context` 是驗證呼叫的自我回報，**不是**「沒有這頁也能答」的證明。這句話存在每一列 `llm_checks.independence`、
  `validator_revision`（`code-grounding-v1+llm-same-model-validate-v1`）和這份文件裡。人工審核仍是 `skipped`。
- **流程**：`POST .../validate`（同樣的 body 與權限；benchmark 必須在 `validating`；不改狀態；一個 benchmark 同時只有一個 active job，不分
  generate／validate）→ 同一個 worker 依 `generate_jobs.phase` 分派 → 每題一次呼叫，走同一套 lab-gen、帳本、預先預留與 500 token 上限。
- **每題兩層檢查**：(1) 免費的程式檢查，對象是**生成時實際送出的文字**：參考答案裡出現的每個數字都必須在文字裡、每個 fact 的內容詞至少 60% 出現在文字裡
  （NFKC、case-fold，CJK 逐字）；(2) 驗證呼叫回 `{supported, answerable_from_evidence, needs_outside_context}`（固定 system prompt；
  evidence／question／reference_answer 都框起來並中和標籤，當資料不當指令）。
- **判定**：兩層都過才 `pass`；程式檢查失敗，或驗證者任何一項明確否定 → `fail`（驗證者救不回程式檢查失敗）；回覆不是預期 JSON → `needs_review`。
  未知永遠不是 pass。**不刪除、不改寫、不修補任何題目**，`fail` 的題目照樣留在 benchmark 裡可見；benchmark 維持 `validating`，這裡不會讓它變成
  `ready_to_freeze` 或 `frozen`。
- **資料表**：`question_validations`（append-only、凍結後不可寫、與 gold 同一套 RLS：participant／observer 看不到）、`generate_jobs.phase`。
  稽核事件只有計數（pass／fail／needs_review）與 `human_review=skipped same_model_validator=true`。
- **預算語意**：`POST .../generate`／`.../validate` 的 `budget_cap_micros` 是**這一次執行**的預算；儲存的每個 benchmark 上限 = 該 benchmark 已花費與已 hold 的金額 + 這個數字（回應的 `benchmark_cap_micros`），所以驗證不會因為先前生成已花錢而被拒。
- **重新驗證**：已有 `pass` 或 `fail` 的題目不會再驗證；只有 `needs_review` 的題目（或還沒驗證的）會被新的驗證 job 撿起。
- **RLS**：worker 在讀 `gold_answers`（建呼叫）與寫結果時，都以 job 的請求者（owner／maintainer）身分設定 `lab.actor_id`；`GET` 進度也以呼叫者身分讀 `question_validations`。測試包含用非 superuser 的應用程式角色跑完整的生成加驗證流程（移除這個設定會以 `no_calls` 失敗）。
- **API**：`GET .../generate` 多回 `validation: {pass, fail, needs_review, unvalidated, note}` 與 job 的 `phase`；仍然不回任何題目文字或答案。
- **這個切片沒有**：cross_page／chart_table／confusable 的生成與驗證、非同一模型的驗證者、人工審核、ready_to_freeze。

## 凍結（freeze）：只有 owner、誠實的 partial（migration 0017，Refs #10；PM-DECISION 項目 2）

- **API**：`POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/freeze`，body 必須是 `{"compliance": "partial"}`（要明說這是 partial，不是完整放行）。
  只有專案 **owner** 能凍結（maintainer、reviewer、participant 403，非成員 404）。benchmark 必須在 `validating`，且**每一題**最新的自動驗證都是 `pass`。
  有 `fail`、`needs_review` 或尚未驗證的題目 → **擋下整個凍結**（409，附各 verdict 的題數）。選擇「擋下」而不是「把壞題排除後凍結」，因為凍結後的集合不可變、
  子資料不會被刪除，排除會需要另一套標記並牽動所有讀取方；owner 要先處理（再驗證，或開新 revision）。
- **compliance 一律 `partial`**：人工審核被 owner 的範圍設為 skipped（D6），而且 quota 桶不完整（chart_table 與 confusable 沒有生成、一般／cross_page 也可能不足 N 與 Q）；
  `compliant` 被拒絕。回應列出對 N 與 Q 的缺額（`shortfall`）、各 bucket 題數、驗證統計，以及三條限制（partial、驗證者與生成者同一模型、沒有 ablation 證明必要性）。
- **資料庫是權威**：migration 0017 重寫 `benchmark_revisions_guard`，任何寫入者（不只這個 API）要把狀態轉成 frozen 都必須：actor 是 project owner、compliance = partial、
  至少一題、每題最新驗證為 pass，否則 `check_violation`／`insufficient_privilege`。函式是 SECURITY DEFINER（才讀得到 gold 保護的 `question_validations`）。
  從草稿 PR #79 併入：新 benchmark revision 只能以 draft／row_version 1／無 frozen_at／無 compliance 插入；測試用的舊式凍結只能由 superuser 工作階段與 superuser 角色
  透過 `lab.test_allow_legacy_freeze` 繞過，應用程式角色不行（用 `current_setting('role')` 辨識 SET ROLE）。
- **沒有做**：凍結時的 manifest 與四部分 hash、`compliant` 路徑、人工審核、對 fail 題的排除機制。

## 在凍結的 benchmark 上跑一個 baseline 並評分（Refs #10、#12；PM-DECISION 項目 3）

- `POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/baseline-run`（owner／maintainer；benchmark 必須是 `frozen`）：對每一題執行既有的本機詞彙檢索 baseline
  （`lab_parser.local_methods`，只看原始 PDF 與題目文字，**看不到系統解析結果或 gold**；整頁區域；不呼叫模型），用既有的表格記錄 run、attempt、被接受的 submission
  （回應 JSON 存成 artifact）與 retrieved_hits，再以規則式評分器（`lab_scoring`，SPEC 16.5）建立一個 `provisional` 的 evaluation 與 `question_scores`。
- 分數：`recall_at_k`、`complete_at_k`（單位在回傳區域涵蓋其每個 gold 區域至少 4/5 時算命中）、`localization_precision`（回傳整頁區域，所以數字本來就低，這是方法的真實性質）；
  `textual_consistency` 不適用（生成題沒有 textual checks）；`correctness`、`citation_precision`、`citation_completeness`、`faithfulness` 需要語意 judge，**尚未執行**，
  記為 `pending`（原因 `semantic_judge_not_run`），不猜、不當 0。
- 讀結果：既有的 `GET /api/v1/runs/{run_id}/results?evaluation_id=…`；本端點只回指標與計數，不回分數或 gold。API 映像因此多帶 `lab_scoring`。
- demo 的第 8、9 步串起：凍結（partial）→ baseline 評分 → 印出每個指標的 scored／pending／mean。
## cross_page 題目（跨兩頁，Refs #7；PM-DECISION 項目 1）

- **何時生成**：同一個生成 job 的同一批呼叫裡，在一般題之後加上 Q 題（Q = ceil(0.10·N)，和 plan 的 cross_page 下限相同）的 cross_page 呼叫；
  乾淨且有文字的頁面少於兩頁時**不產生任何配對、不呼叫**（缺額如實記錄，不湊數）。頁面配對是兩個不同的頁，最近的頁優先，輪流使用。沒有新的 migration。
- **prompt**（版本 `cross-page-v4`，固定在程式裡）：兩個頁面各自框成資料區塊 A 與 B（不含 evidence id 等識別碼），**每頁各自套用字元上限**（6000），
  標籤中和同一般題；模型回 ≤ 500 token 的 JSON：題目、參考答案、2–3 個 fact，每個 fact 標明 `source`（A 或 B），兩邊都至少要有一個。
- **程式導出與檢查**：source answer 的證據組 = A、B 兩頁**實際送出**的單位；每個 fact 的支持組只有它被標到的那一頁的單位。因此契約的檢查是真的在檢查：
  證據組必須跨 ≥ 2 頁、且不能有任何單一頁就支持所有 fact（否則 `cross_page_single_page` 而被丟棄）。模型「這個 fact 在哪一頁」的宣稱還會用程式對照該頁送出的文字
  （內容詞至少 60%、數字必須出現）；對不上就丟棄並計入 invalid。reasoning_type 為 `cross_page_integration`。
- **驗證階段**：驗題 prompt 對跨頁題每頁一個 `<evidence page=…>` 區塊、每頁各自的字元上限；程式的接地檢查對象是兩頁實際送出的文字合併。
- **限制**：「這題真的需要兩頁」目前由契約的結構檢查加上 fact 對頁面的接地檢查把關，**沒有**做移除某頁後是否仍可回答的實測（necessity ablation 屬於之後的工作）。
- API：`GET .../generate` 多回 `cross_page_target` 與 `by_bucket`，`shortfall` 為兩個 bucket 缺額的總和；稽核事件與轉換原因帶 `cross_total/cross_target`。

## confusable 題目（有干擾項，Refs #7；PM 的下一項）

- **何時生成**：同一批呼叫裡，在 general 與 cross_page 之後再加 Q 題 confusable；需要兩個不同的乾淨文字頁（頁 A 回答、頁 B 是看起來相似的干擾來源，兩頁輪流當 A）。
  少於兩頁就沒有配對、不呼叫，缺額如實記錄。沒有新的 migration（`distractors` 表早已存在，與 facts 同一套 gold RLS）。
- **prompt**（`confusable-v4`，固定在程式裡）：A、B 各自框成資料並各自套字元上限、不送任何識別碼；模型回 ≤ 500 token 的 JSON：題目（必須只靠 A 就能答）、參考答案、1–3 個 fact、
  `confusion_reason`（B 裡什麼看起來像答案但不是）、`disambiguating_condition`（區分 A 與 B 的細節）。
- **程式導出與檢查**：gold 證據組 = 頁 A 實際送出的單位；頁 B 實際送出的每個單位都成為 distractor（附同一組 reason 與 condition），所以契約的檢查成立：
  `confusable_without_distractor`（至少一個干擾項）、`distractor_is_gold`（干擾項不能是 gold）。程式再檢查三件事，不過就丟棄並計入 invalid：
  fact 與答案必須能在頁 A 的送出文字裡接地；`disambiguating_condition` 必須能在頁 A 文字或題目裡接地；頁 B **不可**同樣支持這些 fact 與答案
  （否則那是第二個來源，不是干擾項）。reasoning_type 為 `value_lookup`；`conditions` 帶 disambiguating_condition。
- **限制**：「這個干擾項真的會讓人答錯」目前只有上述結構與接地檢查，沒有用模型或人去實測；chart_table 仍然不生成（D5）。

## 後續修正（#160／#163 的 P3，Refs #7、#12）

- **confusable**：B 只要**任何一個** fact 也被支持就拒絕（不再要求全部）；`confusion_reason` 至少要和頁 B 的送出文字共用一個內容詞（這是**弱**檢查：reason 對 B 是否為真並沒有被證明）；
  只有和題目或 fact 有內容詞重疊的 B 單位才成為 distractor，不相關的不算；沒有任何重疊就丟棄該題。
- **接地檢查的斷詞**：CJK 以**相鄰兩字（bigram）**為單位（單字太寬鬆：幾百個常用字就涵蓋大部分文字，幾乎任兩段中文都會「重疊」；整段連續漢字當一個詞則部分重疊永遠對不上）；單獨一個漢字就是它自己；其他文字仍是 3 個字元以上的詞。
- **baseline**：整個評分是一個同步交易，所以題數上限 250（超過回 409，需要改成 job 才能跑更大的 benchmark）；method revision 編號在 (project, method) 的 advisory lock 下計算，
  兩個同時的 run 不會撞號（unique 衝突也會轉成 409）；缺文件或 artifact 列時回 404，不再是 TypeError。

## 第二個方法：`local-retrieval-answer-1`（參與者端、會回答、有引用；Refs #12；PM 核准的設計）

- **啟動**：`POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs`（owner／maintainer；benchmark 必須 `frozen`；body 同生成：`model`、`budget_cap_micros`
  [這一次 run 的預算]、`max_output_tokens` ≤ 500）→ 202，建立 method revision、run（queued）與一個 `answer` job（`generate_jobs.phase = 'answer'` 加 `run_id`，migration 0018，
  CHECK：phase 是 answer 若且唯若有 run_id）。同一個 benchmark 同時只允許一個進行中的 method run（409）。API 不呼叫模型，由 generate worker 執行。
- **參與者端的隔離**：方法只拿到**原始 PDF**（用文件版本的 sha256 重新驗證）、題目的 key 與文字，以及它**自己**用 pypdf 解析後的詞彙檢索結果（top-5 頁，整頁區域）。
  不讀解析結果、證據、`gold_answers`、facts、驗證。三層保護：(1) 參與者階段是獨立模組，測試錄下它執行的每一句 SQL 並斷言沒有任何 gold／解析表；(2) 這個階段在 `lab.actor_id`
  **未設定**的交易裡執行，所以 gold 的 RLS（0008／0015）即使程式退化也會拒絕（對應用程式角色有測試）；(3) 評分在作答記錄寫完**之後**才設定 actor 並讀 gold。
- **作答**：每題一次 claude-haiku-4-5 呼叫（經 lab-gen：≤ 500 輸出 token、預算先預留、key 只進子程序環境）。prompt（`answer-v1`，固定在程式裡）：題目與檢索到的頁面都框成資料並中和標籤；
  回覆必須是嚴格 JSON：`answered`（非空答案、至少一個 claim、每個 claim 至少一個引用）或 `abstained`（附理由）。引用只能指向**真的被檢索到**的 hit；程式再建出經契約驗證的 `QueryResponse`。
- **記錄與評分**：每題都有 attempt 與 submission（回應 JSON 存成 artifact）、retrieved_hits、citations。檢索指標（recall／complete／localization）照常從真實 hits 評分；
  correctness、citation_precision、citation_completeness、faithfulness 在語意 judge 之前一律 `pending`。evaluation 是 `provisional`。
- **失敗與棄答分開**（PM 的 Q3）：模型自己的棄答 → `answer_status = 'abstained'`＋模型的理由；預算用盡、provider 錯誤、回覆或引用不合契約、結果未知 →
  `submissions.failure_reason` 的固定代碼（`budget_exhausted`／`provider_error`／`invalid_model_reply`／`outcome_unknown`），且保留真實 hits；這些題的答案指標記為 `error`（reason＝該代碼），
  **不是** pending、也不是 0、也不是棄答。`GET /api/v1/runs/{id}/results` 的 `submissions` 分別數：`answered`、`abstained_by_model`、`method_failures{代碼: 數量}`。
  run 狀態：全部有效作答 `completed`，否則 `completed_with_errors`；整批都不能送出（預算）時 job 以 `budget_exhausted` 失敗，但每一題仍然有記錄、run 仍然結束。
- **限制**：不用 pgvector（這把 key 只有聊天模型，沒有 embedding 模型）；答案指標需要語意 judge（尚未做）；沒有任何人用真實模型跑過這個方法，測試只用假的 lab-gen。

## 語意 judge（對一個已結束的作答 run 評分；Refs #12；PM 核准的設計）

- **啟動**：`POST /api/v1/runs/{run_id}/judge-runs`（owner／maintainer；body 同 method-runs）→ 202，建立一個 `judge` job（`generate_jobs.phase = 'judge'` 加 `run_id`，migration 0020）。
  run 必須是**作答方法**的 run（有 `answer` job）、狀態 `completed`／`completed_with_errors`、benchmark `frozen`，至少有一題實際作答；同一個 benchmark 同時只有一個進行中的 method／judge job（409）。
- **輸入**：每個「實際作答」的 submission 一次呼叫（≤ 500 輸出 token、預算先預留、fail-closed）。judge 只看到：該題 gold facts（編號 f1…）、submission 的答案文字與 claims、claims 所引用的 hit 文字（每個 hit 截斷）。
  **看不到題目文字、其他 gold、其他題、參與者身分**。資料全部框成標籤並中和，judge 對其中的指令不予理會。claims 超過 12 個的答案不送出（記為 `judge_input_too_large`，不花錢）。
- **輸出與計分**：judge 只回嚴格 JSON（每個 fact：`stated`／`missing`／`contradicted`；每個 claim：`supported` 與支持它的 hit ids）。**所有分數在程式裡由這份判定推導**：
  correctness（fact 權重取自 gold，stated＝1、其餘＝0，不用部分分數）、faithfulness、citation_precision（每個 (claim, hit) 引用是否支持）、citation_completeness（有被支持、且至少一個引用支持它的 claim 比例）。
  fact 或 claim 的 id 多了、少了、重複，或支持的 hit 不是該 claim 引用的 → 該題四個指標都是 `error`（`judge_fact_mismatch`／`judge_claim_mismatch`），不是猜的值。
  無法解析 → `invalid_judge_reply`；預算用盡 → `budget_exhausted`；provider 錯誤 → `provider_error`；結果未知 → `outcome_unknown`。
- **不呼叫 judge 的題目**（不花錢）：模型棄答 → correctness 照既有棄答規則（0，reason `abstained`），其餘三個 `not_applicable`；方法失敗 → 沿用該失敗代碼的 `error`。
- **紀錄**：judge 寫的是一個**新的 evaluation**（`provisional`，grader `rule-based-scorer-1+llm-judge-1`），舊 evaluation 的列完全不動；規則式指標（recall、complete、localization、textual）從上一個 evaluation 原樣複製。
  由 judge 回覆產生的列都帶 `judge_model`、`judge_prompt_revision`（`judge-v1`）、`judge_prompt_sha256`（固定 prompt 的 sha256）與 `judge_limitation`（同一個 provider／模型家族出題、未用人工標註校準、provisional）——
  四個欄位要嘛全有要嘛全空（CHECK），所以沒有限制說明就不可能存下 judge 的分數。再評一次就是再一個新 evaluation。
- **回覆的解析**（PM 後續項目）：judge 回覆的「外殼」和出題回覆一樣寬容（JSON 前後有說明文字、任何位置的 code fence、外面多包一層陣列、無關的多餘頂層欄位都可以），但**內容仍然嚴格**：每個 fact／claim 的鍵與值都必須正確，不修、不猜。
  解析失敗的題目，四個指標都是 `error`，`reason_codes` 是 `["invalid_judge_reply", "<為什麼>"]`（`empty`／`reply_too_long`／`not_json`／`bad_keys`／`bad_facts_shape`／`bad_claims_shape`），不含任何模型文字；job 的失敗計數也帶同樣的代碼。
- **「相反」不等於「沒提到」**：fact 被判 `contradicted`（答案說了相反或不同的值）時，規則仍照 `missing` 計 0 分（規則只有 0／0.5／1），但那一題 correctness 那一列的 `reason_codes` 會是 `["contradicted_fact"]`，
  而且 `GET /runs/{id}/results` 多一個 `judge` 摘要：`judged_questions`、`questions_with_a_contradicted_fact`、`model`、`prompt_revision`、`limitation`（沒有被評過分時是 `null`）。
- **demo 與網頁**：`scripts/demo.sh generate` 的 step 11 對 step 10 的 run 做 judge（沒有 key 或 step 10 沒跑成就 SKIPPED，不會 PASS）；網頁步驟 9 有「用評分模型評分」按鈕（只有模型回答方法的 run 能評；會花錢）。
- **仍然成立**：同模型、未校準，所以分數是 provisional；人工審查仍是 skipped；evaluation 不會被 published；release verdict 不變。

## 第一次真模型 run 找到的問題（#174，已修）

- **generate worker 的連線必須是 autocommit**（`generate_worker_main.connect_factory`）：否則「領取 job + 預留預算」留在同一個沒結束的交易裡，鎖住 job 那一列，
  另一條連線（heartbeat）永遠等它，job 一直是 `queued`。測試現在用 `main()` 實際使用的 factory 對真 PostgreSQL 跑，並有一個「舊 factory 會失敗」的對照。
- **API key 的換行**：`scripts/demo.sh` 讀 `./.env` 時去掉 Windows 的 CR 與前後空白；含空白、控制字元或非 ASCII 的 key 被拒絕（`BLOCKED`，不顯示 key）。
  `load_gen_config` 與 `lab-gen` 也各自拒絕這種 key（`lab-gen` 回 `refused`，不送任何呼叫、不留預留）。
- **沒送出的請求不是「結果未知」**：HTTP client 在送出前就拒絕的請求（例如 header 值不合法）是 `failed`，預留釋放、不計費；同一批後面的呼叫標為 `skipped_after_stop`。
- **`scripts/demo.sh up` 每次都重建 migrate image**，並且在跑 demo 之前確認資料庫的 migration 版本等於 repo 的最新版（不一致就 BLOCKED，不再讓 API 回 503）。

## 出題回覆的解析與落選原因（第一次真實 run 之後；Refs #12、Issue #7）

第一次真實 run：18 次呼叫有 5 個回覆解析失敗、易混淆題 0 題，而且出現「第 5 頁寫什麼」這種題目。之後的規則：

- **容忍無害的偏差，內容仍嚴格檢查**：JSON 前後有說明文字、任何位置的 code fence、外面多包一層單元素陣列、多餘的欄位、fact 寫成純字串、重複的 fact（去重）都可以收；
  但是空欄位、超長（題目 500、答案 600、fact 400 字）、fact 數量不在 1–3（跨頁題 2–3 且 A、B 兩邊都要有）、source 不是 A／B、缺必要欄位，一律丟掉，**不猜、不修**。
- **每個被丟掉的回覆都有固定代碼**，數量記在 `benchmark.questions_generated` 稽核事件的 `reasons={…}`（也在 `GET …/generate` 的 `last_generation`），不含任何模型文字：
  `<桶>:parse:<empty|reply_too_long|not_json|bad_keys|bad_types|empty_field|field_too_long|bad_fact_shape|fact_too_long|bad_fact_source|bad_facts_count>`、
  `<桶>:no_shown_units`、`<桶>:check:<page_reference|answer_or_fact_not_grounded|condition_not_grounded|b_supports_a_fact|reason_not_in_b|no_overlapping_distractor_unit|fact_not_in_its_page|derive_error|contract_invalid|unit_not_stored>`、`duplicate_question`。
  下一次真實 run 就能看出易混淆題為什麼是 0。
- **題目不能提到頁碼**：`page 5`、`p. 5`、`第5頁`、`第五頁` 這類（問的是版面、不是內容，而且頁碼可能是印出來的而不是 PDF 的頁序）→ `check:page_reference`，題目丟掉。
  三個 prompt 也加了這條規則，版本升到 `general-page-v4`／`cross-page-v4`／`confusable-v4`。

## 凍結前排除題目（migration 0019；Refs #10、Issue #7 的 PM 決定）

真實輸出裡 10 題有 3 題沒過驗題，而「每一題都要過才能凍結」的規則下，除了重新出題沒有別的辦法。現在 **owner** 可以在凍結前**排除**題目：

- `GET  /api/v1/benchmarks/{id}/revisions/{rev}/questions/review`（owner／maintainer）：每一題（含已排除的）、最新的自動驗題結論與理由、是否被排除、`can_freeze`。
- `POST /api/v1/benchmarks/{id}/revisions/{rev}/questions/{question_id}/exclusion`，body `{"excluded": true|false, "reason": "…"}`（只有 owner，且 benchmark 要在 `validating`）。
- 排除是**一筆有理由的紀錄，不是刪除**：`question_exclusions` 只能新增（最新一筆為準，`restore` 就是再新增一筆），也寫入 audit。資料庫自己也檢查：只有 owner 寫得進去、凍結後寫不進去。
- 被排除的題目**不凍結、不作答、不計分**：所有讀「benchmark 的題目」的地方都改讀 view `active_questions`。凍結的回應列出 `excluded_questions`，`GET /runs/{id}/results` 也有 `excluded_questions`，進度（`GET …/generate`）的 `questions`／`validation` 是「未被排除」的那些，另有 `excluded` 數量。
- 凍結的條件變成：**每一題「未被排除」的題目的最新驗題都是 pass，而且至少剩一題**（資料庫的 freeze gate 同步改了）。其他規則不變（owner、compliance 只能是 partial）。

## 完整真實 run 之後的小修（Refs #12、Issue #7）

- **baseline 不是「模型棄權」**：詞彙 baseline 只回傳頁面、沒有模型。`GET /runs/{id}/results` 的 `submissions` 現在有 `no_answer_retrieval_only`（沒有 `answer` job 的 run 的無答案提交）；
  `abstained_by_model` 只數真的有模型的方法。網頁寫成「無答案（這個方法只做檢索、不產生答案）N 筆」。網頁的「方法失敗」也改成加總失敗的**提交數**（原本數的是失敗代碼的種類數）。
- **單位一致，只是沒標**：所有指標的值與平均都是 0–100（資料表 `raw_value` 就是 `100 × 分子／分母`，平均是它的平均）。`localization_precision` 的 0.82 是 0.82%（整頁區域天生偏低），不是 0–1 的尺度。
  結果回應多了 `scale` 說明，網頁表頭寫「0–100」，並在該指標說明「0.8 代表 0.8%，不是 80%」。
- **demo 的選用排除步驟**：`DEMO_EXCLUDE_FAILED=1 scripts/demo.sh generate` 在凍結前把驗題沒通過的題目排除（記錄理由、不刪除，輸出會逐題列出並說明這是選用的），凍結的輸出也列出被排除的題目。沒設定就和以前一樣：有題目沒過就停在凍結。
- **出題回覆解析失敗的原因診斷**：每個解析不了的回覆，稽核事件多一個 `parse_shapes={…}`：只記**形狀**，不記內容（第一個與最後一個字元的類別 `{ } [ ] ` letter cjk digit other none`、長度區間、有沒有 code fence、大括號是否成對；
  括號不成對通常是被 500 token 上限截斷）。三個 prompt 升到 v3，多一條「回覆必須以 { 開頭、以 } 結尾，不要前言、不要 code fence，每個欄位寫短一點」。
  **還沒做 assistant prefill（預填 `{`）**：它要改 lab-gen 的請求格式（最後一則 assistant 訊息）而且不是每個模型都支援，先看形狀資料再決定。

## 第三次真實 run 的發現（Refs #12、Issue #7）

- **模型可以用 JSON 拒絕出題**：三個出題 prompt（升到 v4）多一條：如果這一頁沒有適合出題的內容（只有標題、目錄、頁首頁尾、沒有意義的數字、字太少），不要用文字解釋，回 `{"skip": "<一句理由>"}`。
  這是一個**決定**，不是解析失敗：計入 `<桶>:declined`，不增加 `unparseable`。稽核事件多 `declined=N` 與 `declined_where=[{bucket, pages, shown_chars}]`（只有桶、頁序、模型看到的字元數區間，**不存理由文字**），
  可以看出被拒絕的是不是內容很少的頁面。有題目欄位的回覆優先於 skip；理由必須是 1–300 字的字串。純文字的拒絕（沒有 JSON）仍然算解析失敗，形狀記在 `parse_shapes`。
- **judge 對不上時說明是哪一種**：`judge_fact_mismatch` 加上 `fact_missing`／`fact_extra`／`fact_duplicate`（可以用 `+` 並列），`judge_claim_mismatch` 加上 `claim_missing`／`claim_extra`／`claim_duplicate`／`supporting_hit_not_cited`。
  兩個代碼都在那一題四個指標的 `reason_codes` 裡（`["judge_claim_mismatch", "claim_missing"]`），job 的失敗計數也用 `代碼:子代碼`；只有固定詞彙，沒有 id、沒有模型文字。
- **易混淆題一直是 0 的原因（用較豐富的文件驗證）**：測試用兩頁內容不同的文字（Gold 方案年費 120 EUR 五月、Silver 方案年費 60 EUR 六月），一個符合實際的回覆**通過了所有檢查**，寫出易混淆題，干擾項是另一頁；
  兩頁重複同一個事實時則一定被擋（`b_supports_a_fact`：B 頁也支持這個答案，它就不是「容易混淆但答案不同」）。所以合成文件裡 6 頁幾乎一樣的文字本來就出不了易混淆題；檢查本身沒有改。
  真實 run 裡另外出現的 `condition_not_grounded`（區分條件不在 A 頁或題目裡）要等有內容豐富的真實文件才看得出是不是太嚴。
