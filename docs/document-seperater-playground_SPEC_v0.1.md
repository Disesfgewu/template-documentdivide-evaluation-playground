# Document Seperater Playground — SPEC v0.1

日期 2026-11-04。狀態為供實作的設計規格草案。專案名稱沿用 document-seperater-playground。

2026-11-04 補充修訂。新增必要的 PostgreSQL、pgvector、可追溯 log 與 agentKit graph/client 整合要求。agentKit 採原始碼靜態審查，整合可行性成立，但須完成本文件列出的 adapters 與驗收。本次未執行 Go 測試或實際 provider／資料庫端到端測試。

## 1. 目標與責任

建立一個可上傳大型 PDF、生成一次並重複使用 benchmark、接入不同演算法或 Agent 的 LAB。主要衡量參賽方法能否精準切割與搜尋文件、整合跨文本資訊、正確回答並回溯原始證據。

系統端與參賽端必須各自保有獨立的文件處理結果。

| 角色 | 必須完成的工作 | 產出 |
| --- | --- | --- |
| 系統建題端 | 正確解析與切割原始 PDF，連結跨頁及跨文本資訊，生成與驗證題目 | 原始證據索引、四部分 benchmark、品質與配額報告 |
| 使用者方法或 Agent | 自行解析、切割、索引、搜尋、跨文本整合、答題及溯源 | 方法版本、搜尋證據、答案、逐項引用、實驗紀錄 |
| 系統評分端 | 對照凍結答案與原始證據，計算各項分數 | 逐題評分、題型分數、失敗原因、方法比較 |

系統建題方法不要求實作參賽答題流程。可以另外提供範例參賽 adapter 驗證接入，但它與建題器是獨立元件。

本版使用 MUST 表示驗收必要項目，SHOULD 表示建議項目，MAY 表示選配項目。此文件提出的技術選型、預設參數與門檻屬可調整的實作預設，正式實驗必須保存其設定版本。

2026-11-05 細化修訂。第 15–18 節增加逐項需求、禁止事項、契約細節、84 項測試與發布門檻。MUST NOT 表示禁止，違反即驗收失敗。前文未明確定義之處依細化條款實作；存在細節差異時，以第 15–18 節為準。本文定義待完成的要求與測試，不代表已執行系統測試。

2026-11-05 多模態修訂。第 19 節新增 Unstructured／MinerU 官方能力研究、解析 adapter、原圖證據分層、跨頁圖文驗證與 20 項測試；本版合計 30 組強制需求、104 項測試、48 項驗收條件。研究未包含套件執行或效能比較。

## 2. 範圍與預設

- MUST 接受包含文字、表格、圖片、圖表與掃描頁的 PDF，並有 500 頁以上文件的分批處理路徑。
- MUST 支援同一 PDF 的跨段落、跨頁、跨章節、遠距頁面與圖文交叉處理。
- MVP 以單一 PDF 建立 benchmark。資料模型從第一版保留多文件欄位，跨 PDF 聯合建題列為後續擴充。
- MUST 以原始 PDF 座標作為共同溯源基準。系統 chunk ID 與使用者 chunk ID 各自獨立。
- MUST 透過可替換的 LLM adapter 生成問題、答案和語意驗證。圖表流程需要視覺模型或經核對的結構化圖表資料。
- MUST 支援 HTTP 方法服務和外部執行後提交結果兩種接入方式。Python SDK 可包裝同一協定。
- MUST 將建題與實驗執行分開。建立 run 不得觸發重新建題。
- MUST 使用 PostgreSQL 保存可追溯業務資料、版本、血緣、工作狀態、稽核及可查詢 log，內建向量儲存使用 pgvector。
- MUST 以 agentKit graph 建立系統 workflow worker，使用其 client 介面接入適用的文字 LLM 與 embedding 功能，並補齊持久化、視覺輸入和 trace adapters。細節及整合前提見第 12 節。
- 同一次實驗可包含多個 method revision。每個 revision 各自產生 run，結果以同一 benchmark revision 對齊比較。

## 3. 端到端流程

1. 上傳 PDF，計算 SHA-256、頁數，保存原始檔與文件版本。
2. 系統分批解析、OCR、辨識版面與圖表，建立可回指原頁的 evidence units。
3. 系統切分建題用的 context，連結跨頁表格、章節引用、同一實體及相似干擾段落。
4. 依題數與配額規劃候選題，用 LLM 生成四部分資料。
5. 驗證引句、數值、來源、答案與題型有效性。低信心題進人工審核，失敗題重建。
6. 達標後凍結 benchmark revision。對外題目投影只包含問題與公開執行條件。
7. 參賽方法取得原始 PDF，自行 prepare。系統建題中間產物不隨參賽資料發送。
8. LAB 傳送凍結問題，收集方法的答案、搜尋結果與引用，或接收外部產生的相同格式結果。
9. 評分器執行來源比對、規則評分與必要的 LLM 語意評分，保存逐題明細。
10. 以相同 benchmark、評分版本、預算 profile 比較不同方法。

## 4. 原始證據與系統切割

### 4.1 原始位置格式

每個 SourceRegion MUST 保存以下欄位。

| 欄位 | 型別 | 定義 |
| --- | --- | --- |
| document_id | string | 文件邏輯 ID |
| document_sha256 | string | 原始 PDF 位元組雜湊 |
| page_index | integer | 實體頁面索引，從 0 開始 |
| bbox | number[4] | [x0,y0,x1,y1]，各值介於 0 與 1 |
| kind | enum | text、table、figure、image、formula、page |
| quote | string 或 null | 對應原文，圖像可為 null |

座標原點為頁面左上角，套用 PDF 頁面旋轉後，以可見 CropBox 正規化。必須滿足 x0 < x1、y0 < y1。印刷頁碼另存 page_label，僅供顯示。整頁引用使用 [0,0,1,1] 與 kind=page。

EvidenceUnit 包含 evidence_id、一個或多個 SourceRegion、原始抽取內容、正規化內容、抽取器版本、OCR 信心與必要的表格或圖像資產。原始內容與正規化內容需分別保存。

表格保留 cell、row header、column header、單位、續表關係及原頁座標。跨頁表格可形成一個邏輯物件，但每個來源區域仍回指原頁。圖表保留圖像 crop、圖例、軸標籤、單位、圖說與讀值依據。

### 4.2 建題 context 與證據關係

建題 context 可以組合不連續證據，不要求逐頁切割。關係類型至少支援 continues、defines、refers_to、same_entity、similar_but_different、table_continuation。

系統 SHOULD 先建立章節與頁面索引，再以關鍵字、實體、引用或 embedding 找候選關係，最後核對原始內容。摘要可用於路由，標準答案必須有原始證據支持。

系統 MUST 記錄無法解析、低信心與未覆蓋區域。局部失敗不可默默從文件覆蓋率中消失。重跑失敗頁時應復用成功頁結果。

## 5. Benchmark 規則

### 5.1 題數與分配

P 為 PDF 實體頁數，目標題數 N=max(20,P)，每項最低配額 Q=ceil(0.10×N)。500 頁文件目標為 500 題，每項至少 50 題。

題數按整份 PDF 決定，章節分配依可出題內容、重要性與多樣性調整。空白頁、封面等不強制各出一題。報告必須揭露頁面及章節覆蓋率。

| 配額類型 | 有效性條件 |
| --- | --- |
| cross_page | 至少兩個不同實體頁面上的必要證據必須一起使用 |
| chart_table | 必須讀取表格、圖表或具資訊內容的圖像。僅讀圖說即可回答者不計入 |
| confusable | 有已驗證的相似干擾證據，且問題中的條件足以區分正確來源 |

一題可以有多個 tags，但僅指定一個 quota_bucket。各配額以不同題目計數，此為第一版預設。跨頁題另報告頁距與跨章節比例，避免全部集中相鄰頁。

confusable 題必須保存 distractor evidence、干擾原因、正確區分條件，例如年份、版本、族群、測試設定或指標。題目本身仍須有可判定的答案。

若文件無足夠圖表、干擾項或獨立問題，狀態轉為 needs_review，顯示配額與題數缺口。預設不可凍結為合規 benchmark。擁有者可明確建立 partial benchmark，但其報表必須顯示例外，且與合規 benchmark 分組比較。

### 5.2 四部分資料

| 部分 | 必要欄位與規則 |
| --- | --- |
| questions | question_id、question_text、tags、quota_bucket、language |
| semantic_answers | reference_answer、required_facts、每項 fact 的權重及等價說法 |
| textual_answers | 原始引句、實體、數值、單位、運算規則或圖表估讀範圍。只適用的項目才填 |
| source_answers | evidence_sets、每項 fact 的支持證據、distractors、來源核對紀錄 |

四部分以 question_id 聯結，不依陣列位置聯結。題目文字不得包含內部 evidence_id 或預先透露的答案。

evidence_sets 採 OR-of-AND 結構。集合內的所有證據都必要，任一完整集合即可成立。例如 [[e18,e247],[e310]] 表示找到 e18 與 e247，或找到能獨立支持答案的 e310。接受替代來源需要驗證其對全部必要事實的支持能力。

任一有效替代集合若可於單頁獨立支持完整答案，該題不得計入 cross_page 配額。新增此類替代來源需在新 revision 重新分類並補足配額。

required_facts 表示可逐項評分的原子事實。文字答案保存忠於來源所需的檢查條件，允許語意等價表達。對推算題，另存輸入值、公式與答案單位。

### 5.3 建題與驗題

LLM 先接收候選證據及題型要求，回傳符合 schema 的題目和答案。每個候選題依序通過以下檢查。

1. Schema、來源文件版本、頁碼與 bbox 有效。
2. 原文引句可定位。OCR 或圖表讀值需核對原始頁面影像。
3. 答案中的必要事實有足夠原始證據。推算題可重算。
4. 跨頁題透過移除必要證據後再驗證，檢查多處證據是否確實必要。
5. 圖表題檢查答案是否需要讀圖或表格內部資料。
6. 易混淆題檢查干擾來源與問題限定條件，確認不會出現多個互相衝突的合理答案。
7. 題庫去重、章節覆蓋與配額檢查。

驗題輸出 verdict、理由、受檢證據、模型與提示版本。生成器與驗證器使用不同呼叫及提示，模型可相同或不同。LLM 驗題結果屬待校準的判定，低信心、衝突及圖表讀值疑義進人工審核。凍結前記錄人工抽查數量與結果。

### 5.4 凍結與修訂

revision 凍結後不可原地改題或答案。修正題目、增加有效替代來源或變更配額都建立新 revision，保留變更原因。

對既有輸出重新評分時，只有問題與來源文件保持一致者可復用回答。答案或來源修訂可產生新的 evaluation。問題文字改變者需要重新 query。

公開評測 profile 在完成前隱藏標準答案與逐題回饋。開發 profile 可在完成後顯示完整診斷。預設可使用全部題目做開發，正式排名需另設定固定的保留測試集。

## 6. LLM 與成本控制

LLM adapter MUST 支援 text/vision 能力宣告、結構化輸出、逾時、有限重試、token 用量與模型版本記錄。以能力設定選擇 provider，provider 名稱不寫死在建題流程。

| 設定 | 用途 |
| --- | --- |
| question_generator | 生成問題與標準答案 |
| question_verifier | 驗證可答性、證據必要性與題型 |
| vision_reader | 表格與圖表核對，可與前述模型共用 |
| answer_judge | 實驗語意評分 |
| embedding_provider | 系統尋找跨文本候選關係，選配 |

解析快取鍵包含 document hash、parser/OCR version 與設定。建題鍵包含來源集合 hash、生成設定、模型與提示版本、題型及題數。評分快取鍵包含 benchmark revision、question hash、submission hash、評分規則、judge 模型與提示版本。

同一建題鍵若已有凍結成果，直接回傳該 revision。若工作進行中，回傳原 job。候選題、驗題結果與頁面處理結果分別 checkpoint。

生成服務在每次外部呼叫前檢查 token/cost budget，超限轉 paused_budget，保留進度。費用未知的 provider 顯示用量與 unknown cost，不能記為零。

題庫復用省去重建題庫的模型呼叫。參賽方法的回答與新輸出的語意評分仍可能產生費用。規則評分、來源幾何匹配以及完全相同輸出的評分快取應避免額外 LLM 呼叫。

## 7. 方法與 Agent 接入協定

### 7.1 方法身份

MethodRevision MUST 記錄 method_id、revision、名稱、程式或容器版本識別、設定 hash、adapter protocol version、能力列表與執行模式。設定或實作改變應建立新 revision。

能力包含 text、table、image、multi_region、async_query。方法可以自行選擇向量庫、關鍵字搜尋、圖資料庫、混合檢索或 Agent 工具鏈。

### 7.2 HTTP 方法服務

| HTTP | 方法服務路徑 | 行為 |
| --- | --- | --- |
| GET | /v1/capabilities | 回傳協定版本及支援能力 |
| POST | /v1/prepare | 接收文件與方法設定，建立索引，回 202 與 job_id |
| GET | /v1/jobs/{job_id} | 查詢 prepare/query 進度，成功時回結果 |
| POST | /v1/query | 同步回 200 結果或非同步回 202 與 job_id |
| POST | /v1/jobs/{job_id}/cancel | 請求取消工作 |

prepare 請求包含 documents、method_revision、config。每份 document 包含 document_id、sha256、page_count 與限時可讀的 PDF URL。方法下載後驗證 hash。URL 到期時由 LAB 更新傳輸授權，保留相同 job 與文件身份。

prepare 成功回傳 index_id、index_fingerprint、document hashes、方法版本及處理統計。query 必須使用已準備好且文件版本吻合的 index_id。

以下 query 範例的題目與數字皆為協定示例。

```json
{
  "protocol_version": "1.0",
  "request_id": "req_001",
  "run_id": "run_001",
  "question_id": "q_001",
  "index_id": "idx_001",
  "question": "依正式測試的高負載定義，哪個方案的 p99 延遲最低？",
  "budget": {
    "top_k": 10,
    "max_evidence_tokens": 12000,
    "max_visual_regions": 8,
    "timeout_seconds": 180
  }
}
```

回傳結構如下。文件 hash 在實際傳輸中必須是 64 位十六進位值。

```json
{
  "protocol_version": "1.0",
  "request_id": "req_001",
  "question_id": "q_001",
  "index_id": "idx_001",
  "status": "answered",
  "answer": {
    "text": "高負載定義為負載至少 80%。方案 B 的 p99 延遲最低，為 12 ms。",
    "claims": [
      {"claim_id": "c1", "text": "高負載定義為負載至少 80%。"},
      {"claim_id": "c2", "text": "方案 B 的 p99 延遲最低，為 12 ms。"}
    ]
  },
  "retrieved_hits": [
    {
      "hit_id": "h1",
      "rank": 1,
      "content": "正式測試將負載至少 80% 定義為高負載。",
      "regions": [
        {"document_id": "doc_001", "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "page_index": 17, "bbox": [0.1, 0.2, 0.9, 0.3], "kind": "text", "quote": "正式測試將負載至少 80% 定義為高負載。"}
      ]
    },
    {
      "hit_id": "h2",
      "rank": 2,
      "content": "正式高負載測試 p99，A=18 ms，B=12 ms，C=21 ms。",
      "regions": [
        {"document_id": "doc_001", "document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "page_index": 246, "bbox": [0.1, 0.3, 0.9, 0.7], "kind": "table", "quote": null}
      ]
    }
  ],
  "citations": [
    {"claim_id": "c1", "hit_id": "h1", "region_indices": [0]},
    {"claim_id": "c2", "hit_id": "h2", "region_indices": [0]}
  ],
  "telemetry": {
    "latency_ms": 1350,
    "retrieval_calls": 2,
    "model_calls": 1,
    "input_tokens": 2000,
    "output_tokens": 120
  }
}
```

status 可為 answered 或 abstained。abstained 仍可回傳找到的證據與 reason。程式錯誤以 job failed 或 HTTP error 表示，與不知道答案分開記錄。

retrieved_hits 為該題採用並宣告的排序證據集合。citations 必須參照存在的 hit 與 region。Agent 多次搜尋後可合併去重並排序，可選擇附完整 retrieval_trace。未提供 trace 時，LAB 只能驗證已提交證據及答案，執行期間的內部檢索量屬方法自報。

claims 用於對齊引用。評分器亦須檢查完整 answer.text，對未列入 claims 的新增事實補做支持性檢查，避免只列少量容易評分的主張。

### 7.3 外部結果提交

本機或無法供 LAB 連入的 Agent 可下載 PDF 與 questions projection，自行處理後提交 JSONL。每行符合相同 query response schema。提交需綁定 run_id、benchmark revision、method revision 與文件 hash。

外部提交缺失的題目保留在評分分母中。相同 question_id、相同 payload hash 的重送為冪等，相同 ID 卻不同 payload 回 409。要替換結果需新建 attempt。完整答案集只供擁有者和評分器存取。

## 8. LAB 對外 API

所有端點位於 /api/v1。長時間操作回 202 與 job_id，建立一般資源回 201。列表支援 cursor pagination。

| HTTP | 路徑 | 用途 |
| --- | --- | --- |
| POST | /documents | multipart 上傳 PDF，啟動檢查與解析 |
| GET | /documents/{id} | 文件版本、頁數、解析狀態與失敗區域 |
| GET | /documents/{id}/pages/{page_index} | 頁面預覽與座標資料 |
| POST | /benchmarks | 指定 documents、題數策略、配額與生成設定 |
| GET | /benchmarks/{id} | 題庫 revision、建題進度及品質統計 |
| POST | /benchmarks/{id}/revisions | 建立修訂草稿 |
| PATCH | /benchmarks/{id}/revisions/{revision}/questions/{qid} | 在草稿修訂問題、答案或來源，觸發重新驗題 |
| POST | /benchmarks/{id}/revisions/{revision}/freeze | 驗證配額與題目後凍結 |
| GET | /benchmarks/{id}/revisions/{revision}/questions | 依權限輸出公開問題集 |
| GET | /benchmarks/{id}/revisions/{revision}/export | 擁有者匯出四部分資料與 manifest |
| POST | /methods | 註冊 HTTP service 或 submission 模式 |
| POST | /methods/{id}/revisions | 保存新的方法與設定版本 |
| POST | /runs | 指定凍結 benchmark、method revision、budget profile，啟動實驗 |
| POST | /runs/{id}/submissions | 批次提交逐題結果，回逐筆成功或錯誤 |
| POST | /runs/{id}/finalize | 外部提交結束，將缺失題標記後開始評分 |
| GET | /runs/{id} | 執行狀態、完成數、錯誤與預算資訊 |
| POST | /runs/{id}/evaluations | 指定 grader revision 重新評分既有輸出 |
| GET | /runs/{id}/results | 逐題與整體分數，支援題型篩選 |
| GET | /comparisons?run_ids=... | 檢查可比條件並產生比較 |
| GET | /jobs/{id} | 查詢工作進度與錯誤 |
| POST | /jobs/{id}/resume | 從 checkpoint 繼續可恢復工作 |
| POST | /jobs/{id}/cancel | 取消工作並保存既有成果 |
| GET | /resources/{id}/lineage | 查詢指定版本上游或下游，具 direction、depth、分頁及節點上限 |
| GET | /runs/{id}/traces | 列出實驗、重試及評分的 trace |
| GET | /traces/{id} | 呼叫樹、時間軸、跨 trace 連結與缺口 |
| GET | /logs | 依時間、等級、事件、trace、run、question、job、attempt 過濾 |
| GET | /audit-events | 依操作者、資源、動作與時間查詢 |
| POST | /trace-exports | 建立受權限控管的 JSONL/CSV 匯出工作 |

會產生外部呼叫或非同步工作之 POST MUST 支援 Idempotency-Key。相同 key 與相同 body 回原結果，相同 key 與不同 body 回 409。錯誤格式包含 code、message、retryable、request_id、details。

標準錯誤包含 invalid_pdf、unsupported_encryption、invalid_source_region、benchmark_not_frozen、quota_not_met、document_hash_mismatch、adapter_protocol_mismatch、budget_exceeded、timeout、provider_error。

## 9. 評分規格

### 9.1 共同規則

各主分數獨立呈現 0–100，第一版不預設加權總分。比較需一致的 benchmark revision、question subset、grader revision 與 budget profile。

方法回傳的來源先驗證 document hash、頁碼、座標，再與標準證據做對照。不同切割粒度使用共同原始位置比對。文字引句可協助定位，僅靠相同字串而位置錯誤不可計入正確溯源。

定義 gold region 的 coverage 為同頁有效回傳區域聯集與 gold 的相交面積除以 gold 面積。預設 coverage ≥ 0.80 為位置命中，門檻放在 grader revision。gold unit 有多個必要 region 時全部達標才算 unit 命中。

有可信字元或表格 cell 對齊時，使用該細粒度對齊計算 coverage，避免大 bbox 掩蓋缺失內容。圖表使用已核對的必要區域。幾何位置命中與該處是否支持回答分開記錄。

### 9.2 主指標

| 指標 | 定義 |
| --- | --- |
| Answer Correctness | 每題必要事實的加權正確比例，fact 評為 0、0.5、1，附理由 |
| Textual Consistency | 適用的實體、數字、單位、條件與原文檢查通過率 |
| Evidence Recall@k | 前 k 個有效結果對每組可接受證據計算命中比例，取最佳組 |
| Complete Evidence@k | 前 k 個結果是否完整覆蓋至少一組有效 evidence set |
| Evidence Localization Precision | 回傳來源區域聯集有多少落在已驗證的有效 gold 區域上，以每頁正規化面積計算 |
| Citation Precision | 被引用的主張與來源對中，有多少來源真正支持對應主張 |
| Citation Completeness | 最終回答中需證據支持的原子主張，有多少有至少一組完整且有效的引用支持 |
| Answer Faithfulness | 最終答案所有可查證主張中，受其提交證據支持的比例 |

Answer Correctness 對事實錯誤與遺漏給不同原因碼，另外記錄額外錯誤主張數。若額外內容直接否定必要事實，該 fact 計 0。faithfulness 與 correctness 分開保留，錯誤來源即使忠實轉述仍會失去正確性及正確來源分數。

Textual Consistency 中，不適用的 literal/數值項標示 N/A，報告適用題數。數值容差與單位換算在建題時固定，圖表估讀範圍不可看過方法答案後才調整。

Citation Precision 無引用時對可回答題計 0。Citation Completeness 與 Faithfulness 在無實質回答時計 0，並標示 abstained，避免空答案取得滿分。

Localization Precision 表示引用區域的緊密程度。當圖表與表格面積和文字內容量不一致時，需按模態另報，不把面積比例描述為語意相關度。

額外診斷包含 distractor_hit_rate@k、首次有效證據 rank、跨頁完整證據率、各頁距區間分數、建索引時間、query p50/p95、錯誤率、成本及可觀測用量。

### 9.3 預算與無效結果

預設 profile 為 top_k=10、max_evidence_tokens=12000、max_visual_regions=8、每題逾時 180 秒。這些數值是初始實驗設定，可在建立 profile 時調整。

token 由 LAB 固定版本 tokenizer 計數。rank 必須連續且唯一。依 rank 納入結果，直到第一個會超出任何證據預算的 hit 為止，後續不計入預算內來源分數，保留 budget_violation 記錄。

文字與表格抽取、圖像區域與內容對應需要核對。圖像區域可由 LAB 根據 PDF bbox 重建；超大區域同時受到定位精準度影響。不可透過回傳極短摘要卻宣稱整份文件已讀取，取得可比的受控上下文標記。

黑箱 HTTP Agent 和離線提交採 declared-evidence profile。證據預算約束提交結果，內部上下文與工具使用屬自報。受控執行 profile 可另要求完整 trace 與監測。兩種 profile 分開比較。

答題失敗、缺失或逾時在該 run 的 correctness、complete evidence 等整體有效性指標中計 0。格式錯誤按題目記錄，並保留其他有效題。評分服務自身故障標記 pending/error，可重試，未完成前不得發布最終排名。

LLM 低信心評分進 review_required。報表顯示待定數量與 provisional 狀態。整體平均以題目等權 macro average，同時展示各題型分數與樣本數。

### 9.4 替代來源與爭議

初始 gold 可能漏掉有效替代來源。遇到答案正確、引用有支持力、卻未匹配 gold 的結果，建立 alternative_source_review。經驗證後新增 benchmark revision，使用相同新版本重評所有受影響方法。保留舊分數及修訂理由。

## 10. 資料實體與狀態

| 實體 | 主要內容 |
| --- | --- |
| DocumentVersion | 文件 hash、原始檔、頁數、解析版本 |
| Page / Asset | 頁面影像、尺寸、旋轉、圖表 crop |
| EvidenceUnit / EvidenceRelation | 原始位置、抽取內容、跨文本關係 |
| BenchmarkRevision | 文件集合、四部分資料、配額、生成版本、凍結狀態 |
| Question / Fact / EvidenceSet | 題目、必要事實、原文檢查、OR-of-AND 證據 |
| MethodRevision / PreparedIndex | 方法與設定版本、文件 hash、索引識別 |
| Run / Attempt / Submission | 測試設定、逐題呼叫與輸出、payload hash |
| Evaluation / QuestionScore | grader 版本、各項分數、理由、待複核狀態 |
| Job / Checkpoint / UsageEvent | 進度、租約、錯誤、重試、模型用量 |

Benchmark 狀態依序為 draft、parsing、planning、generating、validating、ready_to_freeze、frozen。分支狀態有 needs_review、paused_budget、failed、cancelled。

Run 狀態為 queued、preparing、running、evaluating、completed。外部提交模式以 awaiting_submissions 取代 running，finalize 後進 evaluating。分支狀態有 completed_with_errors、review_required、paused_budget、failed、cancelled。

Job 使用 lease 與 checkpoint，worker 中斷後可接手。資料庫對 idempotency key、run/question/attempt 等建立唯一約束。對外 LLM 請求可能在回覆遺失時已計費，需記錄 outcome_unknown，有限重試並揭露可能重複用量。不能承諾外部呼叫絕對只發生一次。

### 10.1 PostgreSQL 資料要求

PostgreSQL MUST 為業務狀態、版本、資料血緣、稽核與追蹤紀錄的主要資料庫。使用者方法可自行採用其他向量庫，LAB 保存其方法、索引與提交結果的身份和版本。

原始 PDF、頁面圖像與大型模型 payload 可存物件儲存，資料庫保存 artifact_id、不可變 object key/version、SHA-256、size、MIME、project_id、保留狀態與權限。永久引用使用 artifact_id，下載時才生成限時 URL。

| 資料表群組 | 必要內容 |
| --- | --- |
| projects、actors、resource_versions | 專案、使用者/服務身份、resource_id、logical_id、type、revision、content_hash、建立者與時間 |
| document_versions、pages、evidence_units、evidence_regions | 原檔與解析版本、頁碼、座標、跨文本證據 |
| benchmark_revisions、questions、facts、evidence_sets、evidence_set_members | 四部分 benchmark 與必要證據組 |
| method_revisions、prepared_indexes、config_snapshots | 程式版本、索引 fingerprint、去密鑰設定快照 |
| runs、attempts、submissions、retrieved_hits、citations | 實驗、逐題嘗試、輸出、排序與引用 |
| evaluations、question_scores、fact_scores、source_matches | 判定理由、門檻、匹配區域、分數分子及分母 |
| lineage_edges | 上下游 resource_id、relation、operation_id 與時間 |
| operations、external_calls | 每次實際呼叫、provider request ID、輸入輸出 artifact、用量及結果 |
| workflow_revisions、node_revisions、node_executions、graph_checkpoints | workflow/node 版本、分支、執行、state 與恢復點 |
| trace_spans、span_links、log_events、audit_events | 呼叫階層、跨工作連結、事件與操作者變更紀錄 |
| outbox_events、jobs、checkpoints | 交易事件、工作租約與進度 |

核心 ID、版本、狀態與關聯 MUST 使用明確欄位及外鍵，JSONB 用於擴充 metadata 與通過 schema 驗證的內容。關聯須驗證 project 相同，評分須指向該 run 的 submission 與正確 benchmark revision 的 question。

資源版本透過 resource_versions 統一登記，lineage 兩端 MUST 有外鍵，型別資料列與註冊表需一致。時間使用 timestamptz、以 UTC 保存，UI 轉換時區。主鍵使用 UUID。hash 保存演算法與 JSON canonicalization 版本。

MUST 建立以下唯一約束及索引。

- (project_id, resource_type, logical_id, revision) 唯一。
- (project_id, idempotency_scope, idempotency_key) 唯一，保存 request hash。
- (run_id, question_id, attempt_number) 唯一，每次 attempt 的已接受 submission 唯一。
- (evaluation_id, question_id, metric_name) 唯一，fact 層另含 fact_id。
- lineage 上游、下游索引，trace_id、run_id、question_id 與時間的組合索引。
- audit 的 project/resource/time 及 actor/time 索引。

大量 log 可採時間分割與歸檔，但 event_id 去重需處理跨分割重送。migration MUST 版本化，保存 schema 與部署版本，部署前驗證既有資料升級。應用角色與 migration 管理角色分離。

### 10.2 血緣、不可變版本與一致性

任一分數 MUST 能反查實際使用的 submission、答案、hit、引用、method/index revision、問題與四部分 gold、建題 context、原始 PDF、parser、prompt/model、grader、node、workflow revision 及操作者。

lineage relation 至少包含 extracted_from、derived_from、generated_from、validated_against、retrieved_from、answered_using、evaluated_against、supersedes。邊保存確切版本及操作身份，查詢限制深度並處理重訪節點。

建題器保存實際 context manifest，包含 evidence IDs、順序、截斷資訊及輸入 artifact。快取命中建立新的操作紀錄並連回原產出和呼叫，不重複計費或偽造新的模型呼叫。

凍結 benchmark、已接受 submission、已發布 evaluation 與已完成操作結果 MUST 不可原地覆寫。修正建立新版本。可變 job/run 狀態使用 row_version 或等效併發控制，記錄狀態轉移及原因。外部方法未提供內部細節時標示 visibility=declared_only。

業務變更、必要 lineage、audit 與 outbox event MUST 在同一 PostgreSQL transaction 提交，必要稽核失敗則回滾。worker 消費 outbox 採至少一次傳遞與冪等去重。外部 HTTP/LLM 呼叫在交易外進行，呼叫前持久化 intent，呼叫後以另一交易保存結果和狀態。

artifact 採 staging、checksum 驗證、引用提交流程。讀取者只使用 committed artifact，中斷留下的 staged 物件可在寬限期後回收，已被有效版本引用的物件保留。

MUST 提供 PostgreSQL 與 artifact 的配套備份、保留政策、還原程序和部署適用的 RPO/RTO。還原測試須從一筆分數實際開啟 PDF、題目、答案、引用與評分紀錄，核對 hash 和關聯。凍結資料的精確重播以保留產物和規則版本完成，重新呼叫 LLM 的輸出不保證逐字相同。

### 10.3 結構化 Log、Trace 與稽核

啟動時 MUST 讀取 logging/tracing 設定，透過 middleware、worker/node 包裝及 repository/client 包裝記錄流程。可選擇追蹤高頻內部函式，但關鍵工作步驟必須有開始、完成、失敗及耗時紀錄。

LAB log_events 使用結構化 JSON 並可在 PostgreSQL 查詢。每筆包含以下欄位，無適用值者明確為 null。

| 欄位群 | 必要欄位 |
| --- | --- |
| 事件 | event_id、schema_version、occurred_at、observed_at、level、event_name、message |
| 呼叫 | trace_id、span_id、parent_span_id、service、component、operation_name |
| 工作 | project_id、request_id、job_id、run_id、question_id、operation_id、attempt_id、retry_of_attempt_id |
| 版本 | document_version_id、benchmark_revision_id、method_revision_id、evaluation_id、workflow_revision_id、node_revision_id |
| 結果 | status、duration_ms、input_refs、output_refs、error_code、error_type、error_message、stack_ref |
| 模型 | provider、model、provider_request_id、usage、cost、usage_known |
| 來源 | telemetry_origin=lab_observed/participant_reported、attributes |

事件時間採 UTC，耗時以 monotonic clock 計算。ID 表達父子與因果關係，跨機器時間只供顯示。輸入輸出引用包含 artifact/resource ID、hash、大小及去密鑰摘要。

必要事件包含 parse.completed、context.built、question.generated、question.validated、benchmark.frozen、index.prepared、query.completed、submission.accepted、score.computed、cache.hit、retry.scheduled、budget.paused、job.resumed、artifact.unavailable、operation.failed。

INFO 記錄階段成果與數量，WARN 記錄重試、低信心與部分解析，ERROR 記錄失敗，DEBUG 記錄可選細節。業務 log 必須有 operation_id 及可用的資源 ID，讓每筆紀錄可對應到實際工作。

API 與 worker 傳遞 trace context。HTTP 邊界的跨服務關聯由 LAB bridge 實作，外部 ID 不作授權依據。retry 建立新 attempt/span 並連回原 operation，長時間續跑可開新 trace 並保留 span link。worker 崩潰後由 reconciliation 補記 interrupted/outcome_unknown，保留缺少結束事件的事實。

必要業務狀態、audit、WARN/ERROR 與階段 span 預設全保留，DEBUG 可限量與抽樣。collector 需回報 queue depth、寫入失敗、丟棄數及缺口範圍。診斷 log sink 故障採持久緩衝和告警，業務 audit 仍隨交易保存。重送以 event_id 去重。

audit_events MUST 保存 actor、action、resource、before/after revision、reason、outcome、operation 與 trace。涵蓋建立、修訂、凍結、例外核准、重評、人工改判、匯出、權限變更及刪除。一般應用對 audit 採 append-only，修改以追加更正事件處理。

模型實際 prompt、context、輸出及對外判定理由放受限 artifact，一般 log 保存引用與摘要。API key、Authorization、cookie、帶憑證 URL 必須遮罩。遮罩或刪除導致無法重現時，記錄 redaction_policy 與 reproducibility=partial。

log、trace、lineage 和匯出承襲資源權限，gold 相關紀錄不向參賽憑證開放。建議初始熱資料保留 DEBUG 7 天、INFO/span 30 天、WARN/ERROR 90 天，均為部署可調預設。活躍 benchmark/run 的必要血緣、call manifest、audit 與產物至少同壽命，過期 debug 清理不得移除必要重現資料。

hash 與 append-only 提供內容核對及一般角色的修改限制。要求對管理員竄改也可偵測時，需另以獨立憑證保存簽章或不可覆寫外部摘要。受控刪除要涵蓋 artifact、備份及衍生副本，留下必要 tombstone 與稽核。

匯出提供 JSONL/CSV，包含 schema、時間範圍、過濾條件、匯出者、筆數、hash 及遮罩欄位。CSV 需處理公式注入字串。

## 11. UI 規格

| 畫面 | 核心操作與資訊 |
| --- | --- |
| 文件工作區 | 上傳、頁數、解析進度、錯誤頁、PDF 預覽 |
| Benchmark 工作台 | 建題設定、題數與配額、題目與四部分答案、原文核對、凍結與修訂 |
| 方法管理 | 註冊 HTTP endpoint 或外部提交、版本、能力、連線檢查 |
| 實驗執行 | 選 benchmark 與方法、預算 profile、進度、成本、取消或續跑 |
| 結果比較 | 各主分數、題型篩選、延遲與成本、可比條件警示 |
| 單題檢查 | 問題、標準與實際答案、搜尋排序、逐項引用、原始 PDF 高亮、扣分原因 |
| Trace 控制台 | 依 run/question/job/trace、元件與等級篩選，展開呼叫樹、時間軸、耗時、重試、錯誤及輸入輸出引用 |
| 血緣與稽核 | 從分數追到原始 PDF、模型呼叫與修訂紀錄，檢視操作者、理由、缺口、遮罩和歸檔狀態 |
| Workflow 檢視 | 顯示 graph/node 版本、條件分支、各 node 執行狀態及 checkpoint，點選 node 可開啟其 trace |

單題檢查必須區分 gold、方法回傳、實際引用與 distractor。遠距證據可同時開啟多頁。資料來源座標由原始 PDF 預覽核對。

## 12. 執行與存取設計

PostgreSQL 與 pgvector 為首版必要基礎設施。建議 FastAPI/Pydantic 提供 LAB API 與資料契約，Go workflow worker 使用 agentKit graph/client，Python PDF/OCR/視覺服務提供處理能力，透過版本化 HTTP 或工作佇列交接 artifact 引用。物件儲存保存 PDF、圖像和大型 payload，React 與 PDF 預覽元件提供操作介面。套件版本於實作時鎖定與驗證。

內部元件拆成 PDF parser、context builder、question generator、question validator、benchmark repository、adapter runner、source matcher、answer grader。每個元件透過資料契約替換，讓建題策略與評分演算法能各自更新。

方法憑證與 gold 存取權分離，第三方方法只能取得本次授權的 PDF 與題目。API key 不進報表或一般 log。使用者程式在外部自行執行，若後續支援平台託管則另提供隔離 worker 與明確資源限制。

遠端 endpoint 的可連線範圍由部署設定管理。多租戶平台須避免 endpoint 註冊讀取內部服務，個人自架的本機網段例外需明確設定。PDF 文字及方法回答皆視為資料，不能改寫建題或評分系統指令。

500 頁支援的驗證重點是批次、可中斷、可續跑、可觀測的資源使用。具體完成時間與記憶體上限需以指定硬體、文件與模型實測後設定，不在本版捏造效能保證。

### 12.1 agentKit 審查結論與採用範圍

審查基準為 Disesfgewu/agentkit 的 commit ce49a7130b17ccda07c755de96ef44c49d0b719c，核對 README、graph/graph.go、model/model.go、model/remote.go、model/embedding.go、agent/embedding.go、agent/rag.go、agentlog/logger.go、llmrouter/server.go 與 go.mod。結論為可採用作為 workflow 與模型 client 核心，配套整合需完成下表工作。此結論來自程式碼靜態審查，執行環境目前沒有 Go，尚未執行測試或驗證真實模型。

| 能力 | 已核對的現況 | LAB 採用與補足要求 |
| --- | --- | --- |
| Graph/node | 有 Node、State、條件路由、RunCheckpointed 與 Store 介面 | 以 graph 組成建題、驗題與評分流程，實作 PostgreSQL Store |
| 執行模型 | 單一路徑逐 node 執行，無內建並行 supersteps、reducers 或人工 interrupt | 批次並行交由 LAB scheduler，人工等待採持久工作狀態與新 continuation |
| 模型 client | model.Client.Chat、model.Embedder.Embed，RemoteClient 可走 llmrouter | Go worker 直接用介面，非 Go 服務透過既有 HTTP 協定 |
| 模型請求 | Message.Content 為 string，Request 無 image parts 或 JSON schema 欄位 | 文字輸出由 LAB schema 驗證器驗證，視覺經專用 adapter，後續可擴充 client 能力 |
| 向量儲存 | 有 VectorWriter、VectorSearcher，內建 MemoryVectorStore | LAB 實作 PgVectorStore 並保留原始區域映射 |
| 引用驗證 | RAGAgent 主要檢查回答是否包含 [document ID] | LAB 以 SourceRegion 與主張支持性自行評分，citation_valid 不作正確來源判定 |
| Audit | 本機 completion log、begin journal、seal、call/parent ID 與 hash chain | 匯入 PostgreSQL、保留原檔、轉換成 LAB log schema，補跨服務關聯 |
| Router trace | RemoteClient 和 llmrouter 沒有共享呼叫身份的既有連結 | 加 request/trace bridge 與 server/client 對應紀錄 |
| 工具鏈 | go.mod 宣告 Go 1.27.1 | 實作 CI 必須先驗證宣告工具鏈可取得且能編譯，再鎖定可用 runtime |

首版採 Go workflow worker 持有 graph，FastAPI 負責 API 與工作控制，PDF/OCR/vision 可用 Python 服務。這是本 SPEC 的整合選擇。worker 只傳資源 ID、artifact refs 與狀態摘要，避免把 500 頁內容放入 graph.State。

agentKit commit、workflow revision、node revision 及設定 hash MUST 固定於每次工作。升級依賴需先執行相容性測試。使用者方法仍依第 7 節 HTTP/JSONL 協定接入，可使用任意 Agent 框架。

### 12.2 Workflow 與 Node 契約

每個 node MUST 有 node_id、node_revision、input/output schema、config hash、timeout、retry policy、idempotency scope、權限需求與 log 包裝。Node 輸入輸出在邊界驗證，僅成功且已持久化的產物可交給下一步。

| Graph | Nodes 與路由 |
| --- | --- |
| DocumentPreparation | validate_document → parse_batch → extract_evidence → link_context → publish_manifest |
| QuestionGeneration | select_evidence → generate_candidate → validate_sources → validate_answer → classify_candidate |
| BenchmarkFinalization | collect_valid_questions → check_quotas → review_gate → freeze_revision |
| Experiment | resolve_method_index → invoke_method → validate_submission → persist_submission |
| Evaluation | load_frozen_gold → match_sources → score_rules → judge_semantics → persist_scores |

QuestionGeneration 的 classify_candidate 可導向 accept、regenerate 或 needs_review。regenerate 次數與費用有上限，達上限進 needs_review。check_quotas 發現缺口時排程新的題目工作，保持既有合格題。review_gate 將工作存成等待狀態後結束本次 graph，核准後由 scheduler 建立 continuation。

每個頁面批次、候選題、逐題評分使用獨立 thread/work item，由 LAB scheduler 管理並行數、provider 配額與完成彙整。Graph 本身只執行單一工作。MaxSteps 明確按各 graph 與修正迴圈設定，預設 32 不直接套用整份 500 頁流程。RetainPatches=false，詳細輸出保存 PostgreSQL/artifact。

state 至少包含 project_id、job_id、thread_id、workflow_revision_id、resource refs、stage、attempt counter、budget ref、state_schema_version。agentKit 目前只做 top-level state copy，nested map/list 必須視為不可變，node 回傳新的值。checkpoint codec 須保留型別或驗證 JSON 數值轉換，避免恢復後型別斷言失敗。

### 12.3 Checkpoint、重試與模型呼叫

PostgresGraphStore 實作 agentKit 的 Save(threadID, Checkpoint) 與 Load(threadID) 介面，保存 State、Steps、LastNode，另存 graph revision、state schema version、lease fencing token 與遞增 checkpoint sequence。thread_id 全域唯一並綁定 project；scheduler 每個 thread 只允許一個有效 lease，Save 驗證 fencing token 避免舊 worker 覆寫。

agentKit 在 node 回傳後才保存 checkpoint。因此 node 的 DB 副作用與 graph checkpoint 可能分開提交。每個 node 必須以 (work_item_id, node_revision, logical_invocation_id) 記錄 operation，成功結果先持久化，重跑時復用已完成產物，再推進 checkpoint。logical_invocation_id 在第一次外部呼叫前保存，合法迴圈的新一輪使用新 ID，retry 維持原 ID 並新增 attempt。

外部呼叫已送出但結果未知時標記 outcome_unknown，不能只因 checkpoint 缺少就判定未執行。遇到失敗的 Save，新的執行者依已保存 operation 復用結果。這是 LAB 的恢復責任。

graph revision 改變時不得直接沿用舊 LastNode。應維持原 revision 跑完，或經明確 migration 對映後以新 thread 記錄續跑關係。人工等待及 budget pause 採 LAB 狀態管理，避免假設 graph 已有 interrupt/resume 協定。

模型呼叫使用已存在的介面與路徑。

| 接口 | 用途 |
| --- | --- |
| model.Client.Chat(ctx, model.Request) | 建題、文字驗題與語意評分 |
| model.Embedder.Embed(ctx, model.EmbeddingRequest) | 文件及查詢 embedding |
| model.RemoteClient | 由 Go worker 呼叫共用 llmrouter |
| POST /v1/chat | llmrouter chat API，接受 agentKit model.Request |
| POST /v1/embeddings | llmrouter embedding API，接受 model.EmbeddingRequest |
| GET /v1/models | 查詢已載入模型 |

LLMRouter 路徑與 LAB /api/v1、參賽方法 /v1/query 各自使用獨立 base URL。不得把 /v1/chat 當成 OpenAI /v1/chat/completions 的相同 schema。

正式評測固定 model/router entry 與 embedding space。聊天 failover 若被允許，必須保存實際模型，改變模型的輸出單獨標記；embedding 更換模型建立新 space 和索引。RemoteClient 已由 router 負責重試，LAB 只負責工作層有限恢復，記錄所有外部 attempts，避免重試次數相乘。

請求 deadline、node timeout 與 router timeout 必須配套設定。為取得可追溯的逾時結果，router timeout 應短於外層 node deadline，遠端 client transport timeout 再留傳輸餘裕。SPEC 的 180 秒 query profile 與 router 現有較長預設需由部署設定對齊。

視覺與 schema output 的能力在註冊時顯式宣告。第一版新增 VisionReader adapter，輸入 crop refs，回傳來源、讀值、信心及版本，低信心走審核。不能僅將圖像路徑寫進文字 Message 就視為模型已讀圖。結構化文字輸出需 JSON schema validation、有限修復和失敗記錄。

### 12.4 pgvector 內建向量儲存

系統以 PostgreSQL extension vector 提供內建向量存取。pgvector 支援 exact nearest-neighbor，以及 HNSW/IVFFlat approximate search，兩種模式分別記錄於 index revision。預設先提供 exact cosine 作為可核對的參考模式，ANN 參數和召回差異列入實驗資料。[pgvector 官方文件](https://github.com/pgvector/pgvector)

| 實體 | 必要欄位 |
| --- | --- |
| embedding_spaces | provider、實際 model/revision、dimension、normalization、document/query task 設定、preprocess version、metric、config hash |
| vector_collections | project、purpose、method revision、document hashes、embedding_space_id、index revision、狀態 |
| vector_items | collection、chunk/resource ID、content hash、vector、embedding space、created_by_operation_id |
| vector_item_regions | vector item 與多個 SourceRegion 的映射，支援跨頁和非連續來源 |
| vector_index_revisions | exact/HNSW/IVFFlat、資料型別、建索引參數、查詢參數、extension version、corpus manifest hash |
| vector_queries | query artifact/hash、query embedding ref、space、filter、k、metric、排序結果、distance、耗時、trace |

系統建題 collection 與每個使用者方法的 collection MUST 以 project、purpose、method/index revision 隔離。查詢必須限定合法 collection 與 document scope，禁止透過參賽查詢存取 gold 或系統建題內容。

同維度也不能混合不同 embedding space。每次寫入及查詢檢查模型、維度、前處理與 task pair。拒絕 NaN/Infinity，cosine 模式拒絕零向量。不同維度與 metric 採各自相容的欄位或分割及索引，模型升級建立新 collection，保留舊 run 的索引身份。

PgVectorStore 在 LAB 實作 agent.VectorWriter.Upsert 與 agent.VectorSearcher.Search。由 adapter 建立時綁定 collection、space、project 授權與 distance metric，因目前 VectorQuery 僅有 Vector、EmbeddingModel、K。Document.ID 對應 chunk/resource ID，完整 PDF regions 從 LAB 資料表取得。

agentKit 的預設文字 chunker 不保存精準 PDF 區域映射。LAB 提供自訂 Chunk callback 或自行準備 VectorRecord，必須保留一對多 SourceRegion。已切好的 evidence 使用 identity chunker，避免二次切割改變來源對應。

cosine 模式回傳 distance 與 similarity=1-distance，映射至 agent.Document.Score 時使用越高越好的 similarity，與 MinScore 語意一致。門檻依模型和資料校準；距離值、排序與閾值均寫入 vector_queries。

寫入採可重試 batch、唯一 (collection_id, item_id) 及內容 hash。索引狀態 building → ready，完整 manifest 驗證後才發布 ready。凍結 collection 不可原地更換內容，重建產生新 revision。

ANN 套用 filter 可能回傳不足 k 筆，需記錄短缺、使用明確的擴大掃描或 exact fallback 策略，保留實際模式與預算。exact 與 ANN 比較需固定同一資料集合。維度、資料型別與 ANN index 相容性在建索引前驗證，無法建立時明確失敗或採宣告過的 fallback。[pgvector filtering 與 index 文件](https://github.com/pgvector/pgvector#filtering)

向量相似度用於候選搜尋，最終來源命中仍依第 9 節的原始位置及事實支持性判定。benchmark 與向量索引各有獨立快取鍵，重建索引不重建問題集。

### 12.5 agentlog 對接與整合完成條件

現有 agentlog 使用完成紀錄文字行、begin journal 與 close seal，completion 行的前綴固定為 DEBUG。LAB 必須提供 importer 或結構化 sink adapter，保存原始 .log/.journal/.seal artifact，將 (session_id, call_id, parent_call_id) 對映至 trace/span，按實際事件和結果分類業務等級，不能靠原檔前綴判定所有操作都是 debug。

原紀錄 hash/seq 與來源檔保留，轉換後另有 LAB schema version 與 event_id，重送去重。未完成 session 標記 incomplete，Verify 結果與驗證器版本一併保存。匯入必須處理特殊字元與大 payload，解析失敗保留原檔並顯示缺口。新增 sink 時必須保持原稽核紀錄可驗證。

agentlog 的輸入輸出可能含 prompt 和文件，原始檔依受限 artifact 政策保護。原始 audit 必要寫入失敗時可能使 agentKit 呼叫失敗或 panic，LAB worker 需記錄並轉成工作失敗；一般診斷 collector 故障則依第 10 節降級。不能把兩種失敗策略混用。

RemoteClient 與 llmrouter 原始碼沒有跨服務 call ID 傳遞。LAB bridge 必須新增或封裝相關 request headers 與對應日誌。只在 client 增加 header 仍不足，server 必須接收並保存關聯。在橋接驗收前只能顯示兩段相關工作紀錄，標示 provider 內部 trace 不完整。

整合需交付 PostgresGraphStore、PgVectorStore、NodeRunner 包裝、GraphStateCodec、TraceBridge、AuditImporter/結構化 sink、VisionReader 與 JSON output validator。這些是待實作的 LAB 元件名稱，並非宣稱 agentKit 已有同名 API。

審查來源固定於以下版本，供實作比對。

- [graph 與 checkpoint 原始碼](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/graph/graph.go)
- [model client 請求格式](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/model/model.go)
- [RemoteClient](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/model/remote.go)
- [向量儲存介面](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/agent/embedding.go)
- [agentlog](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/agentlog/logger.go)
- [llmrouter HTTP server](https://github.com/Disesfgewu/agentkit/blob/ce49a7130b17ccda07c755de96ef44c49d0b719c/llmrouter/server.go)

## 13. 驗收條件

| ID | 情境 | 通過條件 |
| --- | --- | --- |
| A01 | 500 頁且素材足夠的 PDF | 產生 500 道通過驗題的題目，三種配額各至少 50 題 |
| A02 | 少於 20 頁的 PDF | 目標 20 題，素材不足時顯示缺口，不用重複題補足 |
| A03 | 無圖表的 PDF | 無法符合圖表配額，標示 needs_review 或明確 partial |
| A04 | 系統與使用者切割不同 | 使用者只讀原始 PDF 也可接入，評分不要求匹配系統 chunk ID |
| A05 | 遠距跨頁題 | 少一個必要來源時 complete evidence 為 0，找齊時為 100 |
| A06 | 可接受替代來源 | OR-of-AND 的任一完整組合均可取得完整證據分 |
| A07 | 易混淆來源 | 錯年份或錯測試條件的相似段落不計正確來源命中 |
| A08 | 表格與圖表題 | 單位、欄列、圖例或估讀範圍錯誤反映在答案及一致性分數 |
| A09 | 正確答案但錯來源 | 正確性可得分，來源與引用分數依實際證據扣分 |
| A10 | 整頁及精準區域都覆蓋 gold | coverage 可相同，整頁的定位精準度較低 |
| A11 | 相同 PDF 與建題設定重送 | 返回已存在或進行中的 benchmark，不另發生成請求 |
| A12 | 第二種方法執行 | 題庫 hash 保持相同，只建立新 run 與方法索引 |
| A13 | HTTP Agent 與 JSONL 提交 | 相同輸出經相同 grader 得到相同來源與規則分數 |
| A14 | 分批建題時中斷 | 續跑復用成功頁與成功題，保留未決外部呼叫紀錄 |
| A15 | API 重送與併發 | 冪等請求只建立一個資源，衝突 payload 回 409 |
| A16 | 方法漏答或逾時 | 全部題目仍在分母，錯誤率與原因可追查 |
| A17 | 評分模型失敗 | 狀態為未完成並可續跑，不把評分故障當成參賽錯答 |
| A18 | 嘗試讀取 gold | 參賽憑證無法取得答案、內部證據索引或 verifier 紀錄 |
| A19 | bbox、頁碼或文件 hash 錯誤 | 回傳明確來源錯誤，不計錯誤來源得分 |
| A20 | gold 修訂 | 建立新 revision，所有方法以同版本重評，歷史結果保留 |
| A21 | 分數血緣查詢 | 可追到 submission、gold、PDF、方法、模型呼叫及實際 workflow/node 版本 |
| A22 | audit/outbox 寫入故障 | 業務變更與必要事件一起回滾 |
| A23 | 外部呼叫後中斷 | 未決呼叫標示 outcome_unknown，續跑保存新的 attempt 與關聯 |
| A24 | 跨服務執行 | API、worker、router 的呼叫可串接，第三方不可觀測步驟標示缺口 |
| A25 | 修改凍結結果 | 普通應用無法覆寫，修訂保留新舊版本及操作者原因 |
| A26 | Log 查詢與匯出 | 可按 run/question/trace/level/time 找到重試與錯誤，匯出附 schema 及 manifest |
| A27 | collector 中斷及重送 | 必要 audit 仍隨交易保存，診斷資料可補送去重，丟失範圍可見 |
| A28 | log 含敏感內容 | 一般 log 無密鑰，參賽身份不能經 trace/lineage/匯出讀 gold |
| A29 | 備份還原 | 恢復 PostgreSQL 與 artifact 後可開啟完整實驗證據，hash 及關聯一致 |
| A30 | 清理 DEBUG | 活躍實驗的必要血緣、call manifest、產物及 audit 保留 |
| A31 | artifact 提交中斷 | staged 資料不發布，可重試或回收，已引用物件不誤刪 |
| A32 | 快取命中 | 新操作連原產出，不重複生成或計入新模型用量 |
| A33 | graph checkpoint 失敗後重啟 | 成功 node 的持久產物被復用，無重複副作用，state 型別及 next node 正確 |
| A34 | 兩個 worker 接同一 thread | lease/fencing 只允許有效 worker 推進 checkpoint，舊 worker 寫入被拒絕 |
| A35 | 超過步數及人工等待 | graph 有限停止，等待狀態持久化，核准後 continuation 與原工作可追溯 |
| A36 | Embedding space 不符 | 不同模型、維度或前處理設定的混寫和查詢被拒絕 |
| A37 | pgvector 重建與隔離 | 未完成索引不發布，各 method collection 不互查，重建不生成新題庫 |
| A38 | Exact/ANN 搜尋 | 固定資料上可比較 recall、排序和延遲，filter 不足 k 的情況有記錄 |
| A39 | 圖表及模型 schema | VisionReader 確實取得圖像，文字 client 不誤報視覺能力，無效 JSON 有有限修復與錯誤紀錄 |
| A40 | agentlog 匯入 | begin/end、parent、hash、seq 保留，崩潰 session 標示 incomplete，重送去重 |
| A41 | Agent 引用 [ID] 但來源錯 | LAB 仍依原始區域和支持性扣分，不直接採用 citation_valid |
| A42 | 建置與實際整合 | 固定 agentKit commit 及可用 Go 工具鏈，Go 測試、PostgreSQL/pgvector adapter 與至少一個真實 provider 冒煙測試通過 |

## 14. 實作順序

1. 定義 SourceRegion、四部分 benchmark 與 query response schema，建立 PostgreSQL migrations、版本、血緣、audit/outbox、pgvector collection 和 tracing 基礎，先驗證固定 agentKit commit 的工具鏈與接口。
2. 實作 Go graph worker、PostgresGraphStore、PgVectorStore 與 trace bridge，打通 PDF/OCR/vision 服務、建題驗題、凍結與快取。
3. 實作 HTTP adapter 與 JSONL 提交，用兩個不同切割方法驗證完整流程。
4. 實作來源匹配、答案與引用評分、逐題 PDF 高亮及方法比較。
5. 驗證 500 頁、checkpoint 恢復、預算暫停、版本修訂、併發冪等、log 匯入、向量隔離、ANN/exact 差異與備份還原。

首版完成定義為 A01–A48 全部有可重現驗收結果。方法品質由實驗得分呈現，LAB 的驗收以契約一致、評分行為符合規格及工作可重現為準。

## 15. 強制要求、禁止事項與交付範圍

### 15.1 判定規則

R 為需求、T 為測試、A 為端到端驗收編號。每個需求必須有實作位置、版本、測試與結果證據。只有文字聲稱支援，不能通過驗收。所有 MUST 均屬首版必要項，修改範圍需更新規格與測試映射，不得由實作者默默降級。

數值可在新 config/profile revision 中調整，同一 run 的版本與預算固定。測試不得在看見結果後修改門檻。資料不足可以產生明確 partial benchmark，其品質標記與正式合規結果分開。

| ID | 模組 | 必須有 | 禁止有 | 必測 |
| --- | --- | --- | --- | --- |
| R01 | 匯入 | PDF 解析驗證、hash、頁數、上限、版本與明確錯誤 | 只看副檔名；原地覆寫來源 PDF | T01–T04 |
| R02 | 解析/OCR | 原始及正規化內容、閱讀順序、座標、混合掃描頁、失敗頁記錄 | 靜默漏頁；未核對 OCR 被當可靠 gold | T05–T08 |
| R03 | 圖表/跨文本 | 欄列/單位/圖例/圖說、原圖、續表、遠距證據與各自來源 | 合併後失去來源；只看圖說卻算閱讀圖表題 | T08–T11 |
| R04 | 四部分題庫 | 問題、文意、原文檢查、來源組合以 ID 聯結 | 只存自由文字答案；依陣列順序配對 | T12–T14 |
| R05 | 題數/配額 | N=max(20,P)、Q=ceil(0.1N)、配額獨立計題與覆蓋報告 | 重複題灌數量；虛構圖表或干擾證據；partial 冒充 compliant | T14–T17 |
| R06 | 建題/驗題 | 獨立操作、來源核對、有限修復、低信心及抽樣人工審查 | 生成後直接凍結；不存在的來源進入 gold | T18–T21 |
| R07 | 版本/快取 | 不可變 revision、快取鍵、job checkpoint | 新方法或新 run 重建題庫；原地改標準答案 | T22–T24 |
| R08 | LLM | 能力宣告、固定設定、schema 驗證、用量/未知狀態 | text client 冒充 vision；無限重試；未知費用當零 | T19、T25–T29 |
| R09 | 方法接入 | HTTP 同步/非同步、JSONL、prepare manifest、能力驗證 | 強迫同一 chunker/Agent 框架；在 LAB 主機直接執行上傳程式 | T30–T34 |
| R10 | 提交/溯源 | 答案主張、排序 hit、原始區域、citation、拒答理由 | 引用不存在 hit；印刷頁碼當實體索引 | T33–T36 |
| R11 | 評分 | 獨立分數、逐 fact 理由、分子分母、待定與不適用狀態 | 只給總分；相似度代替來源正確；[ID] 出現即滿分 | T37–T44 |
| R12 | 實驗可比性 | 固定題庫/子集/grader/budget、實際模型、答案選擇規則 | 刪失敗題；每題挑最高分 attempt；混版本排名 | T43–T46 |
| R13 | PostgreSQL | migration、外鍵、唯一鍵、交易、artifact 引用 | 記憶體取代持久化；關聯只放未驗證 JSON | T47–T50 |
| R14 | 血緣 | 分數到來源、版本、實際操作的完整路徑 | 捏造黑箱內部步驟；只有 hash 卻宣稱可還原內容 | T51、T52 |
| R15 | pgvector | embedding space/collection 隔離、batch、ready gate、query 紀錄 | 混模型向量；參賽查到 gold；查詢半成品索引 | T53–T57 |
| R16 | Workflow | graph/node revision、schema、有限路由、scheduler | 單一無限 loop 跑全文件；暗改路由 | T58–T60 |
| R17 | 恢復/併發 | durable Store、codec、lease/fencing、operation 冪等 | 舊 worker 覆寫新 checkpoint；重複副作用未記錄 | T61–T64 |
| R18 | Log/trace | 業務 ID、等級、父子鏈、耗時、輸入輸出引用、缺口 | 無任務關聯的空泛 INFO；缺失 span 假裝成功 | T65–T68 |
| R19 | 稽核/權限 | 同交易 audit、遮罩、原 log 受限、受控刪除 | 由 log/lineage/export 洩漏 gold；普通角色改 audit | T49、T69–T72 |
| R20 | 成本/取消 | 併發 reservation、結算、unknown、取消與重試歷程 | retry 層數相乘；取消後排新付費呼叫 | T27–T29、T63 |
| R21 | UI | 文件/題庫/方法/run/比較/單題/trace/lineage/graph 畫面 | 假分數、假進度、占位資料通過正式驗收 | T73–T76 |
| R22 | API | schema、錯誤碼、分頁、冪等、權限與狀態機 | 所有錯誤都回 500；衝突請求靜默接受 | T31、T48、T70、T77 |
| R23 | 匯出 | 四部分題庫、結果、log、manifest、版本及 hash | 缺題未揭露；匯出無來源版本或未授權內容 | T74、T78 |
| R24 | 維運 | 備份還原、migration、健康/指標、資源限制 | 只看備份完成訊息；宣稱未量測的 SLA | T50、T79–T82 |
| R25 | 發布 | 鎖版依賴、必要測試、人工品質與真 provider 證據 | mock 冒充真模型；skipped/blocked 算通過 | T81–T84 |

### 15.2 首版不包含的功能

首版必須完成單 PDF 的跨頁、跨章節、圖文整合，以及兩種方法接入、建題、評分與追溯。下列項目列為後續擴充，UI/API 不得宣稱已支援。

- 跨多份 PDF 聯合生成 benchmark。
- 在 LAB 主機直接執行上傳的 Python、Go、shell 或任意 container。
- 使用者任意程式碼 node 編輯器或拖曳後直接執行未知程式。
- 模型訓練、微調、自動修復參賽演算法和自動調參競賽。
- 計費平台、公開社群排行榜與公開文件分享。
- 音訊、影片或任意 Office 格式匯入。
- 對黑箱 Agent 的所有內部 token、搜尋和思考過程提供完整觀測保證。

### 15.3 必要交付物

實作交付須包含可啟動程式、鎖定依賴、設定範例、migration、OpenAPI、JSON Schema、fixture/golden manifest、測試程式與指令、兩種 sample 方法、部署與備份還原手冊、驗收報告。

OpenAPI 必須定義第 8 節每條路徑的 request/response、enum、限制、角色、狀態碼及錯誤範例。JSON Schema 必須覆蓋 SourceRegion、四部分題庫、manifest、capabilities、prepare、query、submission、score、log_event、checkpoint、node state。範例必須能通過其 schema 驗證。

sample 方法必須採不同切割策略，並輸出同一 PDF 座標格式。HTTP/JSONL 為接入方式，不能僅換傳輸方式而使用完全相同演算法冒充兩種方法。

## 16. 契約、邊界與錯誤處理細則

### 16.1 基本型別

- API resource ID 為長度 1–128 的不透明字串，內部主鍵使用 UUID 並明確映射。前文 doc_001 等屬示意 ID，外部不得解析 ID 推測專案或頁碼。
- SHA-256 為 64 位小寫十六進位，PDF hash 計原始 bytes，JSON hash 使用已鎖定 canonicalization 版本。
- count、rank、page_index 為 integer，禁止以數字字串或小數替代。座標與分數必須為有限數值。
- SourceRegion 具第 4.1 節全部欄位，quote 可 null，page_index 在 [0,P)，bbox 在 [0,1] 且為正面積。可補 char offsets，但需固定 extraction revision。
- 頂層未知欄位回 422。擴充值放 attributes 並限制大小，不得覆寫核心語意。新增必填欄位或改變語意需升 protocol/schema 版本。
- 必要非空陣列不得用 [] 代替，選配集合使用 []，未知 usage/cost/confidence 使用 null，0 僅代表已確認零值。

### 16.2 題庫欄位與品質條件

| 物件 | 必要欄位與限制 |
| --- | --- |
| Question | question_id、非空 question_text、language、去重 tags、quota_bucket、question_hash、revision 內唯一 ordinal |
| SemanticAnswer | question_id、reference_answer、非空 required_facts；fact 包含 fact_id、statement、weight>0、allowed_equivalences、scoring_rule |
| TextualAnswer | question_id、checks；check 包含 check_id、fact_id、kind、expected、unit、normalization_rule、absolute_tolerance、relative_tolerance、supporting_evidence_ids，不適用欄位 null |
| SourceAnswer | question_id、非空 evidence_sets、fact_support_sets、distractors、validation_refs |
| EvidenceSet | set_id、非空且去重 evidence_ids，整組能支持所有必要事實 |
| FactSupportSet | fact_id 與 support_sets，使用 OR-of-AND，只表達該 fact 的必要證據 |
| Distractor | evidence_id、confusion_reason、disambiguating_condition，confusable 題至少一項 |
| ValidationRecord | validator revision、operation_id、verdict、reason、source checks、confidence 或 null、reviewer、reviewed_at |
| BenchmarkManifest | schema、benchmark/revision ID、document hashes、檔案 hashes、N_target/N_valid、配額、compliance、生成/驗證設定版本、凍結時間 |

fact 評分規則須在建題時定義全對/部分/錯誤。可拆成獨立事實者應拆開；允許 0.5 分時需預先列出部分得分條件，不能評分時臨時發明。

compliant revision 恰有 N 道 valid 題，額外候選留候選池。quota_bucket 為 general、cross_page、chart_table、confusable，特殊三桶各至少 Q 且以不同題計數。選題與去重規則須版本化。所有特殊型問題需能由公開問法判定答案，不能依賴隱藏限定條件。

每個有效跨頁 evidence set 都必須需要至少兩個實體頁面。移除必要證據後無法支持完整答案，才通過跨頁有效性驗證。圖表題需實際讀取 cell 或圖內資料。confusable 題需確認干擾內容不符合問題條件。

凍結前人工抽查預設 min(N,max(20,ceil(0.10N))) 題，涵蓋三種特殊題型及一般題，特殊題每類至少 2 題。所有已知歧義、錯值、缺來源需處理。抽查發現關鍵錯誤則擴查同模板/同來源群組，保留 reviewer、原因及修訂紀錄。partial 缺少某題型時列出無法抽查原因。

### 16.3 接入與提交細則

capabilities 必含 protocol_version、method_id/revision、supported_modalities、async_query、multi_region、max_parallel_queries。prepare 必含 request_id、documents、method_revision、config_hash，成功結果必含 index_id、index_fingerprint、document_hashes、method_revision、status=ready。

HTTP query response 由 request context 綁定 run_id/attempt_id，回傳 request_id/question_id/index_id 必須相符。JSONL 每行使用 envelope 包含 run_id、attempt_id、benchmark_revision_id、method_revision_id、document_hashes、response，response 遵守同一 query response schema。

answered 必須有非空 answer.text，claims 可為空但評分器仍驗證全文。abstained 必須有 reason、空 answer.text 與 claims=[]，可保留搜尋結果。retrieved_hits 與 citations 必填但可為空。telemetry 可省略，未知欄位值為 null。

hit_id 單題內唯一，rank 必須為 1..n，每 hit 至少一個 region。純圖像 hit 可使用空 content 並提供有效圖像區域，LAB 由原始 PDF 重建 crop。citation 的 claim_id、hit_id、region_indices 必須存在。

不可解析 JSON 回 400，缺欄位、重複 rank、無效 citation ID 回 422。來源區域數值型別/結構合法但超頁數或文件 hash 不符時，保存有效答案與 invalid_region 原因，無效來源不計分。不能因來源錯誤而抹掉獨立答題分。

同步 query 回 200，非同步回 202、job_id、poll_after_ms。job 查詢回 status、progress、result/error，只有 succeeded 有終局 result。query deadline 從首次發送起計，包含 poll/排隊/重試。晚到結果保存為 late，不取代已封存答案。

JSONL 每行獨立交易，回 accepted/rejected 數量及逐行 status/code。整份編碼或大小非法則整批拒絕。finalize 與提交使用同一 run lock 建立提交先後邊界，已開始但尚未取得鎖的提交以 finalize 的提交順序判定。封存後新結果回 409，已接受的相同 payload 冪等重送可讀回原結果。

### 16.4 API 錯誤、分頁與權限

| Code | 適用條件 |
| --- | --- |
| 400 | 無法解析 request/body |
| 401 | 缺少或無效認證 |
| 403 | 已授權可見的資源但角色不可執行動作 |
| 404 | 不存在或其他專案且不得揭露存在的資源 |
| 409 | revision/idempotency 衝突、已封存或非法狀態轉移 |
| 413/415 | 大小超限/不支援的格式 |
| 422 | 可解析但 schema、語意、來源或配額不合法 |
| 429 | LAB 速率或排程限制，附 retry_after |
| 502/503/504 | 上游錯誤/服務不可用/操作逾時 |

GET job 的 HTTP 200 僅表示查詢成功，失敗工作使用 status=failed 與 error。error 必含 code、message、retryable、request_id、details，不含 credential/gold。

page_size 預設 50、最大 200，cursor 綁定 filter/order。lineage depth 預設 3、最大 10，每回應最多 1000 nodes，截斷需回 truncated 與續查資訊。

| 動作 | Owner | Maintainer | Reviewer | Participant | Observer |
| --- | --- | --- | --- | --- | --- |
| 匯入/設定解析 | 是 | 是 | 否 | 否 | 否 |
| 讀授權 PDF/公開題目 | 是 | 是 | 是 | 是 | 依授權 |
| 建題/編輯/凍結 | 是 | 是 | 僅審查意見 | 否 | 否 |
| 讀 gold/生成 context | 是 | 是 | 是 | 否 | 否 |
| 註冊方法/提交 | 是 | 是 | 否 | 僅自己的方法/run | 否 |
| 讀 run 結果 | 是 | 是 | 是 | 自己的且依 feedback profile | 已發布彙總 |
| 讀 gold trace/lineage | 是 | 是 | 是 | 否 | 否 |
| 讀一般 run trace | 是 | 是 | 是 | 自己的且去除隱藏內容 | 否 |
| 刪除/變更成員權限 | 是 | 否 | 否 | 否 | 否 |

所有角色限定 project，預設拒絕未授權操作。開發 profile 的 gold 診斷僅對可讀 gold 角色開放。封閉評測 participant 僅得到規定彙總，不能經細分錯誤、trace、向量搜尋或匯出反查答案。

### 16.5 計分公式與答案選擇

結果保留 raw_value、display_value、numerator、denominator、status、reason_codes、grader_revision、support_refs。顯示至小數 2 位，中間計算不先捨入。status 為 scored、not_applicable、pending、error。

令 G_j 為每組可接受必要 evidence 集，M 為預算內有效結果命中的 gold evidence ID 集。

- Recall@k = 100×max_j(|M∩G_j|/|G_j|)，重複 evidence 只計一次。
- Complete@k = 100 若存在 G_j⊆M，否則為 0。
- Correctness = 100×sum(weight_i×fact_score_i)/sum(weight_i)，fact_score∈{0,0.5,1}。
- TextualConsistency = 100×passed_checks/applicable_checks，無適用 checks 為 N/A。
- LocalizationPrecision = 100×sum_page(area(R∩G))/sum_page(area(R))，R 為有效回傳區域聯集，G 為所有確認有效來源區域聯集，無有效 R 時為 0。
- CitationPrecision = 100×支持該主張的引用組數/全部去重引用組數，引用組包含所宣告的必要區域。
- CitationCompleteness = 100×具有完整有效引用支持的主張數/全文可查證主張數。
- Faithfulness = 100×受預算內提交證據支持的主張數/全文可查證主張數，可包含未明確引用但確有證據支持的主張。

char/cell 對齊版本需獨立匹配 profile，不可與面積版本混算。同一 claim 的完全相同 citation 去重，不可重複有效引用稀釋錯誤引用。引用預算外區域不算有效。answer.text 未列入 claims 的新增事實也須納入評估。

拒答/空答案的 correctness、citation 與 faithfulness 為 0，真實 hit 仍可得 evidence 分。timeout/漏答/格式錯誤的有效性主指標為 0。TextualConsistency 原題有適用 checks 時為 0，原題無 checks 才 N/A。grader 自身故障標 pending/error，不得換成零後發布正式排名。

報表須列 total、scored、not_applicable、pending、failed_method。平均以所有應適用題為分母，方法失敗仍包含於分母，pending 未處理完成前僅發布 provisional。

每個 run 固定採每題第一個有效終局 answered/abstained submission。無有效輸出的 transport failure 可有限重試。改答案須新建 run，重評只改 evaluation，不換 submission。第 7.3 節的 attempt 僅用於傳輸或未接受輸出的修復，不允許取代已接受答案。

比較須固定 benchmark revision、question subset hash、grader、budget、observation profile。若僅聲稱比較切割/搜尋，另固定回答器。完整系統比較可用不同回答模型，但實際模型必須揭露。

### 16.6 狀態機與預算

Job 狀態與業務 stage 分開保存，非法轉移回 409。

| From | To | 條件 |
| --- | --- | --- |
| queued | running/cancelled | running 先取得 lease |
| running | succeeded/failed/paused_budget/waiting_review/cancel_requested | 成功前提交產物及 audit |
| running | queued | 可恢復中斷，保存 interrupted 並失效舊 lease |
| paused_budget | queued/cancelled | 新預算授權或取消 |
| waiting_review | queued/cancelled | 審核決定及 continuation 已保存 |
| cancel_requested | cancelled | 保存外部未決呼叫及取消結果 |
| failed | queued | retryable 且未超 attempts，建立新 attempt |
| succeeded/cancelled | 無 | 新要求需新工作 |

取消後不得排新付費呼叫，已送出請求可能仍計費，晚到結果只作稽核及用量。job success、benchmark frozen、run completed 各自驗證完成條件。

以下為首版可調設計預設，並非已實測能力。

| 項目 | 預設與規則 |
| --- | --- |
| PDF | 上限 512 MiB、2000 頁；500 與 600 頁為必要驗收 |
| 解析 | page_batch_size=10、parallel_batches=2 |
| LLM | parallel_calls=2，與 provider 配額取較低者 |
| 建題 | 每候選原生成 1 次，重新生成至多 2 次 |
| JSON 修復 | 每輸出最多額外 1 次，仍失敗交審核 |
| Transport | 每 logical call 最多 3 次實際 attempt，由唯一 retry owner 管理 |
| Query | top_k=10、tokens=12000、visual_regions=8、deadline=180 秒 |
| Payload | JSON 16 MiB；JSONL 每批至多 100 行及 16 MiB |
| Lease | 60 秒，heartbeat 15 秒，舊 worker 以 fencing 阻擋 |
| 模型輸出 | 每 role 必填 max_output_tokens |
| 成本 | 每建題/評分 job 必填 token 或可估價貨幣上限 |

spent+reserved+next_reservation≤limit 才能送出呼叫，預留與結算以 transaction 管理。outcome_unknown 保留 reservation 待核對。金額使用 decimal、currency、pricing revision。無法預留的付費工作拒絕開始，token 和金額上限分開顯示。

agentKit 現有 retry/default timeout 要顯式對齊 profile。不能表達必要設定時，adapter 應拒絕啟動或用經驗收的擴充，不得靜默忽略設定。

### 16.7 PostgreSQL、pgvector 與 UI 細則

- 凍結 payload 與可變狀態分開，普通應用角色直接改 frozen/audit 必須被阻擋。
- 核心外鍵驗證 project，刪除以 RESTRICT 或受控清理處理，不得 cascade 造成已發布分數失去證據。
- source_matches 保存匹配模式、門檻、coverage、原因與 grader，不能只存命中布林值。
- vector cache 包含完整 space、content hash、document/query task、前處理版本，同文字不同 task 不誤用。
- ready 且被使用的 collection 不原地更換內容，相同 batch/hash 冪等，不同 hash 衝突。
- exact profile 必須驗證實際精確路徑；ANN 保存參數、extension、fallback、shortfall。相同 distance 以穩定 item ID 排序。
- migration 必測既有 benchmark、舊 submission/log 和 reference integrity，不能只測空資料庫。
- 每個 UI 畫面須有 loading、empty、ready、permission_denied、partial/error 狀態。進度來自已提交結果，未知 total 顯示未知。
- 單題畫面須呈現 gold/實際答案、證據組、預算內外 hit、引用、理由及 PDF 高亮，修正來源走新 revision。
- trace 畫面須呈現重試、缺失父節點、ERROR/WARN、node 與原始引用。重新整理觀察同一 job，不建立新工作。

## 17. 必要測試計畫

### 17.1 Fixture 與測試層級

固定測試使用人工核對 golden、固定 schema 及 mock provider 回應。真模型測試另外記 provider、版本、prompt、用量和人工核對結果。

| Fixture | 必備資料 |
| --- | --- |
| F01 | 1/19/20/21/500/600 頁 PDF，包含素材充分及不足版本 |
| F02 | 掃描/文字混合、雙欄、旋轉 90/180/270、不同 CropBox、印刷頁碼、中英文 Unicode |
| F03 | 跨頁表、合併 cell、重複 header、圖例、單位、圖說無法獨立答題的圖表 |
| F04 | 相鄰/遠距/跨章節證據、相似段落不同年份/條件、有效替代來源 |
| F05 | 空/假/損壞/加密/超限 PDF、文件內含指令注入測試字串 |
| F06 | 手算可驗證答案與 hit/citation，錯頁/錯單位/空答案/重複/預算邊界 |
| F07 | 已知向量、同維度異模型、錯維度、零/非有限值、tie、跨專案資料 |
| F08 | 舊資料庫快照、frozen 資料、原始 log/journal/seal、缺失及損壞 session |
| F09 | 兩種切割方法、同步/非同步 adapter、等價 JSONL 與上游錯誤 |

fixture 使用合成或已授權資料，保存授權/來源、hash、腳本或固定檔、expected.json 與人工核對紀錄。大 PDF 不得只複製同一頁。U=unit、C=contract、I=integration、E=end-to-end、F=fault、Q=quality、P=performance、S=access/adversarial、UI=browser。

每個 T 編號內的各子情境必須獨立斷言，負向測試的正確拒絕算通過。自動測試保存 actual/trace，人工測試保存操作者、日期、輸入及判定理由。

### 17.2 文件與題庫

| ID/層級 | 輸入與步驟 | 預期結果 |
| --- | --- | --- |
| T01 C/I | F01 上傳並下載 | bytes hash/頁數一致、版本唯一 |
| T02 C | F05 空/假/損壞/錯 content type | 400/415/422 明確、無可用文件與 LLM 呼叫 |
| T03 C/I | 大小/頁數等於限制與超過 1，加密檔 | 邊界合法可處理，超限及不支援加密明確拒絕 |
| T04 I | 同 bytes 及新 bytes 重送 | 同 artifact 可復用，不同 hash 新版本 |
| T05 U/I | F02 bbox 經預覽與原圖往返 | 座標誤差≤0.005，無頁碼 off-by-one |
| T06 I/Q | F02 掃描及混合頁核對指定事實 | 乾淨 fixture 必要事實全部取得，低品質頁標低信心/失敗 |
| T07 F/I | 第 n 頁故障後恢復 | 成功頁不重跑，失敗頁保留於覆蓋率 |
| T08 I/Q | F03 續表、合併 cell、重複 header | 欄列/單位/數值與每頁 region 正確 |
| T09 I/Q | 必須讀圖題 | vision 收到 crop hash，讀值/圖例對應 golden |
| T10 I/Q | F04 第 18/247 頁必要證據 | 各頁可定位，少任一證據無法完整支持 |
| T11 U/Q | 同詞異年/條件來源 | distractor 被區分，不能當等價來源 |
| T12 U/C | 四部分缺欄/不存在 fact/evidence | schema/FK 拒絕並定位欄位 |
| T13 U/C | 四檔順序打亂、缺 ID、重複 ID | 依 ID 正確聯結，缺/重複明確拒絕 |
| T14 U/I | 頁數 1/19/20/21/500/600 | N=20/20/20/21/500/600，Q=2/2/2/3/50/60 |
| T15 U/I | 同題多 tags、重複題灌配額 | 單題只占一桶，重複不增加 valid count |
| T16 I/Q | 無圖表或素材不足 | needs_review/明確 partial，缺口可見且不混榜 |
| T17 U/I | 跨頁題新增單頁替代證據 | 新 revision 重分類與補配額 |
| T18 Q/I | 生成不存在來源或錯答案 | verifier 拒絕，不可直接 freeze |
| T19 C/I | 無效 JSON、一次修復仍無效 | 最多額外 1 次修復，交審核並保留輸出 |
| T20 Q | 移除每份必要證據驗題 | 配額內跨頁題都有必要性，否則重分類 |
| T21 Q/UI | 抽查放入錯單位/歧義題 | 阻擋 freeze、擴查同群組、保留修訂 |
| T22 I | 重送建題、換方法建立 run | 相同題庫 hash，生成呼叫數不增加 |
| T23 I/C | freeze 後 PATCH、改 prompt/model/grader | 原版本不變，新版本與 cache 失效正確 |
| T24 F | 建到第 n 題後重啟 | 已完成題/驗證復用，未決呼叫可追 |

### 17.3 模型、接入與評分

| ID/層級 | 輸入與步驟 | 預期結果 |
| --- | --- | --- |
| T25 C/I | text client 接 vision | 拒絕或交已宣告 adapter，不誤報已讀圖 |
| T26 C/I | model pin、fallback、embedding mismatch | pin 不偷換，實際模型可追，異 space 拒絕 |
| T27 F/I | 429/503/timeout/認證失敗 | 可重試有上限，不可重試不重送，attempts≤profile |
| T28 I | 兩 worker 競爭最後預算 | reservation 不超限，unknown 保留預算 |
| T29 F/I | 付費呼叫中取消或逾時 | 不排新呼叫，late 僅記錄，未知 usage 非零值冒充 |
| T30 C/E | 兩方法 HTTP/JSONL 準備同 PDF | 自主切割索引，manifest 正確，同題集完成 |
| T31 C | 全 API 正常與錯誤/分頁 | 符合 OpenAPI/schema/status code |
| T32 C/F | async poll、過期下載 URL、late reply | 同 job、更新授權有紀錄、晚到不改正式答案 |
| T33 C/I | 缺 ID/重 rank/無效 citation/錯 JSONL 行 | 每行獨立接受或拒絕，不污染其他題 |
| T34 I | submission 重送/衝突/finalize 競態 | 冪等、一致封存，衝突 409 |
| T35 U/I | 合法 schema 但錯頁/hash，答案正確 | region 不得分，答案獨立評，invalid reason 可見 |
| T36 U | 缺必要區域/不存在引用/整頁 | 缺證據扣分、無效 ID 拒絕、整頁定位分較低 |
| T37 U | G1={a,b}、G2={c}，只命中 a | recall=50/complete=0，命中 a,b 或 c 時皆 100 |
| T38 U | fact weight=1,1，scores=1,0.5 | correctness=75，無預設 partial rubric 不可臨時給部分分 |
| T39 U | 原文檢查/單位換算/無適用 checks | 比例準確、按固定換算，無 checks 為 N/A |
| T40 U | 重 hit/citation、重疊 bbox、預算截斷 | 去重不加分、union 不雙算、預算外不支持 citation |
| T41 U/I | 答對源錯/忠於錯來源/只含 [ID] | 各分數獨立，無引用字串捷徑 |
| T42 U/Q | 全文新增未列 claim、無引用、拒答 | 新事實納分母，拒答答案零，hit 可得證據分 |
| T43 U/I | 漏答/逾時/N/A/grader fault 混合 | 方法錯誤入分母，grader pending 阻擋正式排名 |
| T44 U/I | 同結果 HTTP/JSONL 重評 | 規則分數一致，語意評分快取一致且版本可見 |
| T45 I/UI | 不同題庫/subset/grader/budget 比較 | 差異明列、不可同組排名 |
| T46 I | 多 attempts 後答案更高分 | 第一有效終局固定，重答需新 run |

### 17.4 資料庫、向量與工作流

| ID/層級 | 輸入與步驟 | 預期結果 |
| --- | --- | --- |
| T47 I | 真 PostgreSQL 跨 project FK/重版/缺引用 | 約束拒絕，無 orphan |
| T48 I/F | 20 個同 key 請求及異 body | 一個有效邏輯工作，衝突回 409 |
| T49 I/F | audit/outbox SQL 故障 | 業務變更一併回滾，復原可重送 |
| T50 I | F08 舊 DB 升級並查資料 | frozen/舊 submission/log 可讀，hash/分數不變 |
| T51 I/E | 從分數開 lineage 與 PDF | 必要版本及實際操作可達，無未說明斷鏈 |
| T52 I | 黑箱提交/遮罩/artifact 遺失 | declared_only/partial/unavailable 明確 |
| T53 U/I | 錯 space/維度、NaN、零向量 | 寫入與查詢拒絕、不污染索引 |
| T54 I | batch 失敗重送、ready 前查詢 | 不重複、不公開半成品 |
| T55 I/S | 兩方法/專案/gold collection | 零越權結果 |
| T56 I | 固定小 corpus exact cosine 與手算 | distance 誤差≤1e-6，tie 排序穩定 |
| T57 I/P | exact/HNSW、稀疏 filter、不足 k | 保存 recall/延遲/shortfall/fallback/實際模式 |
| T58 U/I | 未知 node、分支、最大步數 | 非法 graph 拒絕，有限停止，state 可追 |
| T59 U/I | node 失敗 patch、nested state 修改、schema 不符 | 失敗 patch 不發布，原 state 未受污染 |
| T60 I | 人工 gate/budget pause/改 graph revision | 持久等待與 continuation，版本不符不直接恢復 |
| T61 F/I | node DB 已提交、Store.Save 前 kill | 復用 operation，無重複副作用，checkpoint 前進 |
| T62 F/I | lease 到期接手後舊 worker 回寫 | fencing 拒絕舊者，唯一 checkpoint 序列 |
| T63 F/I | 外部成功但回應或 DB 結果遺失 | outcome_unknown，retry attempt/用量分開 |
| T64 U/I | checkpoint round trip int/float/nested refs、DB 斷線 | 型別驗證與 state hash 穩定，可從提交點恢復 |

### 17.5 Log、權限、UI 與維運

| ID/層級 | 輸入與步驟 | 預期結果 |
| --- | --- | --- |
| T65 I | API→worker→router→node→grader | trace/operation/run/question 可串接，provider ID 有則保存 |
| T66 I/F | begin 無 end、缺 seal、原 log 改一字 | incomplete/驗證失敗可見，無假完成 |
| T67 I/F | collector 停止/滿 buffer/重送亂序 | audit 保存，缺口/丟棄數可查，去重可重建階層 |
| T68 I/UI | trace/run/question/level/time 過濾匯出 | 正確分頁，匯出帶 schema/manifest |
| T69 S | 測試密鑰/Authorization/URL token | 普通 log/export 無密鑰，原 artifact 不越權 |
| T70 S/C | 所有角色逐端點及跨 project ID | 符合權限矩陣，error/artifact/gold 無洩漏 |
| T71 S/I | 普通帳號修改 frozen/audit、受控刪除 | 非法更新拒絕，合法刪除有 tombstone/audit |
| T72 S/E | 指令注入 PDF/答案、未授權 endpoint、CSV 公式 | 內容不改評分/權限，不連非法 endpoint，公式字串妥善處理 |
| T73 UI/E | 上傳→建題→審核→freeze→方法→run | 真正完成流程，重整不建新 job，進度與後端一致 |
| T74 UI/E | 比較/單題高亮/替代來源/匯出 | 座標正確，新來源走新 revision，檔案與 manifest 相符 |
| T75 UI/E | trace tree/node/錯誤/等待/取消 | 真實 trace 關聯，缺口及重試可辨 |
| T76 UI | loading/empty/permission/error/partial | 狀態可重現且無假成功資料 |
| T77 C/I | 非法狀態、cursor 換 filter、未知欄/超限 | 409/422/413 正確，無非法變更 |
| T78 I | 四部分與結果匯出再匯入驗證 | ID 集合/hash/版本正確，資料完整且權限一致 |
| T79 F/I | 隔離環境備份還原 DB+artifact | 原 run 血緣及結果可重建，RPO/RTO 實測記錄 |
| T80 F/I | staging 中斷與 debug 到期 | orphan 正確回收，活躍證據/audit/manifest 不誤刪 |
| T81 E/P | 500/600 頁、兩方法、重啟一次 | 題數配額正確、全題終局、可追原頁且達資源門檻 |
| T82 P/I | 大 log/vector 資料及併發查詢 | 保存 p50/p95/RSS/connections/queue lag，達 17.7 |
| T83 C/I | 固定 agentKit、清潔 build、真 DB/pgvector | 工具鏈、migration、套件和 adapter 測試通過 |
| T84 E/Q | 真 chat/embedding/vision smoke 與 judge 校準 | 真請求用量可追、人工確認有效性、不以 mock 代替 |

### 17.6 模型品質與測試 Oracle

Unit/contract/fault 使用固定回應或可控制 fake server，斷言呼叫數、預算和 DB 結果。PostgreSQL/pgvector 整合必須用真資料庫，禁止 SQLite/MemoryVectorStore 代替。

生成策略變更後至少人工審查 20 題，一般/跨頁/圖表/易混淆各至少 5 題。生成器同輪自評不能當唯一 oracle。使用版本化 fixture，不能每次叫模型產生不同「標準答案」來測自己。

judge calibration 至少 40 個人工標記回答，特殊題型各至少 8 個，涵蓋全對、部分、錯、錯來源、拒答、額外矛盾。兩名標註者獨立評分，分歧先協議 gold。首版目標為 fact label 一致率≥90%、重大矛盾誤判全對=0、確定性來源/規則評分與 golden 100% 一致，未達阻擋該 grader revision。

固定 judge input 重跑 3 次，保存差異，分差超過 10 分者進人工核對。正式評分固定首次符合政策的結果與快取，禁止重抽至高分。調模型/rubric 建新 grader revision，全部比較對象一致重評。

真 provider smoke 至少涵蓋 chat、embedding、vision 各一能力，可共用 provider。每個部署宣告啟用的 provider/protocol 均需 contract 與真 smoke。缺 credentials 記 blocked，不能算通過。

### 17.7 大文件、效能與限制

初始驗收環境為 8 vCPU、16 GiB RAM、SSD、遠端模型。以下為本次提出的可版本化目標，尚非已量測成果。硬體不同可在測試前建立 performance profile，保留硬體、資料與門檻差異。

- 500/600 頁各跑一次 mock-provider 完整流程，使用真 parser/OCR、DB、索引、scheduler、評分與兩種方法；至少 50 頁包含掃描或圖表，且有遠距證據。
- LAB 全部服務含 DB 的 RSS 峰值合計≤12 GiB，不包含外部方法與遠端模型，分元件另列。
- 500 頁 mock-provider 流程在 60 分鐘內完成，掃描 150 DPI，固定 OCR 設定。人工等待時間獨立列出，不納入執行耗時。
- 100 萬筆 log、10 萬筆固定 768 維向量、10 個讀取 client 持續 10 分鐘，metadata/log indexed query p95≤1 秒，圖像/LLM/匯出另列。
- 內建 HNSW 參考 profile 對至少 200 個 query 的 recall@10≥0.95，exact/ANN 固定 corpus/filter/tie 規則。此門檻只約束內建參考 profile，使用者方法表現如實報分。
- 取消後 5 秒內停止排新工作，已送出外部呼叫另外結算。collector 補送進度可查。
- 真模型大文件執行保存耗時及成本，不套 mock 的 60 分鐘要求，仍須遵守 deadline、預算及恢復規則。

不可測完才降低門檻，也不可因硬體降低題數、配額、資料正確性與隔離要求。

## 18. CI、證據與發布門檻

### 18.1 測試執行時機

| 時機 | 必須執行 |
| --- | --- |
| 每個 PR | schema/OpenAPI、lint/type/build、deterministic unit/contract、受影響 integration、bug 回歸 |
| main 合併前 | 真 DB/pgvector、worker/adapter、權限、checkpoint/交易故障、UI smoke，綁定候選 commit |
| Release candidate | T01–T104 全部必要子情境、500/600 頁、效能、還原、真 provider、真 parser、多模態人工校準 |
| parser/model/prompt/grader 改版 | golden/品質測試與新 revision |
| DB/pgvector/agentKit 升級 | migration、舊資料、vector、state/resume、原始 log 相容性 |

狀態只可為 passed、failed、blocked、skipped，必要測試 blocked/skipped 也阻擋正式發布。失敗重跑保留首次失敗與修正 commit，不得靜默忽略不穩定案例。

### 18.2 測試證據與追溯映射

每筆 report 必含 test_id、R/A IDs、commit、schema/依賴/agentKit 版本、fixture hashes、environment/profile/seed、開始結束時間、步驟、expected/actual/status、trace/artifact refs、CI job 或執行人。每個 R 至少有 positive 與 negative 測試。A 對應最低測試如下。

| A | T | A | T |
| --- | --- | --- | --- |
| A01 | T14,T81 | A22 | T49 |
| A02 | T14,T16 | A23 | T63 |
| A03 | T16 | A24 | T65 |
| A04 | T30 | A25 | T23,T71 |
| A05 | T10,T37 | A26 | T68,T78 |
| A06 | T17,T37 | A27 | T67 |
| A07 | T11,T41 | A28 | T69,T70 |
| A08 | T08,T09,T39 | A29 | T79 |
| A09 | T35,T41 | A30 | T80 |
| A10 | T36,T40 | A31 | T80 |
| A11 | T22 | A32 | T22,T28 |
| A12 | T22,T30 | A33 | T61,T64 |
| A13 | T44 | A34 | T62 |
| A14 | T07,T24 | A35 | T58,T60 |
| A15 | T34,T48 | A36 | T53 |
| A16 | T43 | A37 | T54,T55 |
| A17 | T43 | A38 | T56,T57 |
| A18 | T70 | A39 | T19,T25 |
| A19 | T35,T36 | A40 | T66,T67 |
| A20 | T23,T74 | A41 | T41 |
| A21 | T51,T52 | A42 | T83,T84 |

### 18.3 Definition of Done

正式完成需同時滿足以下條件。

1. R01–R30 全部必要項完成，無 MUST NOT 違反，A01–A48 均有可重現證據。
2. T01–T104 及必要子情境通過，必要項無 blocked/skipped；選配功能適用範圍依第 19.8 節明列。
3. 同一 PDF/凍結題庫可由兩個不同方法完成準備、答題、溯源、評分及比較。
4. PostgreSQL/pgvector、checkpoint、trace、權限與還原均經真整合測試。
5. 每個分數可從 UI/API 回到方法輸出、gold、原頁及實際操作版本。
6. 交付物可由另一開發者於乾淨環境依文件啟動驗證。
7. 報告明確區分 mock、真 provider、人工品質及效能量測，揭露仍存限制。

本文件列出測試不代表測試已執行。僅具上述證據的 passed 紀錄才算通過。

## 19. 多模態文件解析、Unstructured／MinerU 與跨頁圖文驗證

### 19.1 研究結論與適用邊界

本節於 2026-11-05 依官方文件及公開程式碼研究新增。工具名稱為 Unstructured 與 MinerU。本次未安裝、執行或比較兩套工具；以下為可行的整合設計，並非已驗證的準確率或硬體效能。官方文件會更新，正式實作 MUST 鎖定套件／服務版本、模型與輸出 schema，保存文件快照 hash，不能使用浮動 latest 作為可重現依據。

本節細化第 4、5、12、15–18 節；多模態處理有差異時以本節為準。既有題數與三類各至少 10% 配額不變。完整規格現在包含 R01–R30、T01–T104、A01–A48。

| 研究對象 | 已核對的官方能力 | LAB 可採用部分 | 必須補足與禁止推論 |
| --- | --- | --- | --- |
| Unstructured OSS partition | PDF 有 auto／fast／hi_res／ocr_only 路徑，可抽取文件元素；hi_res 可搭配表格結構與圖片區塊抽取 [U1,U2] | 可替換版面／OCR／表格解析 adapter；保留 Table HTML、圖像、元素與頁面資訊 | fast 或 OCR 文字不等於圖表讀值；不得把 API 的 vlm 參數直接當 OSS partition_pdf 能力 |
| Unstructured chunking | 切割可由元素組合，輸出 CompositeElement／Table／TableChunk；orig_elements 可追原元素與座標 [U3] | 原元素先落地，再切割；建立 chunk→elements→regions 映射 | 超大元素仍可能被文字切割；TableChunk 不是已驗證的語意完整表格 |
| Unstructured API／enrichment | API 有獨立策略；table description 會改寫 text，分割後 TableChunk 不一定帶 image_base64 [U4,U5] | 經專用 API adapter 選配視覺解析／描述，原始抽取先保存 | 摘要是衍生資料，不得覆蓋 LAB raw_text 或作唯一 gold；必須區分 OSS／Pipelines／legacy endpoint |
| MinerU 研究時的 4.x 文件 | flash／basic／standard／advanced 品質檔位，與小模型 backend、VLM engine 分開設定 [M1] | 候選主解析器，依文件與硬體選擇經驗收的 profile | 不沿用 3.x pipeline／vlm／hybrid 參數假裝相容；tier 名稱不代表 QA 正確率 |
| MinerU 結構化輸出 | 有 Middle JSON、Structured Content、圖片資產；page_idx 是原始零起算索引，CLI 選頁另有約定 [M2] | 保存結構與原始資產，映射至 LAB 的 SourceRegion | 不只保存 Markdown；不能假定所有入口具有相同輸出或座標欄位 |
| MinerU 跨頁後處理 | 文件記載可選 LLM 標題層級與跨頁表邊界 cell 續接；續接先依規則識別續表，功能預設關閉；標題層級要求完整文件 [M3] | 經測試啟用續表候選／cell 續接，外部 LLM 費用納入總預算 | 非任意跨章節語意理解保證；分批結果不能冒稱完整文件而繞過前提 |

設計決策：首版 MUST 提供 UnstructuredParser 與 MinerUParser 兩個可替換的解析 adapter，並有共同輸出契約。部署可只啟用一個經測試的主 profile，另一個用於審核或對照，不要求每頁雙解析。parser 是系統建題的證據生產元件，參賽方法仍可採用自己的任何工具。這兩個 adapter 名稱為 LAB 待實作元件，非上游現成 API。

不能保證兩個模型一致就正確。重點是原始區域、必要資訊與人工／確定性核對。圖表趨勢、示意圖箭頭、照片細節及跨頁解釋，皆需 LAB 的 VisualReader、ContextLinker、EvidenceVerifier 補足。此處 VisualReader 延伸既有 VisionReader 契約，實作可共用一個 adapter，禁止產生兩套無法追溯的重複服務。

### 19.2 分層證據與能力契約

MUST 分開保存原始觀察、結構與衍生推論，不能將 VLM 敘述回寫成原文。

| 層級 | 必存內容 | 可用範圍 |
| --- | --- | --- |
| L0 原始資產 | 原 PDF hash、頁面 render、圖／表 crop、render 與 crop transform、資產 hash | 最終核對與原頁定位 |
| L1 原始抽取 | OCR 字串、原生 PDF 文字、版面 blocks、原始 HTML／JSON、reading order、工具診斷 | 保留原樣，錯誤可追；不得標成已驗證事實 |
| L2 結構化觀察 | 表格 cell、圖表軸／圖例／series／值、圖像物件與可見關係、對應 regions、核對狀態 | 通過驗證後可支持 gold |
| L3 衍生內容 | 圖文摘要、計算、比較、跨頁關係與解釋；每個 claim 的 L0–L2 依據 | 搜尋路由／建題，驗證前不作標準答案 |

ParseManifest MUST 包含 parser_name、package_or_service_version、adapter_revision、output_schema、model revisions／checksums（可取得時）、requested_profile、effective_profile、OCR language、page selection、page_index_map、render settings、postprocess flags、endpoint deployment ID、開始／結束時間、成本與 warning。上游未揭露的版本記 unknown，禁止補造；需要固定版本卻無法確定者不得進可重現正式 profile。

CapabilityManifest MUST 宣告 layout、ocr、table_structure、cell_geometry、image_crops、chart_reading、cross_page_table、full_document_postprocess 各為 supported／unsupported／unknown，並列限制和 fixture 證據。接受未知能力作試驗不等於通過正式驗收。不能由「有 OCR」推導「能讀圖」。

每次 parse 請求輸入 document artifact ref、原始頁面索引集合、profile revision、budget ref、operation ID。輸出 manifest ref、raw result refs、normalized evidence refs、逐頁 status／diagnostics。API 走第 8 節權限、冪等與錯誤規則；首次實作 MUST 提供版本化 internal parser contract/schema，禁止直接把上游 JSON 暴露為 LAB 核心契約。

Unstructured adapter MUST 保存原始 elements 和 orig_elements，適用時保存 text_as_html 與圖像 assets；API 和 OSS 參數各自驗證。若策略 fallback，保存 requested／effective 與原因，必要能力消失時返回 needs_review 或失敗，不可靜默通過。

MinerU adapter MUST 按實際 schema/version 解析，不手改版本號冒充 migration；保存頁面映射、原始 Middle JSON／Structured Content、圖片引用和執行 tier。若該版沒有 cell bbox，cell_geometry=unsupported，可保留整表 region，但不能虛構細格定位。兩個 adapter 均須驗證座標空間；轉回第 4.1 節 CropBox／旋轉後空間。新輸出 schema 先 quarantine，通過 contract 測試後才入庫。

### 19.3 圖表、表格、圖片的必要處理

1. MUST 先辨識頁面與視覺區塊，再擷取完整圖表 crop。保留含圖說、軸標籤、圖例的 context crop，必要時包含整頁；嵌入式圖片抽取不保證包含 PDF 向量線條和外部標籤，因此頁面 render 裁切為必要回退路徑。
2. MUST 為每個 visual object 建立穩定 visual_object_id、source regions、asset hashes、子圖／parent 關係、caption links、linked text refs、extract／verify 狀態與診斷。未檢出區塊的人工補框也保存 revision 與操作人。
3. 表格 MUST 保存 row／column index、rowspan／colspan、header path、cell raw value、parsed value、unit、null／missing／unreadable 的差別與 cell／table region。重複 header 只能在衍生視圖去重；原頁與原格保留。單位來自表外說明時須引用該文字。
4. 數值圖表 MUST 保存 chart type、axis label／scale（linear／log／其他或 unknown）、series 與圖例對應、x/y value、單位、是否估讀、讀值區間／容許誤差及支持區域。雙軸、百分比／百分點、千／百萬倍率、截斷軸及誤差棒必須可測。
5. 值來源分 observed_label、estimated_from_plot、derived_calculation。估讀不得偽裝精確原文；tolerance 必須於 freeze 前依 fixture／圖像解析度或人工 rubric 設定，禁止看參賽答案後放寬。無法界定誤差時，不出精確數值題；可改為經驗證的趨勢題或 needs_review。
6. 流程圖／架構圖 MUST 區分物件、文字、箭頭方向與可見關係；照片／影像只能陳述可核對的可見內容。不能僅憑外觀推定身份、不可見數據或因果。文件文字聲稱的因果與模型推論分開標示並引用。
7. MUST 比較 OCR／原生文字／表格結構／VLM 觀察的必要事實；衝突保留各版本並進 needs_review。沒有工具 confidence 時記 null，不用模型自報 confidence 替代驗證。
8. crop 太小、模糊或缺標籤時，僅對該資產有限升級 DPI／擴框／另一 profile。預設第一次 150 DPI，最多一次 300 DPI 升級，每次實際像素、裁切與 hash 分開保存；超過像素或成本限制則審核，不無限重試。

本次新增的可調整設計預設：單次 vision 總像素上限 16 MP，crop 最長邊 4096 px。上游限制更小則採更小值，必要時以有 transform 的 tiles 分段；不得靜默縮圖使必要細節消失。這些不是工具官方保證，變更須版本化並重測，與既有 visual8／token／費用上限共同限制。

### 19.4 跨頁與跨文本的證據組裝

ContextLinker MUST 使用明確圖號／表號、章節、實體、年份、條件與單位找候選，再以原始區域核對。embedding 可找候選，不能單獨確立關係。新增 caption_of、explains、qualifies、depicts、derived_from、contradicts 與既有 continues／refers_to／table_continuation 等關係並存。

每條 EvidenceRelation 保存 relation_id、type、from/to evidence refs、candidate reason、支持／反駁 regions、linker revision、validation_status、reviewer decision。conflicting、unresolved 與 verified 分開，不以最高 similarity 強行選一條。

- 跨頁續表需核對表號／續表標記、欄位 header、單位、語境及邊界行。相同欄名不足以合併；相同模板不同年度為必要負例。保留每格來源及原邊界，不能把跨頁拼圖當單頁原件。
- 分批解析可預設帶前後各一頁 overlap 作候選 context，結果依原頁／資產身份去重。MUST 有批次後的全文件關聯階段，處理遠距圖文與批次邊界，不能只靠相鄰 overlap。重複 context 呼叫仍計成本。
- 完整文件限定的上游後處理，只能對其接受且驗證的完整輸入執行；否則用 LAB 自己的全文件 linker，不能偽設 is_full_document。大文件維持 manifest／refs，不把所有頁面塞進一個 LLM prompt。
- 一張圖可能被多段互相限制的文字解釋；必要證據組要同時包含圖、單位／定義、條件或例外。問題需明確年份／實體／適用條件，不能把文件本來就無法消除的歧義當干擾題。
- 本版跨文本指同一 PDF 的跨段／跨章／遠距圖文。跨 PDF 仍依第 2 節為後續功能。

### 19.5 建題、驗題與公平評分

保留四部分 benchmark，不另建第五份無法聯結的答案。新增欄位置於版本化四部分 schema：問題集含 modality_requirements、reasoning_type、visual_object_refs；文意答案含 required claims、計算式／operands、條件；文字答案含 observed text checks 與 numeric_checks；來源答案含 visual regions、必要文字 regions、支持 claim IDs 和 evidence sets。無適用原文字串時 text checks=[]，不是造一段「OCR 原文」。

reasoning_type 至少支援 value_lookup、trend、comparison、calculation、diagram_relation、image_text_integration、cross_page_integration。跨頁且讀圖的題可有多個分析 tags，但既有三個配額桶仍按原規則獨立分配，不重複灌配額。

**所有計入圖表配額的題 MUST 逐題核對原圖**，並執行移除視覺證據的必要性測試。只看 caption／周邊文字即可回答者不計讀圖配額。圖文整合題也逐一移除必要文字／圖像，若仍可完整支持需調整證據組或題型。不能僅因生成 request 帶圖就判定題目測到了圖表能力。

圖表配額內至少涵蓋 3 種不同 reasoning_type；若 Q<3 則至少 min(3,Q) 種。素材足夠時，至少 1 題需必要圖文整合、1 題需必要跨頁圖文；可為同題，不增加既有跨頁／讀圖配額要求，也不更改 N。素材缺乏按既有缺口流程公開，不能硬造題。

高風險 gold MUST 人工核對：estimated 數值、跨頁 cell 合併、衝突、雙軸／log、圖文條件整合及因果類敘述。其餘視覺題至少有獨立 verifier 的原圖核對紀錄，並納入既有人工抽查；同一模型同輪自評不是獨立證據。verifier 可共用 provider，但須獨立呼叫／輸入與紀錄，人工 gate 不能被 confidence 跳過。

numeric_checks 定義 expected value／interval、unit、abs_tol／rel_tol、precision 與確定性 unit conversion。單點條件為 abs(actual-expected)≤max(abs_tol,rel_tol×abs(expected))；interval 使用凍結的 [low,high]，不得再重複擴大容許範圍。單位轉換先做，unsupported／錯單位不得僅因數字相近判對。衍生運算保存公式 AST／受限運算符與 operands，不執行模型生成程式碼。

既有答案正確／來源／faithfulness 分數保持獨立。新增診斷呈現 numeric_accuracy、unit_accuracy、relation_accuracy、visual_evidence_recall、required_context_completeness，使用固定 checks／claims 分子分母及 N/A 規則，不默默改總分權重。圖像語意定位需支持區域核對，不能只用 IoU 或 CLIP 相似度判讀值正確。

參賽者只取得原 PDF 及明確允許的公開資產，不得收到系統的已驗證圖表數值、關係、gold crops 選取提示或建題摘要。若研究另外提供統一解析結果，必須建立 shared_parse_track，與 raw_pdf_track 分榜；前者測搜尋／答題，不能聲稱獨立驗證參賽解析品質。所有方法相同 track／題庫／budget 才可正式比較。

### 19.6 工作流、PostgreSQL、pgvector 與快取

DocumentPreparation 細化為 parse_batch → normalize_geometry → extract_visual_assets → read_visual_objects → link_document_context → verify_evidence → publish_manifest。這是 LAB graph node 設計；頁面／資產平行由 scheduler 管理，沿用既有 agentKit 持久化與冪等要求。parse 或視覺失敗產生 partial／needs_review，不能直接導向 freeze。

MUST 新增或以等效正規化資料表實作下列實體，全部具 project FK、revision／hash 與操作 trace；大型 payload 放 artifact，常查身份／狀態不能只藏 JSON。

| 實體 | 必要欄位／關係 |
| --- | --- |
| parse_runs | document_revision、parser/profile/schema/model、raw artifacts、page map、effective strategy、成本、status |
| visual_objects | evidence refs、type、parent、regions、crop/context assets、geometry transforms、status |
| visual_observations | object revision、L2 data ref、observation_type、confidence nullable、verification、producer operation |
| evidence_relations | 端點 evidence、type、支持／反駁 regions、validation、revision |
| evidence_verifications | checked claims／regions、oracle、結果、理由、操作者、時間、原始圖 refs |
| derivations | inputs、受限公式／relation、output claims、模型／prompt、trace；不取代原文 |

SourceRegion 保持共通座標；推論與觀察 ID 不能取代原 PDF region。發布時檢查外鍵、資產存在與 hash，缺必要圖像不得標 verified。UI MUST 同時展示原頁／crop、OCR、表格／圖表結構、衍生敘述及核對差異。

pgvector MAY 存 visual caption／表格摘要及跨模態 image embeddings。不同 modality／模型／維度／task 分 collection/space，image vectors 不可與無對齊能力的 text vectors 直接比較。使用多路搜尋須保存各路 rank、融合版本和來源映射；摘要命中後仍取原始資產。首版 MUST 保留圖像資產與可搜尋文字描述，但 image embedding 並非首版必要，不能為此強迫所有方法用同一模型。

快取鍵 MUST 加入 parser output schema、模型／權重、requested/effective profile、render/crop transform、圖片 bytes hash、vision prompt、postprocess flags、linker/verifier revision 與 context refs hash。只改 verifier 不重跑未變的 parser；必要證據／數值變動則新 evidence／benchmark revision，舊題庫仍可重用且不原地改 gold。重啟不重付已完成視覺呼叫；外部結果不明沿用 outcome_unknown。

部署預設採按需單解析＋高風險升級，避免 500 頁一律雙解析或一律最高 tier。所有工具內部額外 LLM 呼叫 MUST 可控、計入 reservation／trace；不能控管用量的 feature 預設關閉。高階本地 VLM 的 CPU/RAM/VRAM 與耗時另建立真實 profile，不能沿用第 17.7 節 mock／遠端模型數字宣稱合格。較小主機可用遠端 parser／vision service；跨服務仍受權限與成本限制。套件、模型、服務授權及資料外傳設定按實際鎖版記錄，不能將不同 MinerU 版本一律宣稱同一授權。

### 19.7 新增強制需求與驗收

| R | 必須有 | 禁止有 | 測試 |
| --- | --- | --- | --- |
| R26 | 雙 parser adapter、能力／版本／策略 manifest、共同契約 | OSS/API 參數混用、schema 漂移靜默接受 | T85–T88,T101,T104 |
| R27 | L0–L3、原圖、細節／單位／估讀區間、核對 gate | 摘要覆蓋原文、估讀冒充精確值、捏造 cell bbox | T89–T93,T100 |
| R28 | 批次邊界、全文件圖文 linker、關係證據及反例 | 相似即合併、分批冒充完整輸入、未證因果 | T94–T97 |
| R29 | 真讀圖題、必要圖文／跨頁驗證、固定數值 rubric、公平 track | 帶圖即讀圖、參賽看到 gold、事後放寬容差 | T98–T100,T103 |
| R30 | DB 血緣／快取／用量、原圖 UI、真 parser 驗收 | 模型結論當唯一 gold、隱藏額外付費呼叫 | T101–T104 |

| A | 必須達成 | 最低證據 |
| --- | --- | --- |
| A43 | Unstructured／MinerU 均有固定版 adapter 與真解析證據 | T85–T88,T104 |
| A44 | 原始圖像／文字／結構／推論可分開追溯，必要細節可核對 | T89–T93,T100 |
| A45 | 跨批次與遠距圖文連結正確，相似反例不誤合 | T94–T97 |
| A46 | 圖表與跨頁圖文題確有必要性，數值／單位計分一致 | T98–T100 |
| A47 | Gold 隔離、modalities／track 明確、無摘要資料洩漏 | T102,T103 |
| A48 | 真 parser＋vision／DB 流程及資源用量可重現 | T101,T102,T104 |

### 19.8 新增 Fixtures、測試與發布門檻

F10 MUST 含人工核對的柱／線／散點、雙軸、log、倍率、誤差棒、低解析度、多子圖、圖例缺失、向量繪圖、照片與流程圖。F11 MUST 含跨批次續表、跨頁斷裂 cell、合併 header、同模板異年份、重複圖號跨章節、遠距條件／例外。F12 MUST 含兩工具鎖版原始輸出、enrichment 前後文字、缺座標／缺資產、未知 schema 與受控 parser 失敗。各具原 PDF、來源 regions、人工值／區間／關係／反例與授權記錄，不能由待測模型自己產生 golden。

| T／層級 | 輸入與步驟 | 預期結果 |
| --- | --- | --- |
| T85 C/I | Unstructured OSS 真 hi_res，表格／圖片／文字混合 PDF | elements／HTML／可用 crops 保存；來源映射可回原頁 |
| T86 C/I/F | OSS 傳 API 專屬策略、缺模型導致 fallback | 不合法契約拒絕；有效降級被記錄，缺必要能力不發布 verified |
| T87 C/I | Unstructured chunk／TableChunk／description 前後對照 | orig_elements 可回源；raw_text 不變，無 chunk image 時可找 L0 |
| T88 C/I | MinerU 鎖版真解析／選頁／schema roundtrip | tier／model／頁面映射／素材正確；3.x 或未知 schema 不假兼容 |
| T89 I/Q | F10 向量圖、圖例在圖外、多子圖 | crop 不漏必要標籤，parent/subfigure 及 transform 正確 |
| T90 I/Q | 雙軸／log／倍率／百分點 | series／axis／unit 與人工 golden 相符，不混換算 |
| T91 U/I/Q | 可估讀／模糊／數值不可辨版本 | 保存區間／估讀身份；不可辨 null＋審核，不補造精確值 |
| T92 I/Q | 跨列 cell／空白／missing／unreadable／無 cell bbox | 區別保留；無幾何不捏造，定位能力如實標示 |
| T93 I/Q | 流程箭頭反向、照片隱含資訊、文字聲稱因果 | 只支持可核對主張，箭頭方向正確，推論不冒充觀察 |
| T94 I/Q | F11 表格跨第 10/11 頁 batch 邊界及斷裂行 | 正確續接並保留每格原頁，overlap 去重 |
| T95 I/Q | 同欄名／同圖號但不同年／章節／單位 | 不誤合，衝突或歧義進審核 |
| T96 I/Q | 第 18 頁圖＋247 頁條件＋附錄定義 | 全文件 linker 找必要組，逐證據可定位，無近鄰偏限 |
| T97 C/I | 選頁／分批輸入啟用完整文件限定處理 | 跳過或明確拒絕限定功能，LAB 關聯另可執行並記錄 |
| T98 Q/I | caption-only 題與真正圖文整合題做 evidence ablation | caption-only 不計圖表；缺必要圖／文字不能宣稱完整支持 |
| T99 U/Q | numeric golden、換算、容差上下界與相對誤差零值 | 公式邊界正確，錯單位失敗；gold rubric 凍結不可改 |
| T100 Q/UI | OCR/VLM 衝突、估讀、雙軸及跨頁合格／錯誤案例 | 高風險均有人審；未解決不可 freeze，UI 顯示原圖及差異 |
| T101 I/F | 換 crop／模型／linker／verifier、服務中斷與恢復 | 正確 cache 失效與復用，無新 run 重建題庫或重付已完成操作 |
| T102 I/S | visual lineage、缺 asset、異模型 image/text vector、雙專案 | 必要資產缺失阻擋發布、空間不誤比／越權，原頁血緣完整 |
| T103 C/E/S | raw_pdf_track／shared_parse_track 對照與參賽下載 | 原始 track 無 gold 衍生洩漏，不同 track 不同榜，比較限制可見 |
| T104 E/P/Q | 兩工具分別跑真 parser＋既有真 vision smoke；500頁按需流程 | 固定版、人工 golden 與 trace 有證據；保存頁／圖覆蓋、成本、RSS、VRAM、耗時，符合預先登錄 profile |

測試 R/A 對應以本節表為準，T85–T104 也須有第 18.2 節完整報告欄位。兩工具的 adapter tests／新增確定性 unit-contract 在 PR 執行，真 parser 整合在 adapter 變更及 release candidate 執行；圖表／跨頁品質及 500 頁 resource profile 在 release candidate 或 profile 變更執行。啟用的 API／enrichment／LLM 續表 feature 各有 contract、真 smoke、用量紀錄，未啟用選配的 feature 明列不在本次驗收範圍，不冒稱已支援。共同必要項 blocked／skipped 一律阻擋發布。

首次兩套 parser 驗收 MUST 各至少 20 頁不重複的多模態 golden fixture，表格／圖表／掃描／跨頁案例均涵蓋。預先列為可讀的必要 facts、單位與關係須全部正確或被明確 gate 為未通過，不容許錯誤 gold 被發布；gate 不能用來規避覆蓋，fixture 中指定的可讀 cases 必須產生合格證據。這是 LAB 初始測試目標，並非工具宣稱準確率。額外報告 cell exact match、數值／單位正確率、link precision/recall、人工介入率與未解析率，不能只報 OCR 字元率。

模型品質校準維持第 17.6 節，新增至少 12 個人工標記圖文回答（可包含於原 40 個），其中跨頁圖文至少 4 個，覆蓋錯值、錯軸、錯單位、錯條件、源錯與估讀容差。所有新增 test／gate 通過才能達本版 DoD；研究本身不等於已通過驗收。

### 19.9 官方研究來源

查閱日期為 2026-11-05。以下動態頁僅作本次能力研究依據，部署鎖版後須保存對應版本文件／原始碼與 hash。章節中未標為工具現有能力的 MUST、預設與門檻均屬 LAB 設計要求。

- [U1 Unstructured OSS partitioning](https://docs.unstructured.io/open-source/core-functionality/partitioning)
- [U2 Unstructured strategy 原始碼](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/strategies.py)
- [U3 Unstructured chunking／orig_elements](https://docs.unstructured.io/open-source/core-functionality/chunking)
- [U4 Unstructured API partition strategies，legacy endpoint 範圍](https://docs.unstructured.io/api-reference/legacy-api/partition/partitioning)
- [U5 Unstructured table descriptions 與 text 覆寫限制](https://docs.unstructured.io/concepts/enriching/table-descriptions)
- [M1 MinerU tiers／runtimes／資源限制](https://opendatalab.github.io/MinerU/usage/tiers/)
- [M2 MinerU 輸出格式／Middle JSON／頁面幾何](https://opendatalab.github.io/MinerU/zh/reference/output_files/)
- [M3 MinerU LLM 後處理與完整文件前提](https://opendatalab.github.io/MinerU/usage/quick_usage/)
- [M4 MinerU 官方 README 與授權入口](https://github.com/opendatalab/MinerU/blob/master/README.md)
