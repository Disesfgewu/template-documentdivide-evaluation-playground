# 真實 LLM provider 冒煙測試（準備好，目前 blocked: no credentials）

Refs #7、SPEC A42／T81–T84。**目前沒有任何真實 provider 呼叫的證據。** 這份文件與對應程式只是讓「一有憑證就能跑」，在那之前狀態固定記為 `blocked: no credentials`，不是 skipped-as-pass。

## 缺什麼（需要 owner 提供）

| 項目 | 用途 | 放哪裡 |
|---|---|---|
| provider API key | 真實呼叫 | GitHub Actions secret `LAB_LLM_SMOKE_API_KEY` |
| agentKit 唯讀 token | public repo 的 CI 取不到 private `Disesfgewu/agentkit`（見 `docs/AGENTKIT_SUBMODULE.md`） | secret `AGENTKIT_READ_TOKEN`（fine-grained、只讀、只對 agentkit） |
| 端點與模型 | 呼叫目標 | repository／environment variables `LAB_LLM_SMOKE_BASE_URL`（必須是 https）、`LAB_LLM_SMOKE_ALLOWED_HOST`（key 唯一可以送達的主機，必須等於 URL 的主機）、`LAB_LLM_SMOKE_MODEL`（可選 `LAB_LLM_SMOKE_PROTOCOL`，預設 `openai_chat`） |
| 價格 | 計算費用（不得假設為 0） | variables `LAB_LLM_SMOKE_PRICE_IN_MICROS_PER_MTOK`、`LAB_LLM_SMOKE_PRICE_OUT_MICROS_PER_MTOK`（每百萬 token 的 micro-USD） |

SPEC 另外要求 embedding 與 vision 各一個真 smoke（可共用 provider）；這裡只準備了 chat，embedding／vision 還沒有。

## 程式在哪裡、做什麼

`services/worker/agentflow/smoke_real_test.go`（agentKit 隔離模組，需要私有 checkout，見 `scripts/check.sh --scope worker-agentkit`）。`TestRealProviderSmoke` 用 worker 圖實際使用的路徑 `ChatCall` → `NodeRunner` → `Ledger` 發出**一次**固定模型（pin）的 chat 呼叫（prompt 固定、`max_output_tokens=16`、`max_retries=1`、逾時 60 秒），並寫出報告：

- 狀態 `passed`／`failed`／`blocked`、provider 主機名（只有 host）、模型、協定；
- `usage_known`、輸入／輸出 token、`charged_micros`（用量未知時是 `-1`，**不是 0**）、transport 嘗試次數、耗時；
- 回覆文字只記長度與 sha256；報告不含 key、prompt 或模型文字。

**金鑰保護**：URL 必須是 https、不得含 userinfo／query／fragment，且主機必須等於 `LAB_LLM_SMOKE_ALLOWED_HOST`；HTTP client 永遠不跟隨轉址（部分協定的 key 放在 Go 不會在跨主機轉址時移除的標頭，測試證明轉址目標收到 0 個請求，並以突變驗證）；IP 位址、私有／link-local／metadata 位址、單一標籤與內部網域名稱的主機一律 `real=false`（不能算真 provider）。價格變數有上限（每百萬 token 1,000 USD）。報告另含 `request_sha256`、回應的模型 id；失敗原因都是固定字串。

通過條件：真實執行必須回報用量（`usage_known` 且 token 數大於 0，否則 failed）；回覆非空；用量已知時帳本花費等於依價格計算的費用；用量未知時帳本花費為 0 且保留 reservation（outcome unknown，不得結算成費用）；端點不是 loopback（本機假端點永遠不算真 provider）。

沒有憑證時測試寫出 `status: blocked` 並 `t.Skip`；Go 的 skip 不是通過。

## 在 CI 執行

`workflow_dispatch` 勾選 `llm_smoke`（job `llm-smoke`），**只在 `main` 上執行**並使用受保護的 environment `llm-smoke`（請在該 environment 設定 required reviewers，並把 secrets 放在那裡）；在其他 ref 上 dispatch 會跳過這個 job，secrets 不會交給分支上的程式碼。缺少任何一個 secret／variable 時該 job **失敗**並標示 `BLOCKED: no credentials or configuration`（報告 `status: blocked`），所以紅燈就是「還沒證明」。全部齊備時：以只限單一指令的 token 取出 agentKit pin，先跑私有模組套件的全部測試（`go vet`、`go test ./...`，包含對本機假端點的 harness 測試；**這是這些測試唯一的 CI 路徑**，一般 PR 的 CI 不編譯私有模組，所以它們平常只在有 agentKit 存取權的本機跑），再跑 `TestRealProviderSmoke`，並用報告確認 `status == passed` 且 `real == true` 才算通過；報告以 artifact `llm-smoke` 保存。

安全：這個 job 只能手動 dispatch，且 token 只會交給 dispatch 時選定的 ref；不要對未審查的分支或 fork 啟動它（`docs/AGENTKIT_SUBMODULE.md`：不得把私有 checkout 憑證交給不受信任的 PR 程式）。

## 還沒做（記錄，不宣稱）

- 沒有任何真實 provider 的實跑；`traceability.yaml` 未更新，T81–T84、A42 仍未通過。
- Python worker 目前不呼叫 LLM；真實呼叫屬於 Go/agentKit 圖（SPEC 12.2），尚未接到 `document.parse` 之後的建題流程。
- 沒有 vision／embedding smoke，沒有結構化輸出驗證，沒有持久化的 operation／budget store（目前 `MemoryOperationStore`）。
