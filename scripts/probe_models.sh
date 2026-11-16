#!/usr/bin/env bash
# Which Claude models can YOUR key use? One 1-token request per model, prints "<model> <HTTP status>" and nothing else (never the key).
# Costs a fraction of a cent. Needs ANTHROPIC_API_KEY (or LAB_GEN_API_KEY) in the environment; nothing is read from a file or sent anywhere but api.anthropic.com.
#   ANTHROPIC_API_KEY=... scripts/probe_models.sh [model ...]
set -euo pipefail
key="${ANTHROPIC_API_KEY:-${LAB_GEN_API_KEY:-}}"
if [[ -z "$key" ]]; then echo "set ANTHROPIC_API_KEY (or LAB_GEN_API_KEY) first" >&2; exit 2; fi
models=("$@")
if [[ ${#models[@]} -eq 0 ]]; then models=(claude-haiku-4-5-20251001 claude-haiku-5-5 claude-sonnet-5-5 claude-opus-5-5); fi
for m in "${models[@]}"; do
  if [[ ! "$m" =~ ^[A-Za-z0-9._-]+$ ]]; then echo "skipping a model name with unexpected characters" >&2; continue; fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 https://api.anthropic.com/v1/messages \
    -H "x-api-key: $key" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
    -d "{\"model\":\"$m\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" || echo "curl-failed")"
  case "$code" in
    200) echo "$m 200 usable" ;;
    404) echo "$m 404 no such model for this key" ;;
    401|403) echo "$m $code the key was refused" ;;
    *) echo "$m $code" ;;
  esac
done
