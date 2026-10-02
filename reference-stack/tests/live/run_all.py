"""Run every live check in order and print a summary. Exit code 0 only if all pass.

  cd reference-stack && python3 tests/live/run_all.py          # needs Docker, images (or network), Playwright for 06
01 starts from `docker compose down` (volumes kept) and records time to healthy; the stack is left RUNNING afterwards
(run `docker compose down` yourself, or pass --down).
"""
import subprocess
import sys
import time

from _common import HERE, compose

STEPS = [("01_compose.py", []), ("07_footprint.py", ["idle"]), ("02_gateway_stores.py", []), ("03_kafka_publish.py", []),
         ("04_projector.py", []), ("05_adapters.py", []), ("06_cors.py", []), ("07_footprint.py", ["after-checks"])]


def main():
    results = []
    for script, args in STEPS:
        print(f"\n=== {script} {' '.join(args)}", flush=True)
        t0 = time.time()
        rc = subprocess.call([sys.executable, str(HERE / script), *args], cwd=HERE)
        results.append((script, rc, round(time.time() - t0, 1)))
    print("\n=== summary")
    for s, rc, t in results:
        print(f"  {'PASS' if rc == 0 else 'FAIL'}  {s}  ({t}s)")
    if "--down" in sys.argv:
        compose("down", timeout=300)
    return 0 if all(rc == 0 for _, rc, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
