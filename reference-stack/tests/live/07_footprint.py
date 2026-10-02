"""Check 7: memory (and CPU) per running service from `docker stats --no-stream`, plus host memory.

Not a pass/fail check on a number: it records the table (out/07_footprint_<label>.json; label = first argument, default after-checks) and fails only if a core
service is missing from the output. Run it after the other checks for a "warm" figure, or right after `up` for idle.
"""
import json
import re
import subprocess
import sys

from _common import Checks, STACK, compose

CORE = ["postgres-lakebase", "kafka", "kafka-rest", "debezium-connect", "kurrentdb", "messagedb", "gateway", "projector"]
UNITS = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}


def mib(text):
    m = re.match(r"([\d.]+)\s*([KMG]?i?B)", text.strip())
    return round(float(m.group(1)) * UNITS[m.group(2)], 1) if m else None


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "after-checks"   # e.g. "idle" right after `up`
    c = Checks(f"07_footprint_{label}")
    names = {}
    for line in compose("ps", "--format", "{{.Service}}|{{.Name}}").stdout.splitlines():
        s, n = line.split("|")
        names[n] = s
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names], capture_output=True, text=True, cwd=STACK)
    rows = []
    for line in r.stdout.splitlines():
        d = json.loads(line)
        used, _, limit = d["MemUsage"].partition("/")
        rows.append({"service": names.get(d["Name"], d["Name"]), "mem_mib": mib(used), "mem_limit_mib": mib(limit),
                     "mem_percent": d["MemPerc"], "cpu_percent": d["CPUPerc"], "pids": d["PIDs"]})
    rows.sort(key=lambda x: -(x["mem_mib"] or 0))
    seen = {x["service"] for x in rows}
    for svc in CORE:
        c.check(f"{svc} reported by docker stats", svc in seen)
    total = round(sum(x["mem_mib"] or 0 for x in rows), 1)
    print(f"\n  {'service':20} {'MiB':>9} {'CPU%':>7}")
    for x in rows:
        print(f"  {x['service']:20} {x['mem_mib']:>9} {x['cpu_percent']:>7}")
    print(f"  {'TOTAL':20} {total:>9}")
    free = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
    print("\n" + free)
    return c.finish({"rows": rows, "total_mib": total, "host_free_m": free})


if __name__ == "__main__":
    sys.exit(main())
