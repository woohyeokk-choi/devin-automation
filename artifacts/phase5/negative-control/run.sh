#!/usr/bin/env bash
# Phase 5 negative control: the real validator, run from published automation
# code, against the isolated immutable baseline stack. The baseline is
# expected to FAIL S2 and S1 target contracts while every control holds.
set -uo pipefail

ROOT=/home/ubuntu/phase5-bootstrap
AUT=$ROOT/devin-automation
OUT=/home/ubuntu/repos/devin-automation/artifacts/phase5/negative-control
WEB=http://127.0.0.1:8288
MCP=http://127.0.0.1:5208/mcp
mkdir -p "$OUT"
cd "$AUT"

echo "### automation $(git rev-parse HEAD) dirty=[$(git status --porcelain)]"

run() { # name, exit-expectation note, args...
  local name=$1; shift
  echo "=== $name"
  python3 -m portal.validator "$@" --out "$OUT/$name.json" >/dev/null 2>"$OUT/$name.stderr"
  echo "exit=$?"
}

run baseline --case S2 --case S1 --case N1 --base-url "$WEB" --mcp-url "$MCP"
# Blocked, not failed: nothing answered, so there is no product evidence.
run blocked-web-unreachable --case S2 --base-url http://127.0.0.1:8999 --mcp-url "$MCP"
run blocked-mcp-unreachable --case S1 --base-url "$WEB" --mcp-url http://127.0.0.1:5999/mcp
run blocked-bad-credentials --case N1 --base-url "$WEB" --mcp-url "$MCP" \
  --restricted-password definitely-not-the-password
