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

## Image-bearing VisionReader boundary (#6 foundation)

`lab_parser.vision` provides a callable boundary for a **dedicated injected
vision transport**. It validates actual image bytes before budget admission:
SHA-256, CRC/chunk order, the renderer's RGB8 PNG profile, bounded zlib output,
filter-0 scanlines, dimensions, per-image 4096 px and total 16 MP limits. It
accepts at most eight crops with distinct image hashes per call and never converts an image reference into a text
prompt pretending to read it. Other PNG profiles/JPEG require a separately
validated adapter; this module does not silently transcode or resize them.

```python
import hashlib
import time
from lab_parser.render import render_region
from lab_parser.vision import (
    VisionCall, VisionCapability, VisionImage, read_vision,
)

# pdf_bytes and an authorized committed crop artifact are supplied by caller.
rendered = render_region(pdf_bytes, page_index=0, dpi=150)
image = VisionImage.from_rendered(
    rendered, artifact_ref="committed-crop-id", document_id="document-id",
    document_sha256=hashlib.sha256(pdf_bytes).hexdigest(), page_count=page_count,
)
call = VisionCall(
    operation_id="vision-operation-id", model_revision="pinned-model-revision",
    prompt_revision="vision-prompt-v1", prompt="Describe the visible labels.",
    images=(image,), max_output_tokens=512,
)
capability = VisionCapability(
    adapter_revision="vision-adapter-v1", model_revision=call.model_revision,
    vision=True, max_images=8, max_total_pixels=16_000_000,
    max_output_tokens=512,
)
result = read_vision(
    call, capability=capability, transport=dedicated_vision_transport,
    admit=durably_reserve_budget, deadline=time.monotonic() + 30,
    cancelled=is_job_cancelled,
)
```

The application supplies all named callbacks. `transport(call, timeout_s)`
receives the actual immutable `image.png` bytes and returns bounded UTF-8 JSON
matching `VisionResponse` (inspect `VisionResponse.model_json_schema()`). Its
operation/model/prompt and ordered image hashes must match the call; observations
reference only submitted hashes and always carry `verification="unverified"`.
`kind` is `visible_text` or `visual_description`, `text` is a bounded model
observation, and `confidence` is nullable. Empty observations are permitted for
unreadable images. Unknown input/output token use remains null. The successful
result retains the selected adapter capability, crop provenance, original
restricted response bytes/hash and `status="needs_review"`.

`admit` must durably reserve before returning exactly `True`. The boundary makes
one transport call with no retry or repair, checks cancellation/deadline before
and after admission/call/validation, and withholds late results. It cannot force
an arbitrary synchronous callback to stop: the transport must honor its timeout
and the caller must reconcile cancellation, reservations, usage and
`outcome_unknown`. `VisionError` exposes a fixed code; bounded post-dispatch
responses are retained in `restricted_response` for restricted audit. Oversized
responses and exceptions have no retained payload; the transport/caller must
record their bounded diagnostics and usage. Never put image bytes, prompt,
response or `restricted_response` into ordinary logs or public artifacts.

Image SHA proves byte identity, not derivation from the declared PDF. The caller
must authorize the artifact/project and PDF identity. This boundary checks page
range, normalized crop and renderer-transform self-consistency, and snapshots
mutable transform metadata; it does not independently re-render the PDF to prove
origin. Model/adapter identity is a declared pinned selection, not attestation of
a remote backend. Delivery of image bytes is not proof the interpretation is
correct. Descriptions stay separate from OCR/raw text, verified facts and gold.

Actual coverage is the deterministic R08/R20, T25/T29, A39 input-boundary
subcases in `tests/parser/test_vision.py`, including real PDFium render-to-callback
image delivery. It does **not** claim a real vision provider, semantic chart
reading, human quality gates, durable accounting/trace, PostgreSQL/worker wiring,
T84/T104 or complete #6 acceptance. Those remain integration/release gates.
