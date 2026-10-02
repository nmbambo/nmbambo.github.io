"""Airbyte (installed separately with abctl) -> Kafka destination, observed from the Kafka topic.

NOT RUNNABLE WITHOUT MANUAL SETUP and UNVERIFIED: see cdc/airbyte/README.md. The harness only triggers syncs and
reads the destination topic. Needs, in the environment or cdc/airbyte/airbyte.local.env:
  AIRBYTE_API_URL        default http://localhost:8000/api/public
  AIRBYTE_CLIENT_ID / AIRBYTE_CLIENT_SECRET   from `abctl local credentials`
  AIRBYTE_CONNECTION_ID  Postgres(CDC) -> Kafka connection you created in the UI
  AIRBYTE_TOPIC          default airbyte.decisions   (destination topic_pattern: airbyte.{stream})
Process-level kill/restart of Airbyte's workers is not scripted; those scenarios are reported as skipped.
"""
import json
import pathlib
import time
import urllib.request

from core import parse_airbyte_record
from tools.base import CdcTool, KafkaCollector, ToolNotConfigured


class Airbyte(CdcTool):
    name = "airbyte"
    arrival_basis = "kafka consumer receive time after a triggered batch sync (batch tool)"
    containers: list = []   # abctl runs inside a kind cluster container; sample it by name if you want footprint
    supports_kill = False

    def __init__(self, ctx, interval=10.0):
        super().__init__(ctx)
        env = dict(ctx.env)
        p = pathlib.Path(__file__).resolve().parents[2] / "airbyte" / "airbyte.local.env"
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        self.api = env.get("AIRBYTE_API_URL", "http://localhost:8000/api/public").rstrip("/")
        self.cid, self.secret = env.get("AIRBYTE_CLIENT_ID"), env.get("AIRBYTE_CLIENT_SECRET")
        self.connection = env.get("AIRBYTE_CONNECTION_ID")
        self.topic = env.get("AIRBYTE_TOPIC", "airbyte.decisions")
        self.bootstrap = f"localhost:{env.get('KAFKA_EXTERNAL_PORT', '29092')}"
        self.interval, self.next_run, self.token, self.token_at = interval, 0.0, None, 0.0
        self.collector = None
        if not (self.cid and self.secret and self.connection):
            raise ToolNotConfigured("Airbyte is not configured: set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET, "
                                    "AIRBYTE_CONNECTION_ID (see cdc/airbyte/README.md)")

    def _call(self, method, path, body=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.token or time.time() - self.token_at > 120:
                t = self._call("POST", "/v1/applications/token", {"client_id": self.cid, "client_secret": self.secret}, auth=False)
                self.token, self.token_at = t["access_token"], time.time()
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.api + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def reset(self):
        pass  # Airbyte state is managed in its UI/API; reset the connection there before a measured run

    def start(self):
        self.collector = KafkaCollector(self.bootstrap, self.topic, f"harness-{self.ctx.run_id}",
                                        lambda k, v, now, order: parse_airbyte_record(v, now, order) if v else None)
        self.collector.start(self._add)
        self._trigger()

    def _trigger(self):
        self._call("POST", "/v1/jobs", {"connectionId": self.connection, "jobType": "sync"})
        self.next_run = time.time() + self.interval

    def pump(self):
        if time.time() >= self.next_run:
            try:
                self._trigger()
            except Exception:  # a sync may already be running (HTTP 4xx); try again next interval
                self.next_run = time.time() + self.interval

    def kill(self):
        raise ToolNotConfigured("kill/restart of Airbyte workers is not scripted")

    restart = kill

    def shutdown(self):
        if self.collector:
            self.collector.stop()
