"""In-memory stand-in for NatsLog with JetStream's semantics for the three things the store relies on:
Nats-Msg-Id de-duplication, Nats-Expected-Last-Subject-Sequence, and per-subject lookups."""
import asyncio
import threading

from jsstore import H_EXPECT, H_ID, Msg, WrongLastSequence


class FakeLog:
    def __init__(self):
        self.msgs = []                 # index = seq - 1
        self.by_id = {}
        self.before_publish = None     # hook(subject): lets a test simulate a concurrent writer

    async def publish(self, subject, data, headers):
        if self.before_publish:
            hook, self.before_publish = self.before_publish, None
            hook(subject)
        exp = headers.get(H_EXPECT)
        if exp is not None:
            last = max((m.seq for m in self.msgs if m.subject == subject), default=0)
            if int(exp) != last:
                raise WrongLastSequence(f"wrong last sequence: {last}")
        mid = headers.get(H_ID)
        if mid in self.by_id:
            return self.by_id[mid], True
        seq = len(self.msgs) + 1
        self.msgs.append(Msg(seq, subject, data, dict(headers)))
        if mid:
            self.by_id[mid] = seq
        return seq, False

    async def last_for_subject(self, subject):
        return next((m for m in reversed(self.msgs) if m.subject == subject), None)

    async def get(self, seq):
        return self.msgs[seq - 1] if 1 <= seq <= len(self.msgs) else None

    async def next_for_subject(self, subject, seq):
        return next((m for m in self.msgs if m.subject == subject and m.seq >= seq), None)

    async def last_seq(self):
        return len(self.msgs)

    async def scan_headers(self, upto):
        for m in self.msgs[:upto]:
            yield m.seq, m.headers


class Loop:
    """A running asyncio loop on a thread, like main.py's."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def ev(stream, i, id_=None):
    return {"id": id_ or f"00000000-0000-4000-8000-{i:012d}", "type": "ContentAdded", "stream": stream,
            "data": {"id": f"{stream}#{i}"}, "meta": {"ts": "2026-10-02T00:00:00Z", "schema": 1, "source": "t"}}
