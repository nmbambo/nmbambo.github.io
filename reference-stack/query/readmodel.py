"""The embedded search read model: SQLite (standard library) with FTS5.

Tables
  docs        one row per document id: raw fields for display, the order key of the last event applied (`ev_pos`) and its id,
              and a tombstone flag. A delete keeps the row (deleted=1, position kept), so a late older event cannot resurrect it.
  fts         FTS5, contentless (contentless_delete=1), rowid = docs.id. Columns title, section, text, tags, type, source hold
              the ANALYSED text (stems, see analysis.py), tokenizer unicode61 with diacritics removed. bm25() ranks.
  fts_tri     FTS5 with the `trigram` tokenizer over the folded title/section/text, for substring search (algorithm=substring).
  fts_row / fts_inst   fts5vocab views of `fts`: the vocabulary (fuzzy, prefix) and term positions (phrase proximity).
  events      the log as projected: position, id (UNIQUE: this is the processed-id set), stream, version, type, data, meta.
  checkpoint  per consumer the last stream sequence applied, written in the SAME transaction as the changes it covers.

Idempotency at this hop (the others are publish dedupe, expected sequence, and the id set in `events`):
  * an event whose id is already in `events` is a duplicate: no write at all;
  * a document write is applied only if its order key is newer than the stored one (ES: log position; CDC: source.lsn);
    equal or older is dropped as duplicate/stale. Upsert and delete are the same compare-and-set.
"""
import json
import sqlite3
import threading
import time

from analysis import analyze_column, fold
from mapping import Delete, Upsert, cdc_ops, envelope_ops

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT NOT NULL UNIQUE,
  src TEXT NOT NULL,
  ev_pos INTEGER NOT NULL,
  ev_id TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  title TEXT, section TEXT, text TEXT, tags TEXT, type TEXT, source TEXT, url TEXT,
  applied_ms INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  title, section, text, tags, type, source,
  content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_row  USING fts5vocab(fts, 'row');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_inst USING fts5vocab(fts, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(
  title, section, text, content='', contentless_delete=1, tokenize='trigram');
CREATE TABLE IF NOT EXISTS events(
  position INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  stream TEXT NOT NULL, version INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoint(consumer TEXT PRIMARY KEY, seq INTEGER NOT NULL, updated_ms INTEGER NOT NULL);
"""
FTS_COLUMNS = ("title", "section", "text", "tags", "type", "source")


def now_ms():
    return int(time.time() * 1000)


class ReadModel:
    def __init__(self, path=":memory:"):
        self.path = path
        self._wlock = threading.RLock()
        self._local = threading.local()
        self.version = 0                     # bumped on every change; readers use it to invalidate the vocabulary cache
        self.counters = {}
        uri = path == ":memory:"
        if uri:                              # shared in-memory database so reader threads see the writer (tests)
            self.path = "file:rm%x?mode=memory&cache=shared" % id(self)
        self._w = self._connect()
        self._w.executescript(SCHEMA)
        self._keepalive = self._w

    # -- connections
    def _connect(self):
        c = sqlite3.connect(self.path, uri=self.path.startswith("file:"), check_same_thread=False, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        if not self.path.startswith("file:"):
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def reader(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._connect()
        return c

    def bump(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    # -- checkpoints
    def checkpoint(self, consumer):
        r = self._w.execute("SELECT seq FROM checkpoint WHERE consumer=?", (consumer,)).fetchone()
        return r["seq"] if r else 0

    def has_data(self, src):
        return self._w.execute("SELECT 1 FROM docs WHERE src=? LIMIT 1", (src,)).fetchone() is not None

    def wipe_source(self, src, consumer):
        """The log under a consumer was recreated (its checkpoint is beyond the stream): forget what that source derived."""
        with self._wlock:
            self._w.execute("BEGIN IMMEDIATE")
            try:
                for r in self._w.execute("SELECT id FROM docs WHERE src=?", (src,)).fetchall():
                    self._w.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
                    self._w.execute("DELETE FROM fts_tri WHERE rowid=?", (r["id"],))
                self._w.execute("DELETE FROM docs WHERE src=?", (src,))
                if src == "es":
                    self._w.execute("DELETE FROM events")
                self._w.execute("DELETE FROM checkpoint WHERE consumer=?", (consumer,))
                self._w.execute("COMMIT")
            except Exception:
                self._w.execute("ROLLBACK")
                raise
            self.version += 1

    # -- applying a batch: one transaction, checkpoint inside it
    def apply_batch(self, consumer, messages):
        """messages: [(seq, subject, data_bytes, headers)] in stream order. Returns the per-status counts of this batch."""
        res = {}
        if not messages:
            return res
        with self._wlock:
            self._w.execute("BEGIN IMMEDIATE")
            try:
                for seq, subject, data, headers in messages:
                    for status in self._apply_message(consumer, seq, subject, data, headers):
                        res[status] = res.get(status, 0) + 1
                self._w.execute("INSERT INTO checkpoint(consumer, seq, updated_ms) VALUES(?,?,?) "
                                "ON CONFLICT(consumer) DO UPDATE SET seq=excluded.seq, updated_ms=excluded.updated_ms",
                                (consumer, messages[-1][0], now_ms()))
                self._w.execute("COMMIT")
            except Exception:
                self._w.execute("ROLLBACK")
                raise
            self.version += 1
        for k, v in res.items():
            self.bump(f"{consumer}.{k}", v)
        return res

    def _apply_message(self, consumer, seq, subject, data, headers):
        if consumer == "es":
            return self._apply_es(seq, data, headers)
        return [self._apply_op(op) for op in cdc_ops(subject, data)] or ["ignored"]

    def _apply_es(self, seq, data, headers):
        try:
            env = json.loads(data)
        except (ValueError, TypeError):
            return ["ignored"]
        position = seq - 1
        try:
            self._w.execute("INSERT INTO events(position, id, stream, version, type, data, meta) VALUES(?,?,?,?,?,?,?)",
                            (position, env["id"], env["stream"], int((headers or {}).get("Ingqiqo-Version", 0)), env["type"],
                             json.dumps(env.get("data", {}), separators=(",", ":")), json.dumps(env.get("meta", {}), separators=(",", ":"))))
        except sqlite3.IntegrityError:           # id (or position) already processed
            return ["duplicate"]
        except KeyError:
            return ["ignored"]
        ops = envelope_ops(env, position)
        return [self._apply_op(op) for op in ops] or ["event"]

    def _apply_op(self, op):
        w = self._w
        row = w.execute("SELECT id, ev_pos, ev_id, deleted FROM docs WHERE doc_id=?", (op.doc_id,)).fetchone()
        if row and op.pos <= row["ev_pos"]:
            return "duplicate" if op.pos == row["ev_pos"] and op.event_id == row["ev_id"] else "stale"
        if isinstance(op, Delete):
            if row:
                if not row["deleted"]:
                    self._unindex(row["id"])
                w.execute("UPDATE docs SET deleted=1, ev_pos=?, ev_id=?, title=NULL, section=NULL, text=NULL, tags=NULL, type=NULL, "
                          "source=NULL, url=NULL, applied_ms=? WHERE id=?", (op.pos, op.event_id, now_ms(), row["id"]))
            else:                                # remember the delete so an older upsert that arrives later is dropped
                w.execute("INSERT INTO docs(doc_id, src, ev_pos, ev_id, deleted, applied_ms) VALUES(?,?,?,?,1,?)",
                          (op.doc_id, op.src, op.pos, op.event_id, now_ms()))
            return "deleted"
        f = op.fields
        vals = (op.src, op.pos, op.event_id, f["title"], f["section"], f["text"], json.dumps(f["tags"]), f["type"], f["source"], f["url"], now_ms())
        if row:
            if not row["deleted"]:
                self._unindex(row["id"])
            w.execute("UPDATE docs SET src=?, ev_pos=?, ev_id=?, deleted=0, title=?, section=?, text=?, tags=?, type=?, source=?, "
                      "url=?, applied_ms=? WHERE id=?", (*vals, row["id"]))
            rid = row["id"]
        else:
            cur = w.execute("INSERT INTO docs(doc_id, src, ev_pos, ev_id, deleted, title, section, text, tags, type, source, url, applied_ms) "
                            "VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?)", (op.doc_id, *vals[:3], *vals[3:]))
            rid = cur.lastrowid
        self._index(rid, f)
        return "applied"

    def _index(self, rid, f):
        self._w.execute("INSERT INTO fts(rowid, title, section, text, tags, type, source) VALUES(?,?,?,?,?,?,?)",
                        (rid, analyze_column(f["title"]), analyze_column(f["section"]), analyze_column(f["text"]),
                         analyze_column(f["tags"]), analyze_column(f["type"]), analyze_column(f["source"])))
        self._w.execute("INSERT INTO fts_tri(rowid, title, section, text) VALUES(?,?,?,?)",
                        (rid, fold(f["title"]), fold(f["section"]), fold(f["text"])))

    def _unindex(self, rid):
        self._w.execute("DELETE FROM fts WHERE rowid=?", (rid,))
        self._w.execute("DELETE FROM fts_tri WHERE rowid=?", (rid,))

    # -- reads used by the HTTP layer
    def events(self, from_position=0, limit=1000):
        rows = self.reader().execute("SELECT * FROM events WHERE position >= ? ORDER BY position LIMIT ?", (max(from_position, 0), limit))
        return [{"id": r["id"], "type": r["type"], "stream": r["stream"], "data": json.loads(r["data"]), "meta": json.loads(r["meta"]),
                 "position": r["position"], "version": r["version"]} for r in rows]

    def stats(self):
        c = self.reader()
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {"docs": one("SELECT count(*) FROM docs WHERE deleted=0"), "tombstones": one("SELECT count(*) FROM docs WHERE deleted=1"),
                "events": one("SELECT count(*) FROM events"),
                "checkpoints": {r["consumer"]: r["seq"] for r in c.execute("SELECT consumer, seq FROM checkpoint")},
                "counters": dict(self.counters)}

    def dump(self):
        """Canonical content of the read model, for 'replay is identical' checks: documents (live and tombstones) with their order
        keys, the events, and the FTS5 vocabulary with document counts."""
        c = self.reader()
        docs = [dict(r) for r in c.execute("SELECT doc_id, src, ev_pos, ev_id, deleted, title, section, text, tags, type, source, url "
                                           "FROM docs ORDER BY doc_id")]
        events = [tuple(r) for r in c.execute("SELECT position, id, stream, version, type, data FROM events ORDER BY position")]
        vocab = [tuple(r) for r in c.execute("SELECT term, doc, cnt FROM fts_row ORDER BY term")]
        return {"docs": docs, "events": events, "vocab": vocab}

    def close(self):
        try:
            self._w.close()
        except sqlite3.Error:
            pass
