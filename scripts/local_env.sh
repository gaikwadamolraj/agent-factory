#!/usr/bin/env bash
# Create or complete the root .env for docker compose (never committed).
#
#   ./scripts/local_env.sh
#
# Copies .env.example to .env when .env does not exist, then replaces every value "__GENERATE__" with a fresh
# random value: AUTH_SIGNING_KEY gets an EC P-256 private key (PEM on one line, newlines as \n), every other key
# 48 random hex characters. Values that are already set are never changed. Only key names are printed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
PLACEHOLDER="__GENERATE__"

command -v openssl >/dev/null || { echo "openssl is not installed" >&2; exit 1; }

new_value() {  # a fresh value for one key, on one line
  if [[ "$1" == "AUTH_SIGNING_KEY" ]]; then
    # tr: Windows openssl ends lines with CRLF; awk: one line with literal \n (the auth-service turns it back)
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 2>/dev/null | tr -d '\r' | awk '{printf "%s\\n", $0}'
  else
    openssl rand -hex 24
  fi
}

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  echo "created .env from .env.example"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

completed="$(mktemp)"
trap 'rm -f "$completed"' EXIT
generated=()
while IFS= read -r line || [[ -n "$line" ]]; do
  key="${line%%=*}"
  if [[ "$line" == "$key=$PLACEHOLDER" && "$key" != \#* ]]; then
    printf '%s=%s\n' "$key" "$(new_value "$key")"     # goes into the file only
    generated+=("$key")
  else
    printf '%s\n' "$line"
  fi
done < "$ENV_FILE" > "$completed"
cat "$completed" > "$ENV_FILE"                          # keep the file's permissions

if [[ ${#generated[@]} -gt 0 ]]; then echo "generated: ${generated[*]}"; else echo "nothing to generate"; fi
