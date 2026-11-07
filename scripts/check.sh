#!/usr/bin/env bash
# Local and CI test entrypoint.
#
# Usage: scripts/check.sh [--scope auto|all|<suite>...] [--base <git-ref>]
#   --scope auto (default): select suites from files changed vs --base
#                           (default origin/main); unmapped paths run all suites.
#   --scope all:            run every suite (release / main gate).
# Exit status is non-zero if any selected suite fails or if no suite ran.
set -euo pipefail
cd "$(dirname "$0")/.."

SUITES=(governance api api-db worker parser web contracts fixtures)
scope=auto
base=origin/main
selected=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) shift; scope="$1";;
    --base) shift; base="$1";;
    -h|--help) sed -n '2,9p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
  shift
done

# Map a changed path to the suite(s) that cover it (space-separated). Empty = unmapped.
suite_for_base() {
  case "$1" in
    # Shared test/CI infrastructure affects every suite.
    .github/*|scripts/*|tools/governance/run_tests.py) echo "${SUITES[*]}";;
    tools/governance/go_test_report.py) echo "governance worker";;
    services/api/*|tests/api/*) echo "api api-db";;
    tests/api_db/*|deploy/*) echo api-db;;
    services/worker/*) echo worker;;
    services/parser/*|tests/parser/*) echo parser;;
    web/*) echo web;;
    contracts/*|tests/contracts/*) echo "contracts fixtures";;
    fixtures/*|tests/fixtures/*) echo fixtures;;
    tools/__init__.py|tests/__init__.py|.gitignore|tools/governance/*|tests/governance/*|docs/*|*.md|requirements-dev.txt|LICENSE) echo governance;;
    *) echo "";;
  esac
}

# Markdown anywhere is also covered by governance (relative-link checks).
suite_for() {
  local s; s="$(suite_for_base "$1")"
  if [[ "$1" == *.md && -n "$s" && " $s " != *" governance "* ]]; then s="$s governance"; fi
  echo "$s"
}

run_suite() {
  case "$1" in
    governance)
      python3 -m tools.governance.check_repo
      # Provenance is checked against GitHub for passed entries only (needs GH_TOKEN then).
      python3 -m tools.governance.check_traceability --verify-provenance
      python3 -m tools.governance.run_tests tests/governance
      ;;
    api)
      PYTHONPATH=services/api python3 -m tools.governance.run_tests tests/api
      ;;
    api-db)
      # Real PostgreSQL/pgvector only. Missing database = BLOCKED, never passed.
      if [[ -z "${LAB_TEST_DATABASE_URL:-}" ]]; then
        echo "BLOCKED: suite api-db needs LAB_TEST_DATABASE_URL (see docs/TESTING.md)" >&2
        return 3
      fi
      PYTHONPATH=services/api python3 -m tools.governance.run_tests tests/api_db --fail-on-skip
      ;;
    web)
      # React frontend: locked install, unit tests, type check, production build.
      if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        echo "BLOCKED: suite web needs Node.js/npm (see web/package.json engines)" >&2
        return 3
      fi
      (cd web && node --version && npm ci && npm test && npm run build)
      ;;
    contracts)
      # Versioned data contracts: examples, negative cases, schema drift.
      PYTHONPATH=contracts python3 -m tools.governance.run_tests tests/contracts
      ;;
    fixtures)
      # Fixture registry: rebuild every registered item and compare sha256.
      PYTHONPATH=contracts python3 -m tools.governance.run_tests tests/fixtures
      # Human signoffs are resolved on GitHub; with none recorded no network
      # is used, and an unreachable GitHub fails a human review (never passes).
      PYTHONPATH=contracts python3 -m fixtures.registry_check --verify-signoff
      ;;
    parser)
      PYTHONPATH=services/parser:contracts python3 -m tools.governance.run_tests tests/parser
      ;;
    worker)
      # Go worker. GOTOOLCHAIN=local: never auto-download a different Go;
      # the installed toolchain must satisfy services/worker/go.mod.
      if ! command -v go >/dev/null 2>&1; then
        echo "BLOCKED: suite worker needs Go (version in services/worker/go.mod)" >&2
        return 3
      fi
      local unformatted
      unformatted="$(cd services/worker && gofmt -l .)"
      if [[ -n "$unformatted" ]]; then
        echo "gofmt needed:" >&2
        (cd services/worker && gofmt -d .) >&2
        return 1
      fi
      # go test -json is converted to the shared per-test report (printed
      # like -v; written to LAB_TEST_REPORT_DIR for traceability evidence).
      local gojson rc=0
      gojson="$(mktemp)"
      (cd services/worker && export GOTOOLCHAIN=local && go version && go vet ./...) || { rm -f "$gojson"; return 1; }
      (cd services/worker && GOTOOLCHAIN=local go test -count=1 -json ./... > "$gojson") || rc=$?
      python3 -m tools.governance.go_test_report --module services/worker --input "$gojson" || rc=1
      rm -f "$gojson"
      (( rc == 0 )) || return 1
      (cd services/worker && GOTOOLCHAIN=local go build -o /dev/null ./cmd/lab-worker)
      ;;
    *) echo "unknown suite: $1" >&2; return 2;;
  esac
}

if [[ -n "${CHECK_SH_PRINT_SUITE_FOR:-}" ]]; then
  # Test hook: print the suites mapped to each path argument, one line each.
  for f in $CHECK_SH_PRINT_SUITE_FOR; do echo "$f: $(suite_for "$f")"; done
  exit 0
fi

if [[ "$scope" == all ]]; then
  selected=("${SUITES[@]}")
elif [[ "$scope" == auto ]]; then
  if ! git rev-parse --verify --quiet "$base" >/dev/null; then
    echo "base ref '$base' not found; running all suites" >&2
    selected=("${SUITES[@]}")
  else
    declare -A want=()
    all=0
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      s="$(suite_for "$f")"
      if [[ -z "$s" ]]; then echo "unmapped path '$f' -> all suites"; all=1; else for w in $s; do want[$w]=1; done; fi
    done < <(git diff --name-only "$(git merge-base "$base" HEAD)"; git diff --name-only HEAD; git ls-files --others --exclude-standard)
    if (( all )); then selected=("${SUITES[@]}"); else selected=("${!want[@]}"); fi
    if (( ${#selected[@]} == 0 )); then
      echo "no changes vs $base; running all suites"
      selected=("${SUITES[@]}")
    fi
  fi
else
  read -r -a selected <<< "$scope"
fi

echo "selected suites: ${selected[*]}"
for s in "${selected[@]}"; do
  echo "=== suite: $s ==="
  run_suite "$s"
done
echo "all selected suites passed: ${selected[*]}"
