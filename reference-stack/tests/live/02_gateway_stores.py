"""Check 2: the gateway against the real event stores, STORE=kurrentdb then STORE=messagedb.

Per store: append with expectedVersion -1, idempotent re-post (same ids append nothing), expectedVersion conflicts
(409), correct expectedVersion, sha256-style ids, bad envelope (400), GET /events?from= order and inclusivity,
GET /streams/{s}, GET /events/{id}, GET /events/last. Records what it appended in out/appended-<store>.json for
the Kafka check (03). Leaves the gateway on STORE=kurrentdb (the default) at the end.
"""
import json
import sys

from _common import GATEWAY, OUT, Checks, compose, event, http, run_id, sha_id, wait_until


def switch_store(store):
    compose("up", "-d", "--force-recreate", "--no-deps", "gateway", env={"STORE": store})

    def ready():
        try:
            s, body, _ = http("GET", f"{GATEWAY}/health", timeout=3)
            return s == 200 and body.get("store") == store and body.get("kafkaClusterId")
        except Exception:  # noqa: BLE001
            return False
    wait_until(ready, f"gateway healthy on {store}", timeout=120, every=1.5)


def post(stream, events, expected=None):
    body = {"events": events}
    if expected is not None:
        body["expectedVersion"] = expected
    return http("POST", f"{GATEWAY}/streams/{stream}", body)


def run_store(store, rid):
    c = Checks(f"02_gateway_{store}")
    print(f"== STORE={store}")
    switch_store(store)
    s, h, _ = http("GET", f"{GATEWAY}/health")
    c.check("health reports the store", s == 200 and h["store"] == store, h)
    last_before = http("GET", f"{GATEWAY}/events/last")[1]["position"]

    sa, sb = f"content-live{rid}{store[:2]}a", f"content-live{rid}{store[:2]}b"
    ea = [event(sa, "ContentAdded", {"id": f"live-{rid}-{store[:2]}-a#{i}", "title": f"doc a{i}"}) for i in range(3)]
    s, body, _ = post(sa, ea, -1)
    c.check("append 3 events to a new stream (expectedVersion -1)", s == 200 and body["appended"] == 3, body)
    pos_a = body["positions"]
    c.check("positions are strictly increasing", pos_a == sorted(set(pos_a)), pos_a)

    s, body, _ = post(sa, ea, -1)
    c.check("idempotent re-post of the same ids appends nothing (200, appended 0)", s == 200 and body["appended"] == 0, (s, body))
    s, body, _ = post(sa, ea)
    c.check("idempotent re-post without expectedVersion appends nothing", s == 200 and body["appended"] == 0, (s, body))

    fresh = event(sa, "ContentChanged", {"id": f"live-{rid}-{store[:2]}-a#0", "title": "changed"})
    s, body, _ = post(sa, [fresh], -1)
    c.check("expectedVersion -1 on an existing stream returns 409", s == 409 and body.get("actual") == 2, (s, body))
    s, body, _ = post(sa, [fresh], 1)
    c.check("stale expectedVersion (1, stream at 2) returns 409", s == 409, (s, body))
    s, body, _ = post(sa, [fresh], 5)
    c.check("expectedVersion ahead of the stream returns 409", s == 409, (s, body))
    s, rb, _ = http("GET", f"{GATEWAY}/events/{fresh['id']}")
    c.check("a 409 appended nothing (event not stored)", s == 404, s)
    s, body, _ = post(sa, [fresh], 2)
    c.check("correct expectedVersion (2) appends (200, appended 1)", s == 200 and body["appended"] == 1, (s, body))
    pos_a.append(body["positions"][0])
    ea.append(fresh)

    # second stream, sha256-style deterministic ids, interleaved with the first
    eb = [event(sb, "ContentAdded", {"id": f"live-{rid}-{store[:2]}-b#{i}", "title": f"doc b{i}"},
                eid=sha_id("ContentAdded", f"b{i}", rid, store)) for i in range(2)]
    s, body, _ = post(sb, eb, -1)
    c.check("sha256-hex ids are accepted", s == 200 and body["appended"] == 2, (s, body))
    pos_b = body["positions"]
    s, body, _ = post(sb, eb, -1)
    c.check("re-post of sha256 ids appends nothing", s == 200 and body["appended"] == 0, (s, body))
    s, got, _ = http("GET", f"{GATEWAY}/events/{eb[0]['id']}")
    c.check("GET /events/{sha256 id} returns the event with its original id", s == 200 and got["id"] == eb[0]["id"], s)

    for label, bad in [("missing meta", {"id": ea[0]["id"], "type": "X", "stream": sa, "data": {}}),
                       ("non-uuid id", dict(event(sa, "X", {}), id="not-an-id")),
                       ("data not an object", dict(event(sa, "X", {}), data=[1]))]:
        s, body, _ = post(sa, [bad], 3)
        c.check(f"bad envelope rejected with 400 ({label})", s == 400, (s, body))

    # reading
    allev = [(e, p) for e, p in zip(ea, pos_a)] + [(e, p) for e, p in zip(eb, pos_b)]
    allev.sort(key=lambda t: t[1])
    ours = [e["id"] for e, _ in allev]
    first_pos = allev[0][1]
    s, evs, _ = http("GET", f"{GATEWAY}/events?from={first_pos}&limit=5000")
    got_ids = [e["id"] for e in evs if e["id"] in set(ours)]
    c.check("GET /events?from=<pos> is inclusive and returns our events in append order", s == 200 and got_ids == ours,
            f"{len(got_ids)}/{len(ours)} found")
    c.check("returned positions are strictly increasing", [e["position"] for e in evs] == sorted({e["position"] for e in evs}))
    c.check("first returned event is at the requested position", bool(evs) and evs[0]["position"] == first_pos and evs[0]["id"] == ours[0])
    s, evs2, _ = http("GET", f"{GATEWAY}/events?from={first_pos + 1}&limit=1")
    c.check("from=N+1 skips event N; limit=1 returns one", s == 200 and len(evs2) == 1 and evs2[0]["position"] > first_pos, [e["position"] for e in evs2])
    s, evs3, _ = http("GET", f"{GATEWAY}/events?from=0&limit=3")
    c.check("from=0 starts at the beginning of the store", s == 200 and 1 <= len(evs3) <= 3, len(evs3))
    s, last, _ = http("GET", f"{GATEWAY}/events/last")
    c.check("GET /events/last >= last appended position", s == 200 and last["position"] >= allev[-1][1] > last_before, last)
    s, evs4, _ = http("GET", f"{GATEWAY}/events?from={last['position'] + 1}")
    c.check("from=last+1 returns nothing", s == 200 and evs4 == [], len(evs4))

    s, stream_a, _ = http("GET", f"{GATEWAY}/streams/{sa}")
    c.check("GET /streams/{s} returns versions 0..3 in order with the right data",
            s == 200 and [e["version"] for e in stream_a] == [0, 1, 2, 3] and [e["id"] for e in stream_a] == [e["id"] for e in ea]
            and stream_a[0]["data"] == ea[0]["data"] and stream_a[0]["meta"]["source"] == "live-test", [e["version"] for e in stream_a])
    s, none, _ = http("GET", f"{GATEWAY}/streams/content-live{rid}-nothing")
    c.check("GET on an unknown stream returns []", s == 200 and none == [], (s, none))
    s, got, _ = http("GET", f"{GATEWAY}/events/{ea[1]['id']}")
    c.check("GET /events/{uuid} returns the event", s == 200 and got["id"] == ea[1]["id"] and got["version"] == 1, s)
    s, _, _ = http("GET", f"{GATEWAY}/events/{'0' * 8}-0000-4000-8000-{'0' * 12}")
    c.check("GET /events/{unknown uuid} returns 404", s == 404, s)

    state = {"store": store, "run": rid,
             "events": [{"id": e["id"], "stream": e["stream"], "position": p,
                         "version": next(x["version"] for x in (stream_a + http('GET', f'{GATEWAY}/streams/{sb}')[1]) if x["id"] == e["id"])}
                        for e, p in allev]}
    (OUT / f"appended-{store}.json").write_text(json.dumps(state, indent=2))
    c.finish()
    return c


def main():
    rid = run_id()
    ok = True
    try:
        for store in ("kurrentdb", "messagedb"):
            ok &= run_store(store, rid).ok
    finally:
        switch_store("kurrentdb")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
