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

generate_up() {
  # The key comes from the caller's environment (or, as a convenience, the ANTHROPIC_API_KEY line of the git-ignored root
  # .env, read here without printing it). It is passed to the generate-worker container only.
  if [[ -z "${LAB_GEN_API_KEY:-}" && -f .env ]]; then
    line="$(grep -E '^ANTHROPIC_API_KEY=' .env | head -n1 || true)"
    if [[ -n "$line" ]]; then
      LAB_GEN_API_KEY="${line#ANTHROPIC_API_KEY=}"; LAB_GEN_API_KEY="${LAB_GEN_API_KEY%\"}"; LAB_GEN_API_KEY="${LAB_GEN_API_KEY#\"}"
      export LAB_GEN_API_KEY
      echo "using the API key from ./.env (not shown)"
    fi
  fi
  if [[ -z "${LAB_GEN_API_KEY:-}" ]]; then
    echo "GENERATION STEP: SKIPPED - no LAB_GEN_API_KEY (and no ANTHROPIC_API_KEY in .env). A skipped step is not a pass." >&2
    return 3
  fi
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

down() { "${compose[@]}" --profile demo --profile tools --profile generate down -v; }

case "${1:-}" in
  up) up ;;
  run) run ;;
  down) down ;;
  all) trap down EXIT; up; run ;;
  generate) generate ;;
  *) echo "usage: $0 up|run|down|all|generate" >&2; exit 2 ;;
esac
