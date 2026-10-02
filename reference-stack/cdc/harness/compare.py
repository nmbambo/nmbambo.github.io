#!/usr/bin/env python3
"""Render results/<tool>.json into a markdown comparison table. Prints 'not yet measured' for anything absent.

  python3 compare.py [--results ../../results] [--out ../../results/comparison.md]
"""
import argparse
import json
import pathlib

TOOLS = ["debezium", "olake", "airbyte"]
NA = "not yet measured"


def _get(d, path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _fmt(v, unit=""):
    return NA if v is None else f"{v}{unit}"


def _scen(res, name):
    s = _get(res, ["scenarios", name])
    return s if isinstance(s, dict) else None


def _ok(res, name, fn):
    """fn(scenario) -> str, but only when the scenario ran ok; otherwise show skipped/failed/not measured."""
    s = _scen(res, name)
    if s is None:
        return NA
    if s.get("status") == "skipped":
        return "skipped"
    if s.get("status") != "ok":
        return "failed"
    try:
        return fn(s)
    except (KeyError, TypeError):
        return NA


def rows(results):
    def per(fn):
        return [fn(results.get(t)) if results.get(t) else NA for t in TOOLS]

    spec = [
        ("Initial snapshot (s)", lambda r: _ok(r, "snapshot", lambda s: _fmt(s["seconds"]))),
        ("Latency p50 (ms)", lambda r: _ok(r, "workload", lambda s: _fmt(s["latency_ms"]["p50"]))),
        ("Latency p95 (ms)", lambda r: _ok(r, "workload", lambda s: _fmt(s["latency_ms"]["p95"]))),
        ("Throughput delivered (changes/s, burst)", lambda r: _ok(r, "throughput", lambda s: _fmt(s["throughput"]["delivered_per_s"]))),
        ("Ordering violations per key", lambda r: _ok(r, "workload", lambda s: _fmt(s["ordering"]["violations"]))),
        ("Deletes seen as delete / expected", lambda r: _ok(r, "workload", lambda s: f'{s["deletes"]["observed_as_delete"]}/{s["deletes"]["expected"]}')),
        ("Tombstones seen / expected deletes", lambda r: _ok(r, "workload", lambda s: f'{s["deletes"]["tombstones_observed"]}/{s["deletes"]["expected"]}')),
        ("Schema change: rows carrying new column", lambda r: _ok(r, "schema_change", lambda s: f'{s["rows_with_new_column"]}/{s["rows_updated"]}')),
        ("Schema change: first change after ALTER (s)", lambda r: _ok(r, "schema_change", lambda s: _fmt(s["seconds_to_first_change"]))),
        ("Restart: duplicates", lambda r: _ok(r, "restart_recovery", lambda s: _fmt(s["delivery"]["duplicates"]))),
        ("Restart: missing changes", lambda r: _ok(r, "restart_recovery", lambda s: _fmt(s["delivery"]["missing"]))),
        ("Restart: missing final state (keys)", lambda r: _ok(r, "restart_recovery", lambda s: _fmt(s["delivery"]["missing_final"]))),
        ("Restart: restart to first change (s)", lambda r: _ok(r, "restart_recovery", lambda s: _fmt(s["seconds_from_restart_to_first_change"]))),
        ("Footprint: tool CPU avg / peak (%)", lambda r: _footprint(r, "cpu_avg_pct", "cpu_peak_pct")),
        ("Footprint: tool memory avg / peak (MiB)", lambda r: _footprint(r, "mem_avg_mib", "mem_peak_mib")),
    ]
    return [(label, per(fn)) for label, fn in spec]


def _footprint(res, avg, peak):
    s = _scen(res, "resources")
    if not s or s.get("status") != "ok":
        return NA
    needles = {"debezium": "debezium", "olake": "olake", "airbyte": "airbyte"}[res["tool"]]
    pcs = {k: v for k, v in s.get("per_container", {}).items() if needles in k}
    if not pcs:
        return NA
    a = sum(v[avg] or 0 for v in pcs.values())
    p = sum(v[peak] or 0 for v in pcs.values())
    return f"{round(a, 1)} / {round(p, 1)}"


def render(results):
    head = "| Metric | Debezium | OLake | Airbyte |\n|---|---|---|---|\n"
    body = "".join(f"| {label} | " + " | ".join(vals) + " |\n" for label, vals in rows(results))
    meta = []
    for t in TOOLS:
        r = results.get(t)
        if r:
            p = r.get("params", {})
            meta.append(f"- {t}: {r.get('tool_version') or 'version unknown'}; measured {r.get('measured_at')}; "
                        f"rows={p.get('rows')}, ops={p.get('ops')}, rate={p.get('rate')}/s, txn={p.get('txn_size')}; "
                        f"arrival basis: {r.get('arrival_basis')}; host: {r.get('host', {}).get('cpus')} CPUs")
    note = "\n\n" + "\n".join(meta) + "\n" if meta else "\n\nNo results files found: every cell is \"not yet measured\".\n"
    return head + body + note


def load(results_dir):
    out = {}
    for t in TOOLS:
        p = pathlib.Path(results_dir) / f"{t}.json"
        if p.exists():
            out[t] = json.loads(p.read_text())
    return out


def main():
    here = pathlib.Path(__file__).resolve().parents[2] / "results"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=str(here))
    ap.add_argument("--out", default=None, help="also write the table to this file")
    a = ap.parse_args()
    md = render(load(a.results))
    print(md)
    if a.out:
        pathlib.Path(a.out).write_text(md)


if __name__ == "__main__":
    main()
