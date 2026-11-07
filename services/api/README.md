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
- 對外端點政策驗證（`lab_api.endpoint_policy`）：依 SPEC §12／R19／T72 提供獨立的 URL 與解析後 IP 驗證原語（`EndpointPolicy`、`validate_endpoint_url`）。
  - 採預設拒絕（fail-closed）與精確 `(hostname, port)` 白名單，無通用萬用字元或子網域自動放行；預設僅允許 `https`（需顯式設定 `allow_http=True` 才接受 `http`），拒絕其他 scheme、userinfo／憑證、query、fragment、路徑穿越與非標準數值／十六進位／八進位 IP 表示法。
  - 對所有解析出的 IPv4／IPv6 位址強制檢查全域公開單播範圍，拒絕 loopback、private、link-local（含 `169.254.169.254` metadata）、CGNAT、benchmark、documentation、multicast、unspecified，以及 IPv4-mapped（`::ffff:0:0/96`）、IPv4-translated、NAT64、6to4、Teredo、deprecated IPv4-compatible 等映射或隧道形式，除非由部署端以明確非 `/0` CIDR 白名單（`allowed_nonpublic_cidrs`）授權。任一筆解析結果不合規即整批拒絕，錯誤僅回固定錯誤碼、不回顯原始 URL 或密鑰。
  - **呼叫端義務與範圍限制**：此模組僅為純政策與位址驗證基礎原語，**不包含**即時 HTTP 客戶端整合、不執行連線層 DNS pinning、不處理或跟隨 HTTP redirect，亦不單獨構成完整 SSRF 防護。實際發起連線的呼叫端必須將驗證通過的 `resolved_ips` 直接綁定至 socket／連線層（或於連線建立後核對遠端 peer IP）以防 DNS rebinding／TOCTOU，必須停用自動 redirect（或對每次目標重新執行完整政策驗證），忽略未受控的環境變數 proxy，並強制執行請求 timeout 與回應大小上限。
