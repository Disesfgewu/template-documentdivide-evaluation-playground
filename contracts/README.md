# lab_contracts — 版本化資料契約

SPEC v0.1 第 4.1、16.1、16.4 節的契約，以 pydantic v2 模型為準（`lab_contracts/`）。`schemas/v1/*.schema.json`（JSON Schema 2020-12）由模型產生，不手改：

```bash
PYTHONPATH=contracts python3 -m lab_contracts.export          # 重新產生
PYTHONPATH=contracts python3 -m lab_contracts.export --check  # CI：有漂移就失敗
scripts/check.sh --scope contracts
```

目前範圍（#4 第一個增量）：

- 基本型別：`ResourceId`（1–128 字元不透明字串）、`Sha256`（64 位小寫 hex）、有限數值、`attributes` 大小上限。
- `SourceRegion`：`page_index` 為 integer ≥ 0（不接受 `"3"`、`3.0`、`true`）；bbox 在 [0,1] 且 x0<x1、y0<y1；`kind=page` 必須是 [0,0,1,1]；`quote` 必填但可為 null。和文件相關的檢查（hash 不符、頁碼超出範圍）由 `check_against_document` 回傳原因，不直接拋例外，因為 SPEC 16.3 規定要把這類來源保存為 `invalid_region`。
- `ErrorResponse`（code/message/retryable/request_id/details，可選 retry_after）、`PageRequest`（預設 50、最大 200）、`Page`。
- 所有模型都是 strict：頂層未知欄位會被拒絕（API 回 422），不做型別轉換，物件不可變。

- 方法接入協定（SPEC 7、16.3，`lab_contracts/method.py`）：`Capabilities`、`PrepareRequest`／`PrepareResult`（`check_against_request` 比對文件 hash 與 method revision）、`QueryRequest`、`QueryResponse`、`AsyncAccepted`、`JobStatus`、JSONL `SubmissionLine`。
  - `QueryResponse`：
    - `answered` 需要非空 `answer.text`；`abstained` 需要 `reason`、空的 text 和 `claims=[]`，可保留 hits。
    - `hit_id`、`claim_id` 單題內唯一；rank 必須剛好是 1..n。
    - citation 的 claim、hit 與 region index 都必須存在。
    - 空的 `content` 只允許用在含非文字區域的 hit。
    - telemetry 可省略；有提供時每個 key 都必填，未知的值為 null（0 代表確認為零）。
    - HTTP 綁定以 `check_binding` 比對 request_id、question_id、index_id。
  - 錯頁或 hash 不符但結構合法的 region **不會**讓整份回應被拒，由評分端標成 `invalid_region`（T35）。
  - `parse_query_response`：不是 JSON → 400；schema 或語意錯誤 → 422。
  - `parse_jsonl`：每行獨立接受或拒絕（T33）。整批拒絕的情況有：非 UTF-8 或含 BOM → 415、過大或行數過多 → 413、空內容 → 422。同一批內同一 attempt＋question 的重送：payload 相同視為冪等，不同則拒絕。payload hash 使用鎖定的 canonical JSON（`lab-c14n-1`）。跨批次的冪等與 409、run lock 需要資料庫，屬於 #5 之後。
  - 輸入上限見 `method.py` 頂部常數。

`examples/valid` 和 `examples/invalid` 以 `<Schema>.<case>.json` 命名，每個 schema 至少要有一個合法範例和一個非法範例，全部都在 CI 中驗證。非法範例必須**只**產生一個錯誤，而且錯誤類型、位置（value_error 另比對訊息）要和 `examples/expected_invalid.json` 記錄的一致，避免範例因為無關的錯誤而「剛好」被拒。

尚未完成（後續增量）：benchmark 四部分（Question/SemanticAnswer/TextualAnswer/SourceAnswer/EvidenceSet/…/BenchmarkManifest）、parser manifest、submission/scoring、OpenAPI 匯出、fixture registry 與 R/T/A traceability 檔。
