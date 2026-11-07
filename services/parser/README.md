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

測試（`tests/parser/test_geometry.py`、`test_pages.py`、`test_render.py`）都在測試裡即時產生小型 PDF，不依賴 fixture registry。關鍵測試會在 0/90/180/270 度旋轉、有無 CropBox 偏移的頁面上畫黑框，確認 `to_region_space` 算出的區域經 PDFium 實際渲染後確實是黑的。F02/F05 的 fixture 驗收、匯入檢查與上傳流程等 #34 與 #5/#6 合併後再接。

## 鎖版解析 Normalization 邊界（#8 基礎）

`lab_parser.normalize` 提供鎖版解析輸出的純記憶體正規化邊界（無任何上游程序啟動、外部檔案讀寫或網路 I/O），嚴格將上游結構映射至 LAB 共同契約（`CapabilityManifest`、`ParseResult`、`EvidenceUnit`、`Observation`）：

- **Unstructured OSS 0.27.36**：
  - 鎖定版本 `0.27.36`、schema `unstructured.elements-0.27.36`，支援 `fast`、`hi_res`、`ocr_only` 策略。
  - `infer_table_structure=True` 限定 `hi_res` 策略。
  - 將 PixelSpace 座標系轉換至已旋轉 CropBox 頂點歸一化空間 `[0, 1]`；版面尺寸不符時不採用座標，頁面標記 `needs_review`。
  - 切割元素（`CompositeElement`、`TableChunk`）解壓縮 `orig_elements`（受限於 16 MB 上限與 zlib 完整性檢查）並展開子 Artifact，保留回源觀測記錄，衍生文字不覆蓋原始元素。
  - 保存 Table HTML（`text_as_html`）與圖像/圖表引用。
  - 偵測到 API 端點（`endpoint_deployment_id`）或 postprocess 標記時隔離（`quarantine`）。
- **MinerU 4.1.7 / DocVortex Middle JSON 2.0**：
  - 鎖定版本 `4.1.7`、schema `docvortex.middle-2.0`（依 `upstream_schemas/docvortex-middle-2.0.json` 嚴格校驗），支援 `flash`、`basic`、`standard`、`advanced` 品質檔位。
  - 透過 `docvortex_layout`（version 1）驗證頁面尺寸與 `DisplayFrame` 一致性；未認證時不採納 block bbox，標記 `unattested_layout_geometry`。
  - 不具備 cell 幾何時宣告 `cell_geometry=unsupported`，不捏造儲存格座標。
  - 全文件後處理旗標（`is_full_document`）必須涵蓋全文件頁面，否則隔離；付費或全文件後處理旗標隔離。
  - 遞迴擷取各區塊（文字、程式碼、公式、標題、圖表/圖片標註）文字，保存 `table_body` HTML 與圖片/圖表資產引用。
- **嚴格錯誤與隔離原則**：
  - 未知版本、未鎖定 schema、schema 漂移、區塊或頁面順序錯誤、非有限數值（NaN/Inf）、重複 JSON 鍵等一律隔離（`batch.quarantine_reason` 設定，頁面 `status="failed"`，清空 evidence）。
  - 幾何缺失、策略降級（`fallback_reason` 存在）、未解析資產引用等標記頁面 `needs_review`，不造假幾何或憑空產生 evidence。
  - 測試使用合成上游結構 fixtures（`tests/parser/test_normalize.py`），明確標註為 synthetic，不宣稱真實模型推論。
  - 真實 20 頁多模態 golden 標註、#5/#6/#7 服務與資料庫持久化整合保持 gated 狀態。
