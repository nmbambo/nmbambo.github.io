# Airbyte: separate install, not part of docker compose

Airbyte's supported local install is `abctl`, which runs Airbyte in a Kubernetes-in-Docker (kind) cluster. Forcing that
into this compose file would misrepresent how it is deployed, so it is a separate step. Nothing below was executed
when this stack was written (no Docker daemon was available); treat it as a checklist to confirm, not a proven recipe.

Sources: Airbyte docs for abctl (docs.airbyte.com/platform/deploying-airbyte/abctl), the Postgres source
(docs.airbyte.com/integrations/sources/postgres) and the Kafka destination (docs.airbyte.com/integrations/destinations/kafka),
read on 2026-10-02.

## 1. Install Airbyte

```bash
curl -LsfS https://get.airbyte.com | bash -     # installs abctl
abctl local install                              # default http://localhost:8000
abctl local credentials                          # prints the login and the client id/secret for the API
```

Resource note from the docs era checked: allow several GB of RAM and a few CPUs for Airbyte alone.

## 2. Let Airbyte reach the stack

Airbyte's pods run inside the kind container, not on the compose network. The least invasive option to try first:

```bash
docker network connect ingqiqo-reference_default airbyte-abctl-control-plane
```

Then, from Airbyte, the source host is `postgres-lakebase:5432` and Kafka bootstrap is `kafka:9092` (the INTERNAL listener).
If pod DNS does not resolve those names on your machine, fall back to your host address and add an advertised listener for it
to the Kafka service (the EXTERNAL listener advertises `localhost`, which a pod cannot use). Unconfirmed either way.

## 3. Prepare Postgres

`wal_level=logical`, the `cdc_reader` role (REPLICATION + SELECT) and the `ingqiqo_pub` publication already exist. Create the slot:

```bash
cdc/airbyte/create-slot.sh        # creates airbyte_slot with pgoutput
```

Airbyte's docs: replication identity is required on published tables; our tables have primary keys, so the default
identity is sufficient for key-based CDC.

## 4. Connector settings (enter in the UI, or via Terraform/API)

Source: Postgres
| Field | Value |
|---|---|
| Host / Port / Database | `postgres-lakebase` / `5432` / `lakebase` |
| User / Password | `cdc_reader` / your `CDC_PASSWORD` |
| Replication method | Read Changes using Write-Ahead Log (CDC) |
| Replication slot | `airbyte_slot` |
| Publication | `ingqiqo_pub` |
| Streams | `public.decisions` (add `parties`, `decision_events` if you want parity with the other tools) |
| Sync mode | Incremental - Append (the Kafka destination's docs list Incremental Append as supported and Append + Deduped as not, so every change arrives as a record) |

Destination: Kafka
| Field | Value |
|---|---|
| Bootstrap servers | `kafka:9092` |
| Topic pattern | `airbyte.{stream}` (so the harness reads `airbyte.decisions`; the docs say topics must exist or the broker must auto-create them, and compose enables auto-create) |
| Security protocol | PLAINTEXT |
| Sync producer | on (the docs list this option; it makes delivery observable per record) |

Create the connection with a manual schedule; the harness triggers syncs through the API.

## 5. Point the harness at it

```bash
cat > cdc/airbyte/airbyte.local.env <<'EOF'      # git-ignored
AIRBYTE_API_URL=http://localhost:8000/api/public
AIRBYTE_CLIENT_ID=...        # from `abctl local credentials`
AIRBYTE_CLIENT_SECRET=...
AIRBYTE_CONNECTION_ID=...    # from the connection URL in the UI
AIRBYTE_TOPIC=airbyte.decisions
EOF
python3 cdc/harness/run.py --tool airbyte
```

## What is unverified

- The public API paths used by the harness (`POST /v1/applications/token`, `POST /v1/jobs`) are from memory of Airbyte's API, not checked this session.
- The record shape in the Kafka topic (`_airbyte_data`, `_ab_cdc_deleted_at`). The Postgres source was rewritten on the bulk CDK (3.8.0 in the changelog read), so field names may differ.
- Killing and restarting Airbyte workers is not scripted, so the restart-recovery scenario is recorded as `skipped` for Airbyte. Do it by hand and record it as a note, not as a number in the table.
- Resource footprint: Airbyte runs inside one kind container, so `docker stats` shows one blob for the whole platform. Compare that with care.
