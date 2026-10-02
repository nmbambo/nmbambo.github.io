"""Pure logic for the CDC head-to-head harness: workload generation, metrics, delivery checks, summaries.
No I/O, no clocks (timestamps are passed in), so everything here is unit-tested without containers.
"""
import json
import math
import random
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ----------------------------------------------------------------------------- workload

@dataclass(frozen=True)
class Mutation:
    op: str           # 'insert' | 'update' | 'delete'
    key: int          # decisions.decision_id
    seq: int          # unique, increasing; written to decisions.workload_seq (deletes carry it for bookkeeping only)

    @property
    def mid(self) -> Tuple:
        """Identity of the change as the harness expects to see it downstream.
        A delete leaves no row, so it is identified by key alone (each key is deleted at most once)."""
        return ("d", self.key) if self.op == "delete" else (self.key, self.seq)


DEFAULT_MIX = (0.20, 0.70, 0.10)  # insert, update, delete


def build_workload(alive: List[int], next_key: int, start_seq: int, n_ops: int, seed: int,
                   mix=DEFAULT_MIX) -> Tuple[List[Mutation], List[int], int]:
    """Deterministic mutation list. Returns (mutations, alive_after, next_key_after).

    Guarantees: seq strictly increasing from start_seq; inserts use fresh keys; updates/deletes only target keys
    that are alive at that point; a key is deleted at most once and never touched afterwards. Updates deliberately
    repeat keys, so per-key ordering is actually exercised.
    """
    rng = random.Random(seed)
    alive = list(alive)
    out: List[Mutation] = []
    seq = start_seq
    p_ins, p_upd, _ = mix
    for _ in range(n_ops):
        r = rng.random()
        if r < p_ins or not alive:
            op, key = "insert", next_key
            next_key += 1
            alive.append(key)
        elif r < p_ins + p_upd:
            op, key = "update", alive[rng.randrange(len(alive))]
        else:
            i = rng.randrange(len(alive))
            alive[i], alive[-1] = alive[-1], alive[i]
            op, key = "delete", alive.pop()
        out.append(Mutation(op, key, seq))
        seq += 1
    return out, alive, next_key


def batches(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), max(size, 1)):
        yield items[i:i + size]

# ----------------------------------------------------------------------------- observations

@dataclass
class Obs:
    """One change as observed at the sink, normalised across tools."""
    key: int
    op: str                       # 'c' create | 'u' update | 'd' delete | 'r' snapshot read | 'tombstone'
    seq: Optional[int]            # workload_seq from the row (None for deletes, snapshot rows, tombstones)
    arrival: float                # epoch seconds when the harness saw it at the sink
    order: Tuple = ()             # sink order (e.g. (partition, offset)); used for ordering checks
    cols: frozenset = frozenset()  # column names present in the row image (schema-change check)

    @property
    def mid(self) -> Optional[Tuple]:
        if self.op == "d":
            return ("d", self.key)
        if self.op in ("c", "u") and self.seq is not None:
            return (self.key, self.seq)
        return None


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_debezium(key_raw: Optional[bytes], value_raw: Optional[bytes], arrival: float, order: Tuple = ()) -> Optional[Obs]:
    """Kafka record from lakebase.public.decisions (JsonConverter, schemas disabled) -> Obs.
    A null value is a tombstone (follows each delete when tombstones.on.delete=true)."""
    try:
        key = json.loads(key_raw) if key_raw else None
    except ValueError:
        return None
    if isinstance(key, dict) and "payload" in key:
        key = key["payload"]
    kid = _num((key or {}).get("decision_id")) if isinstance(key, dict) else None
    if value_raw is None:
        return Obs(kid, "tombstone", None, arrival, order) if kid is not None else None
    try:
        v = json.loads(value_raw)
    except ValueError:
        return None
    if isinstance(v, dict) and "payload" in v:
        v = v["payload"]
    if not isinstance(v, dict):
        return None
    op = v.get("op")
    after, before = v.get("after") or {}, v.get("before") or {}
    row = after or before
    kid = kid if kid is not None else _num(row.get("decision_id"))
    if kid is None or op not in ("c", "u", "d", "r"):
        return None
    seq = _num(after.get("workload_seq")) if op in ("c", "u") else None
    return Obs(kid, op, seq, arrival, order, frozenset(after.keys()))


_OLAKE_OPS = {"c": "c", "insert": "c", "i": "c", "u": "u", "update": "u", "d": "d", "delete": "d", "r": "r", "read": "r"}


def parse_olake_row(row: dict, arrival: float, order: Tuple = ()) -> Optional[Obs]:
    """One row of OLake's Parquet/Iceberg output -> Obs. `_op_type` is documented as a column; the exact value
    vocabulary is UNVERIFIED, so several spellings are accepted."""
    op = _OLAKE_OPS.get(str(row.get("_op_type", "")).strip().lower())
    kid = _num(row.get("decision_id"))
    if op is None or kid is None:
        return None
    seq = _num(row.get("workload_seq")) if op in ("c", "u") else None
    return Obs(kid, op, seq, arrival, order, frozenset(k for k, v in row.items() if v is not None or k == "workload_seq"))


def parse_airbyte_record(value_raw: bytes, arrival: float, order: Tuple = ()) -> Optional[Obs]:
    """Airbyte Kafka destination record -> Obs. UNVERIFIED shape: legacy `_airbyte_data` wrapper and the
    `_ab_cdc_deleted_at` marker column are assumed; newer connector versions may differ."""
    try:
        v = json.loads(value_raw)
    except (TypeError, ValueError):
        return None
    row = v.get("_airbyte_data", v) if isinstance(v, dict) else None
    if not isinstance(row, dict):
        return None
    kid = _num(row.get("decision_id"))
    if kid is None:
        return None
    if row.get("_ab_cdc_deleted_at"):
        return Obs(kid, "d", None, arrival, order, frozenset(row.keys()))
    return Obs(kid, "u", _num(row.get("workload_seq")), arrival, order, frozenset(row.keys()))

# ----------------------------------------------------------------------------- metrics

def percentile(values: List[float], p: float) -> Optional[float]:
    """Linear-interpolation percentile (same as numpy's default). None for empty input."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def latencies_ms(commit_ts: Dict[Tuple, float], observations: List[Obs]) -> List[float]:
    """First arrival of each expected mutation minus its commit time, in ms. Unmatched observations are ignored;
    negative values (clock skew, or arrival recorded before commit returned) are clamped to 0."""
    first: Dict[Tuple, float] = {}
    for o in observations:
        m = o.mid
        if m in commit_ts and (m not in first or o.arrival < first[m]):
            first[m] = o.arrival
    return [max(0.0, (a - commit_ts[m]) * 1000.0) for m, a in first.items()]


def latency_summary(lat: List[float]) -> dict:
    return {"n": len(lat), "p50": _r(percentile(lat, 50)), "p95": _r(percentile(lat, 95)),
            "max": _r(max(lat) if lat else None)}


def _r(x):
    return None if x is None else round(x, 1)


def throughput(commit_ts: Dict[Tuple, float], observations: List[Obs]) -> dict:
    """committed_per_s: how fast the writer committed. delivered_per_s: unique mutations delivered per second
    from the first commit until the last arrival (this is the sink-side sustained rate under that load)."""
    if not commit_ts:
        return {"committed_per_s": None, "delivered_per_s": None, "delivered": 0}
    t0, t1 = min(commit_ts.values()), max(commit_ts.values())
    seen = {o.mid: o.arrival for o in observations if o.mid in commit_ts}
    last = max(seen.values()) if seen else None
    return {
        "committed_per_s": _r(len(commit_ts) / (t1 - t0)) if t1 > t0 else None,
        "delivered_per_s": _r(len(seen) / (last - t0)) if last and last > t0 else None,
        "delivered": len(seen),
    }


def check_ordering(observations: List[Obs]) -> dict:
    """Per-key ordering in sink order: update/insert seq must never decrease, and nothing may follow a delete.
    Observations are sorted by (order, arrival) first."""
    obs = sorted(observations, key=lambda o: (o.order, o.arrival))
    last_seq: Dict[int, int] = {}
    deleted = set()
    violations = 0
    keys = set()
    for o in obs:
        if o.op in ("c", "u") and o.seq is not None:
            keys.add(o.key)
            if o.key in deleted or (o.key in last_seq and o.seq < last_seq[o.key]):
                violations += 1
            last_seq[o.key] = max(last_seq.get(o.key, -1), o.seq)
        elif o.op == "d":
            keys.add(o.key)
            deleted.add(o.key)
    return {"keys_checked": len(keys), "violations": violations}


def check_delivery(expected: List[Mutation], observations: List[Obs]) -> dict:
    """duplicates: extra deliveries of an already-seen mutation. missing: expected mutations never seen (a tool that
    coalesces updates reports these even though final state is right). missing_final: keys whose LAST expected
    mutation was never seen, i.e. the sink's end state would be wrong."""
    seen: Dict[Tuple, int] = {}
    for o in observations:
        m = o.mid
        if m is not None:
            seen[m] = seen.get(m, 0) + 1
    exp_ids = {m.mid for m in expected}
    last_by_key: Dict[int, Mutation] = {}
    for m in expected:
        last_by_key[m.key] = m
    return {
        "expected": len(exp_ids),
        "delivered_unique": sum(1 for i in exp_ids if i in seen),
        "duplicates": sum(c - 1 for i, c in seen.items() if i in exp_ids and c > 1),
        "missing": sum(1 for i in exp_ids if i not in seen),
        "missing_final": sum(1 for m in last_by_key.values() if m.mid not in seen),
    }


def check_deletes(expected: List[Mutation], observations: List[Obs]) -> dict:
    dels = {m.key for m in expected if m.op == "delete"}
    got = {o.key for o in observations if o.op == "d"}
    tomb = {o.key for o in observations if o.op == "tombstone"}
    return {"expected": len(dels), "observed_as_delete": len(dels & got), "tombstones_observed": len(dels & tomb)}

# ----------------------------------------------------------------------------- resources (docker stats)

_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_bytes(text: str) -> Optional[float]:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$", text or "")
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


def parse_docker_stats_line(line: str) -> Optional[dict]:
    """One line of `docker stats --no-stream --format '{{json .}}'` -> {name, cpu_pct, mem_mib}."""
    try:
        d = json.loads(line)
        cpu = float(str(d["CPUPerc"]).rstrip("%"))
        mem = parse_bytes(str(d["MemUsage"]).split("/")[0])
        return {"name": d["Name"], "cpu_pct": cpu, "mem_mib": None if mem is None else mem / 1024 ** 2}
    except (ValueError, KeyError, TypeError):
        return None


def summarize_resources(samples: List[dict]) -> dict:
    """samples: [{name, cpu_pct, mem_mib}] across time -> per-container avg/peak."""
    by: Dict[str, List[dict]] = {}
    for s in samples:
        by.setdefault(s["name"], []).append(s)
    out = {}
    for name, ss in by.items():
        cpu = [s["cpu_pct"] for s in ss]
        mem = [s["mem_mib"] for s in ss if s["mem_mib"] is not None]
        out[name] = {"samples": len(ss), "cpu_avg_pct": _r(sum(cpu) / len(cpu)), "cpu_peak_pct": _r(max(cpu)),
                     "mem_avg_mib": _r(sum(mem) / len(mem)) if mem else None, "mem_peak_mib": _r(max(mem)) if mem else None}
    return out

# ----------------------------------------------------------------------------- result files

SCENARIOS = ("snapshot", "workload", "throughput", "schema_change", "restart_recovery", "resources")


def new_result(tool: str, params: dict, arrival_basis: str, tool_version: Optional[str] = None) -> dict:
    return {"tool": tool, "tool_version": tool_version, "arrival_basis": arrival_basis, "params": params,
            "measured_at": None, "host": {}, "scenarios": {}}


def status(ok: bool, **fields) -> dict:
    return {"status": "ok" if ok else "failed", **fields}
