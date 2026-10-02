"""Publish appended events to Kafka through the Confluent REST Proxy v3 (stdlib only).

POST {rest}/v3/clusters/{cluster_id}/topics/{topic}/records
  {"key": {"type": "STRING", "data": <stream>}, "value": {"type": "JSON", "data": <envelope>}}
Keyed by stream, so Kafka keeps per-stream order. The v3 API answers HTTP 200 even when a record failed,
so the body's error_code MUST be checked (Confluent docs say the same).

Delivery: best-effort with retries, then an in-memory retry queue drained by a background thread.
This is NOT a transactional outbox (a crash can lose queued items). The projector is idempotent, so
duplicates are harmless; for no-loss in production use KurrentDB/Message DB subscriptions or the
outbox pattern with Debezium. See README.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("gateway.publisher")


class NullPublisher:
    pending = 0
    cluster_id = None

    def publish(self, events):
        return 0


def build_record(event: dict) -> dict:
    return {"key": {"type": "STRING", "data": event["stream"]},
            "value": {"type": "JSON", "data": event}}


def record_failed(status: int, body: dict) -> bool:
    """True when a v3 produce response indicates failure (HTTP error, or error_code != 200 in a 200 body)."""
    if status >= 300:
        return True
    code = body.get("error_code") if isinstance(body, dict) else None
    return code is not None and code != 200


class KafkaRestPublisher:
    def __init__(self, rest_url, topic="ingqiqo.events", cluster_id=None, timeout=5.0, retries=2, opener=None):
        self.rest_url = rest_url.rstrip("/")
        self.topic = topic
        self.cluster_id = cluster_id
        self.timeout, self.retries = timeout, retries
        self._open = opener or urllib.request.urlopen
        self._queue, self._lock = [], threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain_loop, daemon=True)

    @property
    def pending(self):
        return len(self._queue)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _request(self, method, url, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except ValueError:
                return exc.code, {}

    def resolve_cluster_id(self):
        if not self.cluster_id:
            status, body = self._request("GET", f"{self.rest_url}/v3/clusters")
            if status != 200 or not body.get("data"):
                raise RuntimeError(f"cannot resolve Kafka cluster id (HTTP {status})")
            self.cluster_id = body["data"][0]["cluster_id"]
        return self.cluster_id

    def _send(self, event):
        cid = self.resolve_cluster_id()
        url = f"{self.rest_url}/v3/clusters/{cid}/topics/{self.topic}/records"
        status, body = self._request("POST", url, build_record(event))
        if record_failed(status, body):
            raise RuntimeError(f"produce failed: HTTP {status} error_code={body.get('error_code')}")

    def _send_with_retries(self, event):
        last = None
        for attempt in range(self.retries + 1):
            try:
                self._send(event)
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.2 * (attempt + 1))
        log.warning("publish failed for %s (queued for retry): %s", event.get("id"), last)
        return False

    def publish(self, events):
        """Returns the number of events that could not be sent immediately (now queued)."""
        failed = 0
        for e in events:
            if not self._send_with_retries(e):
                failed += 1
                with self._lock:
                    self._queue.append(e)
        return failed

    def _drain_loop(self):
        while not self._stop.wait(2.0):
            with self._lock:
                batch, self._queue = self._queue, []
            for e in batch:
                try:
                    self._send(e)
                except Exception:  # noqa: BLE001
                    with self._lock:
                        self._queue.append(e)
