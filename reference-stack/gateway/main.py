"""Gateway entry point. Configuration by environment variables:

  STORE=kurrentdb|messagedb     (memory is accepted for local smoke tests only)
  KURRENTDB_URI=kurrentdb://kurrentdb:2113?tls=false
  MESSAGEDB_DSN=postgresql://postgres:...@messagedb:5432/message_store
  KAFKA_REST_URL=http://kafka-rest:8082      (empty disables Kafka publishing)
  KAFKA_TOPIC=ingqiqo.events
  ALLOWED_ORIGINS=http://localhost:8000,...
  SOLACE_REST_URL=                            (optional pass-through)
  SOLR_URL=http://solr:8983/solr/ingqiqo      (enables GET /search; empty disables)
  HOST=0.0.0.0  PORT=8088
"""
import logging
import os
import time

from publisher import KafkaRestPublisher, NullPublisher
from server import serve
from stores import KurrentStore, MemoryStore, MessageDbStore


def build_store(name, env):
    if name == "kurrentdb":
        from kurrentdbclient import KurrentDBClient
        return KurrentStore(KurrentDBClient(uri=env.get("KURRENTDB_URI", "kurrentdb://localhost:2113?tls=false")))
    if name == "messagedb":
        return MessageDbStore(env["MESSAGEDB_DSN"])
    if name == "memory":
        return MemoryStore()
    raise SystemExit(f"STORE must be kurrentdb or messagedb (got {name!r})")


def main(env=os.environ):
    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    name = env.get("STORE", "kurrentdb")
    store = build_store(name, env)
    rest = env.get("KAFKA_REST_URL", "")
    pub = KafkaRestPublisher(rest, topic=env.get("KAFKA_TOPIC", "ingqiqo.events")) if rest else NullPublisher()
    if rest:
        pub.start()
        for _ in range(30):  # REST proxy may still be starting
            try:
                pub.resolve_cluster_id()
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
    origins = [o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    httpd = serve(store, pub, env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8088")),
                  store_name=name, allowed_origins=origins, solace_url=env.get("SOLACE_REST_URL") or None,
                  solr_url=env.get("SOLR_URL") or None)
    logging.getLogger("gateway").info("listening on %s:%s store=%s kafka=%s", *httpd.server_address[:2], name, bool(rest))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
