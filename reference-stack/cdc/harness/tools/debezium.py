"""Debezium Postgres connector on Kafka Connect, observed through the Kafka topic it writes.

Verified from Debezium docs (postgresql connector, 3.7): config keys, snapshot.mode values, tombstones.on.delete.
Not verified by running: everything in this file (no Docker daemon was available when it was written).
Each run uses a fresh connector name and replication slot, because Kafka Connect keeps source offsets by
connector name and a recreated connector with a stale offset would not snapshot.
"""
import json
import pathlib
import time
import urllib.error
import urllib.request

from core import parse_debezium
from tools.base import CdcTool, KafkaCollector

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "debezium" / "postgres-connector.json"


class Debezium(CdcTool):
    name = "debezium"
    arrival_basis = "kafka consumer receive time (harness clock, same host)"
    containers = ["debezium-connect"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.url = f"http://localhost:{ctx.env.get('CONNECT_PORT', '8083')}"
        self.connector = f"lakebase-postgres-{ctx.run_id}"
        self.slot = f"dbz_{ctx.run_id}"
        self.collector = None

    def _http(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def tool_version(self):
        return self.ctx.image_tag("debezium-connect")

    def reset(self):
        # delete any earlier harness connectors, then their (now inactive) slots and topics
        try:
            for name in self._http("GET", "/connectors") or []:
                if name.startswith("lakebase-postgres-"):
                    self._http("DELETE", f"/connectors/{name}")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"Kafka Connect not reachable at {self.url}: {exc}") from exc
        time.sleep(2)
        self.ctx.compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "localhost:9092",
                         "--delete", "--topic", r"lakebase\..*", check=False)

    def start(self):
        cfg = json.loads(CONFIG.read_text())["config"]
        cfg["slot.name"] = self.slot
        self.collector = KafkaCollector(f"localhost:{self.ctx.env.get('KAFKA_EXTERNAL_PORT', '29092')}",
                                        "lakebase.public.decisions", f"harness-{self.ctx.run_id}", parse_debezium)
        self.collector.start(self._add)
        self._http("PUT", f"/connectors/{self.connector}/config", cfg)

    def status(self):
        try:
            return self._http("GET", f"/connectors/{self.connector}/status")
        except (urllib.error.URLError, OSError):
            return None

    def kill(self):
        self.ctx.compose("kill", "debezium-connect")

    def restart(self):
        # the connector config and offsets live in Kafka, so the worker resumes the connector by itself
        self.ctx.compose("start", "debezium-connect")

    def shutdown(self):
        if self.collector:
            self.collector.stop()
        try:
            self._http("DELETE", f"/connectors/{self.connector}")
        except (urllib.error.URLError, OSError):
            pass
