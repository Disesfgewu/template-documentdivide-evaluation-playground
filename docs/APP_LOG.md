# Owner-local application log (partial #191)

`LAB_APP_LOG=off|stages|full` controls the additional diagnostic file. It defaults
to off outside the dev Compose services; the dev services default to full.
`LAB_APP_LOG_FILE` selects the destination. Compose mounts the git-ignored repo
`logs/` directory into API, parse-worker and generate-worker. Run
`scripts/demo.sh logs` to follow `logs/app.log` across rotations.

The file is owner-local and can contain parsed gold diagnostics. Never publish
it, expose it through artifact/API routes, or include it in CI artifacts. It is
not a replacement for PostgreSQL log_events/audit/outbox or their retention and
transaction guarantees. Draft #64 supplies separate HTTP diagnostic middleware;
this increment does not merge or assume acceptance of that draft.

Before direct Compose startup, prepare `logs/` with `mkdir -p logs; chmod 2770 logs`
and export `LAB_LOG_GID` to the host user's group ID. `scripts/demo.sh up` does this
automatically. Services keep UID 10001 and receive only that supplementary group.
The setgid directory lets the host group read new mode-0640 files. Existing log
files must also permit the service to append. Rotation uses a stable separate
lock file, a 20 MiB threshold and five backups; one individual record is bounded.

The decorator covers upload, promote/planning, parse/generation workers, validation,
exclusion/freeze/export, external submission parsing/quote matching, rule scoring,
judge building/parsing and results. Helpers add page statuses, dropped-reply
alarms, quote-not-found alarms, score numerators/denominators and judge source
selection. UTC timestamp, service and request/job context precede each event.
Raw prompts/replies/document-text fields and bytes are represented by length/hash;
unknown objects are never repr-dumped. Secrets and PostgreSQL DSNs are redacted.
The file sink swallows and counts failures (`get_log_failures()`); business audit
failures retain their existing behavior.

This is a draft increment, not complete #191 acceptance. Remaining work includes
an exhaustive every-function path inventory, detailed per-line submission and
SQL/state-transition operations, complete participant-phase telemetry isolation
proof, actual multi-service demo acceptance and a controlled demo overhead
measurement. Fake-child/real-PostgreSQL tests are not real-provider evidence.
The small local test timing comparison is dominated by database setup and must
not be reported as a performance guarantee. SPEC #15/Epic release gates remain.
