"""Messages -> read-model operations (pure functions, no I/O).

Two inputs, one rule for both ("an older event never overwrites a newer one"):
  * stream ES, subject es.<stream>: the site's envelopes. ContentAdded / ContentChanged upsert a document, ContentRemoved
    deletes it. Order key = the envelope's global position (stream sequence - 1).
  * stream CDC, subject lakebase.public.<table>: Debezium Server change events (JSON, no schema). op c|u|r upsert,
    op d delete; Debezium Server's NATS sink does not publish the null-valued tombstone that follows a delete (checked live),
    but an empty payload is still honoured as "nothing to do". Order key = source.lsn (distinct per change, checked live,
    and stable across a Debezium re-delivery, which a stream sequence would not be).
"""
import json
from dataclasses import dataclass, field
from typing import Optional

CONTENT_UPSERT = ("ContentAdded", "ContentChanged")
CONTENT_DELETE = "ContentRemoved"
CDC_PREFIX = "lakebase.public."


@dataclass(frozen=True)
class Upsert:
    doc_id: str
    src: str
    pos: int
    event_id: str
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Delete:
    doc_id: str
    src: str
    pos: int
    event_id: str


def _str_list(v):
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []


def content_fields(d):
    out = {"title": "", "section": "", "text": "", "type": "", "source": "", "url": None, "tags": _str_list(d.get("tags"))}
    for k in ("title", "section", "text", "type", "source"):
        if isinstance(d.get(k), str):
            out[k] = d[k]
    if isinstance(d.get("url"), str):
        out["url"] = d["url"]
    return out


def envelope_ops(env, position):
    """One envelope (+ its global position) -> [Upsert|Delete]. Unknown event types give []."""
    t, d = env.get("type"), env.get("data") or {}
    if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not d["id"]:
        return []
    if t in CONTENT_UPSERT:
        return [Upsert(d["id"], "es", position, env["id"], content_fields(d))]
    if t == CONTENT_DELETE:
        return [Delete(d["id"], "es", position, env["id"])]
    return []


def _flatten(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return v
    if isinstance(v, dict):
        return " ".join(_flatten(x) for x in v.values())
    if isinstance(v, list):
        return " ".join(_flatten(x) for x in v)
    return "" if v is None else str(v)


def row_doc(table, row):
    """A lakebase row image -> (doc_id, fields). Synthetic tables only. (None, None) for tables we do not index."""
    if table == "decisions":
        pk = row.get("decision_id")
        text = " ".join(x for x in (row.get("title"), row.get("status"), f"stakes {row.get('stakes')}",
                                    _flatten(row.get("payload")), f"party {row.get('party_id')}") if x)
        return f"decision:{pk}", {"title": row.get("title") or "", "section": row.get("status") or "", "text": text,
                                  "tags": [t for t in (row.get("status"), f"stakes-{row.get('stakes')}") if t],
                                  "type": "decision", "source": "lakebase.public.decisions", "url": f"/decisions/{pk}"}
    if table == "parties":
        pk = row.get("party_id")
        return f"party:{pk}", {"title": row.get("label") or "", "section": row.get("kind") or "",
                               "text": f"{row.get('label')} {row.get('kind')} {row.get('region')}",
                               "tags": [t for t in (row.get("kind"), row.get("region")) if t],
                               "type": "party", "source": "lakebase.public.parties", "url": f"/parties/{pk}"}
    return None, None


def _pk_doc_id(table, row):
    pk = {"decisions": "decision_id", "parties": "party_id"}.get(table)
    name = {"decisions": "decision", "parties": "party"}.get(table)
    if pk and isinstance(row, dict) and row.get(pk) is not None:
        return f"{name}:{row[pk]}"
    return None


def cdc_ops(subject, data: Optional[bytes]):
    """One Debezium message (subject + payload) -> [Upsert|Delete]. Pure; unusable messages give []."""
    if not subject.startswith(CDC_PREFIX) or not data:
        return []
    table = subject[len(CDC_PREFIX):]
    try:
        msg = json.loads(data)
    except ValueError:
        return []
    if isinstance(msg, dict) and "payload" in msg and "op" not in msg:   # tolerate schemas.enable=true
        msg = msg["payload"]
    if not isinstance(msg, dict):
        return []
    op, src = msg.get("op"), msg.get("source") or {}
    lsn = src.get("lsn")
    if not isinstance(lsn, int):
        return []
    if op in ("c", "u", "r"):
        doc_id, fields = row_doc(table, msg.get("after") or {})
        return [Upsert(doc_id, "cdc", lsn, f"cdc:{doc_id}:{lsn}", fields)] if doc_id else []
    if op == "d":
        doc_id = _pk_doc_id(table, msg.get("before"))
        return [Delete(doc_id, "cdc", lsn, f"cdc:{doc_id}:{lsn}")] if doc_id else []
    return []
