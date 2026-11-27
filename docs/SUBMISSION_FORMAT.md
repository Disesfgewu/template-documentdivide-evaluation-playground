# 開發人員的答案檔（`lab-submission-1`）

評測流程（Issue #189）：owner 凍結題庫後，下載**只有題目**的檔案交給方法的開發人員；開發人員對**同一份 PDF**回答每一題，並附上他引用的原文段落；owner 把答案檔上傳，系統核對引用、評分。

## 給開發人員的題目檔

`GET /api/v1/benchmarks/{id}/revisions/{n}/questions/export?format=jsonl|json`（owner／maintainer，題庫必須已凍結）。標頭有題庫與文件資訊（`document_sha256`、`document_page_count`），
每一題只有 `question_id`、`ordinal`、`question_text`、`language`。**沒有**標準答案、要點、證據、題型、驗題結果、干擾段落：這個查詢只讀題目本身（`active_questions` 的四個欄位），有測試錄下每一句 SQL 並斷言沒有碰任何 gold 表。
被 owner 排除的題目不在檔案裡。

## 答案檔（JSONL，UTF-8，一行一題）

```jsonl
{"method_name": "my-rag", "method_version": "0.3", "notes": "bm25 + reranker"}
{"question_id": "q-8f3a1c", "status": "answered", "answer": "The annual fee is 120 EUR, due in May.", "sources": [{"page": 3, "text": "The annual fee of the Gold plan is 120 EUR and is due in May."}]}
{"question_id": "q-b02d77", "status": "abstained", "reason": "the document does not say"}
```

- 第一行的標頭是選用的（`method_name`、`method_version`、`notes`，值都要是文字）。
- `status`：`answered`（需要非空的 `answer`，可以附 `sources`）或 `abstained`（需要 `reason`，不可以有 `answer` 或 `sources`）。
- `sources[].page`：**原始 PDF 的頁碼，從 1 開始**；`sources[].text`：從那一頁複製的原文段落。
- 上限：`answer` 4000 字、每題最多 10 筆 `sources`、每段 `text` 2000 字（至少 6 個非空白字元，太短的引文什麼都對得上）。
- 範例檔：[`answers-file.example.jsonl`](answers-file.example.jsonl)；JSON schema：[`answers-file.schema.json`](answers-file.schema.json)（也由 `GET /api/v1/submission-format` 提供，網頁可下載）。

## 上傳與評分

`POST /api/v1/benchmarks/{id}/revisions/{n}/submissions`（owner／maintainer，題庫已凍結），body：`{"method_name", "file_content" (檔案文字), "method_version"?, "notes"?, "accept_partial"?}`。

1. **嚴格檢查**：每個有問題的行都用**行號＋固定代碼**列出（`invalid_json`、`unknown_question`、`duplicate_question`、`page_out_of_range`、`source_text_too_short`…，不含檔案內容）。有問題而 `accept_partial` 不是 true → **422，什麼都不匯入**，回應的 `error.details.report` 是檢查報告；
   owner 可以修正後重傳，或帶 `accept_partial: true` 只匯入沒有問題的行（重複的題目只採用第一行）。沒有任何有效的行 → 422。
2. **沒回答的題目記成 `not_submitted`**（`submissions.failure_reason`，答案類指標是 `error`，不是 pending、不是 0、也不猜），題目檔裡有、答案檔裡沒有的題目都是。
3. **核對引用**：每一段 `sources` 變成一個 hit。系統在它自己對這份 PDF 的解析裡，找那一頁上有沒有這段文字（NFKC、不分大小寫、忽略空白）：
   找到 → region 是找到的單位（`exact_in_unit`）或跨單位的範圍（`exact_in_page`）；
   **找不到 → region 是整頁，並記錄 `text_found = false`、`match_quality = not_found`、該頁有多少比例的字詞出現過（`quote_coverage`）**；那一頁系統沒有解析出文字則是 `page_without_text`。
   捏造或抄錯的引文會被數出來、逐題顯示，不會被默默接受。
4. **評分**：規則式指標（recall／complete／localization）和內建方法同一個評分器；`correctness`、`citation_*`、`faithfulness` 在評分模型跑之前是 `pending`。
   `POST /api/v1/runs/{run_id}/judge-runs` 對上傳的 run 一樣可用（同一家模型、沒校準、暫定）。**評分模型只看得到「在引用的頁面上真的找得到」的來源，而且看到的是系統自己解析出的那段原文（`submission_source_matches.matched_text`），不是開發人員寫的字串**：
   - 找不到的引文（含該頁系統沒有文字）完全不給評分模型看；它們不替答案加分，但**仍算在 citation precision 的分母裡**（那筆引用是「不支持」）；
   - 一個沒有任何可看來源的主張（引文全找不到，或根本沒附來源）**依定義就是不被支持**（faithfulness 0、citation precision 0），不論模型怎麼說；答案本身仍然會對照標準答案評 `correctness`；
   - 混合的情況只保留找得到的來源；
   - `GET /runs/{id}/results` 的 `source_checks`：`total`／`found`／`not_found`／`page_without_text`／`excluded_from_judge`，以及 `page_only_retrieval_credit`——找不到的引文的 region 是整頁，所以它們的檢索分數只是「整頁命中」，不是找到了那段文字。網頁在分數旁邊與比較表都顯示「N 段已從評分排除」。
5. **紀錄**：每次上傳是**新的 run**（方法的 `execution_mode` 是 `offline_file`，`visibility` 是 `declared_only`），重傳不會蓋掉舊的；檔案存成 artifact，記錄 sha256、上傳者、方法名稱／版本／備註與檢查報告（`external_submissions`，只能新增）。

## 看結果

`GET /api/v1/benchmarks/{id}/revisions/{n}/submissions` 列出上傳過的方法與狀態；`GET /api/v1/runs/{run_id}/results` 是分數；`GET /api/v1/runs/{run_id}/submission-details`（owner／maintainer，含 gold）是逐題：
他的答案、他的引用（找到或找不到）、分數、標準答案與要點。網頁步驟 9 把每個方法並排比較（每個指標各自一列，不加總、不排名）。

### 覆核分數（owner／maintainer，含 gold）

`POST /api/v1/runs/{run_id}/reviews` 記錄逐題的「答案正確性」覆核值：`{"reviews": [{"question_id": "q-…", "value": 0–100, "reviewer_kind": "human" | "model_assisted", "reviewer_label": "…", "note": "…(選填，500 字內)"}]}`，一次最多 100 題；`question_id` 就是 submission-details 裡的 `question_id`。
`GET` 同一個路徑回每題最新的覆核值（較早的版本留著，`versions` 是次數）和摘要。覆核值只新增、不覆蓋：評分模型的分數一個都不會被改；同一題最新一筆為準；`model_assisted` 永遠標成模型協助、不會顯示成人工。
網頁步驟 9 另外多一列「答案正確性（覆核後）」：覆核過的題用覆核值，其餘用評分模型的值，並寫出覆核了幾題、人工或模型協助。開發人員那一側的端點（results、comparisons）不會回覆核值。
