"""Query service entry point (light stack): JetStream consumers -> SQLite FTS5 read model -> HTTP. Environment:

  NATS_URL=nats://nats:4222   DB_PATH=/data/query.db   ALLOWED_ORIGINS=http://localhost:8000,...   HOST=0.0.0.0  PORT=8089
"""
import asyncio
import logging
import os
import threading
import time

from consumer import SOURCES, caught_up, pull
from readmodel import ReadModel
from server import serve


def main(env=os.environ):
    import nats
    logging.basicConfig(level=env.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    lg = logging.getLogger("query")
    rm = ReadModel(env.get("DB_PATH", "/data/query.db"))
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="nats-loop", daemon=True).start()
    run = lambda coro, t=60: asyncio.run_coroutine_threadsafe(coro, loop).result(t)  # noqa: E731
    url = env.get("NATS_URL", "nats://localhost:4222")
    nc = None
    for _ in range(60):
        try:
            nc = run(nats.connect(url, max_reconnect_attempts=-1, name="ingqiqo-query"))
            break
        except Exception as exc:  # noqa: BLE001
            lg.info("waiting for NATS (%s)", exc.__class__.__name__)
            time.sleep(1)
    if nc is None:
        raise SystemExit(f"cannot reach NATS at {url}")
    js = nc.jetstream()
    stop = asyncio.Event()
    for consumer, stream, subjects in SOURCES:
        asyncio.run_coroutine_threadsafe(pull(js, rm, consumer, stream, subjects, stop), loop)
    state = {"ready": False}

    def health():
        try:
            lag = run(caught_up(js, rm), 5)
        except Exception:  # noqa: BLE001
            lag = {}
        if lag.get("es") == 0:
            state["ready"] = True               # ready once the event log has been read through at least once
        return {"ready": state["ready"] and nc.is_connected, "natsConnected": nc.is_connected, "docs": rm.stats()["docs"], "lag": lag}

    origins = [o.strip() for o in env.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    httpd = serve(rm, env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8089")), allowed_origins=origins, health=health)
    lg.info("listening on %s:%s db=%s", *httpd.server_address[:2], rm.path)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
