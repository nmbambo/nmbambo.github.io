"""Check 1: `docker compose up -d` brings every core service to healthy. Records the time to healthy.

Containers are removed first (volumes and images are kept), so the number is a restart-from-stopped time with
images present, not a first-ever pull/build.
"""
import json
import sys
import time

from _common import OUT, Checks, compose, kafka_end_offsets, wait_until

CORE = ["postgres-lakebase", "kafka", "kafka-rest", "debezium-connect", "kurrentdb", "messagedb", "gateway", "projector"]


def states():
    r = compose("ps", "-a", "--format", "json")
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    return {x["Service"]: x.get("Health") or x.get("State") for x in rows}


def main():
    c = Checks("01_compose")
    try:
        before = kafka_end_offsets()                   # for the persistence check below
    except Exception:  # noqa: BLE001 - stack down, or no topic yet
        before = {}
    compose("down", timeout=300)                       # no -v: keep volumes
    t0 = time.time()
    compose("up", "-d", timeout=600)
    seen = {}

    def all_healthy():
        s = states()
        for k, v in s.items():
            if v == "healthy" and k not in seen:
                seen[k] = round(time.time() - t0, 1)
        return all(s.get(svc) == "healthy" for svc in CORE)
    try:
        wait_until(all_healthy, "all core services healthy", timeout=300, every=1.0)
        total = round(time.time() - t0, 1)
    except TimeoutError as exc:
        total = None
        c.check("all core services healthy", False, exc)
    s = states()
    for svc in CORE:
        c.check(f"{svc} healthy", s.get(svc) == "healthy", f"{s.get(svc)} at {seen.get(svc)}s")
    if total is not None:
        c.check("all core services healthy", True, f"{total}s from `up -d`")
        if sum(before.values()) > 0:
            after = kafka_end_offsets()
            c.check("Kafka topic data survived down/up (log dir is on the volume)", after == before, f"{before} -> {after}")
    return c.finish({"seconds_to_healthy": total, "per_service_seconds": seen})


if __name__ == "__main__":
    sys.exit(main())
