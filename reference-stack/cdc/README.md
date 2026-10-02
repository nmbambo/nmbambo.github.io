# CDC configs and head-to-head harness

| Tool | Config | How it runs here |
|---|---|---|
| Debezium (Postgres -> Kafka) | `debezium/postgres-connector.json`, `debezium/register.sh` | Kafka Connect worker `debezium-connect` in compose (always on) |
| OLake (Postgres -> Parquet, or Iceberg) | `olake/config/*.example.json`, `olake/prepare.py` | one-shot CLI container, compose profile `olake` |
| Airbyte (Postgres CDC -> Kafka) | `airbyte/README.md`, `airbyte/create-slot.sh` | installed separately with `abctl` |
| Harness | `harness/` | Python 3.11 on the host, talks to the three above |

## Debezium

```bash
docker compose up -d
cdc/debezium/register.sh            # PUT /connectors/lakebase-postgres/config; topics lakebase.public.*
```

Config keys used, all confirmed against the Debezium PostgreSQL connector reference (docs for 3.7): `connector.class`,
`plugin.name=pgoutput`, `slot.name`, `publication.name`, `publication.autocreate.mode=disabled` (the publication is created at
DB init, so the connector needs no superuser), `topic.prefix`, `table.include.list`, `snapshot.mode=initial`,
`tombstones.on.delete=true`, `decimal.handling.mode`, `heartbeat.interval.ms`, `database.*`. The password is
`${env:CDC_PASSWORD}`, resolved inside Kafka Connect by `EnvVarConfigProvider` (enabled by two `CONNECT_CONFIG_PROVIDERS*` env vars in compose),
so it is not in the connector JSON or in the Connect config topic. The provider setup is standard Kafka Connect but was not run here.

## OLake

```bash
docker compose --profile olake up -d postgres-lakebase     # the olake service itself is a one-shot runner
python3 cdc/olake/prepare.py      # renders configs from .env, creates a slot, runs `discover`, patches streams.json to CDC
docker compose --profile olake run --rm olake            # one sync run: snapshot first time, WAL since last state after
```

OLake runs in batches, not as a daemon: each `sync` reads until `initial_wait_time` seconds of idle, writes files, exits. The default
destination is local Parquet (`destination.example.json`); `destination.iceberg-jdbc.example.json` shows the Iceberg + JDBC catalog + MinIO
shape from OLake's JDBC catalog guide, but MinIO and the catalog database are not in this compose file. Image: `olakego/source-postgres`
(one image per source driver; the `datazip/olake` name in older material did not resolve on Docker Hub when checked).

## Harness

```bash
python3 -m venv /tmp/cdc-venv && . /tmp/cdc-venv/bin/activate
pip install -r cdc/harness/requirements.txt
python3 cdc/harness/run.py --tool debezium --rows 10000 --ops 5000 --rate 200
python3 cdc/harness/run.py --tool olake
python3 cdc/harness/compare.py --out results/comparison.md
```

### What is measured, and how

All timestamps are the harness's own `time.time()` on one host, so there is no cross-machine clock skew, only the harness's own
polling resolution (about 50 ms for Kafka consumption).

| Metric | Method |
|---|---|
| Initial snapshot time | Seed N synthetic decisions **before** the connector starts. Seconds from start until N distinct snapshot rows are seen at the sink. |
| Change-to-arrival latency p50/p95 | Every mutation sets `decisions.workload_seq` to a unique number (deletes are identified by key). Latency = first arrival at the sink minus the time the writer's COMMIT returned. Paced at `--rate`. |
| Throughput | Unthrottled burst of `--burst` mutations in 100-row transactions; delivered changes per second from first commit to last arrival. |
| Ordering per key | In sink order (Kafka partition/offset; for OLake file mtime then row), `workload_seq` must never decrease for a key and nothing may follow a delete. Updates deliberately repeat keys. |
| Delete / tombstone handling | Each deleted key must arrive as a delete; for Kafka sinks the follow-up tombstone (null value) is counted separately. |
| Schema change | `ALTER TABLE decisions ADD COLUMN risk_band`, then update 50 rows. Count rows whose delivered image carries the new column and time to the first one. |
| Restart recovery | Kill the connector at 1/3 of a workload, keep writing, restart at 2/3. Report duplicates (extra deliveries), missing changes, and keys whose final state never arrived. |
| Resource footprint | `docker stats --no-stream` sampled during the paced workload; per-container average and peak CPU and memory. |

Known limits: the workload touches `decisions` only (the other two tables are replicated but not scored); a tool that coalesces
several updates to one key into one row reports those as `missing` while `missing_final` stays 0, which is why both are shown;
batch tools (OLake, Airbyte) have latency dominated by their batch interval by design; on a laptop, Docker and the harness share
CPU with the tools, so compare tools on the same machine and run, not against other people's numbers.
