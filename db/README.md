# lab_db — PostgreSQL schema（#5 第一個增量）

- `lab_db/migrations/NNNN_*.sql`：版本化 migration。
  - 每個檔案和它在 `schema_migrations` 的紀錄（版本、sha256、deployment）放在同一個交易，失敗時整個回滾。
  - 已套用的檔案若內容被改動，migration 會拒絕執行。
  - 用 advisory lock 確保同時只有一個 migrator。
- **PostgreSQL 用 Docker 執行，schema 由 Alembic 套用。**
  - 啟動資料庫：`docker compose -f deploy/compose.dev.yml up -d --wait`（PostgreSQL 17 + pgvector，映像以 digest 鎖定）。
  - 在容器裡套用：`docker compose -f deploy/compose.dev.yml --profile tools run --rm migrate`（建置 `deploy/Dockerfile.migrate`：只含 Alembic 與 `lab_db`，非 root；預設 `-x deployment=local upgrade head`，要換 deployment id 可改成 `run --rm migrate -x deployment=<id> upgrade head`）。
  - 在本機套用：`pip install -r db/requirements.lock`，然後 `cd db && LAB_MIGRATION_DATABASE_URL=postgresql://… alembic -x deployment=<id> upgrade head`（`alembic.ini` 用 `%(here)s`，從任何目錄都能執行）。等價的舊入口 `PYTHONPATH=db python -m lab_db.migrate --deployment <id>` 仍可用。
  - 連線字串只從環境變數 `LAB_MIGRATION_DATABASE_URL` 讀取（migration 角色），不放在設定檔或命令列；必須指定 deployment id。
- **Alembic 的用法與限制。**
  - SQL 檔案仍是唯一的事實來源（`lab_db/migrations/NNNN_*.sql`）。每個檔案在 `lab_db/schema/versions/` 有一個薄的 revision（`NNNN_*.py`），revision id 就是四位數版本號，鏈必須是單一線性鏈且與 SQL 檔一一對應（`tests/dbunit/test_alembic_schema.py` 驗證）。
  - 每個 migration 仍是各自的交易（連同 `schema_migrations` 的紀錄與 `alembic_version` 的更新），失敗整個回滾；用 advisory lock 保證同時只有一個 migrator；已套用的 SQL 若 checksum 改變，或資料庫比程式碼新，都會拒絕。
  - **只能向前**：沒有 downgrade（`alembic downgrade` 會失敗且不改資料），回復請用備份。不支援 `--sql` 離線輸出（無法驗證 checksum）。
  - **既有資料庫**：只有 `schema_migrations`、沒有 `alembic_version` 的資料庫（舊 runner 套用的）會先驗證 checksum，再標記到目前版本，不會重跑任何 migration。
  - **新增 migration**：新增下一號的 SQL 檔，並在 `lab_db/schema/versions/` 新增對應的 revision（複製前一個、改 `revision`、`down_revision` 與檔名）。版本號必須連續且不可跳號；兩個 PR 同時新增同一號時，後合併的要改成下一號。
- `0001_core`：projects、actors、project_members（角色 enum）、resource_versions、operations、lineage_edges、artifacts、idempotency_keys、audit_events、outbox_events。
  - 跨專案的關聯用 `(project_id, id)` 複合外鍵阻擋；刪除一律 RESTRICT，不用 cascade。
  - resource_versions 由 trigger 保護：
    - 內容欄位不可改。
    - 凍結後不可解凍或修改。
    - 不可刪除。
  - audit_events 只能追加：UPDATE、DELETE、TRUNCATE 都會被 trigger 拒絕，所有角色都一樣。
  - artifact 狀態只能依序 staged → committed → tombstoned：committed 後不可改內容；tombstoned 必須附原因；只有 staged 可以刪除。
- `lab_db/repo.py`：
  - `authorize`：SPEC 16.4 角色矩陣，預設拒絕。非成員得到 NotFound，不透露專案是否存在。
  - `claim_or_replay`：同一個 idempotency key 只會建立一個邏輯操作；key 相同但內容不同時，回 409。
  - `record_change`：版本、lineage、audit、outbox 在同一個交易內寫入。
  - `grant_app_privileges`：應用程式角色只有 DML 權限，沒有 DDL 和 TRUNCATE，audit 只能 INSERT。

其他 migration：
- `0002`：documents、pages、evidence，與四部分 benchmark 及其狀態機。
- `0003`：methods、runs、attempts、submissions、hits/citations、evaluations、scores。
- `0004`：jobs（lease、fencing token）、graph checkpoints、node operations、log_events。
- `0005`：SPEC 19.6 的 parse/visual/relation/verification/derivation 表。
- `0006`：outbox 依 consumer 冪等消費。
- `0007`：跨表完整性強化。
- `0008`：gold 資料 RLS 隔離。只有 owner、maintainer、reviewer 可讀，owner、maintainer 可寫；未設 actor 一律讀不到。

其他模組：`jobs`、`logs`（含 redaction）、`artifacts`、`storage`（兩階段登錄）、`ingest`、`lineage`、`outbox`、`availability`、`export`。

尚未完成（後續增量）：
- API 層的權限端到端測試（T70）。
- 遮罩造成的部分可見性。
- outbox `delivered_at` 與逐 consumer 消費的語意統一。
- parse_runs/observations 的 revision 與 hash 補強。
- pgvector collection（#11）。
