"""Command service entry point (light stack). HTTP -> JetStream. Configuration by environment variables:

  NATS_URL=nats://nats:4222
  ALLOWED_ORIGINS=http://localhost:8000,...
  DUPLICATE_WINDOW_S=86400        JetStream de-duplication window of the ES stream
  CDC_SUBJECTS=lakebase.>         subjects of the CDC stream (Debezium Server's landing zone)
  HOST=0.0.0.0  PORT=8088

At start-up it provisions what the light stack shares: stream ES (append-only: deny_delete + deny_purge), stream CDC
and the KV bucket `checkpoints`, then rebuilds its id index from the log. /health is 503 until that is done, so
debezium-server and the query service (which depend on this service being healthy) never race the provisioning.
"""
import asyncio
import logging
import os
import threading
import time

from jsstore import JetStreamStore, NatsLog, provision
from server import serve


def start_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="nats-loop", daemon=True).start()
    return loop


def main(env=os.environ):
    import nats
    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    lg = logging.getLogger("command")
    loop = start_loop()
    run = lambda coro, t=60: asyncio.run_coroutine_threadsafe(coro, loop).result(t)  # noqa: E731
    url = env.get("NATS_URL", "nats://localhost:4222")
    nc = None
    for attempt in range(60):
        try:
            nc = run(nats.connect(url, max_reconnect_attempts=-1, name="ingqiqo-command"))
            break
        except Exception as exc:  # noqa: BLE001
            lg.info("waiting for NATS (%s)", exc.__class__.__name__)
            time.sleep(1)
    if nc is None:
        raise SystemExit(f"cannot reach NATS at {url}")
    js = nc.jetstream()
    run(provision(js, duplicate_window=float(env.get("DUPLICATE_WINDOW_S", "86400")),
                  cdc_subjects=[s.strip() for s in env.get("CDC_SUBJECTS", "lakebase.>").split(",") if s.strip()]))
    store = JetStreamStore(NatsLog(nc, js), loop=loop)
    n = run(store.load_index(), 300)
    lg.info("indexed %d events from the log", n)
    origins = [o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

    def health():
        return {"ready": store.ready and nc.is_connected, "store": "jetstream", "natsConnected": nc.is_connected,
                "indexed": store.indexed()}

    httpd = serve(store, env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8088")), allowed_origins=origins, health=health)
    lg.info("listening on %s:%s store=jetstream", *httpd.server_address[:2])
    httpd.serve_forever()


if __name__ == "__main__":
    main()
