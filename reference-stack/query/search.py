"""Search over the SQLite FTS5 read model: the site's six methods, answered by the embedded database.

    boolean    the site grammar (fts_query.py) -> one MATCH or a compound SELECT; matches ranked by weighted BM25
    bm25       every query term, scored per term and summed (field weights title 3, section 2, text 1, tags 2)
    fuzzy      unknown terms replaced by their nearest vocabulary neighbours (1 edit for 4-7 letters, 2 for 8+)
    prefix     each term also as the start of longer vocabulary words (top 40 by document frequency)
    phrase     bm25, plus a bonus for the exact phrase and for all terms within 8 words of each other (FTS5 NEAR)
    substring  trigram index: any 3+ character fragment, inside words too (no stemming; ranked by trigram BM25)

Scoring follows assets/search/algorithms.mjs: a term's contribution is FTS5's bm25() for that one term (k1 1.2, b 0.75, IDF
over the live documents) times the term's weight; contributions add up per document. FTS5 returns bm25() as a negative
number (more negative is better), so it is negated here. Pure functions of a sqlite3 connection; no I/O beyond it.
"""
import time

from analysis import PROXIMITY_WINDOW, damerau_levenshtein, fold, max_edits, query_terms, tokenize_parts
from fts_query import SCORE_FILTER, QueryError, collect_terms, plan_boolean, quote

ALGORITHMS = ("boolean", "bm25", "fuzzy", "prefix", "phrase", "substring")
WEIGHTS = "3.0, 2.0, 1.0, 2.0, 0.0, 0.0"            # title, section, text, tags, type, source
COLS = {None: SCORE_FILTER, "title": "title", "section": "section", "text": "text", "tag": "tags", "tags": "tags",
        "type": "type", "source": "source"}
MAX_ROWS = 100
_vocab_cache = {}


def _term_scores(c, term, field=None, weight=1.0, acc=None):
    acc = {} if acc is None else acc
    match = f"{COLS.get(field, SCORE_FILTER)} : {quote(term)}"
    for rowid, s in c.execute(f"SELECT rowid, -bm25(fts, {WEIGHTS}) FROM fts WHERE fts MATCH ?", (match,)):
        r = acc.setdefault(rowid, [0.0, set()])
        r[0] += s * weight
        r[1].add(term)
    return acc


def vocabulary(c, version):
    """[(term, doc_count)] of the main index; cached per read-model version (cleared on every write)."""
    hit = _vocab_cache.get(id(c))
    if hit and hit[0] == version:
        return hit[1]
    v = [(r[0], r[1]) for r in c.execute("SELECT term, doc FROM fts_row")]
    _vocab_cache[id(c)] = (version, v)
    return v


def _has_term(c, term):
    return c.execute("SELECT 1 FROM fts_row WHERE term = ?", (term,)).fetchone() is not None


def _prefix_terms(c, pre, limit=40):
    return [r[0] for r in c.execute("SELECT term FROM fts_row WHERE term >= ? AND term < ? ORDER BY doc DESC, term LIMIT ?",
                                    (pre, pre + "￿", limit))]


def run_bm25(c, query, **_):
    acc = {}
    for _raw, s in query_terms(query):
        _term_scores(c, s, acc=acc)
    return acc


def run_fuzzy(c, query, version=0, **_):
    acc = {}
    for _raw, s in query_terms(query):
        if _has_term(c, s):
            _term_scores(c, s, acc=acc)
            continue
        limit = max_edits(s)
        if not limit:
            continue
        cands = []
        for v, df in vocabulary(c, version):
            if abs(len(v) - len(s)) > limit:
                continue
            d = damerau_levenshtein(s, v, limit)
            if d <= limit:
                cands.append((d, -df, v))
        for d, _df, v in sorted(cands)[:8]:
            _term_scores(c, v, weight=0.6 if d == 1 else 0.4, acc=acc)
    return acc


def run_prefix(c, query, **_):
    acc = {}
    for raw, s in query_terms(query):
        if _has_term(c, s):
            _term_scores(c, s, acc=acc)
        for v in _prefix_terms(c, fold(raw)):
            if v != s:
                _term_scores(c, v, weight=0.75, acc=acc)
    return acc


def run_phrase(c, query, **_):
    terms = [s for _raw, s in query_terms(query)]
    acc = run_bm25(c, query)
    if len(terms) < 2:
        return acc
    body = " ".join(quote(t)[1:-1] for t in terms)
    for bonus, match in ((2.0, f'{SCORE_FILTER} : "{body}"'),
                         (1.0, f"{SCORE_FILTER} : NEAR({' '.join(quote(t) for t in terms)}, {PROXIMITY_WINDOW})")):
        for (rowid,) in c.execute("SELECT rowid FROM fts WHERE fts MATCH ?", (match,)):
            if rowid in acc:
                acc[rowid][0] *= 1 + bonus / 2
    return acc


def run_substring(c, query, **_):
    acc = {}
    frags = [w for w in tokenize_parts(query) if len(w) >= 3][:16]
    for f in frags:
        for rowid, s in c.execute("SELECT rowid, -bm25(fts_tri, 3.0, 2.0, 1.0) FROM fts_tri WHERE fts_tri MATCH ?",
                                  ('"' + f.replace('"', '""') + '"',)):
            r = acc.setdefault(rowid, [0.0, set()])
            r[0] += s
            r[1].add(f)
    return acc


def run_boolean(c, query, version=0, **_):
    plan = plan_boolean(query)
    if plan.empty:
        return {}, None
    if plan.match is not None:
        ids = {r[0] for r in c.execute("SELECT rowid FROM fts WHERE fts MATCH ?", (plan.match,))}
        rendered = plan.match
    else:
        ids = {r[0] for r in c.execute(plan.sql, plan.params)}
        rendered = plan.sql
    ids &= {r[0] for r in c.execute("SELECT id FROM docs WHERE deleted = 0")} if ids else set()
    acc = {}
    for kind, value, fld in collect_terms(plan.ast):
        if kind == "term":
            _term_scores(c, value, fld, acc=acc)
        else:
            for v in _prefix_terms(c, value.replace("-", "")):
                _term_scores(c, v, fld, acc=acc)
    acc = {k: v for k, v in acc.items() if k in ids}
    for i in ids:
        acc.setdefault(i, [0.0, set()])
    return acc, rendered


RUNNERS = {"bm25": run_bm25, "fuzzy": run_fuzzy, "prefix": run_prefix, "phrase": run_phrase, "substring": run_substring}


def search(c, query, *, algorithm="bm25", rows=10, start=0, version=0):
    """-> {algorithm, total, qtimeMs, ftsQuery, results:[{id, score, matched, title, section, url, type, source}]}.
    Raises QueryError (HTTP 400) for malformed Boolean syntax or an unknown method."""
    if algorithm not in ALGORITHMS:
        raise QueryError(f'Unknown search method "{algorithm}".')
    t0 = time.perf_counter()
    rendered = None
    if algorithm == "boolean":
        acc, rendered = run_boolean(c, query, version=version)
    else:
        acc = RUNNERS[algorithm](c, query, version=version)
    ranked = sorted(acc.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rows = max(1, min(MAX_ROWS, int(rows)))
    page = ranked[max(0, int(start)):max(0, int(start)) + rows]
    meta = {}
    if page:
        qs = ",".join("?" * len(page))
        for r in c.execute(f"SELECT id, doc_id, title, section, url, type, source FROM docs WHERE id IN ({qs}) AND deleted = 0",
                           [rid for rid, _ in page]):
            meta[r[0]] = r
    results = []
    for rid, (score, matched) in page:
        m = meta.get(rid)
        if m is None:
            continue
        results.append({"id": m[1], "score": round(score, 6), "matched": sorted(matched), "title": m[2], "section": m[3],
                        "url": m[4], "type": m[5], "source": m[6]})
    return {"algorithm": algorithm, "total": len(ranked), "qtimeMs": round((time.perf_counter() - t0) * 1000, 2),
            "ftsQuery": rendered, "results": results}
