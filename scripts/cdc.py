#!/usr/bin/env python3
"""Change-data-capture: diff data/corpus.json against data/cdc-state.json and append
Content events to data/events.ndjson (append-only, idempotent). Standard library only.
See es-search-build CONTRACT sections 1 and 6.
Usage: python3 scripts/cdc.py [--data DIR] [--repo DIR]
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def doc_hash(doc):
    return sha256(canonical({k: v for k, v in doc.items() if k != "hash"}))


def event_id(etype, doc_id, h, prev=""):
    # prev (the hash being replaced) makes A->B->A->B sequences unique; omitted for
    # first captures so ids stay compatible with the initial log.
    return sha256(f"{etype}|{doc_id}|{prev}|{h}" if prev else f"{etype}|{doc_id}|{h}")


def git_commit(repo):
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo),
                           capture_output=True, text=True, timeout=10)
        c = r.stdout.strip()
        return c if r.returncode == 0 and c else None
    except (OSError, subprocess.SubprocessError):
        return None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def existing_ids(log):
    ids = set()
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    ids.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    pass
    return ids


def run(data_dir=None, repo=ROOT, now=None):
    data = Path(data_dir) if data_dir else Path(repo) / "data"
    corpus_p, state_p, log_p = data / "corpus.json", data / "cdc-state.json", data / "events.ndjson"
    corpus = read_json(corpus_p, None)
    if not isinstance(corpus, list):
        raise SystemExit(f"cannot read corpus at {corpus_p}")
    state = read_json(state_p, {})
    docs = {d["id"]: d for d in corpus}
    hashes = {i: doc_hash(d) for i, d in docs.items()}
    seen = existing_ids(log_p)
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    commit = git_commit(repo)
    events = []

    def emit(etype, doc_id, h, payload, prev=""):
        eid = event_id(etype, doc_id, h, prev)
        if eid in seen:
            return
        seen.add(eid)
        meta = {"ts": ts, "schema": 1, "source": "git-cdc"}
        if commit:
            meta["commit"] = commit
        events.append({"id": eid, "type": etype, "stream": f"content-{doc_id}", "data": payload, "meta": meta})

    for doc_id in sorted(docs):
        h = hashes[doc_id]
        if doc_id not in state:
            emit("ContentAdded", doc_id, h, {**docs[doc_id], "hash": h})
        elif state[doc_id] != h:
            emit("ContentChanged", doc_id, h, {**docs[doc_id], "hash": h}, prev=state[doc_id])
    for doc_id in sorted(set(state) - set(docs)):
        emit("ContentRemoved", doc_id, state[doc_id], {"id": doc_id})

    if events:
        log_p.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        if log_p.is_file() and log_p.stat().st_size:
            with open(log_p, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    prefix = "\n"
        with open(log_p, "a", encoding="utf-8", newline="\n") as f:  # append only
            f.write(prefix + "".join(canonical(e) + "\n" for e in events))
    tmp = state_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hashes, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    tmp.replace(state_p)
    return events


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--repo", default=str(ROOT))
    a = ap.parse_args(argv)
    ev = run(a.data, a.repo)
    counts = {}
    for e in ev:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    print(f"cdc: appended {len(ev)} events {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
