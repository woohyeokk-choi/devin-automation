#!/usr/bin/env bash
# Phase 5 bootstrap proof: published automation code only.
#
# Clones the automation repo at a pushed commit (no working-tree overlay),
# clones Superset at the immutable baseline tag, and brings the stack up in
# its own Compose project on its own loopback ports. The existing baseline
# and Phase 4 environments keep running untouched.
set -euo pipefail

ROOT=/home/ubuntu/phase5-bootstrap
AUTOMATION_SHA=${AUTOMATION_SHA:?}
BASELINE_SHA=394bca55c792b7b3547e23f6e175a7cb0f0757e8
export AUTOMATION_DIR=$ROOT/devin-automation
export SUPERSET_DIR=$ROOT/superset

echo "=== 0. clean checkouts ==="
rm -rf "$AUTOMATION_DIR" "$SUPERSET_DIR"
git clone -q https://github.com/woohyeokk-choi/devin-automation.git "$AUTOMATION_DIR"
git -C "$AUTOMATION_DIR" checkout -q "$AUTOMATION_SHA"
git clone -q https://github.com/woohyeokk-choi/superset.git "$SUPERSET_DIR"
git -C "$SUPERSET_DIR" checkout -q "$BASELINE_SHA"
git -C "$AUTOMATION_DIR" status --porcelain
git -C "$SUPERSET_DIR" status --porcelain
echo "automation $(git -C "$AUTOMATION_DIR" rev-parse HEAD) clean"
echo "superset   $(git -C "$SUPERSET_DIR" rev-parse HEAD) clean"

cd "$AUTOMATION_DIR"
cp stack/.env.example stack/.env
python3 - <<'PY'
import pathlib
p = pathlib.Path("stack/.env")
text = p.read_text()
for old, new in [
    ("SUPERSET_DIR=/home/ubuntu/repos/superset",
     "SUPERSET_DIR=/home/ubuntu/phase5-bootstrap/superset"),
    ("AUTOMATION_DIR=/home/ubuntu/repos/devin-automation",
     "AUTOMATION_DIR=/home/ubuntu/phase5-bootstrap/devin-automation"),
    ("SUPERSET_PORT_HOST=8088", "SUPERSET_PORT_HOST=8288"),
    ("SUPERSET_MCP_PORT_HOST=5008", "SUPERSET_MCP_PORT_HOST=5208"),
    ("PORTAL_PORT_HOST=8090", "PORTAL_PORT_HOST=8290"),
    ("SUPERSET_NETWORK=superset_default", "SUPERSET_NETWORK=phase5check_default"),
    ("SUPERSET_BASE_URL=http://127.0.0.1:8088",
     "SUPERSET_BASE_URL=http://127.0.0.1:8288"),
    ("SUPERSET_MCP_URL=http://127.0.0.1:5008/mcp",
     "SUPERSET_MCP_URL=http://127.0.0.1:5208/mcp"),
    ("PORTAL_RUN_ID=local", "PORTAL_RUN_ID=phase5-bootstrap"),
]:
    assert old in text, old
    text = text.replace(old, new)
p.write_text(text)
PY

set -a; . stack/.env; set +a
export COMPOSE_PROJECT_NAME=phase5check
export SUPERSET_LIGHT_IMAGE=phase5check-superset-light

echo "=== 1. stack up (web + MCP) ==="
cd "$SUPERSET_DIR"
time docker compose -f docker-compose-light.yml \
  -f "$AUTOMATION_DIR/stack/docker-compose.ports.yml" \
  up -d superset-light superset-mcp-light

echo "=== 2. wait for web ==="
for i in $(seq 1 90); do
  if curl -fsS "$SUPERSET_BASE_URL/health" >/dev/null 2>&1; then echo "web healthy after ${i}0s"; break; fi
  sleep 10
done
curl -fsS "$SUPERSET_BASE_URL/health"; echo

echo "=== 3. container health (MCP healthcheck is protocol-level) ==="
for i in $(seq 1 30); do
  state=$(docker inspect -f '{{.State.Health.Status}}' phase5check-superset-mcp-light-1 2>/dev/null || echo none)
  echo "mcp health: $state"
  [ "$state" = healthy ] && break
  sleep 10
done
docker ps --filter "name=phase5check" --format '{{.Names}}\t{{.Status}}'

echo "=== 4. seed ==="
cd "$AUTOMATION_DIR"
python3 -m pip install -q -r requirements.txt
python3 scripts/seed_synthetic.py

echo "=== 5. provenance ==="
python3 scripts/capture_provenance.py
echo "=== bootstrap OK ==="
