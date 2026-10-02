#!/usr/bin/env bash
# Register (or update) the Debezium Postgres connector on the local Kafka Connect worker.
# Usage: cdc/debezium/register.sh [connect-url]      (default http://localhost:8083)
set -euo pipefail
CONNECT_URL="${1:-http://localhost:${CONNECT_PORT:-8083}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$HERE/postgres-connector.json")"

# PUT /connectors/{name}/config is create-or-update, so this script is safe to re-run.
python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))["config"]))' "$HERE/postgres-connector.json" \
  | curl -fsS -X PUT -H 'Content-Type: application/json' --data @- "$CONNECT_URL/connectors/$NAME/config"
echo
echo "Status:"; curl -fsS "$CONNECT_URL/connectors/$NAME/status"; echo
echo "Topics will be: lakebase.public.parties, lakebase.public.decisions, lakebase.public.decision_events"
