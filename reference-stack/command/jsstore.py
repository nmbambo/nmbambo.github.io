"""The event store on NATS JetStream (light stack). One stream `ES`, subjects `es.<stream>`.

  global position  = JetStream stream sequence - 1            (0-based, dense, monotonic: CONTRACT section 1)
  stream version   = header `Ingqiqo-Version` (0-based count) (JetStream's own per-subject number is a *stream sequence*,
                     not a count, so the count lives in a header and the sequence is used as the compare-and-set token)
  event id         = `Nats-Msg-Id` (JetStream de-duplicates within the stream's duplicate window, here 24 h)
  expectedVersion  = optimistic concurrency: read the subject's last message (sequence S, version V); refuse (409) unless
                     V == expectedVersion; publish with `Nats-Expected-Last-Subject-Sequence: S`, so a concurrent writer
                     that got in between makes the broker itself reject ours ("wrong last sequence").

The stream is created with deny_delete and deny_purge: append-only is enforced by the broker, not only by convention.

Store interface (same as gateway/stores.py, synchronous, so server.py is shared in shape):
  append(stream, events, expected_version=None) -> {"appended", "positions", "new", "skipped"}
  read(from_position, limit) / read_stream(stream) / last_position() / get(id)

Two layers: `NatsLog` (the only code that touches nats-py; async) and `JetStreamStore` (the semantics above, sync facade
over an asyncio loop in a background thread). Unit tests drive JetStreamStore with an in-memory log.
"""
import asyncio
import json
import logging
import urllib.parse
from collections import namedtuple

from envelope import ConcurrencyError

log = logging.getLogger("command.jsstore")

ES_STREAM, ES_PREFIX = "ES", "es"
CDC_STREAM, CDC_SUBJECTS = "CDC", ["lakebase.>"]
KV_CHECKPOINTS = "checkpoints"
H_ID, H_VERSION, H_EXPECT = "Nats-Msg-Id", "Ingqiqo-Version", "Nats-Expected-Last-Subject-Sequence"
DUPLICATE_WINDOW_S = 24 * 3600

Msg = namedtuple("Msg", "seq subject data headers")


class WrongLastSequence(Exception):
    """The broker refused our Nats-Expected-Last-Subject-Sequence (someone else appended to the subject)."""


# --------------------------------------------------------------------------------------------------- subject encoding
_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-#/:@=+,~")


def subject_for(stream: str, prefix: str = ES_PREFIX) -> str:
    """`content-booklet/seeing-clearly/p13` -> `es.content-booklet/seeing-clearly/p13`. A NATS subject is dot-separated
    tokens and may not contain whitespace, `*`, `>` or control characters; every other character outside the safe set,
    including `.`, is percent-encoded so that one envelope stream is always exactly ONE subject token."""
    out = []
    for ch in stream:
        out.append(ch if ch in _SAFE else "".join(f"%{b:02X}" for b in ch.encode("utf-8")))
    return f"{prefix}.{''.join(out)}"


def stream_from_subject(subject: str, prefix: str = ES_PREFIX) -> str:
    return urllib.parse.unquote(subject[len(prefix) + 1:])


def to_envelope(msg: Msg) -> dict:
    ev = json.loads(msg.data)
    ev["position"] = msg.seq - 1
    ev["version"] = int((msg.headers or {}).get(H_VERSION, 0))
    return ev


# ----------------------------------------------------------------------------------------------------- the nats-py layer
class NatsLog:
    """JetStream calls needed by the store, over nats-py. Async. Not unit-tested (needs a server): covered live."""

    def __init__(self, nc, js, stream=ES_STREAM):
        self.nc, self.js, self.stream = nc, js, stream

    @staticmethod
    def _msg(raw):
        return Msg(raw.seq, raw.subject, raw.data, dict(raw.headers or {}))

    async def publish(self, subject, data, headers):
        from nats.js.errors import APIError
        try:
            ack = await self.js.publish(subject, data, headers=headers, stream=self.stream, timeout=10)
        except APIError as exc:
            if getattr(exc, "err_code", None) == 10071:
                raise WrongLastSequence(str(exc)) from exc
            raise
        return ack.seq, bool(ack.duplicate)

    async def last_for_subject(self, subject):
        from nats.js.errors import NotFoundError
        try:
            return self._msg(await self.js.get_last_msg(self.stream, subject, direct=True))
        except NotFoundError:
            return None

    async def get(self, seq):
        from nats.js.errors import NotFoundError
        try:
            return self._msg(await self.js.get_msg(self.stream, seq=seq, direct=True))
        except NotFoundError:
            return None

    async def next_for_subject(self, subject, seq):
        from nats.js.errors import NotFoundError
        try:
            return self._msg(await self.js.get_msg(self.stream, seq=seq, subject=subject, direct=True, next=True))
        except NotFoundError:
            return None

    async def last_seq(self):
        return (await self.js.stream_info(self.stream)).state.last_seq

    async def scan_headers(self, upto):
        """Yield (seq, headers) for every message up to `upto` (inclusive), headers only (no payloads)."""
        if upto <= 0:
            return
        from nats.js.api import ConsumerConfig
        sub = await self.js.subscribe(f"{ES_PREFIX}.>", stream=self.stream, ordered_consumer=True,
                                      config=ConsumerConfig(headers_only=True))
        try:
            while True:
                m = await sub.next_msg(timeout=10)
                seq = m.metadata.sequence.stream
                yield seq, dict(m.headers or {})
                if seq >= upto:
                    return
        finally:
            await sub.unsubscribe()


async def provision(js, *, es_stream=ES_STREAM, cdc_stream=CDC_STREAM, cdc_subjects=None, duplicate_window=DUPLICATE_WINDOW_S):
    """Idempotently create the ES stream, the CDC stream (Debezium's landing zone) and the checkpoint KV bucket."""
    from nats.js.api import KeyValueConfig, StorageType, StreamConfig
    from nats.js.errors import BucketNotFoundError, NotFoundError
    for cfg in (
        StreamConfig(name=es_stream, subjects=[f"{ES_PREFIX}.>"], storage=StorageType.FILE, num_replicas=1,
                     duplicate_window=duplicate_window, allow_direct=True, deny_delete=True, deny_purge=True,
                     description="Ingqiqo event store: one subject per event stream, event id = Nats-Msg-Id"),
        StreamConfig(name=cdc_stream, subjects=cdc_subjects or CDC_SUBJECTS, storage=StorageType.FILE, num_replicas=1,
                     allow_direct=True, description="Debezium Server change events (lakebase.<schema>.<table>)"),
    ):
        try:
            await js.stream_info(cfg.name)
            await js.update_stream(cfg)          # converge on the committed config (idempotent)
        except NotFoundError:
            await js.add_stream(cfg)
    try:
        await js.key_value(KV_CHECKPOINTS)
    except (BucketNotFoundError, NotFoundError):
        await js.create_key_value(config=KeyValueConfig(bucket=KV_CHECKPOINTS, history=1, storage=StorageType.FILE,
                                                        description="projector/query-service checkpoints"))


# ----------------------------------------------------------------------------------------------------- store semantics
class JetStreamStore:
    def __init__(self, logger, *, loop=None, timeout=15):
        self._log, self._timeout = logger, timeout
        self._ids = {}                     # envelope id -> stream sequence
        self._lock = None                  # asyncio.Lock, created on the loop: one writer per process (the broker's CAS covers others)
        self._loop = loop
        self.ready = False

    # -- sync <-> async bridge
    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout)

    async def load_index(self):
        """Rebuild id -> sequence from the log (headers only). Called once at start-up, before /health turns ok."""
        last = await self._log.last_seq()
        n = 0
        async for seq, headers in self._log.scan_headers(last):
            mid = headers.get(H_ID)
            if mid:
                self._ids[mid] = seq
                n += 1
        self.ready = True
        return n

    # -- writes
    def append(self, stream, events, expected_version=None):
        return self._run(self._append(stream, events, expected_version))

    async def _append(self, stream, events, expected):
        subject = subject_for(stream)
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            fresh = [e for e in events if e["id"] not in self._ids]
            skipped = len(events) - len(fresh)
            if not fresh:                      # every id already stored: a retry after a lost response is a no-op success
                return {"appended": 0, "positions": [], "new": [], "skipped": skipped}
            last = await self._log.last_for_subject(subject)
            last_seq = last.seq if last else 0
            version = int(last.headers.get(H_VERSION, 0)) if last else -1
            if expected is not None and expected != version:
                raise ConcurrencyError(f'Stream "{stream}" is at version {version}, expected {expected}', stream, expected, version)
            new, positions = [], []
            for ev in fresh:
                version += 1
                headers = {H_ID: ev["id"], H_VERSION: str(version), H_EXPECT: str(last_seq)}
                payload = json.dumps(ev, separators=(",", ":")).encode()
                try:
                    seq, dup = await self._log.publish(subject, payload, headers)
                except WrongLastSequence:
                    actual = await self._actual_version(subject)
                    raise ConcurrencyError(f'Stream "{stream}" changed while appending (now at version {actual})',
                                           stream, expected, actual) from None
                self._ids[ev["id"]] = seq
                if dup:                        # JetStream's own de-duplication caught it (id seen by another process)
                    version -= 1
                    skipped += 1
                    continue
                last_seq = seq
                positions.append(seq - 1)
                new.append(dict(ev, position=seq - 1, version=version))
            return {"appended": len(new), "positions": positions, "new": new, "skipped": skipped}

    async def _actual_version(self, subject):
        last = await self._log.last_for_subject(subject)
        return int(last.headers.get(H_VERSION, 0)) if last else -1

    # -- reads
    def read(self, from_position=0, limit=1000):
        return self._run(self._read(from_position, limit))

    async def _read(self, from_position, limit):
        last = await self._log.last_seq()
        start = max(from_position, 0) + 1
        if start > last or limit < 1:
            return []
        out = []
        for lo in range(start, min(last, start + limit - 1) + 1, 200):      # chunks of 200 concurrent direct gets
            hi = min(last, start + limit - 1, lo + 199)
            msgs = await asyncio.gather(*(self._log.get(s) for s in range(lo, hi + 1)))
            out.extend(to_envelope(m) for m in msgs if m)
        return out

    def read_stream(self, stream):
        return self._run(self._read_stream(stream))

    async def _read_stream(self, stream):
        subject, seq, out = subject_for(stream), 1, []
        while True:
            m = await self._log.next_for_subject(subject, seq)
            if not m:
                return out
            out.append(to_envelope(m))
            seq = m.seq + 1

    def last_position(self):
        return self._run(self._log.last_seq()) - 1

    def get(self, event_id):
        seq = self._ids.get(event_id)
        if not seq:
            return None
        m = self._run(self._log.get(seq))
        return to_envelope(m) if m else None

    def indexed(self):
        return len(self._ids)
