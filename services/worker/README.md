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

## agentflow：worker 的 agentKit 整合核心（#6 子任務）

`services/worker/agentflow/` 也是獨立 module（同樣只在有 agentKit submodule 的 checkout 編譯，由 `--scope worker-agentkit` 以 `-race` 執行）。目前提供不依賴資料庫的語意核心，PostgreSQL 版本等 #5 的 schema 合併後實作，必須通過同一組測試：

- `GraphStateCodec`（`codec.go`，`lab-state-codec-1`）：每個值帶型別標籤，整數用十進位字串、浮點數用精確十六進位，int/int64/float64/巢狀 map/list/`Ref` 恢復後型別不變（T64）；不支援的型別、NaN/Inf 與非正規編碼一律拒絕；`StateHash` 與 map 順序無關、對 1 與 1.0 不同；`ValidateEnvelope` 檢查 SPEC 12.2 要求的 state 欄位與 schema 版本。
- `MemoryCheckpointDB` + fenced `graph.Store`（`store.go`）：thread 全域唯一並綁 project 與 graph revision；lease 60 秒、heartbeat 15 秒，fencing token 只增不減，過期或被接手的 worker 寫入一律拒絕（T62/A34），checkpoint sequence 唯一遞增並記錄 token、revision、codec 與 state hash；revision 不符時不從舊 LastNode 續跑（T60）；讀回時驗證 hash。
- `NodeRunner`（`operation.go`）：以 (work_item_id, node_revision, logical_invocation_id) 記錄 operation，外部呼叫前先 Begin；成功結果先持久化再讓 graph 存 checkpoint，重跑直接沿用（T61）；回應遺失、呼叫中取消或逾時、或重跑時發現 started 都標 `outcome_unknown`，不再自動重送（T63）；每個 logical call 最多 3 次 attempt，attempt 與用量分開記錄；取消後不再預留或送出新呼叫（T29）。
- `Ledger`（`budget.go`）：以 micro 單位整數記帳，`spent+reserved+next ≤ limit` 才可預留，`outcome_unknown` 保留預留直到核對；並行搶最後預算不會超限（T28）。

- `ChatCall`（`model.go`）：把一次 pinned 的 agentKit `model.Client.Chat`（`Router`/`RemoteClient`/`HTTPClient`）包成 NodeRunner effect。transport retry 只由 agentKit HTTPClient 負責：`ValidateModelConfig` 要求 `max_retries` 明確為 1–2（每個 logical call ≤3 次；ce49a71 的 0 代表 5 次重試，因此拒絕）、`timeout_seconds` 與 `requests_per_minute` 明確設定；包成 node 時 runner 必須 `MaxAttempts 1` 且無 `Retryable`，避免重試層數相乘（T27）。必須 pin Router entry（不 failover），回應來自其他模型即拒絕（T26）；`max_output_tokens` 必填（agentKit 會默默補 1024）。HTTP 狀態錯誤是已知失敗；無狀態的傳輸錯誤與呼叫中取消是 `outcome_unknown`；回應沒有 usage 時保留預留為 unknown，不當成 0（T29）。

尚未完成：PostgreSQL 版 store/operation/ledger（依賴 #5）、scheduler、embedding 與 vision 呼叫、JSON 修復上限（T19）、TraceBridge/AuditImporter。
