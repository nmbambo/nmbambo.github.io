#!/usr/bin/env python3
"""Prepare OLake for the harness: render source.json / destination.json from the examples + .env,
create the replication slot, run `discover`, and patch streams.json to CDC for the three lakebase tables.

Stdlib only; shells out to `docker compose`. Run from anywhere:  python3 cdc/olake/prepare.py

UNVERIFIED against a live OLake: the config keys come from OLake's docs (olake.io/docs/connectors/postgres,
install/docker-cli) and the streams.json shape (selected_streams + streams[].stream.sync_mode) from the same
docs. Nothing here has been executed.
"""
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
STACK = HERE.parent.parent
CFG = HERE / "config"
TABLES = ["parties", "decisions", "decision_events"]


def load_env():
    env = dict(os.environ)
    p = STACK / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def sh(*args, **kw):
    return subprocess.run(args, check=True, cwd=STACK, **kw)


def render(env):
    src = json.loads((CFG / "source.example.json").read_text())
    src["username"] = env.get("CDC_USER", "cdc_reader")
    src["password"] = env.get("CDC_PASSWORD", "change-me-cdc")
    src["update_method"]["replication_slot"] = env.get("OLAKE_SLOT", "olake_slot")
    (CFG / "source.json").write_text(json.dumps(src, indent=2))
    (CFG / "destination.json").write_text((CFG / "destination.example.json").read_text())
    (STACK / "cdc" / "olake" / "out").mkdir(exist_ok=True)


def create_slot(env):
    slot = env.get("OLAKE_SLOT", "olake_slot")
    sql = (f"SELECT pg_create_logical_replication_slot('{slot}','pgoutput') "
           f"WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='{slot}');")
    sh("docker", "compose", "exec", "-T", "postgres-lakebase", "psql", "-U", env.get("LAKEBASE_USER", "lakebase"),
       "-d", env.get("LAKEBASE_DB", "lakebase"), "-c", sql)


def discover():
    sh("docker", "compose", "--profile", "olake", "run", "--rm", "olake", "discover",
       "--config", "/mnt/config/source.json")


def patch_streams():
    p = CFG / "streams.json"
    cat = json.loads(p.read_text())
    keep = set(TABLES)
    for s in cat.get("streams", []):
        st = s.get("stream", s)
        if st.get("name") in keep and "cdc" in st.get("supported_sync_modes", []):
            st["sync_mode"] = "cdc"
    sel = cat.get("selected_streams", {})
    for ns, items in list(sel.items()):
        sel[ns] = [i for i in items if i.get("stream_name") in keep]
    p.write_text(json.dumps(cat, indent=2))
    print("streams.json patched:", {k: [i.get("stream_name") for i in v] for k, v in sel.items()})


if __name__ == "__main__":
    e = load_env()
    render(e)
    create_slot(e)
    discover()
    patch_streams()
    print("OLake ready. Run a sync with: docker compose --profile olake run --rm olake")
