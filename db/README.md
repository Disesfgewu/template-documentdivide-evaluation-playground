# lab_db — PostgreSQL schema（#5 第一個增量）

- `lab_db/migrations/NNNN_*.sql`：版本化 migration。
  - 每個檔案和它在 `schema_migrations` 的紀錄（版本、sha256、deployment）放在同一個交易，失敗時整個回滾。
  - 已套用的檔案若內容被改動，migration 會拒絕執行。
  - 用 advisory lock 確保同時只有一個 migrator。
- 執行 migration：`LAB_MIGRATION_DATABASE_URL=… PYTHONPATH=db python -m lab_db.migrate --deployment <id>`。連線字串只從環境變數讀取，不放在命令列。
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

尚未完成（後續增量）：
- 文件、頁面、證據、題庫、方法、run、評分各自的資料表與 19.6 parse/visual/relation 表。
- job 狀態機與 row_version。
- log_events、trace。
- 舊資料升級測試（T50）、lineage 查詢（T51）、redaction。
