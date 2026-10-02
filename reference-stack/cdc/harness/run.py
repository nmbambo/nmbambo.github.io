#!/usr/bin/env python3
"""Run the CDC head-to-head for one or more tools and write results/<tool>.json.

  python3 run.py --tool debezium --rows 10000 --ops 5000 --rate 200
  python3 run.py --tool all --scenarios snapshot,workload

Prerequisites: `docker compose up -d` (and `--profile olake` for OLake), the Debezium connector NOT pre-registered
(the harness registers its own per-run connector), `pip install -r requirements.txt`.

Every number written is measured by this script on the machine it runs on. A scenario that cannot run is written
as {"status": "skipped"|"failed", "reason": ...}; nothing is ever filled in by hand.
"""
import argparse
import json
import os
import pathlib
import platform
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

import core
import db
from tools.base import Ctx, ToolNotConfigured
from tools.debezium import Debezium
from tools.olake import OLake
from tools.airbyte import Airbyte

TOOLS = {"debezium": Debezium, "olake": OLake, "airbyte": Airbyte}
ALL_SCENARIOS = ["snapshot", "workload", "throughput", "schema_change", "restart_recovery"]
RESULTS = pathlib.Path(__file__).resolve().parents[2] / "results"


class Sampler(threading.Thread):
    """Samples `docker stats --no-stream` every 2 s for containers whose name contains any of `needles`."""

    def __init__(self, needles):
        super().__init__(daemon=True)
        self.needles, self.samples, self._halt = needles, [], threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                                     capture_output=True, text=True, timeout=30).stdout
                for line in out.splitlines():
                    s = core.parse_docker_stats_line(line)
                    if s and any(n in s["name"] for n in self.needles):
                        self.samples.append(s)
            except (subprocess.SubprocessError, OSError):
                pass
            self._halt.wait(1.0)

    def finish(self):
        self._halt.set()
        self.join(timeout=35)
        return self.samples


def wait_until(tool, cond, timeout, poll=0.2):
    """Pump the tool until cond() is true or timeout. Returns (ok, elapsed_seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        tool.pump()
        if cond():
            return True, time.time() - t0
        time.sleep(poll)
    return False, time.time() - t0


def delivered(tool, muts):
    want = {m.mid for m in muts}
    return len(want & {o.mid for o in tool.observations() if o.mid is not None}) == len(want)


class State:
    def __init__(self, rows, seed):
        self.alive, self.next_key, self.seq, self.seed = list(range(1, rows + 1)), rows + 1, 1_000_000, seed
        self.n_parties = max(10, rows // 10)
        self.phase = 0

    def make(self, n):
        self.phase += 1
        muts, self.alive, self.next_key = core.build_workload(self.alive, self.next_key, self.seq, n, self.seed + self.phase)
        self.seq += n
        return muts


def scenario_snapshot(tool, a, st, conn):
    snap_keys = lambda: {o.key for o in tool.observations() if o.op in ("r", "c") and o.seq is None}
    t0 = time.time()
    tool.start()
    ok, secs = wait_until(tool, lambda: len(snap_keys()) >= a.rows, a.timeout)
    return core.status(ok, seconds=round(time.time() - t0, 2), rows_expected=a.rows, rows_observed=len(snap_keys()),
                       note="seed rows only (decisions table); time from connector start to last snapshot row seen")


def run_workload(tool, a, st, conn, n, rate, txn):
    muts = st.make(n)
    commit_ts = {}
    t0 = time.time()
    db.apply_workload(conn, muts, st.n_parties, txn, rate, commit_ts)
    ok, drain = wait_until(tool, lambda: delivered(tool, muts), a.timeout)
    return muts, commit_ts, ok, drain, t0


def scenario_workload(tool, a, st, conn):
    start_idx = len(tool.observations())
    sampler = Sampler(tool.containers + ["kafka", "postgres-lakebase"]); sampler.start()
    muts, commit_ts, ok, drain, _ = run_workload(tool, a, st, conn, a.ops, a.rate, a.txn_size)
    res = core.summarize_resources(sampler.finish())
    obs = tool.observations()[start_idx:]
    out = core.status(ok, mutations=len(muts), rate_target_per_s=a.rate, txn_size=a.txn_size, drain_seconds=round(drain, 2),
                      latency_ms=core.latency_summary(core.latencies_ms(commit_ts, obs)),
                      throughput=core.throughput(commit_ts, obs),
                      ordering=core.check_ordering(obs),
                      deletes=core.check_deletes(muts, obs),
                      delivery=core.check_delivery(muts, obs))
    return out, {"status": "ok" if res else "failed", "per_container": res,
                 "note": "sampled every ~3 s via docker stats during the paced workload; shared containers included by name"}


def scenario_throughput(tool, a, st, conn):
    start_idx = len(tool.observations())
    muts, commit_ts, ok, drain, _ = run_workload(tool, a, st, conn, a.burst, 0, 100)
    obs = tool.observations()[start_idx:]
    return core.status(ok, mutations=len(muts), drain_seconds=round(drain, 2), throughput=core.throughput(commit_ts, obs),
                       ordering=core.check_ordering(obs), delivery=core.check_delivery(muts, obs))


def scenario_schema_change(tool, a, st, conn):
    start_idx = len(tool.observations())
    # fresh updates on keys that are alive right now; seq continues the global counter
    upd = [core.Mutation("update", k, st.seq + i) for i, k in enumerate(st.alive[:50])]
    st.seq += len(upd)
    t0 = time.time()
    try:
        add_ts = db.add_column_and_update(conn, upd)
    except Exception as exc:  # noqa: BLE001
        return core.status(False, error=f"{type(exc).__name__}: {exc}")
    want = {m.mid for m in upd}
    ok, secs = wait_until(tool, lambda: want <= {o.mid for o in tool.observations()[start_idx:] if o.mid}, a.timeout)
    obs = [o for o in tool.observations()[start_idx:] if o.mid in want]
    with_col = sum(1 for o in obs if "risk_band" in o.cols)
    first = min((o.arrival for o in obs), default=None)
    return core.status(ok, rows_updated=len(upd), rows_seen=len({o.mid for o in obs}), rows_with_new_column=with_col,
                       seconds_to_first_change=None if first is None else round(first - add_ts, 2),
                       seconds_to_all_changes=round(secs, 2), pipeline_survived=ok)


def scenario_restart(tool, a, st, conn):
    if getattr(tool, "supports_kill", True) is False:
        return {"status": "skipped", "reason": "kill/restart of this tool's workers is not scripted"}
    start_idx = len(tool.observations())
    n = max(300, a.ops // 2)
    muts = st.make(n)
    commit_ts, progress = {}, {"n": 0}
    writer = threading.Thread(target=lambda: db.apply_workload(
        conn, muts, st.n_parties, a.txn_size, a.rate, commit_ts, on_batch=lambda c: progress.update(n=c)), daemon=True)
    writer.start()
    while progress["n"] < n // 3 and writer.is_alive():
        tool.pump(); time.sleep(0.1)
    t_kill = time.time()
    tool.kill()
    while progress["n"] < 2 * n // 3 and writer.is_alive():
        time.sleep(0.1)
    t_restart = time.time()
    tool.restart()
    writer.join(timeout=a.timeout)
    ok, drain = wait_until(tool, lambda: delivered(tool, muts), a.timeout)
    obs = tool.observations()[start_idx:]
    after = [o.arrival for o in obs if o.arrival >= t_restart and o.mid is not None]
    return core.status(ok, mutations=n, killed_at_commit=n // 3, restarted_at_commit=2 * n // 3,
                       downtime_seconds=round(t_restart - t_kill, 2),
                       seconds_from_restart_to_first_change=None if not after else round(min(after) - t_restart, 2),
                       delivery=core.check_delivery(muts, obs), ordering=core.check_ordering(obs))


def run_tool(name, a):
    run_id = uuid.uuid4().hex[:8]
    env = db.load_env()
    ctx = Ctx(env, run_id)
    result = core.new_result(name, {"rows": a.rows, "ops": a.ops, "rate": a.rate, "burst": a.burst, "txn_size": a.txn_size,
                                    "seed": a.seed, "run_id": run_id, "timeout_s": a.timeout}, "")
    result["host"] = {"platform": platform.platform(), "cpus": os.cpu_count(), "python": platform.python_version()}
    try:
        tool = TOOLS[name](ctx)
    except ToolNotConfigured as exc:
        result["scenarios"] = {s: {"status": "skipped", "reason": str(exc)} for s in ALL_SCENARIOS}
        return result
    result["arrival_basis"], result["tool_version"] = tool.arrival_basis, tool.tool_version()
    conn = db.connect(env)
    try:
        tool.reset()
        db.reset(conn)
        db.seed(conn, a.rows, a.seed)
        st = State(a.rows, a.seed)
        wanted = [s for s in ALL_SCENARIOS if s in a.scenarios]
        # later scenarios need the pipeline running, which `snapshot` starts; always run it first
        order = ["snapshot"] + [s for s in wanted if s != "snapshot"]
        fns = {"snapshot": scenario_snapshot, "workload": scenario_workload, "throughput": scenario_throughput,
               "schema_change": scenario_schema_change, "restart_recovery": scenario_restart}
        for s in order:
            try:
                r = fns[s](tool, a, st, conn)
                if s == "workload":
                    r, res = r
                    result["scenarios"]["resources"] = res
                result["scenarios"][s] = r
            except ToolNotConfigured as exc:
                result["scenarios"][s] = {"status": "skipped", "reason": str(exc)}
            except Exception as exc:  # noqa: BLE001 - record and carry on with the next scenario
                result["scenarios"][s] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            _save(name, result)
    finally:
        tool.shutdown()
        conn.close()
    return result


def _save(name, result):
    RESULTS.mkdir(exist_ok=True)
    result["measured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (RESULTS / f"{name}.json").write_text(json.dumps(result, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tool", default="debezium", help="debezium | olake | airbyte | all")
    ap.add_argument("--rows", type=int, default=10_000, help="synthetic decisions seeded before the snapshot")
    ap.add_argument("--ops", type=int, default=5_000, help="mutations in the paced workload")
    ap.add_argument("--rate", type=float, default=200, help="paced workload target mutations/s")
    ap.add_argument("--burst", type=int, default=5_000, help="mutations in the unthrottled throughput run")
    ap.add_argument("--txn-size", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=float, default=300, help="per-wait timeout, seconds")
    ap.add_argument("--scenarios", default=",".join(ALL_SCENARIOS), type=lambda s: s.split(","))
    a = ap.parse_args()
    names = list(TOOLS) if a.tool == "all" else [a.tool]
    for n in names:
        if n not in TOOLS:
            ap.error(f"unknown tool {n}")
        print(f"== {n}", flush=True)
        r = run_tool(n, a)
        _save(n, r)
        print(json.dumps({k: v.get("status") for k, v in r["scenarios"].items()}), flush=True)
    print(f"results written to {RESULTS}/<tool>.json; render with: python3 compare.py")


if __name__ == "__main__":
    main()
