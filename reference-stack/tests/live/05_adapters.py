"""Check 5: the browser's GatewayStore and KafkaRestBroker (assets/es/adapters.mjs), real HTTP from Node.

Runs adapters.live.test.mjs (node:test), then confirms from the Kafka/Postgres side that what the browser classes
published really arrived: the record on its topic, and the browser-originated ContentAdded in the read model.
"""
import json
import subprocess
import sys

from _common import GATEWAY, HERE, OUT, REPO, REST, Checks, kafka_consume_all, psql, wait_until


def main():
    c = Checks("05_adapters")
    r = subprocess.run(["node", "--test", "--test-reporter=spec", str(HERE / "adapters.live.test.mjs")],
                       cwd=REPO, text=True, capture_output=True,
                       env={**__import__("os").environ, "GATEWAY_URL": GATEWAY, "REST_URL": REST})
    print(r.stdout[-3500:])
    if r.returncode != 0:
        print(r.stderr[-1500:])
    c.check("node:test live adapter suite passes (GatewayStore, KafkaRestBroker, fromConfig, Projector.catchUp)", r.returncode == 0)
    pub = json.loads((OUT / "adapters-published.json").read_text())
    t = pub.get("topicTest")
    if t:
        rows = kafka_consume_all(t["topic"])
        hit = [(p, k, v) for p, k, v in rows if isinstance(v, dict) and v.get("id") == t["id"]]
        c.check("record published by KafkaRestBroker is on the Kafka topic, key = stream, value = envelope",
                len(hit) == 1 and hit[0][1] == t["stream"] and hit[0][2]["meta"]["source"] == "browser-live-test", hit[:1])
    pr = pub.get("projectorDoc")
    if pr:
        try:
            row = wait_until(lambda: psql(f"SELECT title, last_event_id FROM rm_content_docs WHERE doc_id = '{pr['doc']}'"),
                             "projector to pick up the browser-published event", timeout=60, every=2)
            c.check("browser-published event was projected into rm_content_docs", row[0].split("|") == ["from the browser", pr["id"]], row)
        except TimeoutError as exc:
            c.check("browser-published event was projected into rm_content_docs", False, exc)
    return c.finish()


if __name__ == "__main__":
    sys.exit(main())
