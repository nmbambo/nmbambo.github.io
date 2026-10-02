"""Event envelope helpers (CONTRACT.md section 1). Pure functions, no I/O.

Envelope: {id, type, stream, data, meta:{ts, schema, source, ...}, position?, version?}
The store assigns `position` (global, monotonic) and `version` (per stream, 0-based).
"""
import re
import uuid

# Fixed namespace so a given envelope id always maps to the same UUID (idempotency across restarts).
ID_NAMESPACE = uuid.UUID("6f1a3f56-5b0e-4f0e-9a43-1d6c2a7b9e10")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_EVENTS_PER_REQUEST = 500


class ValidationError(ValueError):
    pass


class ConcurrencyError(Exception):
    def __init__(self, message, stream=None, expected=None, actual=None):
        super().__init__(message)
        self.stream, self.expected, self.actual = stream, expected, actual


def store_uuid(envelope_id: str) -> uuid.UUID:
    """KurrentDB and Message DB require UUID event ids; the envelope allows a UUIDv4 OR a 64-char sha256 hex.
    UUIDs pass through; sha256 ids map deterministically via uuid5. The original id is kept in metadata."""
    try:
        return uuid.UUID(envelope_id)
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(ID_NAMESPACE, envelope_id)


def valid_id(value) -> bool:
    if not isinstance(value, str):
        return False
    if _HEX64.match(value):
        return True
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def category(stream: str) -> str:
    """Message DB convention: category is the part before the first '-'."""
    return stream.split("-", 1)[0]


def validate_event(ev, stream: str) -> dict:
    """Return a clean copy (no position/version) or raise ValidationError."""
    if not isinstance(ev, dict):
        raise ValidationError("event must be an object")
    if not valid_id(ev.get("id")):
        raise ValidationError("id must be a UUID or a 64-char lowercase sha256 hex")
    t = ev.get("type")
    if not isinstance(t, str) or not t or len(t) > 200:
        raise ValidationError("type must be a non-empty string")
    if ev.get("stream", stream) != stream:
        raise ValidationError("event.stream does not match the stream in the URL")
    if not isinstance(ev.get("data"), dict):
        raise ValidationError("data must be a JSON object")
    meta = ev.get("meta")
    if not isinstance(meta, dict):
        raise ValidationError("meta must be an object")
    if not isinstance(meta.get("ts"), str) or not meta["ts"]:
        raise ValidationError("meta.ts must be an ISO-8601 string")
    if meta.get("schema") != 1:
        raise ValidationError("meta.schema must be 1")
    if not isinstance(meta.get("source"), str) or not meta["source"]:
        raise ValidationError("meta.source must be a non-empty string")
    return {"id": ev["id"], "type": t, "stream": stream, "data": ev["data"], "meta": meta}


def validate_append(stream, body):
    """Validate a POST /streams/{stream} body -> (events, expected_version|None)."""
    if not isinstance(stream, str) or not stream or stream.startswith("$") or len(stream) > 400:
        raise ValidationError("invalid stream name")
    if not isinstance(body, dict) or not isinstance(body.get("events"), list):
        raise ValidationError("body must be {events: [...], expectedVersion?: int}")
    if not body["events"]:
        raise ValidationError("events must not be empty")
    if len(body["events"]) > MAX_EVENTS_PER_REQUEST:
        raise ValidationError(f"at most {MAX_EVENTS_PER_REQUEST} events per request")
    events = [validate_event(e, stream) for e in body["events"]]
    ids = [e["id"] for e in events]
    if len(set(ids)) != len(ids):
        # Same id twice in one request: keep the first, ignore the rest (still idempotent).
        seen, uniq = set(), []
        for e in events:
            if e["id"] not in seen:
                seen.add(e["id"])
                uniq.append(e)
        events = uniq
    ev = body.get("expectedVersion")
    if ev is not None and (isinstance(ev, bool) or not isinstance(ev, int) or ev < -1):
        raise ValidationError("expectedVersion must be an integer >= -1")
    return events, ev


def to_stored(ev: dict):
    """envelope -> (data, metadata) dicts persisted by a store. data is the envelope's data verbatim."""
    return ev["data"], {"id": ev["id"], "meta": ev["meta"]}


def from_stored(*, type_, stream, data, metadata, position, version) -> dict:
    """(stored fields) -> envelope. Returns None for foreign events that were not written by this gateway."""
    if not isinstance(metadata, dict) or "id" not in metadata:
        return None
    return {"id": metadata["id"], "type": type_, "stream": stream, "data": data,
            "meta": metadata.get("meta", {}), "position": position, "version": version}
