#!/usr/bin/env python3
"""Seed N synthetic rows into the lakebase (decisions, parties, decision_events). Synthetic data only.
Usage: python3 seed.py --rows 10000 [--reset] [--seed 42]
"""
import argparse

import db


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="TRUNCATE the three tables first (detach CDC tools first)")
    a = ap.parse_args()
    conn = db.connect()
    if a.reset:
        db.reset(conn)
    parties = db.seed(conn, a.rows, a.seed)
    print(f"seeded {a.rows} decisions, {parties} parties, {a.rows} decision_events (synthetic)")


if __name__ == "__main__":
    main()
