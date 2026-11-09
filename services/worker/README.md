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

## Restricted agentlog import foundation (#6)

The `auditimport` command reads the exact `.log`, `.log.journal` and
`.log.seal` bytes emitted by agentKit revision
`ce49a7130b17ccda07c755de96ef44c49d0b719c` and creates a metadata-only JSON
report. It has no private module dependency. Run from `services/worker`:

```bash
go run ./cmd/auditimport -log /restricted/session.log \
  -out /restricted/new-report.json -project project-1 \
  -operation import-operation-1 -source source-bundle-1
```

The invoking service must authorize these IDs and access to every source artifact.
`source` is a stable, project-scoped identity for one session's artifact group;
it must never be reused for another session. No identity in file contents grants
access. Original files are never changed. The report records raw byte hashes,
sizes, presence and `restricted=true`; retain the original artifact objects under
the same gold/source access policy. This command neither uploads nor stores them.
The CLI targets the Linux worker environment and opens inputs nonblocking before
requiring regular files, so FIFOs/devices cannot stall artifact admission.
Input symlinks are refused; output paths must differ from all source paths, even
when a source or companion is missing.
The report is created with mode 0600 and refuses an existing path or symlink.
Exit 0 means verified, 1 means an invalid/incomplete report was written, and 2
means an invocation/read/write error. Diagnostic messages omit file paths and
raw parser input. Missing companions produce an incomplete report, never success.

Verifier `lab-agentlog-1` checks the journal's call/parent order, strict JSON,
completion sequence, raw-byte SHA-256 chain, journal/function/input correspondence,
consistent producer session, and close-seal counts/final hash/journal hash.
Completion order is independent of begin order, including child-first completion.
It decodes JSON structurally, so embedded delimiter-like text cannot override
metadata. Duplicate/unknown/missing metadata fields, invalid UTF-8, corrupt chains,
truncated lines and malformed records fail closed. A missing seal or unfinished
call is incomplete; malformed existing content takes precedence as invalid.
Zero-call sealed sessions can verify. Hashes detect ordinary alteration; anyone
able to rewrite all three files can forge them. This is a separate LAB verifier,
not a claim that upstream `agentlog.Verify` ran or authenticated the producer.

Events retain begin/end sequence, call and parent IDs, UTC occurrence times,
duration, raw record hash and stable span relationships. The producer session is
represented by SHA-256 (null when unavailable), avoiding user-bearing agent names.
Event IDs hash trusted project/source/call/phase plus the raw record hash, making
exact reimports idempotent; operation IDs do not change those IDs. A future sink
must also detect the same logical source/call/phase with different content, and
transactionally deduplicate/persist the events. Incomplete calls get WARN events;
completion payloads with an `error` member get conservative ERROR/`reported_error`,
others get INFO/`completed`, irrespective of the raw DEBUG prefix. An error member
may be application data: these labels are diagnostics, never authoritative job
state transitions. Missing-event warnings are reconciliation observations rather
than invented completion timestamps.

No input, output, arbitrary function name, error text, URL, prompt or document
content is included in normalized events. `metadata-only-1` deliberately marks
report reproducibility partial: original restricted artifacts are necessary for
reconstruction. Caller IDs and report hashes remain access-controlled metadata.
Limits are 16 MiB per artifact, 32 MiB per bundle, 1 MiB per line and 10,000
calls/completions; CLI reads are bounded before allocation. Larger sessions need
a separately versioned streaming profile, not silently reduced coverage.

Validation: `scripts/check.sh --scope 'worker governance'` covers the public
stdlib importer and CLI, including nested calls, delimiter text, errors, replay,
truncation, tamper, missing artifacts, empty sessions, limits, secret exclusion
and private file creation. This is an independent R18/R19, T66/T67/T69 and A40
foundation only. PostgreSQL transaction/dedup, artifact publication, permissions,
collector buffers/repair, TraceBridge, full log-event schema/API/UI and real
workflow integration remain #5/#6 gates; this report alone does not satisfy them.
