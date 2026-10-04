"""Live check of the LIGHT stack (nats, command, debezium-server, query, postgres-lakebase). Run after `docker compose up -d`.
  1 the site's event log goes in through the command service (POST /streams/{s}); re-posting it is de-duplicated
  2 expectedVersion conflicts are refused with 409
  3 the query service projects it into SQLite FTS5 and answers all six methods
  4 CDC: a row inserted/updated/deleted in the lakebase reaches search through Debezium Server -> JetStream
  5 restarting the query service with its database deleted rebuilds an identical read model
  6 footprint (memory of each container)
"""
import json, os, subprocess, sys, time, urllib.parse, urllib.request
ROOT = os.path.dirname(os.path.abspath(__file__))
CMD, QRY = "http://127.0.0.1:8090", "http://127.0.0.1:8091"
LOG = os.path.join(ROOT, "..", "..", "..", "data", "events.ndjson")
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or c  # noqa: E731
results = []

def http(method, url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

def search(q, alg):
    return http("GET", f"{QRY}/search?" + urllib.parse.urlencode({"q": q, "algorithm": alg, "rows": 5}))[1]

def wait(pred, t=60):
    end = time.time() + t
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def compose(*a):
    return subprocess.run(["docker", "compose", *a], cwd=os.path.join(ROOT, "..", ".."), capture_output=True, text=True).stdout

events = [json.loads(l) for l in open(LOG, encoding="utf-8")]
streams = {}
for e in events:
    streams.setdefault(e["stream"], []).append(e)
t0 = time.time()
codes = [http("POST", f"{CMD}/streams/{urllib.parse.quote(s, safe='')}", {"events": evs})[0] for s, evs in streams.items()]
results.append(ok(all(c in (200, 201) for c in codes), f"1 posted {len(events)} events in {len(streams)} streams ({time.time()-t0:.1f}s)"))
again = [http("POST", f"{CMD}/streams/{urllib.parse.quote(s, safe='')}", {"events": evs})[1] for s, evs in list(streams.items())[:20]]
results.append(ok(all((a or {}).get("new", 1) == 0 or (a or {}).get("skipped") for a in again), "1 re-posting is de-duplicated (no new events)"))
s0 = next(iter(streams))
code, _ = http("POST", f"{CMD}/streams/{urllib.parse.quote(s0, safe='')}", {"events": [{**streams[s0][0], "id": "11111111-2222-4333-8444-555555555555"}], "expectedVersion": -1})
results.append(ok(code == 409, f"2 a stale expectedVersion is refused (HTTP {code})"))
n_live = len({e['data']['id'] for e in events}) - 2
results.append(ok(wait(lambda: http("GET", f"{QRY}/stats")[1]["checkpoints"].get("es", 0) >= len(events)), "3 query service caught up with the log"))
st = http("GET", f"{QRY}/stats")[1]
results.append(ok(st["docs"] >= 200, f"3 read model holds {st['docs']} documents, {st['events']} events"))
for alg, q in [("boolean", 'title:spine OR "decision warranty"'), ("bm25", "meaning coherence"), ("fuzzy", "coherance"),
               ("prefix", "archit"), ("phrase", "decision warranty"), ("substring", "herenc")]:
    r = search(q, alg)
    results.append(ok(r["total"] > 0, f"3 {alg:9} {q!r}: {r['total']} hits in {r['qtimeMs']} ms, top {r['results'][0]['id'] if r['results'] else '-'}"))
psql = lambda sql: subprocess.run(["docker", "compose", "exec", "-T", "postgres-lakebase", "psql", "-U", "lakebase", "-d", "lakebase", "-tAc", sql],
                                   cwd=os.path.join(ROOT, "..", ".."), capture_output=True, text=True)
cols = psql("select string_agg(column_name||':'||data_type, ', ') from information_schema.columns where table_name='decisions'").stdout.strip()
print("   decisions columns:", cols)
pid = 900001
print("   party:", psql(f"insert into parties(party_id, label, kind, region) values ({pid},'Synthetic party','organisation','Gauteng') on conflict do nothing").stderr.strip()[:200])
ins = psql(f"insert into decisions(decision_id, title, status, stakes, party_id, payload) values (900001,'Okapi corridor decision','open',3,{pid},'{{}}') returning decision_id")
did = ins.stdout.strip().splitlines()[0] if ins.stdout.strip() else None
print("   insert:", did, ins.stderr.strip()[:200])
results.append(ok(wait(lambda: search("okapi", "bm25")["total"] == 1, 90), "4 CDC insert reaches search (okapi)"))
psql(f"update decisions set title='Pangolin corridor decision' where decision_id='{did}'")
results.append(ok(wait(lambda: search("pangolin", "bm25")["total"] == 1 and search("okapi", "bm25")["total"] == 0, 60), "4 CDC update replaces the document"))
psql(f"delete from decisions where decision_id='{did}'")
results.append(ok(wait(lambda: search("pangolin", "bm25")["total"] == 0, 60), "4 CDC delete removes it"))
before = http("GET", f"{QRY}/stats")[1]
probe = {a: search(q, a)["results"] for a, q in [("boolean", "coherence -quantum"), ("bm25", "lakehouse"), ("fuzzy", "warrenty")]}
compose("stop", "query"); compose("run", "--rm", "--no-deps", "--entrypoint", "rm", "query", "-f", "/data/query.db", "/data/query.db-wal", "/data/query.db-shm"); compose("start", "query")
results.append(ok(wait(lambda: http("GET", f"{QRY}/health")[0] == 200, 90), "5 query service restarted from an empty database"))
wait(lambda: http("GET", f"{QRY}/stats")[1]["docs"] == before["docs"], 60)
after = http("GET", f"{QRY}/stats")[1]
same = all(search(q, a)["results"] == probe[a] for a, q in [("boolean", "coherence -quantum"), ("bm25", "lakehouse"), ("fuzzy", "warrenty")])
results.append(ok(after["docs"] == before["docs"] and after["events"] == before["events"] and same,
                  f"5 rebuilt read model identical ({after['docs']} docs, {after['events']} events, same ranked results)"))
print(compose("stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}") or subprocess.run(
    ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"], capture_output=True, text=True).stdout)
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
