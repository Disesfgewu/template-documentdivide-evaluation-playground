# LAB parser service（Python 骨架）

#3 的增量：PDF/OCR/視覺處理服務的骨架。真正的 Unstructured 與 MinerU adapter 在 #8，本服務目前**不提供任何解析端點**，不會回傳假的解析結果。

```bash
pip install -r services/parser/requirements.lock
export LAB_PARSER_ENV=dev LAB_PARSER_ARTIFACT_ROOT="$PWD/.lab-artifacts"
mkdir -p "$LAB_PARSER_ARTIFACT_ROOT"
(cd services/parser && python -m lab_parser check-config)
(cd services/parser && uvicorn --factory lab_parser.main:create_app --port 8100)
```

- `/healthz`：程序存活。
- `/readyz`：artifact 根目錄實際可寫才回 200，否則 503。
- `/v1/capabilities`：列出 `unstructured`、`mineru` 是否已安裝與實際版本；`adapter` 一律為 `not_implemented`，直到 #8 完成。

## 頁面幾何與 L0 渲染（#7 子任務）

純函式庫，尚未接到任何端點或資料庫：

- `lab_parser.geometry`：PDF user space（pt，左下原點）與 SourceRegion 空間（[0,1]，左上原點，已套用 `/Rotate` 與可見框）互轉，對應 SPEC 4.1。超出可見框的部分裁掉；完全在框外、零面積框、非 90 度倍數的旋轉一律報錯。
- `lab_parser.pages`：逐頁讀出 MediaBox、CropBox、可見框（兩者交集）、旋轉與旋轉後的顯示尺寸。頁碼標籤只取文件本身的 `/PageLabels`；沒有時為 `None`，不用 pypdf 自動補的 "1"、"2"。
- `lab_parser.render`：用 PDFium 把整頁或某個 SourceRegion 渲染成 L0 PNG（自帶的確定性 RGB 編碼器，不依賴 Pillow），附 sha256 與可重現的轉換資訊（renderer 版本、dpi、裁切 pt）；dpi 限 72–300，單張不超過 16 MP、最長邊 4096 px（SPEC 19.3.8）。

測試（`tests/parser/test_geometry.py`、`test_pages.py`、`test_render.py`）都在測試裡即時產生小型 PDF，不依賴 fixture registry。關鍵測試會在 0/90/180/270 度旋轉、有無 CropBox 偏移的頁面上畫黑框，確認 `to_region_space` 算出的區域經 PDFium 實際渲染後確實是黑的。固定 F01/F02/F05 的真 PDFium 驗收及內部 `lab_parser.importer` 匯入檢查見 [fixture 驗收說明](../../docs/PDF_FIXTURE_ACCEPTANCE.md)。上傳／持久化／批次流程仍待 #5/#6 與 #7 整合。

Local Linux operators can publish a real page/region PNG and its immutable source/transform manifest with the standalone [L0 render bundle command](../../docs/L0_RENDER_BUNDLES.md). Bundles remain `unverified_l0`; this does not add service endpoints, verified evidence or database persistence.

## 有界本機 PDF 檢查 CLI（#7 獨立增量）

```bash
PYTHONPATH=services/parser python -m lab_parser inspect /path/to/document.pdf
PYTHONPATH=services/parser python -m lab_parser inspect /path/to/document.pdf --max-bytes 1048576 --max-pages 100
```

這個命令不需要 `LAB_PARSER_*` 服務設定，只讀取指定的本機 regular file，不建立文件、artifact、job 或模型呼叫。預設上限為 512 MiB／2000 頁，兩個旗標都必須是正整數。讀取同一個已檢查 descriptor，最多讀取 `max_bytes + 1` bytes；FIFO／目錄會拒絕，不等待 FIFO writer。允許 symlink，但只接受實際開啟後 `fstat` 為 regular file 的目標；不先檢查路徑再另開檔。

stdout 為單一 `lab-pdf-inspection-1` JSON；不輸出來源路徑、PDF 內文或原始 parser 警告。完整讀取才提供原始 `sha256`／`size_bytes`。超過 byte 上限時 `complete_read=false`、`bytes_read` 是已讀 prefix 長度，`sha256`／`size_bytes`／`page_count` 為 null，且不進行 PDF parsing。只有 `outcome=accept` 才包含逐頁 geometry；需要人工檢查、加密、格式錯誤與超限皆不提供部分頁面結果。`page_index` 是實體頁索引；`page_label` 僅來自文件 `/PageLabels`。

Exit 0：接受。Exit 1：拒絕或需審核（JSON 的穩定 `reason` 說明原因，例如 `file_too_large`、`encrypted_pdf`、`xref_repaired`、`invalid_page_geometry`）。Exit 2：參數或 I/O 錯誤（`invalid_arguments`、`input_unavailable`、`not_regular_file`）。`check-config` 保持既有行為。

固定 F01／F02／F05 的 CLI subprocess tests 驗證真 bytes/hash/count、CropBox／旋轉、異常 PDF、邊界與有界讀取。本增量只涵蓋 T01–T05 的本機檢查／頁面 metadata 子情境，不宣稱完整 HTTP 上傳、版本唯一性、PG/artifact 持久化、OCR、批次恢復或 A19 通過；這些仍由 #5／#6／#7 後續整合驗收。

## Bounded L3 arithmetic derivations (#9 foundation)

`lab_parser.derivations.evaluate_derivation(request)` evaluates an explicit JSON
arithmetic tree over named, source-linked decimal values. It returns a detached
L3 `derived_calculation` record with the formula, every operand and source-region
snapshot, derivation/operation IDs, evaluator revision and a deterministic
content SHA-256. The status is always `needs_review`: arithmetic does not verify
that a value was read correctly or that an evidence reference exists.

Run the hand-calculated example (0.3 ms minus 0.1 ms = 0.2 ms):

```bash
PYTHONPATH=services/parser python - <<'PY'
import json
from lab_parser.derivations import evaluate_derivation
with open("services/parser/examples/derivation-request.json") as source:
    result = evaluate_derivation(json.load(source))
print(json.dumps(result, indent=2))
PY
```

The internal envelope is versioned `lab-derivation-1`; its exact keys are shown
in [the example](examples/derivation-request.json). Decimal values are ASCII
strings, not JSON floats. Each operand has an `operand_id`, `value`, `unit`,
`value_kind=observed_label`, an `evidence_ref` (ID/revision/SHA-256) and nonempty
`source_regions` using the original PDF's coordinates. All operands must be used.
Every region must identify the same PDF logical ID and bytes hash. Page existence,
evidence hash authenticity and project authorization require repository integration;
this library only checks the supplied structure and immutable identity snapshots.

The AST follows the existing benchmark Calculation key conventions: leaves are
`{"ref":"a"}`, and operators are `{"op":"sub","args":[left,right]}`.
This is a deliberately smaller internal contract, **not** a drop-in implementation
of the public float-valued Calculation schema: v1 supports binary `add`, `sub`,
`mul`, `div` only. Constants, `neg`, `abs`, `min`, `max`, public float operands
and automatic conversion from that contract are unsupported and rejected.

Units are an explicit allowlist (`1`, `s`, `ms`, `m`, `cm`, `kg`, `g`, `count`,
`%`, `percentage_point`). Add/subtract require identical units. Multiplication
requires at least one dimensionless (`1`) operand. Division permits a dimensionless
denominator or equal units (result unit `1`); inverse/compound dimensions are
rejected. There are no implicit scale conversions; `ms+s` and
`%+percentage_point` fail. Estimated values and interval propagation require a
future revision and are rejected, preserving the distinction from exact labels.

The evaluator never executes expression text or model-generated code. It limits
ASTs to 127 nodes and depth 16, operands to 32, each operand to 16 regions,
serialized input to 128 KiB, strings to 8192 characters, and total input nodes to
8192. Cyclic or shared Python containers fail before serialization. Decimal
arithmetic uses an isolated 80-digit context with explicit rounding/exponent/trap
settings; inexact or rounded arithmetic (including 1/3), division by zero,
nonfinite values and intermediate values outside the versioned numeric bounds
fail with `DerivationError.code`. Inputs/results have at most 56 coefficient
digits, fractional scale at most 24 and nonzero adjusted exponent in [-24,24].
No input observations are modified. This callable API does not parse raw JSON;
any future HTTP/CLI transport must separately reject duplicate JSON object keys.

Actual deterministic coverage is in `tests/parser/test_derivations.py`. It covers
restricted arithmetic, original/cross-page provenance, content identity, unit
errors, malicious ASTs, decimal boundaries, cycles and resource limits. It is a
partial foundation for R27/R29 and SPEC 19.5–19.6, not full T99/A44/A46 evidence.
Parser/vision quality, artifact/FK checks, PostgreSQL persistence, workflow trace,
gold publication, human review and end-to-end acceptance remain gated on their
respective Issues and integrations. No provider or parser calls are made here.
