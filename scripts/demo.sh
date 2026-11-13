#!/usr/bin/env bash
# Quickstart demo driver (docs/QUICKSTART_DEMO.md, Refs #7). Needs Docker with compose v2 and nothing else.
#   scripts/demo.sh up     build and start db + migrate + api + parse-worker (first run builds images: minutes)
#   scripts/demo.sh run    upload a synthetic PDF, parse it, print the stored result, cancel a long job
#   scripts/demo.sh down   stop everything and delete the demo database and artifacts
#   scripts/demo.sh all    up, run, then down (exit status is the demo's)
#   scripts/demo.sh generate  up, run INCLUDING question generation with the real model, then leave the stack up
#                          (needs LAB_GEN_API_KEY in the environment; costs a few cents; without a key the step is SKIPPED, never passed)
# Optional: DEMO_PDF=/path/to/your.pdf scripts/demo.sh run   (streamed into the container; your own file, parsed locally, never uploaded anywhere else)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# --env-file /dev/null: never read a developer's .env (the stack uses fixed throwaway dev credentials)
compose=(docker compose --env-file /dev/null -f deploy/compose.dev.yml)

# The migrate image is built from the repo: rebuild it every time, otherwise a stale image stops at an old revision and the API
# answers 503 database_unavailable (#174). After migrating, the database must be at the repo's head revision.
repo_head() { ls db/lab_db/schema/versions/[0-9]*.py | sed 's#.*/\([0-9]*\)_.*#\1#' | sort | tail -n1; }

up() {
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
  up
  generate_up || { code=$?; [[ $code == 3 ]] || return $code; echo "no API key: upload/parse/benchmark work in the page, model steps will fail (not a pass)" >&2; }
  session="$("${compose[@]}" --profile demo exec -T api python - < deploy/ui_session.py)"
  {
    echo "# written by scripts/demo.sh ui: local development session (git-ignored, do not share)"
    echo "VITE_LAB_DEV_PROJECT_ID=$(sed -n 's/^PROJECT_ID=//p' <<<"$session")"
    echo "VITE_LAB_DEV_TOKEN=$(sed -n 's/^TOKEN=//p' <<<"$session")"
  } > web/.env.local
  echo "logged in automatically (web/.env.local). Next: cd web && npm ci && npm run dev, then open http://localhost:5173/"
  echo "stop the backend with: scripts/demo.sh down"
}

down() { "${compose[@]}" --profile demo --profile tools --profile generate down -v; }

[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0      # sourced (tests): only the functions
case "${1:-}" in
  up) up ;;
  run) run ;;
  down) down ;;
  all) trap down EXIT; up; run ;;
  generate) generate ;;
  ui) ui ;;
  *) echo "usage: $0 up|run|down|all|generate|ui" >&2; exit 2 ;;
esac
