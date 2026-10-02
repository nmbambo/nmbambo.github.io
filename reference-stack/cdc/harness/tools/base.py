"""Common interface for the CDC tools under test and the shared runtime context."""
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional

from core import Obs

STACK = pathlib.Path(__file__).resolve().parents[3]


class ToolNotConfigured(RuntimeError):
    """Raised when a tool needs manual setup that has not been done (e.g. Airbyte). Recorded as 'skipped'."""


class Ctx:
    def __init__(self, env: Dict[str, str], run_id: str):
        self.env, self.run_id, self.stack = env, run_id, STACK

    def compose(self, *args, profiles=(), check=True, capture=True, input=None, timeout=None):
        cmd = ["docker", "compose", "-f", str(self.stack / "docker-compose.yml")]
        for p in profiles:
            cmd += ["--profile", p]
        cmd += list(args)
        return subprocess.run(cmd, check=check, cwd=self.stack, capture_output=capture, text=True,
                              input=input, timeout=timeout)

    def image_tag(self, service: str) -> Optional[str]:
        text = (self.stack / "docker-compose.yml").read_text()
        m = re.search(r"^  %s:\n(?:    .*\n|\n)*?    image: (\S+)" % re.escape(service), text, re.M)
        return m.group(1) if m else None


class CdcTool:
    """Contract used by run.py. Implementations collect Obs in the background or inside pump()."""
    name = ""
    arrival_basis = ""
    containers: List[str] = []     # substrings of container names to sample with docker stats

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self._obs: List[Obs] = []
        self._lock = threading.Lock()

    # -- observation store --
    def _add(self, o: Optional[Obs]):
        if o is not None:
            with self._lock:
                self._obs.append(o)

    def observations(self) -> List[Obs]:
        with self._lock:
            return list(self._obs)

    # -- lifecycle (override) --
    def tool_version(self) -> Optional[str]:
        return None

    def reset(self):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def pump(self):
        """Called by the orchestrator while it waits. Continuous tools do nothing; batch tools run a sync cycle."""

    def kill(self):
        raise NotImplementedError

    def restart(self):
        raise NotImplementedError

    def shutdown(self):
        pass


class KafkaCollector:
    """Background confluent-kafka consumer that turns records into Obs with the harness's own arrival clock."""

    def __init__(self, bootstrap, topic, group, parse):
        from confluent_kafka import Consumer
        self.consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group, "auto.offset.reset": "earliest",
                                  "enable.auto.commit": False, "topic.metadata.refresh.interval.ms": 1000})
        self.topic, self.parse = topic, parse
        self.sink = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def start(self, sink):
        self.sink = sink
        self.consumer.subscribe([self.topic])
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            msg = self.consumer.poll(0.05)
            now = time.time()
            if msg is None or msg.error():
                continue
            self.sink(self.parse(msg.key(), msg.value(), now, (msg.partition(), msg.offset())))

    def stop(self):
        self._stop.set()
        self._t.join(timeout=3)
        self.consumer.close()
