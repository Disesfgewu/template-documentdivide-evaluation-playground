# 測試入口

```bash
python3 -m pip install -r requirements-dev.txt
scripts/check.sh                 # 依變更檔案選擇 suite（相對 origin/main）
scripts/check.sh --scope all     # 全部 suite（main push / 發布 gate 使用）
```

- CI：`.github/workflows/ci.yml` 的 `checks` job。PR 走 `--scope auto`，main push/手動走 `--scope all`。
- 未對應任何 suite 的路徑會觸發全部 suite；任一 suite 0 個測試即失敗（防空綠）。
- `api`：LAB API 設定驗證、`/healthz`、`/readyz`（含連不到的真資料庫、artifact 目錄權限），需 `pip install -r services/api/requirements.lock`。
- `api-db`：真 PostgreSQL＋pgvector 就緒測試（建立有/無 `vector` extension 的暫時資料庫並驗證 200/503）。需 `LAB_TEST_DATABASE_URL`（可建立資料庫與 extension 的角色），例如 `docker compose -f deploy/compose.dev.yml up -d --wait` 後設為 `postgresql://lab:lab-dev-only@127.0.0.1:55432/lab`。未設定時 check.sh 印 `BLOCKED` 並以 exit 3 失敗；此 suite 有任何 skipped 也算失敗。CI 以同一份 compose 啟動資料庫。
- `worker`：Go worker（`services/worker`）的 gofmt、`go vet`、`go test`、build。Go 版本依 `services/worker/go.mod`，以 `GOTOOLCHAIN=local` 執行不自動下載；沒有 `go` 時 check.sh 印 `BLOCKED` 並以 exit 3 失敗。CI 用 `actions/setup-go` 依 go.mod 安裝。
- `parser`：parser service 設定、`/healthz`、`/readyz`、`/v1/capabilities`（不宣稱 adapter 可用），以及頁面幾何（SPEC 4.1 座標互轉、可見框、旋轉、真實 `/PageLabels`）與 PDFium L0 渲染（旋轉頁上畫框後實際渲染比對、確定性 PNG、19.3.8 像素上限）；另含固定 F01/F02/F05 registry 驗收（[範圍與重現指令](PDF_FIXTURE_ACCEPTANCE.md)），保留產物 hash、旋轉／CropBox 負例與真 PDFium 證據。PDF 都在測試裡即時產生，需 `pip install -r services/parser/requirements.lock`。
- `web`：React 前端（`web/`）的 `npm ci`（lockfile 鎖版，`engine-strict`）、vitest、`tsc` 型別檢查與 production build，需 Node.js 22.12 以上。沒有 node/npm 時印 `BLOCKED` 並以 exit 3 失敗。
- `contracts`：基礎資料契約（`contracts/`）的合法/非法範例（非法範例必須只因所標示的原因被拒）、schema 漂移檢查、以獨立的 `jsonschema` 驗證匯出的 schema，以及 #21 關閉條件逐項列出的正反例。需要 `pip install -r contracts/requirements.lock -r requirements-dev.txt`。
- `governance` 也會跑 `tools.governance.check_traceability`：`docs/traceability.yaml` 必須涵蓋 SPEC 全部 R/T/A 編號且不多列；status 只能是 not_started、partial、blocked、passed；非 not_started 必須有 evidence 與 note；passed 必須有 main commit、本 repo 的 CI run 與測試，且離線檢查一律拒絕 passed：必須以 `python3 -m tools.governance.check_traceability --verify-provenance` 向 GitHub 確認該 run 屬於本 repo、在 main 上以該 commit 執行且結論為 success，並且該 commit 在 main 上；evidence 引用的測試檔與具名測試都必須實際存在（具名測試只支援 Python `檔案::類別[.方法]` 與 Go `x_test.go::TestX`，其他檔案不可帶名稱；具名測試不得用 glob 路徑），改名或刪除的測試不算證據；測試檔必須是測試執行器會收集的檔案（Python `tests/**/test*.py` 或 Go `*_test.go`），`LICENSE` 之類的檔案不算。passed 另外需要執行證據：該 run 必須是 `.github/workflows/ci.yml` 的完整檢查（push 或 workflow_dispatch），且 run 的 `test-reports` artifact（`tools/governance/run_tests.py` 在設定 `LAB_TEST_REPORT_DIR` 時寫出每個測試的結果）裡，引用的每個測試都必須是 passed；skipped、failed、未執行、沒有報告或報告已過期都不算。Go 測試由 worker suite 以 `go test -json` 執行，`tools.governance.go_test_report` 轉成同格式的 `services_worker_go.json`（鍵為 `services/worker/<目錄>/<檔>_test.go::TestX`，只記頂層測試；子測試失敗即父測試失敗；檔案只從 `go list` 在相同建置設定（環境與 `--tags`）下回報的有效測試檔中找，被 build constraint 排除的檔案不會被記功，同名測試出現在多個有效檔案時直接失敗），因此 Go 測試同樣可作 passed 證據；web（npm）測試尚未產生報告，不能作 passed 證據。引用的測試也必須存在於該 commit（以 contents API 讀取），而不只是目前的 checkout。`scripts/check.sh` 的 governance suite 一律以 `--verify-provenance` 執行（CI 以 `GH_TOKEN` 與 `actions: read` 查詢 run）；只有 passed 項目才會查 GitHub，查不到或無憑證時 passed 項目失敗（屬 blocked，不算通過）。
- `fixtures`：`fixtures/registry.yaml` 登錄 F01–F12。partial/available 的項目會由產生器重建，並比對固定的 sha256；golden 來源不得是模型輸出；available 必須有可追溯的人工覆核紀錄：`human: true`、真實且不在未來的日期、`method`、覆核者的 GitHub 帳號（不得是 agent/bot 身分）、`signoff`（本 repo 內該覆核者的 GitHub 留言或 review 網址），以及 `fixture_digest`（`registry_check.fixture_digest` 對所覆核項目 id 與 sha256、以及 expected/golden 內容的雜湊；任一項目或預期值改變後即視為過期）。`human: true` 只在簽核紀錄被實際解析後才接受：`--verify-signoff`（check.sh 會執行）向 GitHub 讀取該留言或 review，要求它位於網址所指的 Issue/PR、作者就是 `by` 且為一般使用者（非 bot），並含有獨立一行 `FIXTURE-SIGNOFF <fixture id> <fixture_digest>`；離線或無法連上 GitHub 時一律不接受。F01 目前有 1/19/20/21/500/600 頁的合成 PDF，只有純文字的素材不足版本，並以 pypdf 讀回頁數驗證 T14。F02 目前有旋轉 0/90/180/270、CropBox 偏移、印刷頁碼與雙欄標記框的合成 PDF；預期座標（SPEC 4.1 空間）以 PDFium 實際渲染獨立比對，作為 #7 的 T05 基準。F05 目前有空檔、假 PDF、截斷、xref 損壞（可修復）、RC4 加密、2001 頁與內含指令注入文字的 PDF，每個檔案都以 pypdf strict 模式確認它具有所宣稱的特性；預期的匯入行為（拒絕、標記或當作資料接受）記在 `fixtures/F05/expected.json`，供 #7 使用。F06 的評分向量本身由 `contracts` suite 依 16.5 公式重算（`tests/contracts/test_score_vectors.py`），這裡只檢查它的登錄與 sha256。PDF 二進位檔不提交，由產生器重建。需要 `pip install -r requirements-dev.txt -r contracts/requirements.lock`。
- `governance`（必要檔、PR/Issue templates、workflow 無空 job/`continue-on-error`/`|| true`、CODEOWNERS 身份核實、相對連結、密鑰樣式掃描、branch 清理判定）。後續 Issue 新增應用程式 suite 時需在 `scripts/check.sh` 註冊並對應路徑。
- 本文件不代表 T01–T104 已執行；各 PR 驗證表須列實際指令、SHA 與結果。

## 合併後 branch 清理

```bash
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N>            # dry run
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N> --execute  # SHA lease 刪除（git push --force-with-lease），刪後以 ls-remote 與 API 驗證；失敗回 cleanup_pending（exit 2）
```

僅當 PR merged、同 repo、非 default/main/protected、目前 branch head 等於該 PR head、且無其他 open PR 以其為 head/base 時才刪除；否則輸出 `cleanup_pending` 與原因，exit 2。
