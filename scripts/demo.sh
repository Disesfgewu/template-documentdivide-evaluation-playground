#!/usr/bin/env bash
# Quickstart demo driver (docs/QUICKSTART_DEMO.md, Refs #7). Needs Docker with compose v2 and nothing else.
#   scripts/demo.sh up     build and start db + migrate + api + parse-worker (first run builds images: minutes)
#   scripts/demo.sh run    upload a synthetic PDF, parse it, print the stored result, cancel a long job
#   scripts/demo.sh stop   stop the containers, KEEP the database, artifacts and logs (Issue #194)
#   scripts/demo.sh start  bring the stack back up on the existing data (rebuild images, migrate forward, check data, never wipe)
#   scripts/demo.sh status print what the stack holds (documents, benchmarks, runs, uploads) and whether the volumes agree
#   scripts/demo.sh down --wipe   DELETE the demo database and artifacts (refused without --wipe or DEMO_WIPE=1; prints what is lost)
#   scripts/demo.sh all    up, run, then wipe (refused when the stack already holds data, unless DEMO_WIPE=1; exit status is the demo's)
#   scripts/demo.sh generate  up, run INCLUDING question generation with the real model, then leave the stack up
#                          (needs LAB_GEN_API_KEY in the environment; costs a few cents; without a key the step is SKIPPED, never passed)
# Optional: DEMO_EXCLUDE_FAILED=1 scripts/demo.sh generate   excludes the questions that failed the automated validation (recorded with a reason, not deleted) so the freeze can go ahead
# Optional: DEMO_PDF=/path/to/your.pdf scripts/demo.sh run   (streamed into the container; your own file, parsed locally, never uploaded anywhere else)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# --env-file /dev/null: never read a developer's .env (the stack uses fixed throwaway dev credentials)
compose=(docker compose --env-file /dev/null -f deploy/compose.dev.yml)

# The migrate image is built from the repo: rebuild it every time, otherwise a stale image stops at an old revision and the API
# answers 503 database_unavailable (#174). After migrating, the database must be at the repo's head revision.
repo_head() { ls db/lab_db/schema/versions/[0-9]*.py | sed 's#.*/\([0-9]*\)_.*#\1#' | sort | tail -n1; }

up() {
  mkdir -p logs
  chmod 2770 logs
  export LAB_LOG_GID="$(id -g)"
  "${compose[@]}" up -d --wait db
  "${compose[@]}" --profile tools build migrate
  "${compose[@]}" --profile tools run --rm -e LAB_MIGRATION_DATABASE_URL=postgresql://lab:lab-dev-only@db:5432/lab \
    migrate -x deployment=ci upgrade head
  head="$(repo_head)"
  at="$("${compose[@]}" exec -T db psql -U lab -d lab -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
  if [[ "$at" != "$head" ]]; then
    echo "BLOCKED: the database is at migration '$at' but the repository head is '$head'. Rebuild the migrate image and retry." >&2
    return 4
  fi
  "${compose[@]}" --profile demo up -d --build --wait --wait-timeout 600 api parse-worker
  echo "ready: API on http://127.0.0.1:${LAB_API_PORT:-8000} (docs: /docs), worker running"
}

run() {
  extra=()
  if [[ -n "${DEMO_PDF:-}" ]]; then
    # docker cp cannot write into the tmpfs /tmp of the read-only container; stream the file in instead.
    "${compose[@]}" --profile demo exec -T api sh -c 'cat > /tmp/demo-input.pdf' < "$DEMO_PDF"
    extra=(-e DEMO_PDF=/tmp/demo-input.pdf)
  fi
  "${compose[@]}" --profile demo exec -T "${extra[@]}" api python - < deploy/demo_run.py
}

# An API key as a clean token: drop a Windows CR and surrounding blanks and one pair of quotes (a .env saved with CRLF endings made
# every request fail before it was sent, #174). Prints nothing but the cleaned value.
clean_key() {
  local v="${1//$'\r'/}"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  v="${v%\"}"; v="${v#\"}"
  printf '%s' "$v"
}

# 0 = a usable key (printable ASCII, no blanks); never prints the key.
key_is_valid() { [[ -n "$1" ]] && (LC_ALL=C; [[ "$1" =~ ^[[:graph:]]+$ ]]); }

# The cleaned ANTHROPIC_API_KEY line of an env file (empty if none).
env_file_key() {
  local line
  line="$(grep -E '^ANTHROPIC_API_KEY=' "$1" | head -n1 || true)"
  [[ -n "$line" ]] && clean_key "${line#ANTHROPIC_API_KEY=}" || true
}

generate_up() {
  # The key comes from the caller's environment (or, as a convenience, the ANTHROPIC_API_KEY line of the git-ignored root
  # .env, read here without printing it). It is passed to the generate-worker container only.
  if [[ -z "${LAB_GEN_API_KEY:-}" && -f .env ]]; then
    LAB_GEN_API_KEY="$(env_file_key .env)"
    if [[ -n "$LAB_GEN_API_KEY" ]]; then
      export LAB_GEN_API_KEY
      echo "using the API key from ./.env (not shown)"
    fi
  fi
  if [[ -z "${LAB_GEN_API_KEY:-}" ]]; then
    echo "GENERATION STEP: SKIPPED - no LAB_GEN_API_KEY (and no ANTHROPIC_API_KEY in .env). A skipped step is not a pass." >&2
    return 3
  fi
  LAB_GEN_API_KEY="$(clean_key "$LAB_GEN_API_KEY")"
  if ! key_is_valid "$LAB_GEN_API_KEY"; then
    echo "BLOCKED: the API key contains a space, a control character or a non-ASCII character (not shown). Check the line endings of the file or variable it came from." >&2
    return 4
  fi
  export LAB_GEN_API_KEY
  AGENTKIT_COMMIT="$(git rev-parse HEAD:third_party/agentkit)"   # the pinned public agentKit commit the image builds against
  export AGENTKIT_COMMIT
  "${compose[@]}" --profile generate up -d --build --wait --wait-timeout 600 generate-worker
}

generate() {
  up
  extra=(-e DEMO_GENERATE=1)
  if [[ "${DEMO_EXCLUDE_FAILED:-}" == "1" ]]; then
    extra+=(-e DEMO_EXCLUDE_FAILED=1)       # opt-in: exclude (record, never delete) the questions whose automated validation failed
  fi
  if generate_up; then
    echo "prices used for the cost figures are UNVERIFIED list prices (LAB_GEN_PRICE_IN/OUT_MICROS_PER_MTOK override them)"
  else
    code=$?
    [[ $code == 3 ]] || return $code
    extra+=(-e DEMO_NO_KEY=1)               # no key: steps 1-5 still run; the run ends SKIPPED (exit 3), never PASS
  fi
  code=0
  "${compose[@]}" --profile demo exec -T "${extra[@]}" api python - < deploy/demo_run.py || code=$?
  echo "the stack is still up; stop it with: scripts/demo.sh down"
  return $code
}

ui() {
  start_stack || { code=$?; [[ $code == 6 ]] || return $code; echo "continuing although stored files are missing (see the warning above)" >&2; }
  session="$("${compose[@]}" --profile demo exec -T api python - < deploy/ui_session.py)"
  {
    echo "# written by scripts/demo.sh ui: local development session (git-ignored, do not share)"
    echo "VITE_LAB_DEV_PROJECT_ID=$(sed -n 's/^PROJECT_ID=//p' <<<"$session")"
    echo "VITE_LAB_DEV_TOKEN=$(sed -n 's/^TOKEN=//p' <<<"$session")"
  } > web/.env.local
  case "$(sed -n 's/^SESSION=//p' <<<"$session")" in
    reused)  echo "reused the existing web project and owner (your documents and benchmarks are still there); a fresh token was issued" ;;
    created) echo "created a NEW web project and owner (the database had none)" ;;
  esac
  echo "logged in automatically (web/.env.local). Next: cd web && npm ci && npm run dev, then open http://localhost:5173/ and pick a document or benchmark to continue"
  echo "stop the backend, keeping your data, with: scripts/demo.sh stop"
}

# up + the generate worker when there is a key, on whatever data exists. Never wipes.
start_stack() {
  up
  generate_up || { code=$?; [[ $code == 3 ]] || return $code; echo "no API key: upload/parse/benchmark work in the page, model steps will fail (not a pass)" >&2; }
  data_check || return $?
}

start() {
  start_stack || return $?
  echo "started on the existing data. Next: scripts/demo.sh ui   (web page session)   or   scripts/demo.sh status"
}

# KEY=VALUE lines from the api container, or nothing when the stack is not running.
status_values() { "${compose[@]}" --profile demo exec -T api python - < deploy/data_status.py 2>/dev/null || true; }

value_of() { sed -n "s/^$1=//p" <<<"$2" | head -n1; }

describe_data() {
  local v="$1"
  echo "  documents: $(value_of DOCUMENTS "$v"), benchmarks: $(value_of BENCHMARKS "$v") (frozen: $(value_of FROZEN_BENCHMARKS "$v")), questions: $(value_of QUESTIONS "$v"), runs: $(value_of RUNS "$v"), developer uploads: $(value_of DEVELOPER_UPLOADS "$v"), stored files: $(value_of ARTIFACTS "$v")"
}

# After a start the database and the artifact volume must agree: a committed artifact whose object is missing is reported.
data_check() {
  local v dangling
  v="$(status_values)"
  [[ -n "$v" ]] || { echo "data check: the api is not answering, nothing to check" >&2; return 0; }
  describe_data "$v"
  dangling="$(value_of DANGLING_ARTIFACTS "$v")"
  if [[ "${dangling:-0}" != "0" ]]; then
    echo "WARNING: $dangling stored file(s) listed in the database are missing or damaged on the artifacts volume (not wiped by this script; restore the volume or re-upload)" >&2
    return 6
  fi
  echo "data check: the database and the artifact volume agree"
}

status() {
  local v
  v="$(status_values)"
  [[ -n "$v" ]] || { echo "the stack is not running (scripts/demo.sh start); the data volumes are kept while it is stopped"; return 0; }
  echo "migration $(value_of MIGRATION "$v"); this stack holds:"
  describe_data "$v"
  echo "  files missing on the artifacts volume: $(value_of DANGLING_ARTIFACTS "$v")"
}

stop() {
  "${compose[@]}" --profile demo --profile tools --profile generate down
  echo "stopped. The database, the artifacts and logs/ are KEPT. Start again with: scripts/demo.sh start"
}

# The wipe flag: `down --wipe` or DEMO_WIPE=1. Without it nothing is deleted, and the message says what would be lost.
wipe_confirmed() {
  [[ "${DEMO_WIPE:-}" == "1" ]] && return 0
  local arg
  for arg in "$@"; do [[ "$arg" == "--wipe" ]] && return 0; done
  return 1
}

down() {
  local v
  v="$(status_values)"
  if ! wipe_confirmed "$@"; then
    echo "REFUSED: 'down' deletes the demo database and artifacts (the Docker volumes), for good." >&2
    if [[ -n "$v" ]]; then echo "It would destroy:" >&2; describe_data "$v" >&2
    else echo "(the stack is not running, so what it holds cannot be counted; the volumes still exist and would be deleted)" >&2; fi
    echo "To stop and KEEP your data: scripts/demo.sh stop.  To really delete it: scripts/demo.sh down --wipe  (or DEMO_WIPE=1)." >&2
    return 5
  fi
  if [[ -n "$v" ]]; then echo "WIPING:"; describe_data "$v"; fi
  "${compose[@]}" --profile demo --profile tools --profile generate down -v
}

# `all` ends with a wipe: never over data somebody created.
all() {
  local v docs
  v="$(status_values)"
  docs="$(value_of DOCUMENTS "$v")"
  if [[ "${docs:-0}" != "0" && "${DEMO_WIPE:-}" != "1" ]]; then
    echo "REFUSED: 'all' would wipe this stack at the end, and it already holds data:" >&2
    describe_data "$v" >&2
    echo "Use 'scripts/demo.sh run' on the running stack, or DEMO_WIPE=1 scripts/demo.sh all to delete it." >&2
    return 5
  fi
  trap 'DEMO_WIPE=1 down' EXIT
  up
  run
}

[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0      # sourced (tests): only the functions
case "${1:-}" in
  up) up ;;
  run) run ;;
  stop) stop ;;
  start) start ;;
  status) status ;;
  down) shift; down "$@" ;;
  all) all ;;
  generate) generate ;;
  ui) ui ;;
  logs) logs ;;
  *) echo "usage: $0 up|run|stop|start|status|down --wipe|all|generate|ui|logs" >&2; exit 2 ;;
esac
