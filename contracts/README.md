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

`examples/valid` 和 `examples/invalid` 以 `<Schema>.<case>.json` 命名，每個 schema 至少要有一個合法範例和一個非法範例，全部都在 CI 中驗證。

尚未完成（後續增量）：benchmark 四部分（Question/SemanticAnswer/TextualAnswer/SourceAnswer/EvidenceSet/…/BenchmarkManifest）、方法 prepare/query/JSONL、parser manifest、submission/scoring、OpenAPI 匯出、fixture registry 與 R/T/A traceability 檔。
