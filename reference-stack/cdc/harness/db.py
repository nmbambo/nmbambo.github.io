"""Postgres side of the harness: connect, reset, seed synthetic rows, apply mutations. psycopg 3.

SYNTHETIC DATA ONLY (POPIA): labels are 'Synthetic Party 000123' and 'Synthetic decision 000123'; no names,
no emails, no real organisations. Content is derived from a seeded RNG so runs are reproducible.
"""
import os
import pathlib
import random
import time
from typing import Dict, List

STACK = pathlib.Path(__file__).resolve().parents[2]
REGIONS = ["region-a", "region-b", "region-c", "region-d"]
KINDS = ["individual", "organisation", "committee"]
STATUSES = ["open", "deliberating", "decided", "superseded"]


def load_env() -> Dict[str, str]:
    env = {}
    p = STACK / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith(("LAKEBASE", "CDC_", "KAFKA", "CONNECT", "AIRBYTE", "OLAKE"))})
    return env


def connect(env=None):
    import psycopg
    env = env or load_env()
    return psycopg.connect(
        host=env.get("LAKEBASE_HOST", "localhost"), port=int(env.get("LAKEBASE_PORT", "15432")),
        user=env.get("LAKEBASE_USER", "lakebase"), password=env.get("LAKEBASE_PASSWORD", "change-me-lakebase"),
        dbname=env.get("LAKEBASE_DB", "lakebase"), autocommit=False)


def reset(conn):
    """Empty the three tables and remove the schema-change column. Run only while no connector is attached."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE decision_events, decisions, parties RESTART IDENTITY")
        cur.execute("ALTER TABLE decisions DROP COLUMN IF EXISTS risk_band")
    conn.commit()


def drop_inactive_slots(conn, like: str):
    with conn.cursor() as cur:
        cur.execute("SELECT slot_name FROM pg_replication_slots WHERE slot_name LIKE %s AND NOT active", (like,))
        for (name,) in cur.fetchall():
            cur.execute("SELECT pg_drop_replication_slot(%s)", (name,))
    conn.commit()


def seed(conn, n_decisions: int, seed_value: int = 42) -> int:
    """Insert n synthetic decisions (keys 1..n), n/10 parties, 1 event per decision. Returns number of parties."""
    rng = random.Random(seed_value)
    n_parties = max(10, n_decisions // 10)
    with conn.cursor() as cur:
        with cur.copy("COPY parties (party_id, label, kind, region) FROM STDIN") as cp:
            for i in range(1, n_parties + 1):
                cp.write_row((i, f"Synthetic Party {i:06d}", rng.choice(KINDS), rng.choice(REGIONS)))
        with cur.copy("COPY decisions (decision_id, party_id, title, status, stakes, payload) FROM STDIN") as cp:
            for i in range(1, n_decisions + 1):
                cp.write_row((i, 1 + i % n_parties, f"Synthetic decision {i:06d}", rng.choice(STATUSES),
                              rng.randint(1, 5), '{"synthetic": true}'))
        with cur.copy("COPY decision_events (decision_id, event_type, payload) FROM STDIN") as cp:
            for i in range(1, n_decisions + 1):
                cp.write_row((i, "DecisionRecorded", '{"synthetic": true}'))
        cur.execute("ANALYZE")
    conn.commit()
    return n_parties


def _apply(cur, m, n_parties: int):
    if m.op == "insert":
        cur.execute("INSERT INTO decisions (decision_id, party_id, title, status, stakes, payload, workload_seq) "
                    "VALUES (%s,%s,%s,%s,%s,'{\"synthetic\": true}',%s)",
                    (m.key, 1 + m.key % n_parties, f"Synthetic decision {m.key:06d}", "open", 1 + m.seq % 5, m.seq))
    elif m.op == "update":
        cur.execute("UPDATE decisions SET status=%s, stakes=%s, title=%s, workload_seq=%s, updated_at=now() "
                    "WHERE decision_id=%s",
                    (STATUSES[m.seq % 4], 1 + m.seq % 5, f"Synthetic decision {m.key:06d} r{m.seq}", m.seq, m.key))
    else:
        cur.execute("DELETE FROM decisions WHERE decision_id=%s", (m.key,))


def apply_batch(conn, muts: List, n_parties: int) -> float:
    """One transaction. Returns the epoch time at which COMMIT returned (the change's 'commit time')."""
    with conn.cursor() as cur:
        for m in muts:
            _apply(cur, m, n_parties)
    conn.commit()
    return time.time()


def apply_workload(conn, muts, n_parties, txn_size, rate, commit_ts: Dict, on_batch=None):
    """Apply muts in transactions of txn_size at `rate` mutations/s (0 = unthrottled), recording commit times."""
    from core import batches
    t_next = time.time()
    for b in batches(muts, txn_size):
        ts = apply_batch(conn, b, n_parties)
        for m in b:
            commit_ts[m.mid] = ts
        if on_batch:
            on_batch(len(commit_ts))
        if rate > 0:
            t_next += len(b) / rate
            delay = t_next - time.time()
            if delay > 0:
                time.sleep(delay)


def add_column_and_update(conn, muts: List) -> float:
    """Schema change: add risk_band, then update rows to carry it (muts are 'update' mutations)."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS risk_band text")
    conn.commit()
    with conn.cursor() as cur:
        for m in muts:
            cur.execute("UPDATE decisions SET risk_band='high', workload_seq=%s, updated_at=now() WHERE decision_id=%s",
                        (m.seq, m.key))
    conn.commit()
    return time.time()
