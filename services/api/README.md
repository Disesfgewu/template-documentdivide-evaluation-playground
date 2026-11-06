# LAB API（FastAPI 骨架）

#3 的第一個增量：設定驗證、liveness/readiness，以及真 PostgreSQL＋pgvector 的就緒檢查。業務端點、migration、權限在後續 Issue（#4、#5）。

## 乾淨環境啟動

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r services/api/requirements.lock
docker compose -f deploy/compose.dev.yml up -d --wait     # PostgreSQL 17 + pgvector 0.8.6（digest 鎖定）

export LAB_ENV=dev LAB_PROVIDER_MODE=mock \
       LAB_DATABASE_URL=postgresql://lab:lab-dev-only@127.0.0.1:55432/lab \
       LAB_ARTIFACT_ROOT="$PWD/.lab-artifacts"
mkdir -p "$LAB_ARTIFACT_ROOT"
(cd services/api && python -m lab_api check-config)
(cd services/api && uvicorn --factory lab_api.main:create_app --port 8000)
curl -s localhost:8000/healthz; curl -s localhost:8000/readyz
```

變數說明見 `services/api/.env.example`（程式不讀 .env 檔）。

## 行為

- `/healthz`：只表示程序存活，不檢查依賴。
- `/readyz`：真的連資料庫並確認 `vector` extension 存在，並在 artifact 根目錄實際建立後刪除暫存檔。任一項失敗回 503 `not_ready`；檢查本身拋例外也算失敗。回應只帶例外類別名稱，不回傳連線字串或主機資訊。
- 設定：`LAB_ENV`、`LAB_PROVIDER_MODE`、`LAB_DATABASE_URL`、`LAB_ARTIFACT_ROOT` 沒有預設值，缺少或不合法時啟動失敗。資料庫只接受 `postgresql://`（不以 SQLite 代替）。
- mock 與真 provider 分開：`mock` 是本機決定性假回應；`real` 目前**直接拒絕啟動**，因為尚無真 provider adapter（T84 待後續）。`prod` 禁止搭配 `mock`。

## Method-service HTTP transport foundation (#11)

`lab_api.method_client.MethodClient` makes one real HTTP request per operation,
using the merged `lab_contracts.method` models. It implements capabilities,
prepare acknowledgement, synchronous/asynchronous query acknowledgement, one
job poll, and cancellation acknowledgement. It validates terminal query echo
IDs and prepare document/method manifests against the original request.

The configured origin is the sole operator allowlist entry: only an HTTP(S)
IP-literal origin is supported in this increment. DNS hostnames, URL credentials,
path prefixes, query/fragment configuration, redirects and environment proxies
are unsupported. The standard TLS certificate verifier remains enabled. Private
IP origins are allowed only because deployment explicitly chooses the method
service address; this client is not an arbitrary user URL fetcher. Future method
registration must authorize the configured origin before constructing it.

Every response has a whole-exchange timeout (including a slowly trickled body)
and at most 16 MiB of body bytes. Per-instance limits may only lower these
bounds. Only JSON with the exact expected status and contract is accepted;
duplicate keys, non-finite values, schema drift and wrong bindings fail with a
stable `MethodClientError.code` without remote body or URL diagnostics.

`query(request, deadline=...)`, `poll(job_id, request, deadline=...)` and
`cancel(job_id, request, deadline=...)` require an **absolute `time.monotonic()`
deadline**. The caller establishes it before first query dispatch (no later
than the query budget) and reuses it across queueing/polls/retries; each call
also respects the shorter configured transport timeout. A 202 acknowledgement
is not a completed operation or proof of cancellation. No automatic retry or
poll loop is performed. A durable scheduler must retain original job/request
binding, polling cadence and the overall deadline, and persist intent before
network dispatch. This transport does not implement those durable integrations.

Run the real local TCP service tests with
`PYTHONPATH=services/api:contracts python -m unittest tests.api.test_method_client`
or `scripts/check.sh --scope api`. Tests cover sync/async, prepare/cancel,
strict JSON, binding mismatches, redirects, 503 without retry, bounded chunked
bodies and a slow-trickle deadline. The HTTP test server is synthetic; this is
not a real method/provider acceptance or T30/T32 end-to-end result. #11's
PostgreSQL jobs, authorization, refreshable download grants, participant PDF
parsing, vector store and two independent example methods remain unfinished.
