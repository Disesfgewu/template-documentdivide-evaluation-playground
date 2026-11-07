# 發布證據盤點與查核（Release Evidence Inventory & Checker）

本文依據 [SPEC v0.1 第 18、19 節](document-seperater-playground_SPEC_v0.1.md) 與 Issue #15 規範，定義評測平台正式發布的強制證據清單、六項欄位血緣要求、六大真實閘門（Real Gates）以及 fail-closed 查核機制。

## 1. 核心原則與邊界

1. **強制完整性（Mandatory SPEC Coverage）**：
   - 包含 R01–R30（30 項強制需求）、A01–A48（48 項驗收準則）、T01–T104（104 項測試情境），合計 182 個強制 SPEC 項目。
   - 所有項目必須全部於 main 分支具備有效證據且為 `passed`，缺少任何一項、或存在任何 `blocked` / `skipped` / `missing` / `partial` / `not_started` 狀態，均一律阻擋正式發布（fail-closed）。
2. **六項欄位血緣（6-Part Provenance Contract）**：
   - 每筆通過發布驗證的測試證據必須具備：
     1. `commit`：main 分支上的 40 碼十六進位 SHA（非全同字元或虛構代碼）。
     2. `schema`：資料契約／綱要版本（例如 `v1`、`lab-schema-v0.1`）。
     3. `fixture`：固定測試集識別碼或雜湊（例如 `F01`、`F10`、sha256）。
     4. `profile`：執行環境、設定檔或亂數種子（例如 `ci-linux-x86_64`、`real-profile-v1`）。
     5. `actual`：實際執行結果（必須為 `passed`）。
     6. `trace`：可追溯之 CI run 網址、追蹤 ID 或 artifact 參照。
3. **禁止以 Mock 代替真實閘門（No Mock on Real Gates）**：
   - 發布門檻必須區分 mock 與真服務。下列六大閘門涉及關鍵整合、真實解析器、模型供應商、資料庫、人工覆核、效能量測與還原驗證，**嚴禁以 mock、stub 或虛構結果充數**：
     - **真實解析器閘門（Real Parser Gate）**：涵蓋 T85–T88、T104、A43、A48、R26、R30。要求鎖版之 Unstructured OSS 及 MinerU 真實解析器執行紀錄與產物，不得使用 `mock` 或未鎖版 latest。
     - **真實模型供應商閘門（Real Provider Gate）**：涵蓋 T84、T104、A42、A48、R08、R25、R30。要求真實 chat / embedding / vision 呼叫與用量紀錄，不得使用 `provider_mode: mock`。
     - **真實資料庫閘門（Real DB / pgvector Gate）**：涵蓋 T47、T49、T50、T83、A21、A22、A42、R13、R17、R30。要求真 PostgreSQL＋pgvector 容器／執行個體、外鍵約束、遷移與交易復原，不得使用 SQLite 或記憶體假資料庫。
     - **人工審核閘門（Human Review Gate）**：涵蓋 T21、T84、T100、A44、A46、R06、R25。要求真實人類簽核（非 bot 或自動放行），覆核歧義題目、OCR 衝突與 judge 校準。
     - **效能與規模閘門（Performance & Scale Gate）**：涵蓋 T81、T82、T104、A01、A48、R24、R25、R30。要求實測 500/600 頁處理流程及 RSS、VRAM、耗時等實際指標，不得以合成單頁替代。
     - **備份與拋棄式還原閘門（Restore Gate）**：涵蓋 T79、T80、A29、A30、R24。要求隔離環境中實測資料庫與 artifact 封裝還原，驗證 RTO／RPO，不得以指令成功退出碼假充還原通過。
4. **驗收項依賴約束（Acceptance Dependencies）**：
   - 依 SPEC 18.2 與 19.7 對照表，驗收項目（A01–A48）標示 `passed` 時，其所依賴之全部底層測試（T）必須均為 `passed`。

## 2. 查核工具使用指令

工具位於 `tools/governance/check_release_evidence.py`。

### 一般檢查（Check Mode，由 `scripts/check.sh` governance suite 自動執行）
核對 `docs/traceability.yaml` 之結構完整性、SPEC 編號一致性與閘門宣告合法性：
```bash
python3 -m tools.governance.check_release_evidence --check
```

### 發布門檻檢查（Release Mode，Fail-Closed）
驗證是否達到正式發布標準（必須 100% passed，任何未完成或 blocked 項目皆以 exit 1 失敗）：
```bash
python3 -m tools.governance.check_release_evidence --release
```

### 完整證據清單與盤點報告（Inventory Report）
印出全部 182 個項目的狀態統計、六大真實閘門通過率及 blocked 清單：
```bash
# 人類可讀文字報告
python3 -m tools.governance.check_release_evidence --inventory

# 機器可讀 JSON 格式
python3 -m tools.governance.check_release_evidence --inventory --format json
```

### 外部證據檔查核
亦可指定待驗證之發布候選證據清單檔（例如 release candidate YAML/JSON）：
```bash
python3 -m tools.governance.check_release_evidence --evidence path/to/candidate.yaml --release
```

## 3. 目前狀態與限制說明

當前倉庫處於開發階段，前置 Issue #14（維運、備份還原、隔離）尚未完成，真實模型服務、真實解析器與 500 頁效能測試尚未執行：
- 強制項目總數：182
- 已通過項目（passed）：0
- 部分完成（partial）：7（契約層測試，尚待資料庫與整合流程）
- 阻擋項目（blocked）：1（T83 因私有 repo CI 憑證受限記錄為 blocked）
- 未開始（not_started）：174
- 發布判定結果：`RELEASE_BLOCKED`

因此，目前的 `--release` 檢查維持 fail-closed，忠實反映尚未達到發布門檻，不虛構通過紀錄。
