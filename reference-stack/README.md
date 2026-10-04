# Reference stack

The server side of the event-sourced search on this site, plus a harness for comparing three CDC tools.
**A reference implementation for one workstation, not a production deployment.** Everything binds to `127.0.0.1`, runs on
synthetic data only (POPIA), and the browser never talks to any of it unless you configure it to.

**The LIGHT stack is the default.** `docker compose up -d` starts it: Postgres (the "lakebase"), NATS JetStream, a command
service, Debezium Server, a query service, and a KrakenD edge gateway in front of the two services. About 0.5 GB of RAM in
total, against about 1.5 GB for the heavy stack (Kafka, KurrentDB, Message DB and friends), which is now the opt-in profile
`heavy` (see "Heavy profile" below).

**Status (2026-10-04):** the light stack, including the KrakenD edge, was brought up on real containers and checked end to end
(see "Verified live (2026-10-04)"). The heavy profile was verified on 2026-10-02, before the split into profiles, and has not been
re-run since. Not run at all: the CDC head-to-head (`cdc/`; its results table is empty on purpose), Solace, OLake, Airbyte, and
anything under "Telemetry sources". Anything not listed under a "Verified live" heading is "designed to", not observed.

## What is in the light stack

| Service | What it is |
|---|---|
| `krakend` | KrakenD Community Edition 2.10 (`krakend:2.10`), the single edge on `127.0.0.1:8092`. Declarative and stateless: one file, `krakend/krakend.json`. Routes writes to `command` and reads to `query`, adds CORS from one place and a rate limit on the write route. Responses and status codes pass through unchanged (`no-op` encoding). |
| `command` | Python standard-library HTTP service, host port 8090 (8088 inside). `POST /streams/{stream}` validates the envelope and appends to JetStream stream `ES` (idempotent by event id, `expectedVersion` honoured, 409 on a mismatch). Same routes as the heavy gateway, so the browser's `GatewayStore` works unchanged. Creates the streams and the checkpoint KV bucket on start. |
| `nats` | NATS 2.15.0 with JetStream: broker, event store (stream `ES`), CDC landing stream (`CDC`) and checkpoint bucket. |
| `postgres-lakebase` | Postgres 16, `wal_level=logical`. Synthetic schema `parties`, `decisions`, `decision_events`, a `cdc_reader` role, publication `ingqiqo_pub`. |
| `debezium-server` | Debezium Server 3.7.0.Final with the NATS JetStream sink (no Kafka Connect): lakebase changes land on stream `CDC`, subjects `lakebase.<schema>.<table>`. Config in `cdc/debezium-server/application.properties`; the password arrives as an environment variable. |
| `query` | Python service, host port 8091 (8089 inside). Durable JetStream consumers on `ES` and `CDC` project into a SQLite FTS5 read model; `GET /search`, `/events`, `/stats`, `/health`. Delete its database and it rebuilds the same read model from the log. Read only. |

`command` and `query` stay published on their own host ports (8090, 8091) for direct access and for `tests/live/10_light.py`;
KrakenD is an addition in front of them, not a replacement.

```
                         127.0.0.1:8092
 browser / curl ──► KrakenD (edge: CORS, rate limit on POST, no-op pass-through)
                      │  POST /streams/{s}                      GET /events /search /stats
                      ▼                                          ▼
                  command :8088 ──append──► NATS JetStream ◄──consume── query :8089 ──► SQLite FTS5
                                            stream ES (events)            ▲            (read model)
 lakebase Postgres ──WAL──► Debezium Server ──► stream CDC (lakebase.*) ───┘
                  checkpoint KV bucket `checkpoints` (query's positions)
```

## Run it

```bash
cd reference-stack
cp .env.example .env              # then change every CHANGE-ME value; .env is git-ignored
docker compose up -d --build      # light stack: builds command and query on first start
docker compose ps                 # every service has a healthcheck; all reach "healthy", Debezium's JVM last
curl -s 127.0.0.1:8092/health/command ; curl -s 127.0.0.1:8092/health/query
```

Stop with `docker compose down` (add `-v` to delete data; the event log, the read model, Debezium's offsets and the lakebase live
on named volumes and survive a plain `down`). In a sandbox that re-signs TLS add `-f docker-compose.yml -f docker-compose.sandbox.yml`
(see "Running in a restricted sandbox").

Smoke test through the edge (any shell; a throwaway UUID, a stream name of your own):

```bash
ID=$(python3 -c 'import uuid;print(uuid.uuid4())')
curl -s -XPOST 127.0.0.1:8092/streams/content-demo -H 'Content-Type: application/json' -d \
 '{"events":[{"id":"'$ID'","type":"ContentAdded","stream":"content-demo","data":{"id":"demo#1","title":"Zebra crossing"},"meta":{"ts":"2026-01-01T00:00:00Z","schema":1,"source":"manual"}}],"expectedVersion":-1}'
# -> {"appended":1,"positions":[...],"skipped":0}; the same call again -> {"appended":0,...,"skipped":1}; with expectedVersion -1 and a new id -> 409
curl -s '127.0.0.1:8092/search?q=zebra&algorithm=bm25&rows=5'
curl -s '127.0.0.1:8092/search?q=alpha+AND+(beta&algorithm=boolean'    # 400 with the character position of the error
curl -s 127.0.0.1:8092/stats
```

Routes at the edge (`krakend/krakend.json`): `POST /streams/{stream}` to `command`; `GET /events` (`from`, `limit`), `GET /search`
(`q`, `algorithm`, `rows`, `start`) and `GET /stats` to `query`; `GET /health/command` and `GET /health/query`; KrakenD's own
`/__health`. Only the listed query parameters are forwarded and only `Content-Type` and `Content-Length` are forwarded on the POST.
Other paths are 404 at the edge. CORS (`security/cors`) allows `http://localhost:8000`, `http://127.0.0.1:8000` and
`http://localhost:8080` (the `.env.example` value of `ALLOWED_ORIGINS`), methods GET, POST and OPTIONS, no credentials; **if you
change `ALLOWED_ORIGINS`, edit `allow_origins` in `krakend/krakend.json` to match** (the file is static by design). The POST route is limited to
20 requests/s overall and 10/s per client IP (`qos/ratelimit/router`); beyond that the edge answers 429. Port: `KRAKEND_PORT` (default 8092).

Two things found by running it, both worth knowing if you adapt the config:

- **The `devopsfaith/krakend` repository has no 2.10 tag** (`not found` on 2026-10-04); the official `krakend:2.10` image (KrakenD 2.10.2) is used.
- **KrakenD forwards a POST body with `Transfer-Encoding: chunked`, and the command service reads only `Content-Length`**, so every append arrived
  as an empty body (400 "body must be ..."). The fix is in the edge config, not the service: `Content-Length` is in the route's `input_headers`, and KrakenD then sends it unchunked.

## Footprint (MiB RSS, `docker stats --no-stream`)

| Service | MiB |
|---|---|
| nats | 9–12 |
| command | 24–31 |
| query | 30 |
| krakend | 18 (after the edge test and a 40-request burst) |
| postgres-lakebase | 45–56 |
| debezium-server (`-Xmx256m`) | 317–344 |
| **Light total (with krakend)** | **about 450–490** |
| solr (profile `solr`) | 681–707 |
| solr-sink (profile `solr`) | 24 |
| heavy profile, for comparison | about 1,500 (see "Heavy profile") |

Ranges are two samples (the first measured before krakend was added, the second at the end of the 2026-10-04 run); Debezium's JVM is the least stable figure.

## Verified live (2026-10-04)

Run on one sandbox (2 vCPU, 8 GB RAM, Docker Engine 29.8, Compose 5.5). Both scripts are standard-library Python, create their own
uniquely named data, and can be re-run without cleaning up.

| Script | What it proves | Result |
|---|---|---|
| `tests/live/10_light.py` | The site's event log (244 events, 224 streams) goes in through the command service; re-posting is de-duplicated; a stale `expectedVersion` is 409; the query service catches up and answers all six search methods (boolean, bm25, fuzzy, prefix, phrase, substring); CDC insert, update and delete in the lakebase reach search through Debezium Server and JetStream; the query service restarted with its database deleted rebuilds an identical read model; footprint | pass, 16 of 16 |
| `tests/live/12_edge.py` | Everything through `127.0.0.1:8092`: append (200), same id again de-duplicated, stale `expectedVersion` gives 409 with the service's body, correct `expectedVersion` accepted, bad envelope 400; the new document is found by `GET /search`; `rows`/`start` pass through; a malformed boolean query and a non-numeric `rows` give 400 from the query service; `/stats`, `/events?limit=`, both health routes; unrouted path 404; an allowed origin gets `Access-Control-Allow-Origin` and no credentials header, a disallowed origin gets none, on a plain request and on a preflight | pass, 18 of 18 |

Also observed by hand: a burst of 40 POSTs of `{}` from one client was answered 400 (the service's validation) for the first 17 and 429 from the edge after that, as the per-client limit intends.
Not verified: CORS from a real browser against the edge (the header logic is verified with `urllib`; the heavy gateway's Chromium check, `06_cors.py`, was not repointed);
the `solr` profile was healthy on this run but its parity scripts were not re-run; the KrakenD config was not load-tested.

## Profiles

| Profile | Adds | Start |
|---|---|---|
| (none) | the light stack above | `docker compose up -d` |
| `solr` | `solr` (Solr 9.10.1, core `ingqiqo` from the committed configset) and `solr-sink` (`projector/solr_sink.py --nats`: durable JetStream consumers feed Solr idempotently, no Kafka). Optional: SQLite FTS5 in `query` is the default read model. | `docker compose --profile solr up -d` |
| `heavy` | `kafka`, `kafka-rest`, `debezium-connect`, `kurrentdb`, `messagedb`, `gateway`, `projector` (below) | `docker compose --profile heavy up -d` |
| `messagedb` | `messagedb` alone | `docker compose --profile messagedb up -d` |
| `olake` | OLake CLI image, one-shot batch runner (`cdc/olake/README.md`) | `docker compose --profile olake run --rm olake <args>` |
| `solace` | Solace PubSub+ Standard for the `SolaceRestBroker` seam | `docker compose --profile solace up -d` |

Profiles combine (`--profile heavy --profile solr`). Airbyte is not in compose; see `cdc/airbyte/README.md`.

## Telemetry sources (designed to, not run)

[OpenDAX](https://opendax.org) is open-source Data Acquisition and Control software (its GUI is qDAX). The intended use here: an OpenDAX instance
publishes tag changes to NATS subjects `daq.<plant>.<tag>`, and those subjects are a Bronze source next to the event log and the CDC stream. **None of this
is implemented**: no OpenDAX instance was run, no `daq.*` stream is created by compose, and `query` does not consume it.

## Heavy profile

The original stack: Kafka and the Confluent REST Proxy for the browser's broker seam, two event stores (KurrentDB, Message DB) behind a gateway, Debezium on
Kafka Connect, and an idempotent Kafka projector into Postgres read models. Start it with `docker compose --profile heavy up -d` (light and heavy together).
Its section below is the 2026-10-02 write-up, kept as it was except for the start command. The live scripts `01` to `07` predate the profile split: they call plain
`docker compose up -d`, so run them with `COMPOSE_PROFILES=heavy` set (not re-run since the split).

### What is in the heavy profile

| Piece | What it is |
|---|---|
| `postgres-lakebase` | Postgres 16, `wal_level=logical`, `max_slot_wal_keep_size=2GB`. Synthetic schema `parties`, `decisions`, `decision_events`, a `cdc_reader` replication role, publication `ingqiqo_pub`. A second database `projections` holds the projector's tables. No replication slots are created at init (an unused slot retains WAL); each tool creates its own. |
| `kafka` | Apache Kafka, KRaft single node, no ZooKeeper. |
| `kafka-rest` | Confluent REST Proxy. The browser's `KafkaRestBroker` posts to `/v3/clusters/{id}/topics/{topic}/records`. CORS is set from `ALLOWED_ORIGINS`. |
| `debezium-connect` | Kafka Connect with Debezium. Connector config in `cdc/debezium/`. |
| `kurrentdb` | KurrentDB single insecure node (event store 1). |
| `messagedb` | Postgres 16 plus the Message DB v1.3.0 schema, installed from the SQL vendored in `messagedb/vendor/` (no network at build time; see "Message DB vendoring") (event store 2). |
| `gateway` | Python 3.11, standard-library HTTP. The site's `GatewayStore` seam. Writes KurrentDB or Message DB (`STORE=kurrentdb\|messagedb`) and publishes each new event to Kafka topic `ingqiqo.events`. |
| `projector` | Idempotent Kafka consumer. Checkpoint table + processed-event-id set in Postgres, so at-least-once delivery has effectively-once effect. Builds `rm_content_docs` and `rm_algorithm_stats`. |
| Airbyte | Not in compose. Installed with `abctl`; see `cdc/airbyte/README.md`. |

```
browser ──POST /streams/{s}──► gateway ──► KurrentDB | Message DB      (event store, source of truth)
   │                              └──► kafka-rest ──► Kafka ingqiqo.events ──► projector ──► Postgres read models
   └──(optional) KafkaRestBroker ───────►┘
lakebase Postgres ──WAL──► Debezium ──► Kafka lakebase.public.*          (CDC leg, measured by the harness)
                     └───► OLake ──► Parquet/Iceberg      └───► Airbyte
```


### Run the heavy profile

```bash
cd reference-stack
cp .env.example .env              # then change every CHANGE-ME value; .env is git-ignored
docker compose --profile heavy up -d   # first start builds gateway, projector and messagedb images
docker compose ps                 # every core service has a healthcheck and reaches "healthy" (about 60 s, Debezium last)
curl -s localhost:8088/health     # {"status":"ok","store":"kurrentdb","kafkaClusterId":"..."}
cdc/debezium/register.sh          # start CDC from the lakebase into Kafka
```

Switch the event store with
`STORE=messagedb docker compose --profile heavy up -d --force-recreate --no-deps gateway`. Stop with `docker compose down` (add `-v` to delete data; the Kafka log, both event stores and the lakebase all live on named volumes and survive a plain `down`).

Smoke test (any shell; uses a throwaway UUID):

```bash
ID=$(python3 -c 'import uuid;print(uuid.uuid4())')
curl -s -XPOST localhost:8088/streams/content-demo -H 'Content-Type: application/json' -d \
 '{"events":[{"id":"'$ID'","type":"ContentAdded","stream":"content-demo","data":{"id":"demo#1","title":"x"},"meta":{"ts":"2026-01-01T00:00:00Z","schema":1,"source":"manual"}}],"expectedVersion":-1}'
curl -s 'localhost:8088/events?from=0'
docker compose exec -T postgres-lakebase psql -U lakebase -d projections -c 'select * from rm_content_docs'
```


### Point the site at the heavy profile

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

Serve the site locally from an origin in `ALLOWED_ORIGINS` (`python3 -m http.server 8000` from the repo root matches the default). CORS from a page on an allowed origin was verified in Chromium (below). An HTTPS page calling
`http://localhost` may be blocked by mixed-content or private-network rules depending on browser; not tested. The REST proxy ignores the cluster id in the URL (single-cluster v3), so a wrong `clusterId` still produces; the browser needs the id only to build a valid path.

How the gateway meets the contract (`es-search-build/00_brief/CONTRACT.md` sections 1 and 5):

- `POST /streams/{stream}` body `{events, expectedVersion?}`: idempotent by `id`; 200 `{appended, positions}`; 409 on a version mismatch (the site maps 409 to `ConcurrencyError`); 400 on a bad envelope.
- `GET /events?from=&limit=`, `GET /streams/{stream}`, `GET /events/last`, `GET /events/{id}` (404 when absent), matching what `GatewayStore` calls.
- The envelope is validated (UUID or 64-hex sha256 `id`, object `data`, `meta.ts/schema=1/source`). Both stores require UUID event ids, so sha256 ids map to a UUID by uuid5 and the original `id` is kept in event metadata and returned unchanged.
- Differences you should know about: (1) `position` is global and increasing but **not dense** (KurrentDB commit position, which jumps by hundreds; Message DB `global_position - 1`), and clients must only advance it. `GET /events?from=N` means "position >= N" even when N is not an existing position (the browser's `Projector.catchUp` asks for `last+1`): KurrentDB rejects such a commit position, so the gateway resumes at the next known one (fixed 2026-10-02, found live). (2) If every event in a request already exists the request is a no-op success even when `expectedVersion` is stale, so retries after a lost response do not 409; the browser `MemoryStore` checks the version first. (3) KurrentDB cannot look an event up by id, so the gateway indexes ids in memory (one gateway instance assumed). (4) The 409 body carries `expected` and `actual` for both stores.
- No request bodies or event data are logged.

Kafka publishing is best-effort with retries and an in-memory retry queue (`/health` shows `kafkaPending`). It is **not** a transactional outbox: a gateway crash can lose queued items. The projector tolerates duplicates, not losses. For no-loss, publish from the store (a KurrentDB subscription, or Message DB polling) or use the outbox pattern with Debezium.


### Heavy profile: verified live (2026-10-02)

Run on one sandbox (2 vCPU, 8 GB RAM, Docker Engine 29.4, Compose 5.1) with `python3 tests/live/run_all.py`. Each row is a script in
`tests/live/` that creates its own uniquely named streams and synthetic documents, so it can be re-run without cleaning up.
The stack must be running for 02 to 07 (01 starts it). 06 needs Playwright for Python and Chromium.

| # | Script | What it proves | Result |
|---|---|---|---|
| 1 | `01_compose.py` | `docker compose up -d`: all eight core services reach `healthy`; Kafka data survives `down`/`up` | pass; 61 s and 64 s in two runs, from `up -d` with images present and volumes kept (Debezium's JVM is last, about 60 s; the others are healthy within about 23 s) |
| 2 | `02_gateway_stores.py` | Gateway on **KurrentDB** and then on **Message DB**: append with `expectedVersion -1`; re-posting the same ids appends nothing; stale, ahead and `-1`-on-existing `expectedVersion` give 409 and store nothing; sha256 ids round-trip; bad envelopes give 400; `GET /events?from=` is inclusive, in append order, strictly increasing; `/streams/{s}`, `/events/{id}`, `/events/last` | pass, 27 of 27 per store |
| 3 | `03_kafka_publish.py` | Every appended event is on `ingqiqo.events` exactly once (re-posts are not republished), keyed by stream, with the store's position and version, in order within a stream | pass, 14 of 14 |
| 4 | `04_projector.py` | About 170 events (30 docs with several versions and removals, three search sessions). The projector is `SIGKILL`ed a third of the way through the posting and restarted two thirds of the way through. The read model equals one computed independently from the posted events; counters exact. Replay A: checkpoint rows deleted and the consumer group reset, the whole topic re-delivered, read model byte-identical. Replay B: every projector table truncated and rebuilt from Kafka, identical | pass, 13 of 13 |
| 5 | `05_adapters.py` + `adapters.live.test.mjs` | The browser's own `GatewayStore`, `KafkaRestBroker` and `fromConfig` (unmodified `assets/es/adapters.mjs`), Node's real `fetch`: append, idempotent re-append, `ConcurrencyError` on 409, sha256 ids, `has()`, `read()`, a `Projector.catchUp` against the live gateway, a produce to Kafka that is then read back, a browser-originated event that reaches the read model, the REST proxy's HTTP-200-with-`error_code` failures | pass, 10 Node tests and 3 checks |
| 6 | `06_cors.py` | Chromium (Playwright) loads a page from `http://localhost:8000` that imports the site's adapter modules and calls the gateway (POST, GET, a 409) and the REST proxy on another origin: accepted, preflights seen in both servers' logs. From `http://localhost:9999` (not in `ALLOWED_ORIGINS`) Chromium blocks all of it | pass, 14 of 14 |
| 7 | `07_footprint.py` | `docker stats --no-stream` per service | see below |

Memory (MiB, `docker stats --no-stream`; an idle stack about a minute after `up`, then again after the checks above):

| Service | Idle | After the checks |
|---|---|---|
| debezium-connect (no connector registered) | 436 | 439 (1,047 seen at another sample, JVM heap varies) |
| kurrentdb | 405 | 434 |
| kafka | 319 | 344 |
| kafka-rest | 218 | 231 |
| projector | 25 | 26 |
| gateway | 25 | 25 |
| postgres-lakebase | 23 | 25 |
| messagedb | 21 | 23 |
| **Total** | **1,472** | **1,546** |

The Debezium row is the one to watch: its JVM is the largest and the least stable figure. Solace was not run (below).

### Running in a restricted sandbox

The sandbox this was verified in has no running Docker daemon by default, an anonymous-pull rate limit on Docker Hub, and an egress proxy that re-signs TLS.

1. Start the daemon as root: `(setsid dockerd > /tmp/dockerd.log 2>&1 &)`, then `docker info`.
2. Pull through a registry mirror: `/etc/docker/daemon.json` with `{"registry-mirrors":["https://mirror.gcr.io"]}`, so plain image names (`postgres:16.10-bookworm`) come through Google's mirror instead of hitting Docker Hub's 429. `public.ecr.aws/docker/library/*` also works.
3. The gateway and projector builds run `pip install` inside a build container that does not trust the proxy's CA. `docker-compose.sandbox.yml` passes the CA bundle as an optional BuildKit secret (`buildca`, default `/root/.ccr/ca-bundle.crt`, override with `BUILD_CA_FILE`); the Dockerfiles use it only when present. Put `COMPOSE_FILE=docker-compose.yml:docker-compose.sandbox.yml` in your local `.env` and every `docker compose` command (and the live scripts) picks it up. A normal workstation needs neither.
4. `cp .env.example .env`, replace the `CHANGE-ME` values, `docker compose up -d --build`, then `python3 tests/live/run_all.py`.
5. Message DB needs no network at build time (next section).
6. This sandbox caps open files at a hard limit of 20,000, which is why the `solace` profile cannot start here.

### Message DB vendoring

The release tarball could not be downloaded from the build sandbox, so the v1.3.0 SQL is vendored in `messagedb/vendor/`: the `database/` directory
(schema, functions, installer scripts) from `git clone --depth 1 --branch v1.3.0 https://github.com/message-db/message-db`
(commit `e6999a6bd95ace6e8d70ec62a00c66b27ce8bf3b`), unmodified, plus `LICENSE` (MIT) and `SOURCE.txt`. `vendor/SHA256SUMS` lists a hash for every file.
`messagedb/Dockerfile` copies `vendor/` into the image, verifies `SHA256SUMS` during the build, and `install-message-db.sh` runs Message DB's own `install.sh` on first start.
The build was repeated with `docker build --network none --no-cache`: it succeeds. Verify the files yourself with `(cd messagedb/vendor && sha256sum -c SHA256SUMS)`.

### Corrections found by running it

- **Kafka lost its data on every `docker compose down`.** `apache/kafka` writes to `/tmp/kraft-combined-logs` by default, not to the `kafka-data` volume. `KAFKA_LOG_DIRS=/var/lib/kafka/data` is now set; 01 checks that the end offsets survive a down/up.
- **Gateway on KurrentDB: `GET /events?from=N` returned 500** unless N was exactly an existing commit position, which broke the browser's `Projector.catchUp` (it asks for `last+1`). Fixed in `gateway/stores.py` with a unit test (the in-process fake now rejects non-boundary positions like a real node).
- **Gateway on Message DB: every append failed** (`function acquire_lock(character varying) does not exist`). Message DB's functions call each other unqualified and need `search_path = message_store`; the gateway connects as `postgres`. The connection now sets `options=-c search_path=message_store,public`. Unit-tested.
- **Message DB 409 bodies had `actual: null`**; the stream version is now parsed from the error, matching the KurrentDB store.
- **Healthchecks** added for `kafka-rest`, `debezium-connect`, `gateway` and `projector` (the projector touches `/tmp/projector.heartbeat` every poll), so "healthy" means something; the gateway now waits for `kafka-rest` to be healthy, not merely started.
- **Solace**: the 10.26.7 start-up self-test (POST) demands a hard open-files limit of 1,048,576; the compose file said 38,048. Changed to 1,048,576 (what the broker asked for; the broker itself has not started here).
- **Projector, one edge to know about:** after a Kafka topic is recreated (offsets start again from 0) a stale checkpoint above the new end offset makes the consumer reset to the beginning, which `processed_events` absorbs, but the stored checkpoint (monotonic by design) stays high. After deliberately recreating a topic, run `TRUNCATE projector_checkpoint` and reset the consumer group. The Kafka volume fix above removes the usual way to get there.


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

Heavy-profile tags were checked 2026-10-02 with `docker manifest inspect`; those images were then pulled and run ("Heavy profile: verified live"); `olake` and `solace` were pulled, and `solace` was started once (it did not get past its start-up self-test here); `olake` was not run.

| Service | Image and tag | Status |
|---|---|---|
| krakend | `krakend:2.10` (official image, KrakenD 2.10.2, Alpine 3.21) | pulled and run 2026-10-04. `devopsfaith/krakend:2.10` returned "not found" |
| nats | `nats:2.15.0-alpine` | pulled and run 2026-10-04 |
| debezium-server | `quay.io/debezium/server:3.7.0.Final` | pulled and run 2026-10-04 |
| solr | `solr:9.10.1` | pulled and run (profile `solr`) |
| kafka | `apache/kafka:4.1.2` | tag verified |
| kafka-rest | `confluentinc/cp-kafka-rest:8.0.8` | tag verified |
| debezium-connect | `quay.io/debezium/connect:3.7.0.Final` | tag verified (Debezium docs reviewed are for 3.7) |
| postgres-lakebase, messagedb base | `postgres:16.10-bookworm` | tag verified |
| kurrentdb | `docker.kurrent.io/kurrent-latest/kurrentdb:25.1.0` | tag verified. Docker Hub `kurrentplatform/kurrentdb` also lists 26.1.2 and 26.2.0 (verified there); `docker.kurrent.io` did not resolve `26.2.0` or `26.1.2`, and the 25.1.0 line is what the client library and env var names below were checked against |
| olake | `olakego/source-postgres:v0.11.2` | tag verified. `datazip/olake` returned "object not found" on Docker Hub |
| solace | `solace/solace-pubsub-standard:10.26.7` | tag verified |
| command, query, gateway, projector | `python:3.11-slim` base | tag verified; floating patch version |
| Message DB | tag `v1.3.0` | vendored in `messagedb/vendor/` with a checksum list; the running database reports `message_store_version()` = 1.3.0 |

Python packages are pinned to the versions PyPI served on 2026-10-02: `kurrentdbclient==1.3.3` (the official client, formerly `esdbclient`; it accepts `kurrentdb://` URIs), `psycopg[binary]==3.3.6`, `confluent-kafka==2.15.1`, `pyarrow==25.0.1`.

**Confirmed live, 2026-10-02:** Kafka KRaft environment variable names for `apache/kafka:4.1.2` (broker starts and serves with the names in the compose file; one correction, `KAFKA_LOG_DIRS`, added) and `KAFKA_REST_*` for `cp-kafka-rest:8.0.8` (including `KAFKA_REST_ACCESS_CONTROL_ALLOW_ORIGIN` and `..._METHODS`, which answered real preflights); KurrentDB env names and gRPC access through `kurrentdbclient==1.3.3` on `kurrentdb:25.1.0`; CORS end to end from Chromium; the Message DB store against a live database; the KurrentDB store against a live node; that `kurrentdbclient`'s `read_all(commit_position=N)` is inclusive and accepts only an exact record position; REST proxy v3 produce shape (HTTP 200 with `error_code`, and it ignores the cluster id in the URL); the projector's checkpoint plus processed-id set across a hard kill and two kinds of replay.

**Still could not confirm** (read these as assumptions): the Solace default VPN accepting anonymous REST publishes on port 9000, and its REST auth. The broker could not start: its start-up self-test requires a hard open-files limit of 1,048,576 and this sandbox allows 20,000 (container rejected any higher `nofile`, even `--privileged`). The compose value was corrected but not proven; `SolaceRestBroker` and the gateway's `POST /TOPIC/...` pass-through remain untested against a live broker. Also unconfirmed: `EnvVarConfigProvider` working through the Debezium image's `CONNECT_*` mapping (Connect starts healthy and answers `/connectors`, but no connector was registered, to leave the CDC benchmark a clean slate); OLake's output directory layout, `_op_type` values and `streams.json` patching; every Airbyte API path and record shape; an HTTPS page calling `http://localhost` (mixed content and private-network rules); a gateway crash losing queued Kafka publishes (documented, not exercised).


## Sources

Debezium PostgreSQL connector reference (debezium.io/documentation/reference/stable/connectors/postgresql.html); Confluent REST Proxy v3 produce and config reference (docs.confluent.io/platform/current/kafka-rest); Solace REST messaging protocol (docs.solace.com/API/RESTMessagingPrtl); KurrentDB installation (docs.kurrent.io/server/v25.1/quick-start/installation.html) and `kurrentdbclient` on PyPI; Message DB (github.com/message-db/message-db, `database/install.sh`, `write-message`, `get-stream-messages`); OLake (olake.io docs and github.com/datazip-inc/olake-docs, github.com/datazip-inc/olake); Airbyte (docs.airbyte.com). All read 2026-10-02. KrakenD: krakend.io/docs (configuration, CORS, rate limiting, no-op encoding), read 2026-10-04 alongside the live runs.


## Tests

```bash
(cd gateway && python3 -m unittest)         # heavy: envelope, stores (memory + KurrentDB fake), HTTP server, Kafka publisher
(cd projector && python3 -m unittest)       # idempotency, replay, ordering guard, checkpoint
(cd cdc/harness && python3 -m unittest)     # workload, parsers, metrics, delivery checks, compare table
```

They need only the standard library. Run them as is; nothing here starts a container. The live checks are separate. Light stack and edge, against a running stack: `python3 tests/live/10_light.py` and `python3 tests/live/12_edge.py`. Heavy profile: `COMPOSE_PROFILES=heavy python3 tests/live/run_all.py` (starts containers; add `--down` to stop the stack at the end). Site-side: `node --test "tests/es/*.test.mjs"` from the repo root.
