"""OLake (Postgres source -> Parquet on local disk), driven as one-shot `sync` runs, observed from the files.

OLake's CLI image is a batch runner: each `sync` snapshots (first run) or reads the WAL since the last state,
writes files, and exits after `initial_wait_time` of idle. So arrival time here is file mtime, and latency
includes that batching; it is a property of the tool's model, not a harness artefact. See README.

Config keys follow olake.io docs (connectors/postgres, install/docker-cli, writers/parquet/local). The output
directory layout and the `_op_type` vocabulary are UNVERIFIED: the file discovery below globs for any Parquet
file with 'decisions' in its path. Nothing in this file has been executed against a live OLake.
"""
import json
import pathlib
import subprocess
import time

from core import parse_olake_row
from tools.base import CdcTool, ToolNotConfigured

OLAKE_DIR = pathlib.Path(__file__).resolve().parents[2] / "olake"
CFG = OLAKE_DIR / "config"


class OLake(CdcTool):
    name = "olake"
    arrival_basis = "parquet file mtime after each batch sync (batch tool; includes its idle-wait)"
    containers = ["olake"]

    def __init__(self, ctx, interval=3.0):
        super().__init__(ctx)
        self.slot = f"olake_{ctx.run_id}"
        self.interval = interval
        self.proc = None
        self.next_run = 0.0
        self.seen_files = set()
        self.rel_out = f"run-{ctx.run_id}"
        self.out = OLAKE_DIR / "out" / self.rel_out
        self.state = f"/mnt/config/state-{ctx.run_id}.json"

    def tool_version(self):
        return self.ctx.image_tag("olake")

    def _profiles(self):
        return ("olake",)

    def reset(self):
        from db import connect, drop_inactive_slots
        c = connect(self.ctx.env)
        drop_inactive_slots(c, "olake\\_%")
        c.close()

    def _write_configs(self):
        src = json.loads((CFG / "source.example.json").read_text())
        src["username"] = self.ctx.env.get("CDC_USER", "cdc_reader")
        src["password"] = self.ctx.env.get("CDC_PASSWORD", "change-me-cdc")
        src["update_method"]["replication_slot"] = self.slot
        (CFG / f"source-{self.ctx.run_id}.json").write_text(json.dumps(src, indent=2))
        dst = {"type": "PARQUET", "writer": {"local_path": f"/mnt/out/{self.rel_out}"}}
        (CFG / f"destination-{self.ctx.run_id}.json").write_text(json.dumps(dst, indent=2))
        self.out.mkdir(parents=True, exist_ok=True)

    def _create_slot_and_discover(self):
        env = self.ctx.env
        sql = f"SELECT pg_create_logical_replication_slot('{self.slot}','pgoutput')"
        self.ctx.compose("exec", "-T", "postgres-lakebase", "psql", "-U", env.get("LAKEBASE_USER", "lakebase"),
                         "-d", env.get("LAKEBASE_DB", "lakebase"), "-c", sql)
        self.ctx.compose("run", "--rm", "olake", "discover", "--config", f"/mnt/config/source-{self.ctx.run_id}.json",
                         profiles=self._profiles())
        # patch streams.json to CDC for the three tables (same logic as cdc/olake/prepare.py)
        cat = json.loads((CFG / "streams.json").read_text())
        keep = {"parties", "decisions", "decision_events"}
        for s in cat.get("streams", []):
            st = s.get("stream", s)
            if st.get("name") in keep and "cdc" in st.get("supported_sync_modes", []):
                st["sync_mode"] = "cdc"
        for ns, items in list(cat.get("selected_streams", {}).items()):
            cat["selected_streams"][ns] = [i for i in items if i.get("stream_name") in keep]
        (CFG / "streams.json").write_text(json.dumps(cat, indent=2))

    def _launch(self):
        cmd = ["docker", "compose", "-f", str(self.ctx.stack / "docker-compose.yml"), "--profile", "olake", "run", "--rm",
               "olake", "sync", "--config", f"/mnt/config/source-{self.ctx.run_id}.json",
               "--streams", "/mnt/config/streams.json", "--destination", f"/mnt/config/destination-{self.ctx.run_id}.json",
               "--state", self.state]
        self.proc = subprocess.Popen(cmd, cwd=self.ctx.stack, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self):
        if not (CFG / "source.example.json").exists():
            raise ToolNotConfigured("cdc/olake/config/source.example.json missing")
        self._write_configs()
        self._create_slot_and_discover()
        self._launch()

    def _scan(self):
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ToolNotConfigured("pyarrow is required to read OLake output (pip install -r requirements.txt)") from exc
        files = sorted(p for p in self.out.rglob("*.parquet") if "decisions" in str(p.relative_to(self.out)) and "decision_events" not in str(p.relative_to(self.out)))
        for idx, p in enumerate(files):
            sig = (str(p), p.stat().st_size)
            if sig in self.seen_files:
                continue
            self.seen_files.add(sig)
            mtime = p.stat().st_mtime
            for r, row in enumerate(pq.read_table(p).to_pylist()):
                self._add(parse_olake_row(row, mtime, (int(p.stat().st_mtime_ns), idx, r)))

    def pump(self):
        if self.proc is not None and self.proc.poll() is not None:   # a sync finished
            self.proc = None
            self._scan()
            self.next_run = time.time() + self.interval
        if self.proc is None and time.time() >= self.next_run and self._wanted:
            self._launch()

    _wanted = True

    def kill(self):
        self._wanted = False
        out = subprocess.run(["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=olake"],
                             capture_output=True, text=True).stdout.split()
        for cid in out:
            subprocess.run(["docker", "kill", cid], capture_output=True)
        if self.proc is not None:
            self.proc.wait(timeout=60)
            self.proc = None

    def restart(self):
        self._wanted = True
        self.next_run = 0.0
