#!/usr/bin/env bash
# Create the logical replication slot Airbyte's Postgres source needs (pgoutput), on demand.
# Airbyte's docs require a slot used by exactly one source. We do NOT create it at DB init: an unused slot
# retains WAL. Drop it with:  SELECT pg_drop_replication_slot('airbyte_slot');
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
set -a; [ -f .env ] && . ./.env; set +a
docker compose exec -T postgres-lakebase psql -U "${LAKEBASE_USER:-lakebase}" -d "${LAKEBASE_DB:-lakebase}" -v ON_ERROR_STOP=1 -c \
 "SELECT pg_create_logical_replication_slot('airbyte_slot','pgoutput') WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='airbyte_slot');"
