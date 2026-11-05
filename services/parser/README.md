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
