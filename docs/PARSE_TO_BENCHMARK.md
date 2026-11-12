# 解析結果如何接到出題與凍結：設計備忘（只有文件，沒有程式）

Refs #7、#10、#11、#12。這份文件是 PM-DECISION（Issue #7，00:55Z）清單 (c) 的產出：只寫設計與待決事項。**文中標示「提議」的東西都沒有實作**；標示「已存在」的東西在 main 上並經程式碼確認。真實的出題、獨立驗題與 judge 仍然被 LLM 憑證擋住（見 `docs/LLM_SMOKE.md`），這裡沒有任何一項宣稱完成。

## 1. 已存在（main 上）

| 元件 | 位置 | 狀態 |
|---|---|---|
| 上傳與頁面幾何（L0） | `documents`、`document_versions`、`pages`（0002）；`POST /api/v1/documents` | 可用；每個 document version 有 `pages` 列（頁面大小、旋轉） |
| 解析 job 與消費者 | `parse_jobs`（0011）、`POST /documents/{id}/parse`、`lab_api.parse_worker`、`parse_worker_main`、`lab_parser.batch_parse` | 可用；真實 unstructured fast，已有 500／600 頁量測（#123） |
| 解析結果 | `parse_results`、`parse_page_results`（0012，append-only）、`GET .../parse-results/{job_id}[/pages/{n}]` | 可用；每頁 status、diagnostics、evidence（契約 `EvidenceUnit`，含 `regions[]`）、observations |
| 證據表（出題用） | `evidence_units`、`evidence_regions`（0002）：`evidence_key`、`raw_content`、`normalized_content`、`extractor_revision`、`ocr_confidence`；region 有 `page_index`、bbox（`x0<x1`、`y0<y1`、0..1）、`kind`、`quote`，外鍵到 `pages` | 表與約束存在，**解析路徑沒有寫入它**（目前只有測試 fixture 寫入） |
| 題庫表 | `benchmark_revisions`（狀態機：draft → parsing → planning → generating → validating → ready_to_freeze → frozen；凍結後不可改）、`questions`、`facts`、`evidence_sets`、`evidence_set_members`、`distractors`（0002） | 表、狀態機與凍結不可變存在；`GET /api/v1/benchmarks/{logical_id}/revisions/{revision}[/questions]` 是**唯讀** API，沒有建立題庫的 API |
| 多模態解析與證據關係表 | `parse_runs`、`visual_objects`、`evidence_relations`、`evidence_verifications`、`derivations`（0005） | 表存在，**解析路徑沒有使用**（見 D2） |
| 題數與配額規劃 | `lab_contracts.planning`（#52 的確定性草稿規劃器，已合併）、`lab_parser.batch_coverage`（頁面批次與覆蓋計算） | 純函式 |
| 快取身份契約 | #59 | 草稿、與 main 衝突（DIRTY），未合併 |
| 凍結閘門 | #79（草稿；migration 0013） | 草稿：目前的 schema 無法驗證獨立驗題、人工審查與配額，所以**任何凍結一律失敗（fail closed）** |
| LLM 呼叫骨架 | `services/worker/agentflow`（Go，`ChatCall`＋預算帳本）、`smoke_real_test.go` | 沒有真實 provider 呼叫的證據（blocked: no credentials） |

## 2. 缺口（解析結果 → 題庫）

1. **G1 形狀轉換**：`parse_page_results.evidence` 的 JSON 與 `evidence_units`／`evidence_regions` 的欄位大致對得上（`evidence_id` → `evidence_key`；`raw_content`、`normalized_content`、`extractor_revision`＝`raw-normalization-1`、`ocr_confidence`＝null；`regions[]` → region 列，`kind` 的集合相同，bbox 都已驗證在 0..1 且 `x0<x1`），但**沒有把前者寫成後者的程式**，也沒有決定何時寫。
2. **G2 身份不穩定**：`evidence_id` 由 raw artifact 的雜湊決定，而 `UNIQUE (document_version_id, evidence_key)`。重新解析（不同批次大小、重試、不同 job）會得到不同的 id；同一份文件的多次解析會在 `evidence_units` 累積。需要「這個題庫用哪一次解析」的明確記錄（目前 `evidence_units` 沒有 parse job／result 欄位）。
3. **G3 兩套解析註冊表**：`parse_runs`（0005：parser 版本、profile、raw／manifest artifact、狀態）與 `parse_results`（0012）功能重疊，沒有人決定誰是正式的那一個。
4. **G4 覆蓋與缺口**：fast 路徑沒有表格結構（#113）、90／270 度頁沒有幾何（#115）、批次中 `needs_review` 的頁面沒有 evidence。配額規劃器（每個 quota bucket 至少 `ceil(0.1N)` 等）需要讀頁面層級的狀態，並把這些頁面算成「素材不足」而不是靜默略過。
5. **G5 出題與驗題**：選證據、寫題、獨立驗題、去重、圖表／跨頁必要性 ablation 都需要 LLM（blocked）；人工審查依 owner 範圍略過，而 #79 的凍結閘門要求的就是這些紀錄。
6. **G6 版本與凍結**：題庫版本要綁定 document version 與所選的解析結果；凍結後不可變，重新解析只能產生新的題庫版本。
7. **G7 gold 隔離**：解析輸出目前只有 owner／maintainer／reviewer 可讀（participant 與 observer 403，#107）；由解析結果衍生的題目與證據集進入 gold 之後的存取規則由 0008 管，這個備忘沒有改它。

8. **G8 看不見的文字會變成 evidence（PM-FINDING，#130 的實測，2026-11-10）**：真實 unstructured 0.27.36 fast 對 render mode 3（`3 Tr`，隱形）的文字回傳一般元素，頁面 `ok`、有 evidence、沒有診斷（合成 PDF，run 42765494711）。結果是題目、答案區段與標出的來源區域可能建立在人類讀者看不到的文字上（藏字／prompt injection 的路徑），也破壞來源忠實度（SPEC：文件文字是資料，不是權威）。同一次量測還顯示：CropBox 裁掉的文字仍回傳元素但沒有 evidence（`needs_review`）、90／270 度頁產生約 30 個字元層級元素且沒有 evidence、單獨的淺灰浮水印回傳 1 個沒有 evidence 的元素。**狀態（2026-11-10）：(a) 已在 #133 合併（頁面層級隔離 render mode 3 與 7：`needs_review`＋`text_not_visible`，無 evidence、無 observation；內容無法分析時 `content_unparseable`，同樣 fail closed）；(b) 渲染墨跡交叉檢查已實作於本分支的 PR（見 `docs/PARSE_WORKER.md`：每個 evidence 區域底下要有渲染出的墨跡，否則連同 observation 一起丟掉並標 `region_without_ink`），**尚待 PM 接受；在合併並被接受之前仍然 BLOCKED**，仍列在 go/no-go 的 blocked 清單。** (a) 的已知盲區（(b) 補上其中的白底白字、頁面外與被裁掉；仍抓不到被其他墨跡蓋住或與可見筆跡同框的文字：一條表格線、邊框、色塊或形狀穿過或蓋住單行隱形文字的框就能讓它通過（PM 實測，見 `docs/PARSE_WORKER.md`））：白底白字、被蓋住、頁面外或被裁掉的文字、不由 `Tr` 控制的隱形文字、**Type3 字型的 glyph procedure** 與**註解外觀串流**（皆未掃描）。

   **硬規則（P-A `promote_parse_result` 與任何出題前置）**：看不見的文字——至少 render mode 3，理想上還有頁面外、白底白字、被裁掉的文字——**不得**被提升為 evidence，也不得成為題目、答案或來源區域的依據。選項（待 owner／PM 決定，未實作）：(a) 在 L0 幾何／pypdf 階段偵測 render mode 3 與類似情況，把相關元素／頁面標為 `needs_review` 並加專屬診斷 `text_not_visible`；(b) 以 PDFium 渲染的墨跡交叉檢查每個 evidence 區域（#117 的 e2e 已經拿 union box 對照墨跡），沒有墨跡的區域不給 evidence。兩者可以併用；(b) 抓得到 (a) 抓不到的情況（白底白字、被覆蓋），但需要渲染依賴與成本。在選項決定並實作之前，promote 函式不得存在，或必須對 render-mode-3 以外「無法證明可見」的文字 fail closed，而且這個缺口要出現在 go/no-go 的 blocked 清單裡。

## 3. 提議的流程（尚未實作）

```
POST parse（已有） ──► parse_results（已有）
        │
        ▼  [提議 P-A] promote：把「被選定的」解析結果寫成 evidence_units／evidence_regions（同一個 transaction、以 job 為冪等鍵），
        │               並記錄 (benchmark revision ↔ parse job) 的連結（新 migration，需要 D1、D3）
        ▼  [提議 P-B] benchmark: draft → parsing（排隊解析）→ planning（規劃器讀頁面狀態與覆蓋，輸出配額與缺口）
        ▼  generating / validating（blocked：LLM）  →  ready_to_freeze
        ▼  [#79] freeze gate：沒有驗題、人工審查與配額紀錄就 fail closed
```

## 4. 需要 owner 決定的事

| # | 決定 | 選項（我的傾向） |
|---|---|---|
| D1 | 題庫版本用哪一次解析結果？ | (a) 在 `parsing → planning` 時釘住一個 parse job id（**傾向**）；(b) 永遠用最新成功的一個（會讓凍結的題庫與可變的解析脫鉤） |
| D2 | `parse_runs`（0005）與 `parse_results`（0012）的關係 | (a) `parse_runs` 當註冊表指向 `parse_results`（**傾向**，需要 migration）；(b) 停用 `parse_runs`；(c) 兩者並存、各管一半（不建議） |
| D3 | 何時把證據寫進 `evidence_units`？ | (a) 懶：題庫規劃時 promote（**傾向**，避免為沒有題庫的解析產生資料）；(b) 解析完成就寫 |
| D4 | 沒有幾何的頁面（`needs_review`、90／270 度）可否出題？ | 提議：不可；算入素材不足缺口，不補座標 |
| D5 | fast 路徑沒有表格結構時，表格題配額怎麼辦？ | 提議：標示素材不足（compliance 只能 `partial`），或等 hi_res（目前 blocked: 沒有模型／GPU 環境）；不用文字猜儲存格的列關係 |
| D6 | 人工審查略過（owner 範圍）如何與凍結閘門相容？ | #79 目前把 `partial` 限定 owner 才能凍結；請確認「略過人工審查 ＝ partial」是你要的語意，以及它在報告上的措辭 |
| D7 | LLM provider、模型、價格、單次與整體預算、獨立驗題用哪個模型 | blocked：需要你提供（見 `docs/LLM_SMOKE.md`） |
| D8 | 參賽方法（raw track）能看到什麼？ | 現行：解析輸出不給 participant；請確認 raw track 的來源是「參賽方法自己從原 PDF 解析」（SPEC），系統的解析結果與 gold 不外洩 |
| D9 | 解析輸出的保留與存取 | raw artifact 以 `restricted` 保留；是否需要到期與刪除流程 |

## 5. 被 LLM 憑證擋住的 vs 沒有被擋住的

- **沒有被擋住（可以先做，需要 D1–D4）**：promote 函式與連結表（P-A）、benchmark 的 `parsing → planning` 接線與規劃器對真實頁面狀態的讀取（P-B）、對應的唯讀 API 與測試。
- **被擋住**：選證據與寫題、獨立驗題與去重、圖表／跨頁必要性 ablation、judge 與評分的語意部分（#10 的生成與驗證、#12 的 judge）；真實 provider 的 smoke（chat、embedding、vision 各一）。
- **不是憑證問題但仍缺**：hi_res／MinerU（沒有模型／GPU 環境）、人工校準（人力）、500／600 頁含 OCR 的完整流程（只量過解析）。

## 6. 建議的切法（提議，每個一個小 PR）

1. P-A `promote_parse_result`：把指定的解析結果寫成 `evidence_units`／`evidence_regions`＋連結表（migration 0014；需要 D1、D2、D3）。測試：冪等、`pages` 外鍵與 bbox 約束、重新解析不污染已凍結的題庫、不可變。
2. P-B benchmark 建立與 `parsing → planning`：建立 API（owner／maintainer）、排隊解析並釘住 job、規劃器讀頁面狀態並輸出缺口（需要 D4、D5）。
3. P-C 把 #79 的凍結閘門改成讀實際的驗題／配額紀錄（等 LLM 之後才有紀錄；之前仍 fail closed）。
4. P-D 生成與驗題（blocked）。

## 7. 這份備忘沒有做的

沒有實作任何一項、沒有改任何 schema、沒有更新 `traceability.yaml`；沒有驗證 #52 規劃器能吃真實頁面狀態（只確認它是純函式）；SPEC 對題庫快取與斷點恢復（#10）的要求這裡只列為缺口，沒有設計。

## 8. 實作狀態（P-A：`promote_parse_result`，Refs #7、#10）

owner 的決定（Issue #7，2026-11-11）：D1、D2、D4、D5、D6、D8、D9 照作者傾向；D3＝題庫建立時才 promote（懶）；D7＝Anthropic、單次輸出 ≤ 500 token（模型代號待確認，見 Issue #7）。

已做（migration `0013_parse_promotion.sql` ＋ Alembic `0013`，`lab_db/promote.py`）：
- **D1**：`benchmark_parse_sources`（append-only）：一個題庫版本只釘一個 parse job，釘了就不能換；重新解析只能開新題庫版本。觸發器檢查該 job 是這個題庫文件版本的解析，且題庫不在 `frozen`／`cancelled`／`failed`。
- **D3**：`promote_parse_result(conn, project, benchmark, parse_job, actor)` 在一個交易裡釘住 job，並把**乾淨頁**的 evidence 寫進 `evidence_units`／`evidence_regions`；`evidence_unit_sources`（append-only）記錄每個單元來自哪個 job、哪個解析端 evidence id、哪一頁。同一個單元（同一 job 的同一 evidence id）只寫一次，兩個題庫共用同一個 parse job 時共用單元；同一個 job 對同一題庫重複呼叫是冪等的。
- **G8 硬規則（程式強制）**：只有狀態 `ok` 且診斷不含 `text_not_visible`／`content_unparseable`／`region_without_ink` 的頁面會被提升；`needs_review`、`partial`、`failed`、沒有幾何、隱形或無法分析的文字、被丟掉區域的頁面**一律不提升**，回報為素材不足並附固定理由（D4）。evidence 若不符合契約、頁碼或文件雜湊對不上，該頁也不提升。
- **殘餘限制寫進每個釘住的 provenance**（`RESIDUAL_LIMIT`）：藏在其他可見墨跡之間或底下的隱形文字、Type3 glyph procedure、註解外觀串流抓不到，所以被提升的頁面「不保證沒有隱藏文字」。
- D5（fast 路徑沒有表格結構、表格題配額標示素材不足）與 D6（略過人工審查＝partial，措辭用 skipped）由後續的規劃與凍結步驟處理；**這個 PR 沒有做 D2（`parse_runs` 當註冊表）**：promote 只讀 `parse_results`，`parse_runs` 仍未被解析路徑使用。



- 授權與稽核（PM 審查 #147 後補）：`promote_parse_result` 在鎖住 benchmark 列之後自行授權（`build_edit_freeze`，owner／maintainer；非成員 `NotFound`、其他角色 `PermissionDenied`）；同一交易寫一筆 `benchmark.parse_promoted` 稽核事件（只有計數）；同一 document version 的並行 promote 以交易 advisory lock 序列化；證據單位已存在時，仍為這個 parse job 補一筆 `evidence_unit_sources`。`missing_or_invalid_geometry_or_asset_reference` 也列入 UNSAFE_DIAGNOSTICS。
P-D（LLM 出題）：slice 1–3 已做（lab-gen、`llm_calls` 帳本、general 桶出題與 `gold_answers`），見 `docs/LAB_GEN.md`；其他三個桶、人工審查與凍結尚未做。

### P-B：建立題庫並讀素材規劃（Refs #7、#10，沒有新 migration）

- `POST /api/v1/benchmarks`（`build_edit_freeze`＝owner／maintainer；reviewer 只能評論所以 403）：為某個 document version 建立題庫版本（N、Q 由 `target_counts(page_count)` 決定，沒有旋鈕），寫 resource version、`derived_from` lineage、audit，並把狀態 `draft → parsing`；`logical_id` 重複 → 409；不支援 `Idempotency-Key`（冪等性靠 `logical_id` 唯一）。
- `POST /api/v1/benchmarks/{logical_id}/revisions/{n}/plan`（同權限）：釘住一個**已成功**的 parse job（不指定就用該文件版本最新成功的一個；沒有 → 409 並說明先 `POST /documents/{id}/parse`），經 `promote_parse_result`（G8 硬規則）提升乾淨頁的 evidence，`parsing → planning`，回傳素材規劃；重複呼叫冪等，指定另一個 job → 409（釘不會移動）。
- `GET .../plan`（`read_gold_and_generation_context`）：同一份規劃（決定性、由 pin 的 provenance 與已提升的 evidence 算出，沒有另外存）；還沒釘 → 409。
- **素材規劃**（`material_plan.py`，純函式）：可用頁、因 D4 不能出題的頁（附固定理由）、四個配額桶各自「有沒有素材」（**必要條件**，不是「一定寫得出題」）；D5：沒有 table／figure／image evidence 時 `chart_table` 為 `insufficient_material`（不用文字猜儲存格）；D6：`human_review.status = "skipped"`、`compliance_projection = "partial"`，措辭是 skipped，不是 passed，凍結只有 owner；每份規劃都帶 G8 殘餘限制。
- 路由是「應用程式提供、契約覆蓋層沒有描述」的一類（見 `tests/api/test_openapi_contracts.py` 的 expected 清單，我加了兩個路徑）。


### P-D slice 3：general 桶出題（Refs #7、#10）

`document.generate` job 只用已提升的乾淨 evidence（G8），每頁一個呼叫，只出 general 桶；題目、facts、evidence sets、`gold_answers`
（migration 0015，RLS 隔離）與 `llm_calls` 在同一交易寫入；benchmark `generating → validating`，不足 `q_min` 時在轉換理由記錄 shortfall，
不補題、不凍結、人工審查 skipped。詳細規則與限制（無人工審查、品質未量測、模型可能出不需看頁面的題）見 `docs/LAB_GEN.md`。
