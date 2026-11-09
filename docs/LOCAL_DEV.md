# 乾淨環境啟動（本機開發）

這是 #3 的骨架階段。本文件只描述目前**真的存在**的服務與檢查，不代表 #5 以後的功能已經完成。

## 需要的工具

| 工具 | 版本來源 | 用途 |
| --- | --- | --- |
| Docker（含 compose v2） | `deploy/compose.dev.yml` 以 digest 鎖定 `pgvector/pgvector:0.8.6-pg17` | PostgreSQL＋pgvector |
| Python 3.12 | `services/api/requirements.lock`、`services/parser/requirements.lock` | LAB API、parser service |
| Go | `services/worker/go.mod`（`GOTOOLCHAIN=local`，不自動下載） | worker |
| Node.js 22.12 以上 | `web/package.json` engines、`web/package-lock.json` | web |

缺任何一項，`scripts/check.sh` 對應的 suite 會印 `BLOCKED` 並以 exit 3 失敗，不會當作通過。

## 步驟（在 repo 根目錄執行）

```bash
# 1. 資料庫：第一次啟動時由 deploy/postgres/init 建立 vector extension
docker compose -f deploy/compose.dev.yml up -d --wait

# 2. 共用設定（帳密只給本機拋棄式容器用）
export LAB_ENV=dev LAB_PROVIDER_MODE=mock \
       LAB_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab \
       LAB_ARTIFACT_ROOT="$PWD/.lab-artifacts"
export LAB_PARSER_ENV=dev LAB_PARSER_ARTIFACT_ROOT="$LAB_ARTIFACT_ROOT"
mkdir -p "$LAB_ARTIFACT_ROOT"

# 3. Python 依賴
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -r services/api/requirements.lock -r services/parser/requirements.lock

# 4. 先驗證設定，再啟動（每個服務各開一個終端機）
(cd services/api && python -m lab_api check-config)
(cd services/api && uvicorn --factory lab_api.main:create_app --port 8000)
(cd services/parser && python -m lab_parser check-config)
(cd services/parser && uvicorn --factory lab_parser.main:create_app --port 8100)
(cd services/worker && go run ./cmd/lab-worker check-config && go run ./cmd/lab-worker serve)
(cd web && npm ci && npm run dev)

# 5. 確認
curl -s localhost:8000/readyz   # database 與 artifact_storage 都 ok 才是 200 ready
curl -s localhost:8100/readyz
curl -s localhost:8081/healthz
```

服務的啟動參數與設定細節，請見各服務目錄下的 `README.md`（`services/api/`、`services/parser/`、`services/worker/`、`web/`）。容器化執行（API 與 parser service 的非 root Docker runtime 映像）請見 [Docker 執行映像說明](../deploy/README.md)。

`LAB_PROVIDER_MODE=mock` 是本機開發用的路徑，畫面會標示「本機假資料，非真模型」。`real` 模式目前會被拒絕（沒有真 provider adapter，T84 還沒做），不會偷偷退回 mock。

## 清除重來

```bash
docker compose -f deploy/compose.dev.yml down -v   # 連同 lab-db volume 一起刪除
rm -rf .lab-artifacts
```

## Artifact storage：本機目錄 backend（PM 於 #3 Q3 接受）

目前的 artifact storage 就是一個本機目錄（`LAB_ARTIFACT_ROOT`；parser 用 `LAB_PARSER_ARTIFACT_ROOT`，本機開發指向同一個目錄）。

已經做到的部分：

- **啟動前驗證**：路徑必須是絕對路徑，沒有預設值。缺少或不合法時 `check-config` 和啟動都會失敗。
- **可寫檢查**：`/readyz` 會在根目錄實際建立暫存檔再刪掉。目錄不存在、不是目錄或不可寫時回 503，錯誤只帶例外類別名稱。
- **持久路徑**：資料放在主機目錄，服務重啟後還在。預設 `.lab-artifacts/` 已列入 `.gitignore`，不會被 commit。

**明確限制**（還沒做到，不能拿這個 backend 宣稱以下項目已完成）：

- 只適用單機。沒有複寫，也沒有跨主機共享；多個副本同時寫入時沒有任何鎖定或一致性保證。
- 目前沒有任何服務真的寫入 artifact。上傳、原 PDF、頁面影像與資產屬於 #7；manifest、hash、交易與權限屬於 #5。
- 沒有內容 hash 驗證，也沒有 immutable 保護。檔案可能被主機上的其他程序改掉或刪掉，而且不會被發現。
- 沒有備份與還原，這部分在 #14。`down -v` 和 `rm -rf` 會永久刪除資料。
- 權限沿用主機檔案系統，沒有應用層的存取控制，也沒有 gold 資料隔離。
- 不提供 S3 相容 adapter。實際採用遠端部署時，要在對應的 Issue 裡驗收選定的 backend。

請不要把 proprietary PDF、gold 資料或 provider 原始 payload 放進 repo 或公開 artifact。
