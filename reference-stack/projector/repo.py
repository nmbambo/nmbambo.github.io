"""Postgres repo for the projector (psycopg 3) and an in-memory twin used by the tests.

Both implement the same contract; the SQL below mirrors the in-memory rules:
  * UpsertDoc / RemoveDoc only win if their stream version is not older than what is stored
    (guards against out-of-order delivery across restarts); versions of None always apply.
  * The checkpoint is monotonic (GREATEST).
"""
from contextlib import contextmanager

from logic import BumpStat, RemoveDoc, STAT_COLUMNS, UpsertDoc


class MemoryRepo:
    def __init__(self):
        self.processed, self.docs, self.stats, self.checkpoints = set(), {}, {}, {}
        self.transactions = 0

    @contextmanager
    def transaction(self):
        snapshot = (set(self.processed), {k: dict(v) for k, v in self.docs.items()},
                    {k: dict(v) for k, v in self.stats.items()}, dict(self.checkpoints))
        try:
            yield
            self.transactions += 1
        except Exception:
            self.processed, self.docs, self.stats, self.checkpoints = snapshot
            raise

    def mark_processed(self, projector, event_id):
        key = (projector, event_id)
        if key in self.processed:
            return False
        self.processed.add(key)
        return True

    @staticmethod
    def _newer_or_equal(existing, version):
        return existing is None or version is None or version >= (existing.get("stream_version") or -1)

    def apply(self, op):
        if isinstance(op, UpsertDoc):
            if self._newer_or_equal(self.docs.get(op.doc_id), op.stream_version):
                self.docs[op.doc_id] = {"title": op.title, "section": op.section, "doc_type": op.doc_type,
                                        "url": op.url, "hash": op.hash, "removed": False,
                                        "stream_version": op.stream_version, "last_event_id": op.event_id}
        elif isinstance(op, RemoveDoc):
            cur = self.docs.get(op.doc_id)
            if cur is None:
                self.docs[op.doc_id] = {"title": None, "section": None, "doc_type": None, "url": None,
                                        "hash": None, "removed": True, "stream_version": op.stream_version,
                                        "last_event_id": op.event_id}
            elif self._newer_or_equal(cur, op.stream_version):
                cur.update(removed=True, stream_version=op.stream_version, last_event_id=op.event_id)
        elif isinstance(op, BumpStat):
            assert op.column in STAT_COLUMNS
            row = self.stats.setdefault(op.algorithm, dict.fromkeys(STAT_COLUMNS, 0))
            row[op.column] += 1

    def save_checkpoint(self, projector, topic, partition, next_offset):
        k = (projector, topic, partition)
        self.checkpoints[k] = max(self.checkpoints.get(k, 0), next_offset)

    def load_checkpoint(self, projector, topic, partition):
        return self.checkpoints.get((projector, topic, partition))


class PgRepo:
    def __init__(self, conn):
        self.conn = conn  # psycopg connection, autocommit=False
        self._cur = None

    @contextmanager
    def transaction(self):
        try:
            with self.conn.cursor() as cur:
                self._cur = cur
                yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._cur = None

    def mark_processed(self, projector, event_id):
        self._cur.execute("INSERT INTO processed_events (projector, event_id) VALUES (%s, %s) "
                          "ON CONFLICT DO NOTHING", (projector, event_id))
        return self._cur.rowcount == 1

    def apply(self, op):
        c = self._cur
        if isinstance(op, UpsertDoc):
            c.execute(
                "INSERT INTO rm_content_docs (doc_id, title, section, doc_type, url, hash, removed, stream_version, last_event_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,false,%s,%s) "
                "ON CONFLICT (doc_id) DO UPDATE SET title=EXCLUDED.title, section=EXCLUDED.section, "
                "doc_type=EXCLUDED.doc_type, url=EXCLUDED.url, hash=EXCLUDED.hash, removed=false, "
                "stream_version=EXCLUDED.stream_version, last_event_id=EXCLUDED.last_event_id, updated_at=now() "
                "WHERE EXCLUDED.stream_version IS NULL OR rm_content_docs.stream_version IS NULL "
                "OR EXCLUDED.stream_version >= rm_content_docs.stream_version",
                (op.doc_id, op.title, op.section, op.doc_type, op.url, op.hash, op.stream_version, op.event_id))
        elif isinstance(op, RemoveDoc):
            c.execute(
                "INSERT INTO rm_content_docs (doc_id, removed, stream_version, last_event_id) "
                "VALUES (%s,true,%s,%s) "
                "ON CONFLICT (doc_id) DO UPDATE SET removed=true, stream_version=EXCLUDED.stream_version, "
                "last_event_id=EXCLUDED.last_event_id, updated_at=now() "
                "WHERE EXCLUDED.stream_version IS NULL OR rm_content_docs.stream_version IS NULL "
                "OR EXCLUDED.stream_version >= rm_content_docs.stream_version",
                (op.doc_id, op.stream_version, op.event_id))
        elif isinstance(op, BumpStat):
            assert op.column in STAT_COLUMNS  # column name is interpolated; whitelist above
            c.execute(f"INSERT INTO rm_algorithm_stats (algorithm, {op.column}) VALUES (%s, 1) "
                      f"ON CONFLICT (algorithm) DO UPDATE SET {op.column} = rm_algorithm_stats.{op.column} + 1",
                      (op.algorithm,))

    def save_checkpoint(self, projector, topic, partition, next_offset):
        self._cur.execute(
            "INSERT INTO projector_checkpoint (projector, topic, partition, next_offset) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (projector, topic, partition) DO UPDATE SET "
            "next_offset = GREATEST(projector_checkpoint.next_offset, EXCLUDED.next_offset), updated_at = now()",
            (projector, topic, partition, next_offset))

    def load_checkpoint(self, projector, topic, partition):
        with self.conn.cursor() as cur:
            cur.execute("SELECT next_offset FROM projector_checkpoint WHERE projector=%s AND topic=%s AND partition=%s",
                        (projector, topic, partition))
            row = cur.fetchone()
        self.conn.commit()
        return row[0] if row else None
