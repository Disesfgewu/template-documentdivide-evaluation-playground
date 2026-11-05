# lab_contracts — 版本化資料契約（基礎）

#21：SPEC 4.1、16.1、16.4 的基礎型別、SourceRegion、錯誤格式與分頁。#23：SPEC 5、16.2、19.5 的四部分題庫契約（`benchmark.py`）。方法接入協定、parser、評分等契約在 #4 的後續子任務。

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
