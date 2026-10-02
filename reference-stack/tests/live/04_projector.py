"""Check 4: the projector builds the read model; survives a hard kill mid-stream; replay changes nothing.

1. Post a workload through the gateway (content docs with several versions and removals, plus search events
   whose counters would be corrupted by a double apply), using run-unique doc ids and algorithm names.
2. SIGKILL the projector container part-way through the posting, finish posting, restart it.
3. Wait for the checkpoint to reach the end of every partition; compare the run's rows with an expected model
   computed here, independently, from the events that were posted.
4. Replay A: stop it, delete only the checkpoint rows and reset the consumer group to earliest, start it: the whole
   topic is re-delivered; processed_events must make every redelivery a no-op (whole read model identical).
5. Replay B: stop it, truncate every projector table, reset the group, start it: rebuilt read model identical.
"""
import random
import sys
import threading
import time

from _common import (GATEWAY, TOPIC, Checks, compose, event, http, kafka_end_offsets, psql, run_id, wait_until)

DOCS, SESSIONS = 30, 3


def build_workload(rid):
    rnd = random.Random(int(rid, 16))
    posts, expected_docs, expected_stats = [], {}, {}
    algos = [f"live{rid}-bm25", f"live{rid}-fuzzy", f"live{rid}-boolean"]
    for d in range(DOCS):
        doc = f"live-{rid}#d{d}"
        stream = f"content-live{rid}d{d}"
        evs, state = [], None
        for v in range(rnd.randint(2, 4)):
            kind = "ContentAdded" if v == 0 else "ContentChanged"
            data = {"id": doc, "title": f"title {d}.{v}", "section": f"s{v}", "type": "page", "url": f"/p/{d}", "hash": f"h{d}-{v}"}
            evs.append(event(stream, kind, data))
            state = dict(data, removed=False)
        if d % 5 == 0:
            evs.append(event(stream, "ContentRemoved", {"id": doc}))
            state = dict(state, removed=True)
        expected_docs[doc] = dict(state, stream_version=len(evs) - 1)
        for i in range(0, len(evs), 2):          # two events per request at most
            posts.append((stream, evs[i:i + 2]))
    for s in range(SESSIONS):
        stream = f"search-live{rid}s{s}"
        evs = []
        for i in range(25):
            a = algos[rnd.randrange(3)]
            r = rnd.random()
            if r < 0.5:
                evs.append(event(stream, "SearchSubmitted", {"query": "q", "algorithm": a})); expected_stats.setdefault(a, {}); expected_stats[a]["submitted"] = expected_stats[a].get("submitted", 0) + 1
            elif r < 0.85:
                evs.append(event(stream, "ResultOpened", {"query": "q", "docId": "x", "rank": 1, "algorithm": a})); expected_stats.setdefault(a, {}); expected_stats[a]["opened"] = expected_stats[a].get("opened", 0) + 1
            else:
                b = algos[(algos.index(a) + 1) % 3]
                evs.append(event(stream, "AlgorithmOverridden", {"query": "q", "proposed": a, "chosen": b}))
                for alg, col in ((a, "overridden_from"), (b, "overridden_to")):
                    expected_stats.setdefault(alg, {}); expected_stats[alg][col] = expected_stats[alg].get(col, 0) + 1
        for i in range(0, len(evs), 5):
            posts.append((stream, evs[i:i + 5]))
    per_stream = {}
    for st, evs in posts:                      # chunks stay in creation order within a stream
        per_stream.setdefault(st, []).append(evs)
    return per_stream, posts, expected_docs, expected_stats, algos


def snapshot(rid=None):
    """Read-model rows (as text lines). With rid: only this run's rows."""
    like_doc = f" WHERE doc_id LIKE 'live-{rid}#%'" if rid else ""
    like_alg = f" WHERE algorithm LIKE 'live{rid}-%'" if rid else ""
    docs = psql("SELECT doc_id, coalesce(title,''), coalesce(section,''), coalesce(doc_type,''), coalesce(url,''), "
                "coalesce(hash,''), removed, coalesce(stream_version::text,''), last_event_id FROM rm_content_docs"
                f"{like_doc} ORDER BY doc_id")
    stats = psql(f"SELECT algorithm, submitted, opened, overridden_from, overridden_to FROM rm_algorithm_stats{like_alg} ORDER BY algorithm")
    return docs, stats


def lag_zero():
    ends = kafka_end_offsets()
    rows = psql("SELECT partition, next_offset FROM projector_checkpoint ORDER BY partition")
    cp = {int(r.split("|")[0]): int(r.split("|")[1]) for r in rows}
    return all(cp.get(p, 0) >= o for p, o in ends.items()) and ends


def wait_caught_up(timeout=120):
    return wait_until(lag_zero, "projector checkpoint to reach the end of every partition", timeout=timeout, every=2)


def wait_processed(ids, timeout=180):
    """Direct and independent of checkpoints: every posted event id is in processed_events."""
    quoted = ",".join(f"'{i}'" for i in ids)

    def done():
        return int(psql(f"SELECT count(*) FROM processed_events WHERE event_id IN ({quoted})")[0]) == len(ids)
    return wait_until(done, "every posted event id to appear in processed_events", timeout=timeout, every=2)


def counts():
    return (int(psql("SELECT count(*) FROM processed_events")[0]), int(psql("SELECT count(*) FROM rm_content_docs")[0]))


def reset_group():
    compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-consumer-groups.sh", "--bootstrap-server", "localhost:9092",
            "--group", "ingqiqo-projector", "--topic", TOPIC, "--reset-offsets", "--to-earliest", "--execute")


def main():
    c = Checks("04_projector")
    rid = run_id()
    per_stream, posts, exp_docs, exp_stats, algos = build_workload(rid)
    c.check("projector container healthy before the run", "healthy" in compose("ps", "projector").stdout)
    wait_caught_up()
    before_events, _ = counts()

    # --- 1+2: post, kill the projector part-way, finish, restart ---------------------------------------
    all_ids = [e["id"] for chunks in per_stream.values() for evs in chunks for e in evs]
    queue = {s: list(chunks) for s, chunks in per_stream.items()}   # per-stream FIFO (versions must be in order)
    total_requests = sum(len(v) for v in queue.values())
    sent, posted_events, queued = 0, 0, 0
    killed_at = None
    streams = list(queue)
    expected_version = {s: -1 for s in streams}
    i = 0
    t0 = time.time()
    while any(queue.values()):
        st = streams[i % len(streams)]
        i += 1
        if not queue[st]:
            continue
        evs = queue[st].pop(0)
        s, body, _ = http("POST", f"{GATEWAY}/streams/{st}", {"events": evs, "expectedVersion": expected_version[st]})
        if s != 200 or body["appended"] != len(evs):
            c.check("workload append", False, (s, body))
            return c.finish()
        expected_version[st] += len(evs)
        queued += body.get("kafkaQueued", 0)
        sent += 1
        posted_events += len(evs)
        if killed_at is None and sent >= total_requests // 3:
            compose("kill", "projector")
            killed_at = sent
            print(f"  ... projector SIGKILLed after {sent}/{total_requests} requests")
        if killed_at is not None and sent == (2 * total_requests) // 3:
            compose("start", "projector")
            print(f"  ... projector restarted after {sent}/{total_requests} requests (posting continues)")
    c.check("every event was handed to Kafka at once (kafkaQueued == 0 on every response)", queued == 0, queued)
    c.check("posted the workload through the gateway", True, f"{posted_events} events in {sent} requests, {time.time() - t0:.1f}s")
    # make sure it was restarted even if the arithmetic above skipped it
    compose("start", "projector")
    wait_until(lambda: "healthy" in compose("ps", "projector").stdout, "projector healthy", timeout=90, every=2)
    wait_processed(all_ids)
    time.sleep(3)
    ends = wait_caught_up()
    c.check("projector caught up to the end of every partition after the kill/restart", bool(ends), ends)

    docs, stats = snapshot(rid)
    got_docs = {}
    for line in docs:
        doc_id, title, section, typ, url, h, removed, ver, last = line.split("|")
        got_docs[doc_id] = {"id": doc_id, "title": title, "section": section, "type": typ, "url": url, "hash": h,
                            "removed": removed == "t", "stream_version": int(ver)}
    want_docs = {d: dict(v, **{}) for d, v in exp_docs.items()}
    mismatch = [d for d in want_docs if got_docs.get(d) != want_docs[d]]
    c.check("read model docs equal the model computed from the posted events", not mismatch and len(got_docs) == DOCS,
            f"{len(got_docs)}/{DOCS} docs, mismatches {mismatch[:3]}")
    got_stats = {}
    for line in stats:
        a, sub, op, of, ot = line.split("|")
        got_stats[a] = {"submitted": int(sub), "opened": int(op), "overridden_from": int(of), "overridden_to": int(ot)}
    want_stats = {a: {k: exp_stats.get(a, {}).get(k, 0) for k in ("submitted", "opened", "overridden_from", "overridden_to")} for a in exp_stats}
    c.check("search counters equal the expected counts (no event applied twice or lost)", got_stats == want_stats,
            f"got {got_stats} want {want_stats}" if got_stats != want_stats else f"{len(got_stats)} algorithms")
    n_proc = int(psql(f"SELECT count(*) FROM processed_events WHERE event_id IN (SELECT last_event_id FROM rm_content_docs WHERE doc_id LIKE 'live-{rid}#%')")[0])
    c.check("processed-id set holds the latest event of every doc", n_proc == DOCS, n_proc)
    after_events, _ = counts()
    c.check("processed_events grew by exactly the number of events posted (no duplicates, none missed)",
            after_events - before_events == posted_events, f"+{after_events - before_events} vs {posted_events}")

    # --- 4: replay A: checkpoint removed, group reset -> full redelivery, processed set absorbs it -------
    full_before = snapshot()
    ev_before = counts()
    compose("stop", "projector")
    psql("DELETE FROM projector_checkpoint")
    reset_group()
    compose("start", "projector")
    wait_until(lambda: "healthy" in compose("ps", "projector").stdout, "projector healthy", timeout=90, every=2)
    wait_caught_up(180)
    time.sleep(2)
    c.check("replay A (checkpoint deleted, topic re-read from earliest): whole read model identical",
            snapshot() == full_before, "docs+stats compared incl. last_event_id")
    c.check("replay A: processed_events and doc count unchanged", counts() == ev_before, f"before {ev_before}, after {counts()}")

    # --- 5: replay B: all projector state wiped -> rebuild from the topic ---------------------------------
    compose("stop", "projector")
    psql("TRUNCATE rm_content_docs, rm_algorithm_stats, processed_events, projector_checkpoint")
    reset_group()
    compose("start", "projector")
    wait_until(lambda: "healthy" in compose("ps", "projector").stdout, "projector healthy", timeout=90, every=2)
    wait_caught_up(240)
    time.sleep(2)
    rebuilt = snapshot()
    c.check("replay B (all projector tables truncated, rebuilt from Kafka): read model identical", rebuilt == full_before,
            f"docs {len(rebuilt[0])}/{len(full_before[0])}, stats {len(rebuilt[1])}/{len(full_before[1])}")
    c.check("replay B: processed_events and doc counts identical to before", counts() == ev_before, f"before {ev_before}, after {counts()}")
    c.check("records on the topic == rows in processed_events (nothing lost, nothing extra)",
            sum(kafka_end_offsets().values()) == counts()[0], f"{sum(kafka_end_offsets().values())} vs {counts()[0]}")
    return c.finish({"events_posted": posted_events, "requests": sent, "killed_after_requests": killed_at})


if __name__ == "__main__":
    sys.exit(main())
