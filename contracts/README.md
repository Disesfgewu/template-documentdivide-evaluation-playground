# lab_contracts — 版本化資料契約（基礎）

#21：SPEC 4.1、16.1、16.4 的基礎型別、SourceRegion、錯誤格式與分頁。#23：SPEC 5、16.2、19.5 的四部分題庫契約（`benchmark.py`）。#25：SPEC 7、16.3、16.6 的方法接入協定與 JSONL 提交（`method.py`）、method-service OpenAPI（`openapi.py`）。parser、評分、fixture registry、traceability 在 #4 的後續子任務。

## 來源與產生

以 pydantic v2 模型為準（`lab_contracts/`）。`schemas/v1/*.schema.json`（JSON Schema 2020-12）由模型產生，不可手改：

```bash
pip install -r contracts/requirements.lock -r requirements-dev.txt
PYTHONPATH=contracts python3 -m lab_contracts.export          # 重新產生
PYTHONPATH=contracts python3 -m lab_contracts.export --check  # CI：有漂移就失敗
scripts/check.sh --scope contracts
```

## 內容

- **基本型別**：
  - `ResourceId`：1–128 字元的不透明字串，不解析其中意義。
  - `Sha256`：剛好 64 位小寫 hex。
  - `FiniteFloat`：拒絕 NaN 和 ±Infinity。
  - `Attributes`：必須能序列化成 JSON（不能含 set、物件、NaN），UTF-8 JSON 編碼後不超過 4096 bytes。
- **`SourceRegion`**：
  - `page_index`：0 起算的 JSON integer；`"3"`、`3.0`、`true` 都會被拒絕。
  - `bbox`：在 [0,1] 範圍內，且 x0<x1、y0<y1（正面積）；座標以套用旋轉後的可見 CropBox 為準，左上角為原點。
  - `kind=page` 必須是 [0,0,1,1]。
  - `quote` 必填，但可以是 null。
  - hash 不符或頁碼超出範圍時，由 `check_against_document` 回傳原因，**不丟例外**。SPEC 16.3 規定這種來源要保存為 `invalid_region`，不能因此抹掉獨立的答案。
- **`ErrorResponse`**：code、message、retryable、request_id、details，以及可選的 retry_after。
- **`PageRequest`**：預設 50，範圍 1–200。
- **`Page`**：items 數量不能超過 page_size；`next_cursor` 必填，最後一頁為 null，空字串不合法。
  - cursor 必須綁定 filter/order，HTTP status 的對應規則在後續 API 實作。**這裡沒有實作，也不宣稱已完成。**

## 四部分題庫（#23，`lab_contracts/benchmark.py`）

- **四部分**：`Question`、`SemanticAnswer`、`TextualAnswer`、`SourceAnswer`，**只以 `question_id` 聯結**，不依陣列位置。沒有第五份答案集。另有 `EvidenceUnit`（原 PDF regions、原始與正規化內容分開保存）、`ValidationRecord`、`BenchmarkManifest`。
- `Question` 保存 `modality_requirements`、`reasoning_type`、`visual_object_refs`，`tags` 不可重複，`ordinal` 為 strict int。
- `SemanticAnswer.required_facts` 不能是空的；每個 fact 的 `weight` 必須大於 0，`scoring_rule.partial_credit` 為 null 時代表不允許 0.5 分。
- 公式 `Calculation` 是**受限的 AST**：
  - 只允許 add、sub、mul、div、neg、abs、min、max。
  - 每個 op 的參數數量固定，深度上限 16，常數必須是有限值。
  - 每個引用都必須是已宣告的 operand，宣告的 operand 也都必須被使用。
  - **不會執行任何模型產生的程式碼**（沒有 eval/exec）。
- `TextCheck`：
  - quote/entity 類：數值欄位與 unit 都是 null。
  - numeric 類：單點值必須帶 tolerance，預期值為 0 時必須有絕對容差；區間 `[low, high]` 不得再帶 tolerance 或 precision，避免容許範圍被重複放寬。
  - `numeric_matches` 只用於結構驗證與測試，單位換算與正式評分在 #12。
- `SourceAnswer`：
  - `evidence_sets` 是 OR-of-AND，不能是空的，集合內的證據不可重複。
  - `fact_support_sets` 表達每個 fact 各自的 OR-of-AND。
  - `distractors` 必須附上 confusion_reason 與 disambiguating_condition。
- **`validate_bundle(...)`**：跨物件檢查，回傳帶欄位路徑的問題清單，不丟例外。檢查項目：
  - 缺少、多出或重複的 question_id；重複的 ordinal 或 question_hash。
  - 引用到不存在的 fact、evidence 或 validation 紀錄，或驗證紀錄屬於別題。
  - 證據來自多份文件。
  - 某個 evidence set 沒有涵蓋每個 fact 的任一支持組合。
  - fact 沒有支持證據。
  - distractor 出現在任何 gold 欄位（evidence set、fact support、textual check、公式 operand）。
  - cross_page 題：任一證據集合只有一頁，或每個 fact 都能在同一頁找到支持。
  - chart_table 題沒有 table/figure/image 證據。
  - confusable 題沒有 distractor。
- **只驗證結構**：`validate_bundle` 不能證明證據在語意上是必要的、圖表真的有被讀過、confusable 題沒有歧義。這些都是驗證器與人工閘門的工作（#9、#10），結果記在 `ValidationRecord`。
- **`target_counts(P)`**：N=max(20,P)、Q=ceil(0.1N)，用整數運算。
- **`BenchmarkManifest`**：
  - N/Q 必須和頁數一致；各桶數量加總等於 n_valid（每題只占一桶）。
  - compliant：必須剛好 N 題，且三個特殊桶都至少 Q 題。
  - partial：必須列出 exceptions。
  - `check_manifest` 會再和實際題目比對數量。
- **手算 fixtures**：`examples/bundles/*.json` 都是**合成的契約層 fixture**，不能當作 parser、視覺判讀或人工品質已通過的證據。每個檔案附上 `_scenario`、`_expected_problems`、`_counts`，測試會逐一比對：

| 檔案 | 情境 | 預期 |
| --- | --- | --- |
| `minimal.json` | 一般題、必要跨頁題、confusable 題 | 沒有問題 |
| `or_alternative_cross_page.json` | 兩組證據集合都需要兩頁 | 沒有問題 |
| `single_page_alternative_vetoes_cross_page.json` | 有一組單頁就能成立的替代集合 | 否決 cross_page |
| `chart_table_with_caption.json` | 圖像加圖說文字 | 沒有問題 |
| `chart_table_without_visual.json` | 圖表題沒有視覺證據 | 報錯 |
| `confusable_without_distractor.json` | confusable 題沒有 distractor | 報錯 |
| `distractor_used_as_gold_support.json` | distractor 混進 gold | 報錯 |

## 方法接入協定（#25，`lab_contracts/method.py`）

- **模型**：`Capabilities`、`PrepareRequest`／`PrepareResult`、`QueryRequest`、`QueryResponse`、`AsyncAccepted`、`JobStatus`、JSONL `SubmissionLine`。HTTP 方法服務與外部 JSONL 提交共用同一個 `QueryResponse` schema。
- **協定版本**：`protocol_version` 只接受 `SUPPORTED_PROTOCOL_VERSIONS`（目前 `1.0`），未知版本回 422。
- **`QueryResponse`**：
  - `answered` 需要非空 `answer.text`；`abstained` 需要 `reason`、空的 text 與 `claims=[]`，可以保留 hits。
  - `hit_id`、`claim_id` 單題內唯一；rank 必須剛好是 1..n，不能重複或跳號。
  - citation 的 claim、hit、region index 都必須存在，越界就拒絕。
  - 重複的 `region_indices`（PM #4 Q1）：接受；`normalized_region_indices` 會保留原順序並去重，評分端必須用它，所以重複不會增加 coverage，也不會稀釋錯誤引用。payload hash 一律用原始提交計算。
  - 空的 `content`（PM #4 Q3）：只要至少有一個可從原 PDF 重新裁切的視覺 region（image、figure、table、page、formula），就可以同時附上必要的文字 region（圖說、條件）。只有空白 content 加上 text region 會被拒。這是結構規則，**不代表幾何位置已在語意上支持答案**（#9、#12）。
  - 結構合法、但 hash 錯誤或頁碼超出範圍的 region **不會**讓整份回應被拒；回應會保留，由評分端標成 `invalid_region`（T35），不冒稱來源有效。
  - telemetry 可以省略；提供時每個 key 都必填，未知值用 null（0 只代表確認為零）。
  - HTTP 綁定：`check_binding` 比對 request_id、question_id、index_id。`PrepareResult.check_against_request` 比對文件 hash 與 method revision。
- **`JobStatus`**：只有 succeeded 帶 result，而且型別必須符合 kind；failed 必須帶 error，其他狀態不能帶。
- **嚴格 JSON（`strict_loads`）**：拒絕語法錯誤、重複 key、NaN/Infinity，以及超出有限 binary64 範圍的數字（溢位成 inf，或非零卻下溢成 0，例如 `1e-999`）；小數與指數數字以精確的 `Decimal` 回傳，不先轉成 float；過深的巢狀會轉成 `StrictJSONError`，不會讓 `RecursionError` 往外丟。
- **`parse_query_response(body, request=None)`**：超過 16 MiB → 413；非 UTF-8 或不是嚴格 JSON → 400；schema 或語意錯誤 → 422；有傳入 request 但身分不符 → 422；合法 → 200。
- **`parse_jsonl(body)`**（SPEC 16.6：最多 100 行、16 MiB）：
  - 整批拒絕：過大或行數超過上限 → 413；非 UTF-8 或含 BOM → 415；沒有內容或只有空白 → 422。
  - 其他情況逐行判斷（T33），壞的一行不會吞掉合法的行。逐行錯誤碼：`invalid_json`（含批內空行）、`line_too_large`、`schema_invalid`、`envelope_mismatch`（同一批的 run、benchmark、method、document_hashes 必須一致）、`duplicate_in_batch`。
  - 同一批內同一 attempt＋question 的重送：payload 相同時接受，並以 `duplicate_of` 標出第一次出現的行，避免重複寫入；payload 不同時拒絕。
- **canonical payload hash（`lab-c14n-2`，取代 `lab-c14n-1`）**：
  - hash 是對**實際傳送的 JSON 值**（經嚴格解析）計算，不是對 model dump 計算，所以日後新增選填欄位不會改變既有 payload 的 hash。
  - 規則由 Python 定義，不是 RFC 8785 JCS：key 依 code point 排序、無空白、UTF-8（不跳脫非 ASCII）、禁止非有限數字。字串不做 Unicode 正規化，所以 NFC 與 NFD 會得到不同的 hash。
  - 數字以**原始 token 的精確十進位值**寫出，不經 binary64 捨入：去掉尾端的 0，數學上相等的寫法相同（`1`、`1.0`、`10e-1`；`0.1`、`1e-1`；`-0` 與 `0`），不同的值一定不同（`9007199254740992.0` 與 `9007199254740993.0`）。十進位指數在 [-7, 21) 內用一般寫法，其他用 `d.ddde<指數>`。Python float 依最短 round-trip 表示處理。
  - `lab-c14n-1` 先把數字轉成 float，會讓不同的值得到相同的 hash，並把非零的下溢值當成 0（PR #26 review），所以升級版本。原有的黃金向量在兩個版本下相同。
  - 以黃金向量測試固定（`test_method.py`）。改任何規則都必須升級 `CANONICAL_JSON_VERSION`。
- 模型驗證（`model_validate_json`）仍使用 float；hash 的身分與模型的數值是分開的兩條路徑。
- **上限**：見 `method.py` 頂部的常數（文件數、問題與答案長度、hits、regions、claims、citations、trace）。測試直接引用這些常數，不另外放寬。
- **OpenAPI**：`openapi/method-service.v1.json` 精確列出 SPEC 7.2 的 5 個端點，包含 200/202、錯誤與 job 結構。由模型產生，`lab_contracts.export --check` 會檢查漂移。這是**契約文件**，不代表已有可呼叫的正式方法服務。

### 尚未包含（runtime，後續 Issue）

- 跨批次的冪等（相同 hash 回相同結果、不同 hash 回 409）、每行一個交易、finalize 與 run lock：#5、#11。
- late、cancel、deadline 的實際執行，以及 job 狀態轉移：#6、#11。
- region 是否命中 gold、`invalid_region` 標記與評分：#12。
- 方法獨立性、gold 隔離（A18）等整合 gate：#5、#11、#14。目前不標 passed。

## required、null、empty 的區別

- **必填且可為 null**（`quote`、`next_cursor`）：缺少這個欄位是 `missing` 錯誤；明確給 null 是合法的。
- **選填**（`page_label`、`retry_after`、`cursor`）：可以省略，省略時為 null。
- **必要的非空陣列**：不能用 `[]` 代替，例如之後契約裡的 `evidence_sets`。
- **可以為空的集合**：使用 `[]`。
- **未知數值**：用 null 表示，0 只代表確認為零。

## 嚴格模式與不可變的範圍

所有模型都是 `extra="forbid"`、`strict=True`、`frozen=True`：
- 頂層未知欄位會被拒絕（API 回 422）。
- 不做型別轉換。
- **`frozen` 只阻擋對模型屬性重新賦值**。巢狀的 dict 和 list（例如 `details`、`attributes`）仍然是一般的 Python 物件，可以被修改，請把它們當成唯讀使用。

## JSON Schema 無法表達、只由 runtime validator 檢查的規則

- bbox 的 x0<x1、y0<y1，以及 `kind=page` 必須是整頁。
- `Page` 的 items 數量不能超過 page_size。
- `Attributes` 的大小上限，以及「必須能序列化成 JSON」。
- strict integer：JSON Schema 的 `integer` 會接受 `3.0`。
- 和文件相關的檢查（hash、頁數），由 `check_against_document` 處理。
- 題庫的所有跨欄位、跨物件規則：公式 AST 的參數數量、深度與引用；TextCheck 的形狀；`validate_bundle` 與 manifest 的配額規則。
- Question 的 `ordinal` 是 strict integer：JSON Schema 的 `integer` 會接受 `3.0`。
- 方法協定的跨欄位規則：answered／abstained、rank 1..n、ID 唯一、citation 引用、空 content 需要視覺 region、JobStatus 的終態、支援的 protocol 版本；嚴格 JSON、JSONL 批次規則與 payload hash。

`tests/contracts/test_schema_validation.py` 用獨立的 `jsonschema` 驗證器確認兩件事：匯出的 schema 接受所有合法範例，也拒絕所有純結構性的非法範例。上面列出的規則只由 pydantic 檢查。

## 範例

`examples/valid` 與 `examples/invalid` 以 `<Schema>.<case>.json` 命名，每個 schema 至少要有一個合法範例和一個非法範例。

非法範例必須**只**產生一個錯誤，而且錯誤類型、位置（value_error 另外比對訊息）要和 `examples/expected_invalid.json` 一致，避免範例因為無關的錯誤而「剛好」被拒。

## 版本與相容策略

- schema 版本是 `SCHEMA_VERSION = "v1"`，輸出到 `schemas/v1/`。
- 以下變更必須升級版本（v2），舊版本的檔案保留：
  - 新增必填欄位、改變欄位語意。
  - 收緊既有限制，例如縮小範圍或改變 enum。
  - 刪除欄位。
- 在同一版本內，只允許新增選填欄位，或不改變既有合法輸入的修正。
- 擴充的值放在 `attributes`，不能用來覆寫核心語意。

## Draft selection

The deterministic candidate planner reuses the four-part contracts and
returns selected IDs and visible quota deficits. See
[draft benchmark planning](../docs/BENCHMARK_PLANNING.md) for its validation
boundary and the remaining verifier/publishing gates.

## Run-bound external JSONL preflight (#11 foundation)

`lab_contracts.submission` validates an external JSONL file against explicit,
trusted run metadata before ingestion. It uses the same `SubmissionLine`, strict
JSON parsing and raw `lab-c14n-2` payload hashes as the method protocol. It does
not persist answers, authorize callers or execute participant code.

Convert the example response envelope to a single JSONL physical line:

```bash
python3 -c 'import json; print(json.dumps(json.load(open("contracts/examples/valid/SubmissionLine.spec_example.json"))))' > /tmp/lab-submission.jsonl
PYTHONPATH=contracts python3 -m lab_contracts.submission \
  --context contracts/examples/internal/SubmissionRunContext.json \
  --input /tmp/lab-submission.jsonl
```

The internal context contains run/benchmark/method IDs, document SHA-256 and page
counts, plus a `queries` list of `{attempt_id, request}` bindings. `request` is the
existing public `QueryRequest`; no gold/context/evidence is needed. Each
(attempt_id, question_id) pair must be unique. Multiple transport attempts for
one question can have distinct request/index bindings. The service must derive
this metadata from authorized, frozen run records; participant-supplied context
is useful for local checks but is not an authorization boundary.

`preflight_jsonl(body, context)` returns each line's `report` and its validated
`submission` (or `None` on rejection). Batch encoding/size/100-line failures reject
the whole file. Other errors remain per-line, including the first line: a wrong
envelope cannot bind later valid lines. Identical validated resends return
`duplicate_of`; changed payloads for the same attempt/question are rejected.
Hashing precedes numeric model conversion and retains exact decimal identity.

A structurally valid answer with an unknown document, wrong hash or out-of-range
physical page keeps its original answer, hits and citations. `invalid_regions`
identifies each hit/region and reason; downstream source scoring must exclude
those regions while preserving independent answer scoring. Page zero is valid.
The CLI prints only diagnostics/hashes, never answer/quote text, and marks
`persisted:false`. Exit 0 means every line validated (some source regions may
still be invalid); 1 means line rejection; 2 means batch/input/context rejection.
The complete diagnostic report is capped at 16 MiB before writing stdout.
Both inputs must be bounded regular files; special files cannot block the read.

Validation command: `scripts/check.sh --scope "contracts fixtures governance"`.
`tests/contracts/test_submission.py` exercises the deterministic R09/R10/R22,
T33–T36 input-binding/source-diagnostic subset. A13/A15/A16/A19 end-to-end
acceptance remains open. Durable per-line PostgreSQL transactions, cross-batch
idempotency, run/finalize locks, first-terminal selection, authorization,
audit/outbox, late/cancel handling and HTTP/grader equivalence remain #5/#6/#11/#12
integration gates. This preflight is not a submission endpoint or a completed run.
## Private benchmark transfer

The callable codec and CLI `python -m lab_contracts.transfer` preserve the four
parts plus evidence/validation snapshots with versioned hashes and bounded
strict import. Its separate participant projection has an explicit three-field
allowlist. See [benchmark transfer](../docs/BENCHMARK_TRANSFER.md) for format,
commands, limits and the remaining authorization/freeze/API gates.


## Generation control

The deterministic accept / regenerate / needs_review routing with attempt and cost caps, and the quota-deficit scheduling decision, are in `lab_contracts/generation_control.py`. See [generation control](../docs/GENERATION_CONTROL.md) for the rules and their limits.
