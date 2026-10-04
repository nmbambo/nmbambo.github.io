"""Solr sink: Kafka -> idempotent search read model in Apache Solr (standalone core "ingqiqo").

A second consumer group next to the Postgres projector (same image, same envelope parser, own offsets), so Solr being
slow or down never stalls the Postgres read models, and either can be replayed on its own.

Two kinds of input, one rule for both:
  * the site's event envelopes on KAFKA_TOPIC (ingqiqo.events): ContentAdded / ContentChanged upsert a document,
    ContentRemoved deletes it. Order key: the envelope's per-stream `version` (one stream per document).
  * Debezium change events on lakebase.public.decisions / .parties: op c|u|r upsert, op d delete, and the null-valued
    Kafka tombstone that follows a delete is honoured too. Order key: the change's `source.lsn`.

Idempotency lives in Solr, not in a side table:
  * the document id is the uniqueKey, so a replay overwrites instead of duplicating;
  * every document stores the position of the last event applied (`ev_pos`) and its id (`ev_id`). An event is applied only
    if its position is not older than the stored one (older -> "stale", dropped), and an event whose id is already stored
    is a "duplicate" (no write);
  * every write is a compare-and-set on Solr's own `_version_` (-1 = must not exist): if anything changed the document
    between our read and our write, Solr answers 409 and we re-read and decide again;
  * a delete does not remove the document, it overwrites it with a tombstone (deleted=true, position kept). Without that,
    a late ContentChanged that was overtaken by a ContentRemoved would resurrect the document. Searches exclude tombstones
    (the /select handler appends fq=-deleted:true).
Offsets are committed after the write, so delivery is at-least-once and every redelivery is a no-op.

  KAFKA_BOOTSTRAP=kafka:9092  KAFKA_TOPIC=ingqiqo.events  KAFKA_GROUP=ingqiqo-solr-sink
  SOLR_URL=http://solr:8983/solr/ingqiqo  SOLR_COMMIT_WITHIN_MS=250
"""
import json
import logging
import os
import pathlib
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from logic import parse_message

log = logging.getLogger("solr-sink")
HEARTBEAT = "/tmp/solr-sink.heartbeat"
DEBEZIUM_PREFIX = "lakebase.public."


class SolrError(Exception):
    pass


class SolrConflict(SolrError):
    """HTTP 409: the _version_ we sent no longer matches (optimistic concurrency)."""


@dataclass(frozen=True)
class Upsert:
    doc_id: str
    fields: dict
    pos: Optional[int]
    event_id: str


@dataclass(frozen=True)
class Delete:
    doc_id: str
    pos: Optional[int]
    event_id: str
    unversioned: bool = False   # a Kafka tombstone carries no position of its own


# ---------------------------------------------------------------------------------------------------- mapping (pure)
def _str_list(v):
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []


def content_fields(d):
    out = {"id": d["id"]}
    for k in ("title", "section", "text", "type", "source", "url"):
        if isinstance(d.get(k), str):
            out[k] = d[k]
    tags = _str_list(d.get("tags"))
    if tags:
        out["tags"] = tags
    return out


def _flatten(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return v
    if isinstance(v, dict):
        return " ".join(_flatten(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(_flatten(x) for x in v)
    return "" if v is None else str(v)


def row_fields(table, row):
    """A lakebase row (Debezium after/before image) -> Solr fields. Synthetic tables only."""
    if table == "decisions":
        pk = row.get("decision_id")
        text = " ".join(x for x in (row.get("title"), row.get("status"), f"stakes {row.get('stakes')}",
                                    _flatten(row.get("payload")), f"party {row.get('party_id')}") if x)
        return f"decision:{pk}", {"id": f"decision:{pk}", "title": row.get("title") or "", "section": row.get("status") or "",
                                 "text": text, "tags": [t for t in (row.get("status"), f"stakes-{row.get('stakes')}") if t],
                                 "type": "decision", "source": "lakebase.public.decisions", "url": f"/decisions/{pk}"}
    if table == "parties":
        pk = row.get("party_id")
        return f"party:{pk}", {"id": f"party:{pk}", "title": row.get("label") or "", "section": row.get("kind") or "",
                               "text": f"{row.get('label')} {row.get('kind')} {row.get('region')}",
                               "tags": [t for t in (row.get("kind"), row.get("region")) if t],
                               "type": "party", "source": "lakebase.public.parties", "url": f"/parties/{pk}"}
    return None, None


def _key_id(table, key):
    pk = {"decisions": "decision_id", "parties": "party_id"}.get(table)
    try:
        k = json.loads(key)
    except (TypeError, ValueError):
        return None
    if isinstance(k, dict) and "payload" in k and pk not in k:
        k = k["payload"]
    if pk and isinstance(k, dict) and pk in k:
        return f"{'decision' if table == 'decisions' else 'party'}:{k[pk]}"
    return None


def to_ops(topic, key, raw):
    """One Kafka message -> list of Upsert/Delete. Pure. Unknown or unusable messages give []."""
    key = key.decode() if isinstance(key, (bytes, bytearray)) else key
    if topic.startswith(DEBEZIUM_PREFIX):
        table = topic[len(DEBEZIUM_PREFIX):]
        if raw is None:                                   # Debezium's tombstone after a delete
            doc_id = _key_id(table, key)
            return [Delete(doc_id, None, f"tombstone:{topic}:{key}", unversioned=True)] if doc_id else []
        try:
            v = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(v, dict) and "payload" in v and "op" not in v:   # converter with schemas.enable=true
            v = v["payload"]
        if not isinstance(v, dict) or v.get("op") not in ("c", "u", "r", "d"):
            return []
        src = v.get("source") or {}
        lsn = src.get("lsn")
        pos = lsn if isinstance(lsn, int) else None
        op = v["op"]
        eid = f"{table}:{op}:{lsn}:{v.get('ts_ms')}:{json.dumps(v.get('after') or v.get('before'), sort_keys=True)[:200]}"
        row = v.get("before") if op == "d" else v.get("after")
        if not isinstance(row, dict):
            return []
        doc_id, fields = row_fields(table, row)
        if doc_id is None or doc_id.endswith(":None"):
            return []
        return [Delete(doc_id, pos, eid)] if op == "d" else [Upsert(doc_id, fields, pos, eid)]
    ev = parse_message(raw)
    if ev is None:
        return []
    t, d = ev["type"], ev["data"]
    ver = ev.get("version")
    pos = ver if isinstance(ver, int) else None
    if t in ("ContentAdded", "ContentChanged") and isinstance(d.get("id"), str):
        return [Upsert(d["id"], content_fields(d), pos, ev["id"])]
    if t == "ContentRemoved" and isinstance(d.get("id"), str):
        return [Delete(d["id"], pos, ev["id"])]
    return []


# --------------------------------------------------------------------------------------------------------- Solr I/O
class SolrClient:
    """The two Solr calls the sink needs, over urllib. `opener` is injectable for tests."""

    def __init__(self, base_url, timeout=10.0, commit_within_ms=250, opener=None):
        self.base = base_url.rstrip("/")
        self.timeout, self.commit_within = timeout, int(commit_within_ms)
        self._open = opener or urllib.request.urlopen

    def _call(self, req):
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise SolrConflict("version conflict") from exc
            raise SolrError(f"solr HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SolrError(f"solr unreachable: {exc}") from exc

    def get(self, doc_id):
        """Real-time get (sees uncommitted writes): {id, _version_, ev_pos, ev_id, deleted} or None."""
        q = urllib.parse.urlencode({"id": doc_id, "fl": "id,_version_,ev_pos,ev_id,deleted"})
        return self._call(urllib.request.Request(f"{self.base}/get?{q}")).get("doc")

    def add(self, doc):
        body = json.dumps([doc]).encode()
        req = urllib.request.Request(f"{self.base}/update?commitWithin={self.commit_within}&overwrite=true", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        self._call(req)


# ----------------------------------------------------------------------------------------------------- the rule (pure)
class SolrSink:
    def __init__(self, client, max_attempts=8):
        self.client, self.max_attempts = client, max_attempts
        self.counts = {"applied": 0, "stale": 0, "duplicate": 0, "noop": 0, "conflict_retries": 0}

    def handle(self, topic, key, raw):
        return [self.apply(op) for op in to_ops(topic, key, raw)] or ["skipped"]

    def apply(self, op):
        """Returns 'applied' | 'stale' | 'duplicate' | 'noop'. Raises SolrError if Solr cannot be reached."""
        for _ in range(self.max_attempts):
            cur = self.client.get(op.doc_id)
            if cur is not None:
                if cur.get("ev_id") == op.event_id:
                    return self._count("duplicate")
                cur_pos = cur.get("ev_pos")
                if op.pos is not None and cur_pos is not None and op.pos < cur_pos:
                    return self._count("stale")
                if isinstance(op, Delete) and op.unversioned and cur.get("deleted"):
                    return self._count("duplicate")
            elif isinstance(op, Delete) and op.unversioned:
                return self._count("noop")           # nothing indexed, no position to protect
            pos = op.pos if op.pos is not None else (cur or {}).get("ev_pos")
            if isinstance(op, Upsert):
                doc = dict(op.fields, deleted=False)
            else:
                doc = {"id": op.doc_id, "deleted": True}
            doc["ev_id"] = op.event_id
            if pos is not None:
                doc["ev_pos"] = pos
            doc["_version_"] = cur["_version_"] if cur else -1   # compare-and-set; -1 = must not exist yet
            try:
                self.client.add(doc)
            except SolrConflict:
                self.counts["conflict_retries"] += 1
                continue
            return self._count("applied")
        raise SolrError(f"gave up on {op.doc_id} after {self.max_attempts} version conflicts")

    def _count(self, what):
        self.counts[what] += 1
        return what


# ----------------------------------------------------------------------------------------------------------- service
def main(env=os.environ):
    from confluent_kafka import Consumer, KafkaError   # imported here so the pure parts test without the wheel

    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sink = SolrSink(SolrClient(env.get("SOLR_URL", "http://solr:8983/solr/ingqiqo"),
                               commit_within_ms=int(env.get("SOLR_COMMIT_WITHIN_MS", "250"))))
    pattern = env.get("SOLR_TOPICS") or r"^(%s|lakebase\.public\.(decisions|parties))$" % re.escape(
        env.get("KAFKA_TOPIC", "ingqiqo.events"))
    consumer = Consumer({
        "bootstrap.servers": env.get("KAFKA_BOOTSTRAP", "kafka:9092"),
        "group.id": env.get("KAFKA_GROUP", "ingqiqo-solr-sink"),
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "topic.metadata.refresh.interval.ms": 5000,      # a regex subscription notices new topics (Debezium's) this fast
    })
    consumer.subscribe([pattern])
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    n = 0
    try:
        while not stop["now"]:
            pathlib.Path(HEARTBEAT).touch()
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() not in (KafkaError._PARTITION_EOF, KafkaError.UNKNOWN_TOPIC_OR_PART):
                    log.error("kafka error: %s", msg.error())
                continue
            while not stop["now"]:
                try:
                    sink.handle(msg.topic(), msg.key(), msg.value())
                    break
                except SolrError as exc:                 # Solr down or busy: wait, retry the same message, do not commit
                    log.warning("%s; retrying in 2 s", exc)
                    pathlib.Path(HEARTBEAT).touch()
                    time.sleep(2)
            else:
                break
            consumer.commit(message=msg, asynchronous=False)   # after the Solr write => at-least-once
            n += 1
            if n % 500 == 0:
                log.info("processed %s %s", n, sink.counts)
    finally:
        consumer.close()


# ------------------------------------------------------------------------------------------- JetStream (light stack)
def nats_to_kafka_shape(subject, data, seq):
    """A JetStream message -> the (topic, key, raw) the mapping above already understands, so both transports share one
    mapping. es.<stream> carries a site envelope: its global position (stream sequence - 1) is set as `version`, the
    order key. lakebase.<schema>.<table> carries a Debezium Server change event, unchanged (order key: source.lsn)."""
    if subject.startswith("es."):
        try:
            ev = json.loads(data)
        except (TypeError, ValueError):
            return None
        if not isinstance(ev, dict):
            return None
        ev["version"] = seq - 1
        return "ingqiqo.events", None, json.dumps(ev).encode()
    if subject.startswith("lakebase."):
        return subject, None, data
    return None


def main_nats(env=os.environ):
    """Durable pull consumers `solr-sink` on streams ES and CDC; ack after the Solr write (at-least-once; the sink is
    idempotent, so a redelivery is a no-op). Started with `python solr_sink.py --nats`."""
    import asyncio
    import nats
    from nats.errors import TimeoutError as NatsTimeout

    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sink = SolrSink(SolrClient(env.get("SOLR_URL", "http://solr:8983/solr/ingqiqo"),
                               commit_within_ms=int(env.get("SOLR_COMMIT_WITHIN_MS", "250"))))

    async def run():
        nc = await nats.connect(env.get("NATS_URL", "nats://nats:4222"), max_reconnect_attempts=-1, name="ingqiqo-solr-sink")
        js = nc.jetstream()
        subs = [await js.pull_subscribe(subj, durable=f"solr-sink-{stream.lower()}", stream=stream)
                for stream, subj in (("ES", "es.>"), ("CDC", "lakebase.>"))]
        while True:
            pathlib.Path(HEARTBEAT).touch()
            for sub in subs:
                try:
                    msgs = await sub.fetch(256, timeout=0.5)
                except NatsTimeout:
                    continue
                for m in msgs:
                    shaped = nats_to_kafka_shape(m.subject, m.data, m.metadata.sequence.stream)
                    while shaped:
                        try:
                            sink.handle(*shaped)
                            break
                        except SolrError as exc:
                            log.warning("%s; retrying in 2 s", exc)
                            pathlib.Path(HEARTBEAT).touch()
                            await asyncio.sleep(2)
                    await m.ack()

    asyncio.run(run())


if __name__ == "__main__":
    import sys
    main_nats() if "--nats" in sys.argv else main()
