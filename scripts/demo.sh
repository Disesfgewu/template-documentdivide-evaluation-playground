#!/usr/bin/env bash
# Quickstart demo driver (docs/QUICKSTART_DEMO.md, Refs #7). Needs Docker with compose v2 and nothing else.
#   scripts/demo.sh up     build and start db + migrate + api + parse-worker (first run builds images: minutes)
#   scripts/demo.sh run    upload a synthetic PDF, parse it, print the stored result, cancel a long job
#   scripts/demo.sh down   stop everything and delete the demo database and artifacts
#   scripts/demo.sh all    up, run, then down (exit status is the demo's)
# Optional: DEMO_PDF=/path/to/your.pdf scripts/demo.sh run   (streamed into the container; your own file, parsed locally, never uploaded anywhere else)
set -euo pipefail
cd "$(dirname "$0")/.."
# --env-file /dev/null: never read a developer's .env (the stack uses fixed throwaway dev credentials)
compose=(docker compose --env-file /dev/null -f deploy/compose.dev.yml)

up() {
  "${compose[@]}" up -d --wait db
  "${compose[@]}" --profile tools run --rm -e LAB_MIGRATION_DATABASE_URL=postgresql://lab:lab-dev-only@db:5432/lab \
    migrate -x deployment=ci upgrade head
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

down() { "${compose[@]}" --profile demo --profile tools down -v; }

case "${1:-}" in
  up) up ;;
  run) run ;;
  down) down ;;
  all) trap down EXIT; up; run ;;
  *) echo "usage: $0 up|run|down|all" >&2; exit 2 ;;
esac
