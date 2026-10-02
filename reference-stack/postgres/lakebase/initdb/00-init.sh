#!/usr/bin/env bash
# Runs once, on first start of the lakebase data directory (docker-entrypoint-initdb.d).
# Creates: synthetic schema, replication role, publication, and a separate `projections` database.
# It deliberately does NOT create replication slots: an unused slot retains WAL forever.
# Each CDC tool creates (or the harness creates) its own slot when it is first used.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v cdc_user="${CDC_USER:-cdc_reader}" -v cdc_password="${CDC_PASSWORD:?CDC_PASSWORD is required}" \
  -f /docker-entrypoint-initdb.d/10-schema.sql.in

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
CREATE DATABASE projections;
SQL
