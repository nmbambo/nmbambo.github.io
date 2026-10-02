# Reference stack

The server side of the event-sourced search on this site, plus a harness for comparing three CDC tools.
**A reference implementation for one workstation, not a production deployment.** Everything binds to `127.0.0.1`, runs on
synthetic data only (POPIA), and the browser never talks to any of it unless you configure it to.

**Status when this was written:** the Docker daemon was not available, so no image was pulled and nothing was run. What was
checked: `docker compose config` (with and without `--profile solace --profile olake`), image tags via `docker manifest inspect`, the
Python unit tests (gateway, projector, harness; none need containers), and the docs cited below. Every runtime claim in this
README is therefore "designed to", not "observed". The results table at the bottom is empty on purpose.

## What is in it

| Piece | What it is |
|---|---|
| `postgres-lakebase` | Postgres 16, `wal_level=logical`, `max_slot_wal_keep_size=2GB`. Synthetic schema `parties`, `decisions`, `decision_events`, a `cdc_reader` replication role, publication `ingqiqo_pub`. A second database `projections` holds the projector's tables. No replication slots are created at init (an unused slot retains WAL); each tool creates its own. |
| `kafka` | Apache Kafka, KRaft single node, no ZooKeeper. |
| `kafka-rest` | Confluent REST Proxy. The browser's `KafkaRestBroker` posts to `/v3/clusters/{id}/topics/{topic}/records`. CORS is set from `ALLOWED_ORIGINS`. |
| `debezium-connect` | Kafka Connect with Debezium. Connector config in `cdc/debezium/`. |
| `kurrentdb` | KurrentDB single insecure node (event store 1). |
| `messagedb` | Postgres 16 plus the Message DB schema, built from the pinned release by `messagedb/Dockerfile` (event store 2). |
| `gateway` | Python 3.11, standard-library HTTP. The site's `GatewayStore` seam. Writes KurrentDB or Message DB (`STORE=kurrentdb\|messagedb`) and publishes each new event to Kafka topic `ingqiqo.events`. |
| `projector` | Idempotent Kafka consumer. Checkpoint table + processed-event-id set in Postgres, so at-least-once delivery has effectively-once effect. Builds `rm_content_docs` and `rm_algorithm_stats`. |
| `olake` (profile `olake`) | OLake CLI image, one-shot batch runner. |
| `solace` (profile `solace`) | Solace PubSub+ Standard for the `SolaceRestBroker` seam. |
| Airbyte | Not in compose. Installed with `abctl`; see `cdc/airbyte/README.md`. |

```
browser ──POST /streams/{s}──► gateway ──► KurrentDB | Message DB      (event store, source of truth)
   │                              └──► kafka-rest ──► Kafka ingqiqo.events ──► projector ──► Postgres read models
   └──(optional) KafkaRestBroker ───────►┘
lakebase Postgres ──WAL──► Debezium ──► Kafka lakebase.public.*          (CDC leg, measured by the harness)
                     └───► OLake ──► Parquet/Iceberg      └───► Airbyte
```

## Run it

```bash
cd reference-stack
cp .env.example .env              # then change every CHANGE-ME value; .env is git-ignored
docker compose up -d              # first start builds gateway, projector and messagedb images
docker compose ps
curl -s localhost:8088/health     # {"status":"ok","store":"kurrentdb","kafkaClusterId":"..."}
cdc/debezium/register.sh          # start CDC from the lakebase into Kafka
```

Optional: `docker compose --profile solace up -d`, `docker compose --profile olake ...` (see `cdc/README.md`). Switch the event store with
`STORE=messagedb docker compose up -d gateway`. Stop with `docker compose down` (add `-v` to delete data).

Smoke test (any shell; uses a throwaway UUID):

```bash
ID=$(python3 -c 'import uuid;print(uuid.uuid4())')
curl -s -XPOST localhost:8088/streams/content-demo -H 'Content-Type: application/json' -d \
 '{"events":[{"id":"'$ID'","type":"ContentAdded","stream":"content-demo","data":{"id":"demo#1","title":"x"},"meta":{"ts":"2026-01-01T00:00:00Z","schema":1,"source":"manual"}}],"expectedVersion":-1}'
curl -s 'localhost:8088/events?from=0'
docker compose exec -T postgres-lakebase psql -U lakebase -d projections -c 'select * from rm_content_docs'
```

## Point the site at it

Edit `assets/es/config.json` on a local checkout (this stack does not touch the site):

```json
{
  "broker": { "type": "kafka-rest", "url": "http://localhost:8082", "clusterId": "<kafkaClusterId from /health>", "topicPrefix": "ingqiqo." },
  "remoteStore": { "url": "http://localhost:8088" }
}
```

Solace instead of Kafka REST: `"broker": { "type": "solace-rest", "url": "http://localhost:8088", "vpn": "default" }`, with `SOLACE_REST_URL=http://solace:9000` in `.env`
and `--profile solace`. The gateway forwards `POST /TOPIC/...` to Solace and adds CORS, because Solace's REST docs describe no CORS support for browsers
(unconfirmed). It drops the `Solace-Message-VPN` header the site sends: that header is not in Solace's REST header list; the VPN is chosen by client username and port.

Serve the site locally from an origin in `ALLOWED_ORIGINS` (`python3 -m http.server 8000` from the repo root matches the default). An HTTPS page calling
`http://localhost` may be blocked by mixed-content or private-network rules depending on browser; not tested.

How the gateway meets the contract (`es-search-build/00_brief/CONTRACT.md` sections 1 and 5):

- `POST /streams/{stream}` body `{events, expectedVersion?}`: idempotent by `id`; 200 `{appended, positions}`; 409 on a version mismatch (the site maps 409 to `ConcurrencyError`); 400 on a bad envelope.
- `GET /events?from=&limit=`, `GET /streams/{stream}`, `GET /events/last`, `GET /events/{id}` (404 when absent), matching what `GatewayStore` calls.
- The envelope is validated (UUID or 64-hex sha256 `id`, object `data`, `meta.ts/schema=1/source`). Both stores require UUID event ids, so sha256 ids map to a UUID by uuid5 and the original `id` is kept in event metadata and returned unchanged.
- Differences you should know about: (1) `position` is global and increasing but **not dense** (KurrentDB commit position; Message DB `global_position - 1`), and clients must only advance it. (2) If every event in a request already exists the request is a no-op success even when `expectedVersion` is stale, so retries after a lost response do not 409; the browser `MemoryStore` checks the version first. (3) KurrentDB cannot look an event up by id, so the gateway indexes ids in memory (one gateway instance assumed).
- No request bodies or event data are logged.

Kafka publishing is best-effort with retries and an in-memory retry queue (`/health` shows `kafkaPending`). It is **not** a transactional outbox: a gateway crash can lose queued items. The projector tolerates duplicates, not losses. For no-loss, publish from the store (a KurrentDB subscription, or Message DB polling) or use the outbox pattern with Debezium.

## Head-to-head method

Full detail in `cdc/README.md`. In short: seed N synthetic rows, start the tool (snapshot), apply a fixed seeded mutation workload (inserts, updates, deletes; every row carries a unique `workload_seq`), and measure snapshot time, p50/p95 change-to-arrival latency, burst throughput, per-key ordering, delete/tombstone handling, an `ADD COLUMN` mid-run, kill-and-restart (duplicates, gaps), and `docker stats` footprint. `cdc/harness/run.py` writes `results/<tool>.json`; `cdc/harness/compare.py` renders the table.

```bash
python3 cdc/harness/run.py --tool debezium      # also: olake, airbyte, all
python3 cdc/harness/compare.py --out results/comparison.md
```

Same machine, same run, no other load. Run each tool more than once before believing a difference. Nothing is written to a results file unless the harness measured it.

## Results

**Not yet measured.** Filled by `compare.py` from `results/*.json`; no number below was produced by hand.

| Metric | Debezium | OLake | Airbyte |
|---|---|---|---|
| Initial snapshot (s) | not yet measured | not yet measured | not yet measured |
| Latency p50 (ms) | not yet measured | not yet measured | not yet measured |
| Latency p95 (ms) | not yet measured | not yet measured | not yet measured |
| Throughput delivered (changes/s, burst) | not yet measured | not yet measured | not yet measured |
| Ordering violations per key | not yet measured | not yet measured | not yet measured |
| Deletes seen as delete / expected | not yet measured | not yet measured | not yet measured |
| Tombstones seen / expected deletes | not yet measured | not yet measured | not yet measured |
| Schema change: rows carrying new column | not yet measured | not yet measured | not yet measured |
| Schema change: first change after ALTER (s) | not yet measured | not yet measured | not yet measured |
| Restart: duplicates | not yet measured | not yet measured | not yet measured |
| Restart: missing changes | not yet measured | not yet measured | not yet measured |
| Restart: missing final state (keys) | not yet measured | not yet measured | not yet measured |
| Restart: restart to first change (s) | not yet measured | not yet measured | not yet measured |
| Footprint: tool CPU avg / peak (%) | not yet measured | not yet measured | not yet measured |
| Footprint: tool memory avg / peak (MiB) | not yet measured | not yet measured | not yet measured |

## Design fit of each CDC tool (from the docs; not from measurements)

- **Debezium** is log-based streaming into Kafka, which is the shape this architecture already has. The PostgreSQL connector reference describes a delete event followed by a tombstone with the same key, and its delivery guarantee as exactly-once in normal operation and at-least-once in abnormal situations (such as a restart after a crash). That is why the projector dedupes by event id. Costs: Kafka Connect to operate, and with default replica identity a delete carries the key only (set `REPLICA IDENTITY FULL` if you need the before-image). It gives row changes, not decision events: the lakebase's `decision_events` table is the better source for event semantics. *Docs: debezium.io PostgreSQL connector reference.*
- **OLake** targets Postgres to Apache Iceberg (or Parquet), with full refresh plus CDC through `pgoutput`, and an Iceberg upsert mode that deduplicates with equality deletes unless `append_mode` is on. It fits the analytical leg (query history in a lakehouse), not the event bus: its CLI runs sync-and-exit batches, so latency is a batch property. It warns against sharing a slot or publication between jobs, and notes wal2json is deprecated for `pgoutput`. The OLake docs say to prefer version-pinned images, as done here. *Docs: olake.io Postgres connector, Docker CLI, JDBC catalog and Parquet writer pages, plus the datazip-inc/olake README.*
- **Airbyte** is an ELT platform with a UI, many destinations, and scheduled batch syncs; CDC reads a dedicated slot. It is the heaviest to run (abctl plus a Kubernetes-in-Docker cluster). Its Kafka destination lists Incremental Append as supported and Append + Deduped as not, so downstream dedupe is the consumer's job (the projector already does it). Its docs warn that exceeding `max_slot_wal_keep_size` invalidates the slot and fails the sync until you recreate the slot and reset the connection (this stack sets that limit to 2GB). *Docs: docs.airbyte.com abctl, Postgres source, Kafka destination.*

## Image tags

Checked 2026-10-02 with `docker manifest inspect` (the registries were reachable; the daemon was not, so **no image was pulled or run**).

| Service | Image and tag | Status |
|---|---|---|
| kafka | `apache/kafka:4.1.2` | tag verified |
| kafka-rest | `confluentinc/cp-kafka-rest:8.0.8` | tag verified |
| debezium-connect | `quay.io/debezium/connect:3.7.0.Final` | tag verified (Debezium docs reviewed are for 3.7) |
| postgres-lakebase, messagedb base | `postgres:16.10-bookworm` | tag verified |
| kurrentdb | `docker.kurrent.io/kurrent-latest/kurrentdb:25.1.0` | tag verified. Docker Hub `kurrentplatform/kurrentdb` also lists 26.1.2 and 26.2.0 (verified there); `docker.kurrent.io` did not resolve `26.2.0` or `26.1.2`, and the 25.1.0 line is what the client library and env var names below were checked against |
| olake | `olakego/source-postgres:v0.11.2` | tag verified. `datazip/olake` returned "object not found" on Docker Hub |
| solace | `solace/solace-pubsub-standard:10.26.7` | tag verified |
| gateway, projector | `python:3.11-slim` base | tag verified; floating patch version |
| Message DB | release `v1.3.0` | `database/VERSION.txt` reads 1.3.0 and files at the `v1.3.0` tag were readable on raw.githubusercontent.com. The release tarball that the Dockerfile downloads could not be fetched from the build sandbox, and its checksum is not pinned |

Python packages are pinned to the versions PyPI served on 2026-10-02: `kurrentdbclient==1.3.3` (the official client, formerly `esdbclient`; it accepts `kurrentdb://` URIs), `psycopg[binary]==3.3.6`, `confluent-kafka==2.15.1`, `pyarrow==25.0.1`.

**Could not confirm** (read these as assumptions): Kafka KRaft environment variable names for `apache/kafka` and `KAFKA_REST_*` names for `cp-kafka-rest` (standard conventions, not re-read); CORS actually working end to end from a browser; the Solace default VPN accepting anonymous REST publishes on port 9000; `EnvVarConfigProvider` working through the Debezium image's `CONNECT_*` mapping; OLake's output directory layout, `_op_type` values and `streams.json` patching; every Airbyte API path and record shape; whether `kurrentdbclient`'s `read_all` boundary is inclusive (the gateway filters defensively); the Message DB store against a live database (its logic has no unit test; the KurrentDB store is tested only against an in-process fake).

## Sources

Debezium PostgreSQL connector reference (debezium.io/documentation/reference/stable/connectors/postgresql.html); Confluent REST Proxy v3 produce and config reference (docs.confluent.io/platform/current/kafka-rest); Solace REST messaging protocol (docs.solace.com/API/RESTMessagingPrtl); KurrentDB installation (docs.kurrent.io/server/v25.1/quick-start/installation.html) and `kurrentdbclient` on PyPI; Message DB (github.com/message-db/message-db, `database/install.sh`, `write-message`, `get-stream-messages`); OLake (olake.io docs and github.com/datazip-inc/olake-docs, github.com/datazip-inc/olake); Airbyte (docs.airbyte.com). All read 2026-10-02.

## Tests

```bash
(cd gateway && python3 -m unittest)         # envelope, stores (memory + KurrentDB fake), HTTP server, Kafka publisher
(cd projector && python3 -m unittest)       # idempotency, replay, ordering guard, checkpoint
(cd cdc/harness && python3 -m unittest)     # workload, parsers, metrics, delivery checks, compare table
```

They need only the standard library. Run them as is; nothing here starts a container.
