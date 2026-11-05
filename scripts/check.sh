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

SUITES=(governance)
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

# Map a changed path to the suite that covers it. Empty output = unmapped.
suite_for() {
  case "$1" in
    .github/*|scripts/*|tools/__init__.py|tests/__init__.py|.gitignore|tools/governance/*|tests/governance/*|docs/*|*.md|requirements-dev.txt|LICENSE) echo governance;;
    *) echo "";;
  esac
}

run_suite() {
  case "$1" in
    governance)
      python3 -m tools.governance.check_repo
      python3 -m tools.governance.run_tests tests/governance
      ;;
    *) echo "unknown suite: $1" >&2; return 2;;
  esac
}

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
      if [[ -z "$s" ]]; then echo "unmapped path '$f' -> all suites"; all=1; else want[$s]=1; fi
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
