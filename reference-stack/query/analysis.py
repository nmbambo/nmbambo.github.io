"""Text analysis for the SQLite FTS5 read model. A port of assets/search/tokenize.mjs (fold, scan, stem) and of the helpers in
assets/search/algorithms.mjs (queryTerms, maxEdits, damerauLevenshtein), so the FTS5 index holds exactly the stems the
in-browser engine indexes and a query term is analysed the same way on both sides. Pure, standard library only.

What is deliberately not ported: hyphenated compound tokens ('human-in-the-loop' as one extra term). The JS index stores
the compound next to its parts; here only the parts are indexed (phrases and bare terms never use the compound, see
tokenizeParts), and a hyphenated prefix such as `human-in*` is answered as the phrase-prefix `"human in"*`.
"""
import re
import unicodedata

MAX_QUERY_TERMS = 32
PROXIMITY_WINDOW = 8
FIELD_STRIDE = 1_000_000
STOPWORDS = frozenset((
    "a an and are as at be but by for from has have he her his i if in into is it its me my no not of on or our she so "
    "than that the their them then there these they this to up us was we were what when which who will with you your"
).split())

_WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
_VOWEL = re.compile(r"[aeiouy]")


def fold(text):
    """NFKD, strip combining marks, lowercase (tokenize.mjs fold)."""
    s = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def tokenize_parts(text):
    """Words without hyphenated compounds, in order: 'human-in-the-loop' -> human, in, the, loop."""
    out = []
    for m in _WORD.finditer(fold(text)):
        out.extend(m.group(0).split("-"))
    return out


def _undouble(w):
    n = len(w)
    if n > 3 and w[-1] == w[-2] and not re.search(r"[aeioulsz]", w[-1]):
        return w[:-1]
    return w


def stem(word):
    """tokenize.mjs stem(): ies->y, es, s, ing, ed, ly; never below 3 characters."""
    w0 = str(word or "").lower()
    if len(w0) < 4 or "-" in w0 or re.search(r"\d", w0):
        return w0
    w = w0
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif re.search(r"(ches|shes|xes)$", w) and len(w) > 4:
        w = w[:-2]
    elif w.endswith("s") and not re.search(r"(ss|us|is)$", w):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        b = w[:-3]
        if len(b) >= 3 and _VOWEL.search(b):
            w = _undouble(b)
    elif len(w) > 4 and w.endswith("ied"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("ed"):
        b = w[:-2]
        if len(b) >= 3 and _VOWEL.search(b):
            w = _undouble(b)
    if len(w) > 5 and w.endswith("ily"):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("ly"):
        w = w[:-2]
    if len(w) < 3:
        w = w0
    return w


def stems(text):
    """The analysed form of a text: its words, stemmed, in position order (what is stored in the FTS5 columns)."""
    return [stem(w) for w in tokenize_parts(text)]


def analyze_column(value):
    """A column value (string, or list of strings such as tags) -> the space-joined stems stored in FTS5."""
    if value is None:
        return ""
    pieces = value if isinstance(value, (list, tuple)) else [value]
    return " ".join(" ".join(stems(str(p))) for p in pieces if p is not None)


def max_edits(term):
    """0 below 4 characters, 1 for 4-7, 2 for 8+ (algorithms.mjs maxEdits)."""
    return 2 if len(term) >= 8 else 1 if len(term) >= 4 else 0


def query_terms(query):
    """algorithms.mjs queryTerms(): [(raw, stem)], stop words dropped unless nothing else is left, de-duplicated by stem,
    capped at MAX_QUERY_TERMS."""
    raws = tokenize_parts(query if isinstance(query, str) else "")
    kept = [w for w in raws if w not in STOPWORDS] or raws
    seen, out = set(), []
    for raw in kept:
        s = stem(raw)
        if s in seen:
            continue
        seen.add(s)
        out.append((raw, s))
        if len(out) >= MAX_QUERY_TERMS:
            break
    return out


def damerau_levenshtein(a, b, limit=None):
    """Optimal-string-alignment distance with early exit; returns limit + 1 when over the limit (algorithms.mjs)."""
    if limit is None:
        limit = 10 ** 9
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    if a == b:
        return 0
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            row_min = min(row_min, v)
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]
