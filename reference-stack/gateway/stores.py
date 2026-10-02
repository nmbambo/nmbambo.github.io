"""Event stores behind the gateway. Same interface for all three:

  append(stream, events, expected_version=None) -> {"appended": int, "positions": [int], "new": [envelope]}
  read(from_position=0, limit=1000) -> [envelope]       (position order, position >= from_position)
  read_stream(stream) -> [envelope]
  last_position() -> int                                 (-1 when empty)
  get(id) -> envelope | None

Semantics (differ deliberately from the browser MemoryStore in ONE way, see README):
  * Events whose id already exists are skipped (idempotent).
  * If EVERY event in a request already exists, the request is a no-op success and expectedVersion is not
    checked, so a client that retries after a lost response does not get a spurious 409.
  * Otherwise expectedVersion (-1 = stream must not exist) is checked against the stream version.
  * `position` is global and strictly increasing but NOT dense for KurrentDB (commit position) or
    Message DB (global_position - 1, gaps after rolled-back transactions). Clients must only compare/advance it.
"""
import json
import threading

from envelope import ConcurrencyError, from_stored, store_uuid, to_stored


class MemoryStore:
    """Reference semantics, dense positions. Used by the unit tests and STORE=memory (dev only)."""

    def __init__(self):
        self._events, self._ids, self._versions = [], {}, {}
        self._lock = threading.Lock()

    def append(self, stream, events, expected_version=None):
        with self._lock:
            fresh = [e for e in events if e["id"] not in self._ids]
            current = self._versions.get(stream, 0) - 1
            if fresh and expected_version is not None and expected_version != current:
                raise ConcurrencyError(
                    f'Stream "{stream}" is at version {current}, expected {expected_version}',
                    stream, expected_version, current)
            new = []
            for e in fresh:
                version = self._versions.get(stream, 0)
                stored = dict(e, position=len(self._events), version=version)
                self._events.append(stored)
                self._ids[e["id"]] = stored
                self._versions[stream] = version + 1
                new.append(json.loads(json.dumps(stored)))
            return {"appended": len(new), "positions": [n["position"] for n in new], "new": new}

    def read(self, from_position=0, limit=1000):
        return [dict(e) for e in self._events[max(0, from_position):][:limit]]

    def read_stream(self, stream):
        return [dict(e) for e in self._events if e["stream"] == stream]

    def last_position(self):
        return len(self._events) - 1

    def get(self, event_id):
        e = self._ids.get(event_id)
        return dict(e) if e else None


class KurrentStore:
    """KurrentDB via the official `kurrentdbclient` (PyPI; formerly esdbclient). gRPC, insecure local node.

    KurrentDB cannot look an event up by id, so the gateway keeps an in-process id index built lazily by
    scanning $all once (fine at reference scale; a single gateway instance is assumed). Appends are
    serialised by a lock so the index and the stream versions cannot race.
    """

    def __init__(self, client, kdb=None):
        self._c = client
        if kdb is None:
            import kurrentdbclient as kdb  # imported lazily so unit tests need no gRPC stack
        self._k = kdb
        self._lock = threading.Lock()
        self._index = None  # envelope id -> (stream, version)

    # --- helpers -------------------------------------------------------------------------------
    def _to_envelope(self, rec):
        try:
            data = json.loads(rec.data or b"{}")
            md = json.loads(rec.metadata or b"{}")
        except ValueError:
            return None
        return from_stored(type_=rec.type, stream=rec.stream_name, data=data, metadata=md,
                           position=rec.commit_position, version=rec.stream_position)

    def _stream_version(self, stream):
        v = self._c.get_current_version(stream)
        return -1 if v == self._k.StreamState.NO_STREAM else int(v)

    def _ensure_index(self):
        if self._index is None:
            idx = {}
            for rec in self._c.read_all():
                env = self._to_envelope(rec)
                if env:
                    idx[env["id"]] = (env["stream"], env["version"])
            self._index = idx
        return self._index

    # --- interface -----------------------------------------------------------------------------
    def append(self, stream, events, expected_version=None):
        with self._lock:
            idx = self._ensure_index()
            fresh = [e for e in events if e["id"] not in idx]
            if not fresh:
                return {"appended": 0, "positions": [], "new": []}
            current = self._stream_version(stream)
            if expected_version is not None and expected_version != current:
                raise ConcurrencyError(
                    f'Stream "{stream}" is at version {current}, expected {expected_version}',
                    stream, expected_version, current)
            new_events = []
            for e in fresh:
                data, md = to_stored(e)
                new_events.append(self._k.NewEvent(
                    type=e["type"], data=json.dumps(data).encode(), metadata=json.dumps(md).encode(),
                    id=store_uuid(e["id"])))
            # Optimistic concurrency done by us above, enforced by the server too: pass the exact version.
            expect = self._k.StreamState.NO_STREAM if current == -1 else current
            try:
                self._c.append_to_stream(stream, events=new_events, current_version=expect)
            except self._k.exceptions.WrongCurrentVersionError as exc:
                raise ConcurrencyError(str(exc), stream, expected_version, current) from exc
            written = self._c.get_stream(stream, stream_position=current + 1, limit=len(new_events))
            new = []
            for rec in written:
                env = self._to_envelope(rec)
                if env:
                    idx[env["id"]] = (env["stream"], env["version"])
                    new.append(env)
            return {"appended": len(new), "positions": [n["position"] for n in new], "new": new}

    def read(self, from_position=0, limit=1000):
        out = []
        kwargs = {"commit_position": from_position} if from_position > 0 else {}
        for rec in self._c.read_all(**kwargs):
            if rec.commit_position < from_position:
                continue  # read_all may include the boundary event; positions are inclusive-from
            env = self._to_envelope(rec)
            if env:
                out.append(env)
                if len(out) >= limit:
                    break
        return out

    def read_stream(self, stream):
        try:
            recs = self._c.get_stream(stream)
        except self._k.exceptions.NotFoundError:
            return []
        return [e for e in (self._to_envelope(r) for r in recs) if e]

    def last_position(self):
        for rec in self._c.read_all(backwards=True, limit=1):
            env = self._to_envelope(rec)
            if env:
                return env["position"]
        return -1

    def get(self, event_id):
        loc = self._ensure_index().get(event_id)
        if not loc:
            return None
        stream, version = loc
        for rec in self._c.get_stream(stream, stream_position=version, limit=1):
            return self._to_envelope(rec)
        return None


class MessageDbStore:
    """Message DB (Postgres). Uses message_store.write_message() for writes, as the project intends.

    * write_message requires `id` to be a UUID and is NOT idempotent (duplicate id -> unique violation),
      so we pre-check and also treat a unique violation as 'already stored'.
    * write_message raises 'Wrong expected version: ...' when expected_version mismatches.
    * Reads query message_store.messages directly (get_stream_messages refuses names with no '-').
    * position = global_position - 1 (global_position starts at 1).
    """

    SELECT = ("SELECT id::text, type, stream_name, position, global_position, data::text, metadata::text "
              "FROM message_store.messages ")

    def __init__(self, dsn, connect=None):
        if connect is None:
            import psycopg

            def connect():
                return psycopg.connect(dsn, autocommit=False)
        self._connect = connect
        self._conn = None
        self._lock = threading.Lock()

    def _db(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn

    @staticmethod
    def _row(r):
        id_, type_, stream, pos, gpos, data, md = r
        return from_stored(type_=type_, stream=stream, data=json.loads(data), metadata=json.loads(md or "{}"),
                           position=gpos - 1, version=pos)

    def append(self, stream, events, expected_version=None):
        with self._lock:
            db = self._db()
            try:
                new, first = [], True
                with db.cursor() as cur:
                    for e in events:
                        uid = str(store_uuid(e["id"]))
                        cur.execute("SELECT 1 FROM message_store.messages WHERE id = %s::uuid", (uid,))
                        if cur.fetchone():
                            continue
                        data, md = to_stored(e)
                        exp = expected_version if (first and expected_version is not None) else None
                        cur.execute("SELECT message_store.write_message(%s, %s, %s, %s::jsonb, %s::jsonb, %s)",
                                    (uid, stream, e["type"], json.dumps(data), json.dumps(md), exp))
                        first = False
                        cur.execute(self.SELECT + "WHERE id = %s::uuid", (uid,))
                        new.append(self._row(cur.fetchone()))
                db.commit()
                return {"appended": len(new), "positions": [n["position"] for n in new], "new": new}
            except Exception as exc:  # noqa: BLE001 - classify then re-raise
                db.rollback()
                if "Wrong expected version" in str(exc):
                    raise ConcurrencyError(str(exc).splitlines()[0], stream, expected_version) from exc
                raise

    def _query(self, sql, params=()):
        with self._lock:
            db = self._db()
            try:
                with db.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                db.commit()
                return rows
            except Exception:
                db.rollback()
                raise

    def read(self, from_position=0, limit=1000):
        rows = self._query(self.SELECT + "WHERE global_position >= %s ORDER BY global_position LIMIT %s",
                           (from_position + 1, limit))
        return [e for e in (self._row(r) for r in rows) if e]

    def read_stream(self, stream):
        rows = self._query(self.SELECT + "WHERE stream_name = %s ORDER BY position", (stream,))
        return [e for e in (self._row(r) for r in rows) if e]

    def last_position(self):
        rows = self._query("SELECT coalesce(max(global_position), 0) FROM message_store.messages")
        return int(rows[0][0]) - 1

    def get(self, event_id):
        rows = self._query(self.SELECT + "WHERE id = %s::uuid", (str(store_uuid(event_id)),))
        return self._row(rows[0]) if rows else None
