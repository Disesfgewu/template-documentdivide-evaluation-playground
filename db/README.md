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
- 真 embedding model 的呼叫、method-service 與 benchmark 建題流程對 collection 的實際接線（#11／#10／#6）。

## pgvector 向量儲存（#11 增量，migration 0010、`lab_db.vectors`）

- `embedding_spaces`：不可變的 embedding 身分（`UNIQUE(project_id, config_hash)`）；不同 space 的向量不能互相比較。
- `vector_collections`：屬於一個 project、一個 space 與一種用途（`method` 或 `benchmark_generation`，兩者互相隔離）；`method` 才需要 `method_revision`。狀態 `building → ready → frozen`（或 `failed`）；`ready` 必須有 `manifest_hash`、`item_count` 與 `ready_at`，發佈前以 `manifest` 重算核對，未 ready 的 collection 不能查詢。
- `vector_items`／`vector_item_regions`：append-only。向量欄位不指定維度，由 trigger 對照 space 的維度；只在 `building` 時可寫入，限同 project 與文件範圍，cosine 不接受零向量。
- `vector_index_revisions`：每個 collection 一個部分 HNSW 表達式索引（`((embedding::vector(d)) ops) WHERE collection_id = ...`）或精確掃描。查詢以 `EXPLAIN` 驗證確實使用索引；沒用到就丟 `IndexNotUsed`，不默默改成全表掃描。
- `vector_queries`：append-only，記錄要求的模式、實際使用的模式、缺額（shortfall）與 fallback 原因；`fallback` 只在實際模式與要求不同時才有值。精確 fallback 必須由呼叫端明確允許。

API：`create_space`、`create_collection`、`upsert`、`manifest`／`manifest_hash_of`、`publish`、`build_index`（`exact` 或 `hnsw`）、`search`（回傳 `QueryResult`）。測試 `tests/db/test_vectors.py` 涵蓋 T53–T57：隔離、ready 閘、維度與零向量、HNSW 召回（真 PostgreSQL＋pgvector）。
