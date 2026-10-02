"""Projector service: Kafka topic -> idempotent read models in Postgres.

Delivery is at-least-once (offsets are committed to Kafka AFTER the database commit). The database holds its
own checkpoint and a set of processed event ids, so a redelivered or replayed event changes nothing.
On partition assignment we seek to the DB checkpoint, not the consumer-group offset, so losing the group
offsets (or resetting the group) cannot cause reprocessing to double count either.

  KAFKA_BOOTSTRAP=kafka:9092   KAFKA_TOPIC=ingqiqo.events   KAFKA_GROUP=ingqiqo-projector
  PROJECTIONS_DSN=postgresql://user:pass@host:5432/projections
"""
import logging
import os
import pathlib
import signal

import psycopg
from confluent_kafka import Consumer, KafkaError, TopicPartition

from logic import PROJECTOR, process
from repo import PgRepo

log = logging.getLogger("projector")


def main(env=os.environ):
    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    topic = env.get("KAFKA_TOPIC", "ingqiqo.events")
    conn = psycopg.connect(env["PROJECTIONS_DSN"], autocommit=False)
    with conn.cursor() as cur:
        cur.execute(pathlib.Path(__file__).with_name("schema.sql").read_text())
    conn.commit()
    repo = PgRepo(conn)

    consumer = Consumer({
        "bootstrap.servers": env.get("KAFKA_BOOTSTRAP", "kafka:9092"),
        "group.id": env.get("KAFKA_GROUP", "ingqiqo-projector"),
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })

    def on_assign(c, partitions):
        for tp in partitions:
            nxt = repo.load_checkpoint(PROJECTOR, tp.topic, tp.partition)
            if nxt is not None:
                tp.offset = nxt
        c.assign(partitions)

    consumer.subscribe([topic], on_assign=on_assign)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    counts = {"applied": 0, "duplicate": 0, "skipped": 0}
    try:
        while not stop["now"]:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    log.error("kafka error: %s", msg.error())
                continue
            result = process(repo, msg.topic(), msg.partition(), msg.offset(), msg.value())
            counts[result] += 1
            consumer.commit(message=msg, asynchronous=False)  # after the DB commit => at-least-once
            if sum(counts.values()) % 500 == 0:
                log.info("processed %s", counts)
    finally:
        consumer.close()
        conn.close()


if __name__ == "__main__":
    main()
