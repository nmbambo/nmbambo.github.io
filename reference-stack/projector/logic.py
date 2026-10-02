"""Pure projection logic. No Kafka, no Postgres, no clock, no randomness.

An event is turned into a list of Ops; a Repo applies them. `process()` is the only place that decides
whether an event is new, so idempotency lives in one small function that the unit tests exercise.
"""
import json
from dataclasses import dataclass
from typing import Optional

PROJECTOR = "ingqiqo-readmodels"


@dataclass(frozen=True)
class UpsertDoc:
    doc_id: str
    title: Optional[str]
    section: Optional[str]
    doc_type: Optional[str]
    url: Optional[str]
    hash: Optional[str]
    stream_version: Optional[int]
    event_id: str


@dataclass(frozen=True)
class RemoveDoc:
    doc_id: str
    stream_version: Optional[int]
    event_id: str


@dataclass(frozen=True)
class BumpStat:
    algorithm: str
    column: str  # submitted | opened | overridden_from | overridden_to


STAT_COLUMNS = {"submitted", "opened", "overridden_from", "overridden_to"}


def parse_message(raw) -> Optional[dict]:
    """Kafka message value (bytes/str) -> envelope dict, or None if it is not a usable envelope.
    Poison messages are skipped (and still advance the checkpoint) so one bad record cannot wedge the topic."""
    try:
        ev = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not (isinstance(ev, dict) and isinstance(ev.get("id"), str) and isinstance(ev.get("type"), str)
            and isinstance(ev.get("data"), dict)):
        return None
    return ev


def project(ev: dict) -> list:
    """envelope -> ops. Pure; unknown event types yield no ops."""
    t, d, version = ev["type"], ev["data"], ev.get("version")
    version = version if isinstance(version, int) else None
    if t in ("ContentAdded", "ContentChanged"):
        doc_id = d.get("id")
        if not isinstance(doc_id, str):
            return []
        return [UpsertDoc(doc_id, d.get("title"), d.get("section"), d.get("type"), d.get("url"),
                          d.get("hash"), version, ev["id"])]
    if t == "ContentRemoved":
        doc_id = d.get("id")
        return [RemoveDoc(doc_id, version, ev["id"])] if isinstance(doc_id, str) else []
    if t == "SearchSubmitted" and isinstance(d.get("algorithm"), str):
        return [BumpStat(d["algorithm"], "submitted")]
    if t == "ResultOpened" and isinstance(d.get("algorithm"), str):
        return [BumpStat(d["algorithm"], "opened")]
    if t == "AlgorithmOverridden":
        ops = []
        if isinstance(d.get("proposed"), str):
            ops.append(BumpStat(d["proposed"], "overridden_from"))
        if isinstance(d.get("chosen"), str):
            ops.append(BumpStat(d["chosen"], "overridden_to"))
        return ops
    return []


def process(repo, topic: str, partition: int, offset: int, raw) -> str:
    """Apply one Kafka message exactly once in effect. Returns 'applied' | 'duplicate' | 'skipped'.

    repo contract (all inside ONE transaction opened by repo.transaction()):
      mark_processed(projector, event_id) -> True if newly recorded, False if already present
      apply(op)
      save_checkpoint(projector, topic, partition, next_offset)   # never moves backwards
    The checkpoint advances for every message, applied or not, in the same transaction.
    """
    ev = parse_message(raw)
    with repo.transaction():
        if ev is None:
            result = "skipped"
        elif repo.mark_processed(PROJECTOR, ev["id"]):
            for op in project(ev):
                repo.apply(op)
            result = "applied"
        else:
            result = "duplicate"
        repo.save_checkpoint(PROJECTOR, topic, partition, offset + 1)
    return result
