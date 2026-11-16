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
Provider prompts, raw model replies, generic `text`/`content` fields, document page text and bytes are
represented by length + sha256 only. The owner's OWN data (question text, reference answer, facts, a developer's
answer and quotes, the system's matched text, verdict reasons) is shown as `<N chars sha256:..>'first 120 chars'`
(redacted first); the file is owner-local and git-ignored, and a banner line (`app_log_start`) says so once per
process and file. Functions that receive a model reply under a generic name pass `hash_only=("text",)` to `app_traced`.
Item identifiers such as `question_key` are not redacted; credentials (`*key*`, token, secret, auth, url, dsn) are.
Unknown objects are never repr-dumped. Secrets and PostgreSQL DSNs are redacted.
The file sink swallows and counts failures (`get_log_failures()`); business audit
failures retain their existing behavior.

Coverage added for #191 (PM-DECISION): `code_checks`, `verdict_for`, `parse_validator_reply` (reply hash-only),
each submission line (`validate_line`, valid or its fixed code), `_check_entry`, each quote (`quote_located` with
match quality and coverage), each `unit_hit` decision, `derive`/`check_verdict_ex` and the unsupported-by-construction
branch, the freeze gate counts (`freeze_gate`), review/authorize/export/exclusion, and each API request (CALL with
the method; RETURN with the route template, status and `duration_ms`).
Still not done: a run on the owner's real stack with the real model (the coding agent never runs a real model; the
owner's `scripts/demo.sh generate` run is theirs). The local measurement used real PostgreSQL and a fake lab-gen child;
its timing is dominated by database setup and is not a performance guarantee. SPEC #15/Epic release gates remain.

HTTP completion is observed at the ASGI send boundary, after the final body and
any requested trailers. Stream failures, failed sends, cancellation and incomplete
disconnected responses produce ERROR instead of a premature successful RETURN;
original messages, results and exceptions are preserved. Generate-worker phase
entries are removed on every handler exit, including nonterminal and exception
paths. API log IDs still require canonical integration with the separate trace
correlation increment (#65); the current log-only ID must not be treated as the
returned error ID.

Scoring decisions (Issue #197): every line written while a question is being scored or judged carries `q=<question_key>#<ordinal>` after the
job/run id, so `grep 'q=q-…#9' logs/app.log` is that question's whole story. A MetricResult is logged as its fields (metric, status, raw_value,
numerator, denominator, reason_codes), never as `<object>`. Strings shorter than 40 characters (ids, verdicts) are shown as they are; only longer
owner text gets the length + sha256 + 120-char preview. The judge writes one `question_summary` line per question: the judge's fact verdicts, the
final ones after the number check, `overruled=[f1:number_other_entity]`, claims supported/total, claims with no passage, and the derived scores.
`retrieval_credit_withheld` says a not-found source got no retrieval credit; `judge_overruled_by_number_check` says why a fact the judge called
stated was scored missing.
