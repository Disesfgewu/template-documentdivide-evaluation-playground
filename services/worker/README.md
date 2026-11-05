# LAB worker（Go 骨架）

#3 的 Go workflow worker 骨架：設定驗證（規則與 LAB API 相同）與 `/healthz`。agentKit graph、PostgresGraphStore、job 執行屬於 #6，這裡尚未實作，也沒有 `/readyz`（沒有依賴檢查之前不提供假就緒）。

## 工具鏈

- Go 版本以 `go.mod` 的 `go 1.27.1` 為準（SPEC §12.1：agentKit `go.mod` 宣告 Go 1.27.1）。CI 用 `actions/setup-go` 依 `go.mod` 安裝，並以 `GOTOOLCHAIN=local` 執行，不會自動下載其他版本。
- 目前只用標準函式庫，沒有外部依賴，所以沒有 `go.sum`。

## agentKit 鎖定（尚未完成，blocked）

SPEC 指定 agentKit commit `ce49a7130b17ccda07c755de96ef44c49d0b719c`。`Disesfgewu/agentkit` 是 private repo，而本 repo 是 public：

- CI 的 `GITHUB_TOKEN` 讀不到其他 private repo，需要 maintainer 提供唯讀憑證（deploy key 或 fine-grained PAT，存成 Actions secret）並設定 `GOPRIVATE`；
- 也要確認 public repo 依賴 private module 是否可接受（外部 clone 的人無法建置）。

在這之前 `go.mod` **不會**寫入 agentKit require，避免沒有 `go.sum`、沒有實際編譯驗證的假鎖定。

## 執行

```bash
cd services/worker
export LAB_ENV=dev LAB_PROVIDER_MODE=mock \
       LAB_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab
go run ./cmd/lab-worker check-config
go run ./cmd/lab-worker serve          # LAB_WORKER_HTTP_ADDR，預設 127.0.0.1:8081
curl -s localhost:8081/healthz
```

`LAB_DATABASE_URL` 目前只驗證格式，不會連線（連線與 migration 在 #5/#6）。
