"""Check 3: every event the gateway appended (check 02, both stores) is on Kafka topic ingqiqo.events.

Consumes the topic from the beginning with the stock console consumer. Per event: present exactly once (the
idempotent re-posts in 02 must not have been published again), keyed by stream, value equal to the stored envelope
including position and version. Per stream: versions arrive in order. Needs 02_gateway_stores.py to have run.
"""
import json
import sys

from _common import OUT, TOPIC, Checks, http, GATEWAY, kafka_consume_all


def main():
    c = Checks("03_kafka_publish")
    rows = kafka_consume_all(TOPIC)
    c.check("consumed the topic", len(rows) > 0, f"{len(rows)} records")
    by_id = {}
    for part, key, val in rows:
        if isinstance(val, dict):
            by_id.setdefault(val.get("id"), []).append((part, key, val))
    for store in ("kurrentdb", "messagedb"):
        f = OUT / f"appended-{store}.json"
        if not f.exists():
            c.check(f"{store}: appended list present (run 02 first)", False, f)
            continue
        want = json.loads(f.read_text())["events"]
        missing = [e["id"] for e in want if e["id"] not in by_id]
        c.check(f"{store}: all {len(want)} appended events are on the topic", not missing, f"missing {len(missing)}")
        dup = [e["id"] for e in want if len(by_id.get(e["id"], [])) > 1]
        c.check(f"{store}: each published exactly once (re-posts were not republished)", not dup, f"duplicated {len(dup)}")
        bad_key = [e["id"] for e in want for p, k, v in by_id.get(e["id"], []) if k != e["stream"]]
        c.check(f"{store}: record key is the stream name", not bad_key, f"{len(bad_key)} wrong")
        bad_pv = [e["id"] for e in want for p, k, v in by_id.get(e["id"], [])
                  if (v.get("position"), v.get("version")) != (e["position"], e["version"])]
        c.check(f"{store}: published position/version match what the store assigned", not bad_pv, f"{len(bad_pv)} differ")
        # per-stream order as consumed (a stream maps to one partition)
        streams = {}
        for e in want:
            streams.setdefault(e["stream"], []).append(e["id"])
        order_ok, same_part = True, True
        for st, ids in streams.items():
            recs = [by_id[i][0] for i in ids if i in by_id]
            parts = {p for p, _, _ in recs}
            same_part &= len(parts) == 1
        # consumed order within a partition is offset order: re-walk rows
        seen = {}
        for part, key, val in rows:
            if isinstance(val, dict) and val.get("id") in {e["id"] for e in want}:
                seen.setdefault(val["stream"], []).append(val["version"])
        order_ok = all(v == sorted(v) for v in seen.values())
        c.check(f"{store}: each stream sits on one partition", same_part)
        c.check(f"{store}: versions arrive in order within each stream", order_ok, {k[-6:]: v for k, v in seen.items()})
        # the gateway's own view agrees with Kafka for one event
        e0 = want[0]
        s, got, _ = http("GET", f"{GATEWAY}/events/{e0['id']}") if store == http("GET", f"{GATEWAY}/health")[1]["store"] else (None, None, None)
        if s == 200:
            c.check(f"{store}: Kafka value equals the gateway's stored envelope", by_id[e0["id"]][0][2] == got, "")
    return c.finish({"records_on_topic": len(rows)})


if __name__ == "__main__":
    sys.exit(main())
