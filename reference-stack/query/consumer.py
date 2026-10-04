"""JetStream -> read model. Two independent consumers, one per stream:

    es   stream ES  (subjects es.>)        the site's envelopes, order key = stream sequence - 1
    cdc  stream CDC (subjects lakebase.>)  Debezium Server change events, order key = source.lsn

Each consumer is an ordered, ephemeral pull from `checkpoint + 1`. The checkpoint lives in SQLite and is written in the same
transaction as the changes it covers (readmodel.apply_batch), so a crash between "applied" and "acknowledged" cannot
happen: there is nothing to acknowledge. Restarting resumes exactly after the last committed message; replaying from 0
(delete the database file) rebuilds an identical read model, because every write is idempotent (readmodel.py).

If a stream was recreated (its last sequence is now below our checkpoint), what that consumer derived is wiped and it
starts again from 1. The NATS calls are kept in `pull()`, so tests drive `drain()` with an in-memory log.
"""
import asyncio
import logging

log = logging.getLogger("query.consumer")

SOURCES = (("es", "ES", "es.>"), ("cdc", "CDC", "lakebase.>"))
BATCH = 256


def drain(rm, consumer, messages):
    """Apply one batch [(seq, subject, data, headers)] that starts after the checkpoint. Returns the counts."""
    cp = rm.checkpoint(consumer)
    fresh = [m for m in messages if m[0] > cp]
    return rm.apply_batch(consumer, fresh)


async def pull(js, rm, consumer, stream, subjects, stop: asyncio.Event, idle=1.0):
    from nats.js.api import ConsumerConfig, DeliverPolicy
    from nats.js.errors import NotFoundError
    from nats.errors import TimeoutError as NatsTimeout
    while not stop.is_set():
        try:
            info = await js.stream_info(stream)
        except NotFoundError:
            await asyncio.sleep(idle)
            continue
        cp = rm.checkpoint(consumer)
        if cp > info.state.last_seq:
            log.warning("%s: stream %s is behind our checkpoint (%d > %d): rebuilding this source", consumer, stream, cp, info.state.last_seq)
            rm.wipe_source(consumer, consumer)
            cp = 0
        cfg = ConsumerConfig(deliver_policy=DeliverPolicy.BY_START_SEQUENCE, opt_start_seq=max(cp + 1, info.state.first_seq or 1))
        sub = await js.subscribe(subjects, stream=stream, ordered_consumer=True, config=cfg)
        try:
            while not stop.is_set():
                batch = []
                try:
                    m = await sub.next_msg(timeout=idle)
                    batch.append(m)
                    while len(batch) < BATCH:
                        batch.append(await sub.next_msg(timeout=0.05))
                except NatsTimeout:
                    pass
                if batch:
                    res = drain(rm, consumer, [(x.metadata.sequence.stream, x.subject, x.data, dict(x.headers or {})) for x in batch])
                    log.debug("%s: %s", consumer, res)
        except Exception as exc:  # noqa: BLE001  (connection churn: resubscribe from the committed checkpoint)
            log.info("%s: resubscribing after %s", consumer, exc.__class__.__name__)
            await asyncio.sleep(idle)
        finally:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass


async def caught_up(js, rm):
    """True when both checkpoints have reached their stream's last sequence (used by /health 'lag')."""
    lag = {}
    for consumer, stream, _ in SOURCES:
        try:
            last = (await js.stream_info(stream)).state.last_seq
        except Exception:  # noqa: BLE001
            last = None
        lag[consumer] = None if last is None else max(0, last - rm.checkpoint(consumer))
    return lag
