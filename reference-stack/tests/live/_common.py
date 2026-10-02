"""Shared helpers for the live checks. Standard library only. Run from anywhere: paths are resolved here.

These scripts talk to real containers (docker compose, project in reference-stack/). They create their own
uniquely-named streams/docs per run, so they can be re-run without cleaning up, and they use synthetic data only.
"""
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
STACK = HERE.parent.parent            # reference-stack/
REPO = STACK.parent                   # site root
OUT = HERE / "out"                    # git-ignored
OUT.mkdir(exist_ok=True)


def load_env():
    env = {}
    p = STACK / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
GATEWAY = f"http://localhost:{ENV.get('GATEWAY_PORT', '8088')}"
REST = f"http://localhost:{ENV.get('KAFKA_REST_PORT', '8082')}"
TOPIC = "ingqiqo.events"
LAKEBASE_USER = ENV.get("LAKEBASE_USER", "lakebase")


def compose(*args, env=None, check=True, timeout=600, capture=True):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(["docker", "compose", *args], cwd=STACK, env=e, text=True, timeout=timeout,
                       capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {(r.stderr or '')[-800:]}")
    return r


def http(method, url, body=None, headers=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    h = {"Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode(errors="replace")
        return exc.code, parsed, dict(exc.headers)


def wait_until(fn, what, timeout=90, every=1.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


def run_id():
    return uuid.uuid4().hex[:8]


def event(stream, type_, data, eid=None, source="live-test"):
    return {"id": eid or str(uuid.uuid4()), "type": type_, "stream": stream, "data": data,
            "meta": {"ts": "2026-10-02T00:00:00Z", "schema": 1, "source": source}}


def sha_id(*parts):
    import hashlib
    return hashlib.sha256("␟".join(parts).encode()).hexdigest()


def psql(sql, db="projections"):
    r = compose("exec", "-T", "postgres-lakebase", "psql", "-U", LAKEBASE_USER, "-d", db,
                "-At", "-F", "|", "-c", sql)
    return [line for line in r.stdout.splitlines() if line != ""]


def kafka_end_offsets(topic=TOPIC):
    r = compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-get-offsets.sh",
                "--bootstrap-server", "localhost:9092", "--topic", topic)
    out = {}
    for line in r.stdout.splitlines():
        t, p, o = line.strip().split(":")
        out[int(p)] = int(o)
    return out


def kafka_consume_all(topic=TOPIC, timeout_ms=10000):
    """Read a topic from the beginning with the stock console consumer. -> [(partition, key, value-dict|raw)]"""
    r = compose("exec", "-T", "kafka", "/opt/kafka/bin/kafka-console-consumer.sh",
                "--bootstrap-server", "localhost:9092", "--topic", topic, "--from-beginning",
                "--timeout-ms", str(timeout_ms), "--property", "print.key=true",
                "--property", "print.partition=true", "--property", "key.separator=\t",
                check=False, timeout=timeout_ms // 1000 + 60)
    rows = []
    for line in r.stdout.splitlines():
        if not line.startswith("Partition:"):
            continue
        head, _, rest = line.partition("\t")
        key, _, val = rest.partition("\t")
        try:
            val = json.loads(val)
        except ValueError:
            pass
        rows.append((int(head.split(":")[1]), key, val))
    return rows


class Checks:
    def __init__(self, name):
        self.name, self.results = name, []

    def check(self, label, ok, detail=""):
        self.results.append({"check": label, "ok": bool(ok), "detail": str(detail)})
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail != "" else ""), flush=True)
        return bool(ok)

    @property
    def ok(self):
        return all(r["ok"] for r in self.results)

    def finish(self, extra=None):
        doc = {"script": self.name, "ok": self.ok, "date": time.strftime("%Y-%m-%d"), "checks": self.results}
        doc.update(extra or {})
        (OUT / f"{self.name}.json").write_text(json.dumps(doc, indent=2))
        print(f"{self.name}: {'PASS' if self.ok else 'FAIL'} ({sum(r['ok'] for r in self.results)}/{len(self.results)})")
        return 0 if self.ok else 1
