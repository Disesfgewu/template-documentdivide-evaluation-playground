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
- `parser`：parser service 設定、`/healthz`、`/readyz`、`/v1/capabilities`（不宣稱 adapter 可用），需 `pip install -r services/parser/requirements.lock`。
- `web`：React 前端（`web/`）的 `npm ci`（lockfile 鎖版，`engine-strict`）、vitest、`tsc` 型別檢查與 production build，需 Node.js 22.12 以上。沒有 node/npm 時印 `BLOCKED` 並以 exit 3 失敗。
- `contracts`：基礎資料契約（`contracts/`）的合法/非法範例（非法範例必須只因所標示的原因被拒）、schema 漂移檢查、以獨立的 `jsonschema` 驗證匯出的 schema，以及 #21 關閉條件逐項列出的正反例。需要 `pip install -r contracts/requirements.lock -r requirements-dev.txt`。
- `db`：真 PostgreSQL 的 migration（套用、重跑、checksum 竄改、失敗回滾、舊資料升級）、約束（跨專案、重複 revision、缺引用、凍結、audit append-only、artifact 生命週期）、交易（T48 同時 20 個相同 key、T49 audit/outbox 失敗回滾）、角色矩陣、gold RLS 隔離、job 狀態機、outbox 消費、artifact 清理與可取得性。每個測試類別建立一個新資料庫，需要 `LAB_TEST_DATABASE_URL`；沒有時印 `BLOCKED` 並以 exit 3 失敗，任何 skipped 也算失敗。
- `db-unit`：不需資料庫的 `lab_db` 程式（匯出、redaction、本機 artifact store）。
- `governance`（必要檔、PR/Issue templates、workflow 無空 job/`continue-on-error`/`|| true`、CODEOWNERS 身份核實、相對連結、密鑰樣式掃描、branch 清理判定）。後續 Issue 新增應用程式 suite 時需在 `scripts/check.sh` 註冊並對應路徑。
- 本文件不代表 T01–T104 已執行；各 PR 驗證表須列實際指令、SHA 與結果。

## 合併後 branch 清理

```bash
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N>            # dry run
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N> --execute  # SHA lease 刪除（git push --force-with-lease），刪後以 ls-remote 與 API 驗證；失敗回 cleanup_pending（exit 2）
```

僅當 PR merged、同 repo、非 default/main/protected、目前 branch head 等於該 PR head、且無其他 open PR 以其為 head/base 時才刪除；否則輸出 `cleanup_pending` 與原因，exit 2。
