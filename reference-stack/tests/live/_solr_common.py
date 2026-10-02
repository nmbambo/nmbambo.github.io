"""Helpers for the Solr live checks (08_solr_*.py). Standard library only; builds on _common.

Each Solr check starts with fresh_stack(): a brand-new Kafka topic (ingqiqo.solr.<run id>) for the gateway and the
solr-sink, and an empty index. That keeps the synthetic events that checks 01-07 left in `ingqiqo.events`, and the events
of earlier Solr runs, out of the index (the parity check needs exactly the site corpus) and makes a "replay" mean
exactly this run's events. The current topic is remembered in out/solr-topic.txt so later scripts (agent, cdc) use the
same one; every compose call passes KAFKA_TOPIC so the gateway and sink never drift apart. Solr itself is reached
directly only from here (bound to 127.0.0.1); the site-facing path is always the gateway's GET /search.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from _common import ENV, GATEWAY, REPO, STACK, Checks, compose, event, http, wait_until  # noqa: F401

from _common import OUT  # noqa: E402

TOPIC_FILE = OUT / "solr-topic.txt"
SINK_GROUP = "ingqiqo-solr-sink"
SOLR_PORT = ENV.get("SOLR_PORT", "8983")
SOLR = f"http://localhost:{SOLR_PORT}/solr/ingqiqo"


def current_topic():
    try:
        return TOPIC_FILE.read_text().strip() or "ingqiqo.solr.events"
    except OSError:
        return "ingqiqo.solr.events"


def sc(*args, env=None, **kw):
    """docker compose with the current Solr-check topic pinned."""
    return compose(*args, env={"KAFKA_TOPIC": current_topic(), **(env or {})}, **kw)


def fresh_stack(rid, store="kurrentdb"):
    """Hermetic start: new topic, gateway on `store`, sink recreated on the new topic, empty index."""
    TOPIC_FILE.write_text(f"ingqiqo.solr.{rid}")
    sc("up", "-d", "--force-recreate", "--no-deps", "gateway", "solr-sink", env={"STORE": store})
    wait_until(lambda: _gateway_on(store), f"gateway healthy on {store}", timeout=120, every=1.5)
    wait_until(lambda: sc("ps", "--format", "{{.Health}}", "solr-sink").stdout.strip() == "healthy", "solr-sink healthy",
               timeout=90, every=2)
    solr_clear()


def _gateway_on(store):
    try:
        s, body, _ = http("GET", f"{GATEWAY}/health", timeout=3)
        return s == 200 and body.get("store") == store and body.get("kafkaClusterId")
    except Exception:  # noqa: BLE001
        return False


def solr(path, params=None, body=None, method=None, timeout=20):
    url = SOLR + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def solr_clear():
    """Delete every document (tests only; the sink never does this)."""
    s, body = solr("/update", {"commit": "true"}, {"delete": {"query": "*:*"}})
    assert s == 200, body


def solr_dump(fq="*:*", fl="*", rows=1000):
    """All stored fields plus _version_ for every document, tombstones included (/audit), sorted by id."""
    s, body = solr("/audit", {"q": "*:*", "fq": fq, "fl": fl + ",_version_", "rows": rows, "sort": "id asc"})
    assert s == 200, body
    return body["response"]["docs"]


def solr_count(fq="*:*"):
    s, body = solr("/audit", {"q": "*:*", "fq": fq, "rows": 0})
    return body["response"]["numFound"]


def visible_count(prefix=None):
    fq = "-deleted:true" + (f" AND id:{prefix}*" if prefix else "")
    return solr_count(fq)   # via /audit, which (unlike /select) does not hide tombstones itself


def sha_remap(orig_id, rid):
    return hashlib.sha256(f"{rid}:{orig_id}".encode()).hexdigest()


def remap_events(events, rid):
    """Re-key a recorded event log so it can be replayed into stores that already hold it: new sha256 event ids and
    stream names (prefix r<rid>-); document ids (data.id), types, data, meta and the order are untouched."""
    out = []
    for e in events:
        c = dict(e)
        c["id"] = sha_remap(e["id"], rid)
        c["stream"] = e["stream"].replace("content-", f"content-r{rid}-", 1)
        out.append(c)
    return out


def post_events(events, workers=1):
    """POST every event to the gateway, one request each, in order. Returns (n_ok, failures)."""
    ok, bad = 0, []
    for e in events:
        s, body, _ = http("POST", f"{GATEWAY}/streams/{urllib.parse.quote(e['stream'], safe='')}", {"events": [e]})
        if s == 200 and body.get("appended") == 1:
            ok += 1
        else:
            bad.append((e["id"][:10], s, body))
    return ok, bad


def switch_store(store):
    sc("up", "-d", "--force-recreate", "--no-deps", "gateway", env={"STORE": store})
    wait_until(lambda: _gateway_on(store), f"gateway healthy on {store}", timeout=120, every=1.5)


def group_lag(topic_filter=None, group=SINK_GROUP):
    """{topic: total lag} for the sink's consumer group, from the stock kafka-consumer-groups tool. None if no data."""
    r = sc("exec", "-T", "kafka", "/opt/kafka/bin/kafka-consumer-groups.sh", "--bootstrap-server", "localhost:9092",
           "--describe", "--group", group, check=False)
    lag, seen = {}, False
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) >= 6 and p[0] == group and p[2].isdigit():
            if topic_filter and not p[1].startswith(topic_filter):
                continue
            seen = True
            lag[p[1]] = lag.get(p[1], 0) + (int(p[5]) if p[5].isdigit() else 10 ** 9)
    return lag if seen else None


def wait_sink_caught_up(topic=None, timeout=120):
    topic = topic or current_topic()

    def caught():
        lag = group_lag(topic)
        return lag is not None and all(v == 0 for v in lag.values())
    wait_until(caught, f"solr-sink lag 0 on {topic}", timeout=timeout, every=1.0)
    time.sleep(1.5)     # commitWithin (250 ms) + the soft commit that makes the last write searchable


def sink_logs():
    return sc("logs", "--no-color", "solr-sink", check=False).stdout


def produce(envelope, topic=None):
    topic = topic or current_topic()
    """Put one envelope on the topic through the Kafka REST proxy (v3), keyed by stream: used to deliver events out of
    order or with hand-picked versions, which the gateway (it assigns versions itself) would never produce."""
    cid = http("GET", f"http://localhost:{ENV.get('KAFKA_REST_PORT', '8082')}/v3/clusters")[1]["data"][0]["cluster_id"]
    s, body, _ = http("POST", f"http://localhost:{ENV.get('KAFKA_REST_PORT', '8082')}/v3/clusters/{cid}/topics/{topic}/records",
                      {"key": {"type": "STRING", "data": envelope["stream"]}, "value": {"type": "JSON", "data": envelope}})
    assert s == 200 and body.get("error_code") == 200, (s, body)


def search(q, algorithm="bm25", rows=10, start=0):
    return http("GET", f"{GATEWAY}/search?" + urllib.parse.urlencode({"q": q, "algorithm": algorithm, "rows": rows, "start": start}))


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)
