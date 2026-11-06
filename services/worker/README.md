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

## agentKit smoke（#28，需私有存取）

`services/worker/agentkitsmoke/` 是獨立的 Go module，用 `replace` 指向 `third_party/agentkit` submodule，所以一般 clone、`services/worker` 的 `go build ./...` 與公開 CI 都不會編譯它，也不需要私有憑證。有 agentKit 讀取權限的 checkout 先依 `docs/AGENTKIT_SUBMODULE.md` 初始化 submodule，再執行：

```sh
scripts/check.sh --scope worker-agentkit
```

這個 suite 不在 `--scope all/auto` 內。它會確認 gitlink 與實際 checkout 是同一個 commit，執行 agentKit 的 `go test ./graph ./model`，再跑 smoke：以真正的 `agentkit/graph` 建一個確定性的本地流程（inspect → 依頁數條件路由到 parse/finalize 或 reject），驗證條件路由、路由錯誤會停止、以及 checkpoint/resume——parse 第一次模擬崩潰後，換新的 graph 實例從 checkpoint 續跑，已提交的 inspect 不會重跑；checkpoint 同時用記憶體與只存 JSON bytes 的 store 測試。agentKit 的稽核 log 寫到測試暫存目錄，並在結束時以 `agentlog.Verify` 驗證雜湊鏈。沒有 submodule 時回報 BLOCKED，不算通過。持久 store、lease/fencing 與真 provider 屬 #6。
