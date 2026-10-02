#!/usr/bin/env bash
# Runs once on first start. Uses Message DB's own installer (database/install.sh), which creates the
# message_store role, the message_store database, the schema, the messages table and the functions
# (write_message, get_stream_messages, get_category_messages, stream_version, ...).
set -euo pipefail
export PGUSER="${POSTGRES_USER:-postgres}"
export DATABASE_NAME="${MESSAGE_DB_NAME:-message_store}"
cd /opt/message-db/database
./install.sh
