"""Translate the site's Boolean search grammar and its five ranking algorithms into Solr (edismax) requests.

Pure functions, standard library only, no I/O. The JS originals are assets/search/boolean.mjs (lexer, parser, grammar),
assets/search/algorithms.mjs (the five algorithms) and assets/search/tokenize.mjs (tokenizer, stemmer); the lexer and
parser below are a line-by-line port so the two accept and reject the same strings. Contract: CONTRACT.md section 7.

Grammar (lowest to highest precedence), identical to the JS:
    expr  := and ( OR and )*            OR also | ||
    and   := unary ( [AND] unary )*     AND also & && ; adjacency is an implicit AND
    unary := (NOT | ! | -) unary | + unary | primary
    primary := ( expr ) | "phrase" | field:term | field:"phrase" | term | term*

Solr rendering (every group is parenthesised, so Solr never has to guess precedence):
    A AND B           ->  (+A +B)
    A OR B            ->  (A OR B)
    NOT A / -A        ->  (id:[* TO *] -A)  alone,  -A  inside an AND group
    +A B              ->  (+A B)    B is optional: it only affects ranking, exactly as in boolean.mjs
    "a b" / f:"a b"   ->  "a b"  /  f:"a b"
    term*             ->  term*     (a prefix query)
    field:term        ->  field:term   (tag/tags -> tags)

Per-algorithm translation (translate()):
    boolean  grammar above; edismax qf = title^3 section^2 tags^2 text, tie=1 (field scores are summed, as in the JS)
    bm25     bare terms (stop words dropped as in queryTerms()), qf as above, mm=1 (any term matches)
    fuzzy    each term as term~1 (4-7 letters) or term~2 (8+) against the UNSTEMMED shadow fields *_u (the same
             thresholds as maxEdits(), measured on the JS stem); shorter terms stay exact
    prefix   each term as term* against the unstemmed shadow fields
    phrase   bm25 plus edismax pf and pf2 with ps=ps2=8 (the JS window is 8 positions)

Safety: Solr local params ({!...}) are rejected outright. Every term that reaches Solr is made only of letters and
digits (the tokenizer discards everything else), and escape_term() is applied again as a second layer, so Lucene
syntax characters, _query_ / _val_ hooks, $param references and the like are neutralised by construction.
"""
import re
import unicodedata
from dataclasses import dataclass, field

MAX_QUERY_LENGTH = 1000
MAX_DEPTH = 40
MAX_QUERY_TERMS = 32
PROXIMITY_WINDOW = 8
ALGORITHMS = ("boolean", "bm25", "fuzzy", "prefix", "phrase")

QF = "title^3 section^2 tags^2 text"          # the JS FIELD_WEIGHTS: title x3, section x2, tags x2, text x1
QF_UNSTEMMED = "title_u^3 section_u^2 tags_u^2 text_u"
FIELD_NAMES = {"title", "section", "text", "tag", "tags", "type", "source"}
SOLR_FIELD = {"title": "title", "section": "section", "text": "text", "tag": "tags", "tags": "tags",
              "type": "type", "source": "source"}
FL = "id,title,section,type,source,url,tags,score"

STOPWORDS = frozenset((
    "a an and are as at be but by for from has have he her his i if in into is it its me my no not of on or our she so "
    "than that the their them then there these they this to up us was we were what when which who will with you your"
).split())


class QueryError(ValueError):
    """The query cannot be translated (malformed boolean syntax, or something that is refused). `.position` is a
    character offset like the JS ParseError."""

    def __init__(self, message, position=0):
        super().__init__(message)
        self.position = position


# ----------------------------------------------------------------------------------------------------------- tokenizer
_WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


def fold(text):
    """NFKD, strip combining marks, lowercase (tokenize.mjs fold)."""
    s = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def tokenize_parts(text):
    """Words without hyphenated compounds: 'human-in-the-loop' -> human, in, the, loop (tokenize.mjs tokenizeParts)."""
    out = []
    for m in _WORD.finditer(fold(text)):
        out.extend(m.group(0).split("-"))
    return out


_VOWEL = re.compile(r"[aeiouy]")


def _undouble(w):
    n = len(w)
    if n > 3 and w[-1] == w[-2] and not re.search(r"[aeioulsz]", w[-1]):
        return w[:-1]
    return w


def stem(word):
    """Port of tokenize.mjs stem(): light English suffix stripping. Used only to measure term length for fuzzy edit
    thresholds, so that the thresholds equal the JS ones; Solr does its own stemming at index time."""
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


def max_edits(term):
    """algorithms.mjs maxEdits(): 0 below 4 characters, 1 for 4-7, 2 for 8+."""
    return 2 if len(term) >= 8 else 1 if len(term) >= 4 else 0


def query_terms(query):
    """algorithms.mjs queryTerms(): bare terms, stop words dropped unless nothing else is left, de-duplicated by stem,
    capped. Returns the folded raw words (what Solr is sent), in query order."""
    raws = tokenize_parts(query if isinstance(query, str) else "")
    kept = [w for w in raws if w not in STOPWORDS] or raws
    seen, out = set(), []
    for raw in kept:
        s = stem(raw)
        if s in seen:
            continue
        seen.add(s)
        out.append(raw)
        if len(out) >= MAX_QUERY_TERMS:
            break
    return out


_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|<>=$#@%\s])')


def escape_term(term):
    """Backslash-escape every Lucene/edismax syntax character (and whitespace). Applied to every term as a second
    layer; terms are already letters and digits only."""
    return _SPECIAL.sub(r"\\\1", term)


# -------------------------------------------------------------------------------------------------------------- lexer
def _lex(q):
    toks, n, i = [], len(q), 0

    def read_phrase(open_):
        close = q.find('"', open_ + 1)
        if close == -1:
            raise QueryError(f"The quote that opens at character {open_ + 1} is never closed. Add a closing \" or remove it.", open_)
        value = q[open_ + 1:close]
        if not re.search(r"[^\W_]", value, re.UNICODE):
            raise QueryError(f"There is nothing to search for between the quotes at character {open_ + 1}.", open_)
        return value, close + 1

    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
        elif c == "(":
            toks.append({"t": "LP", "pos": i}); i += 1
        elif c == ")":
            toks.append({"t": "RP", "pos": i}); i += 1
        elif c == '"':
            value, i2 = read_phrase(i)
            toks.append({"t": "PHRASE", "value": value, "pos": i, "field": None}); i = i2
        elif c == "|":
            toks.append({"t": "OR", "pos": i, "text": "OR"}); i += 2 if q[i + 1:i + 2] == "|" else 1
        elif c == "&":
            toks.append({"t": "AND", "pos": i, "text": "AND"}); i += 2 if q[i + 1:i + 2] == "&" else 1
        elif c == "!":
            toks.append({"t": "NOT", "pos": i, "text": "NOT"}); i += 1
        elif c in "-+":
            nx = q[i + 1:i + 2]
            if nx and not nx.isspace():
                toks.append({"t": "NOT" if c == "-" else "PLUS", "pos": i, "text": "NOT" if c == "-" else "+"})
            i += 1
        else:
            j = i
            while j < n and not re.match(r'[\s()"|&]', q[j]):
                j += 1
            w = q[i:j]
            if w in ("AND", "OR", "NOT"):
                toks.append({"t": w, "pos": i, "text": w}); i = j; continue
            fm = re.match(r"^([A-Za-z]+):", w)
            if fm and fm.group(1).lower() in FIELD_NAMES:
                fld = "tag" if fm.group(1).lower() == "tags" else fm.group(1).lower()
                rest = w[len(fm.group(0)):]
                if rest == "":
                    if q[j:j + 1] == '"':
                        value, i2 = read_phrase(j)
                        toks.append({"t": "PHRASE", "value": value, "field": fld, "pos": i}); i = i2
                        continue
                    raise QueryError(f'"{fm.group(1)}:" needs a word or a "quoted phrase" straight after it (character {i + 1}).', i)
                toks.append({"t": "WORD", "value": rest, "field": fld, "pos": i})
            else:
                toks.append({"t": "WORD", "value": w, "field": None, "pos": i})
            i = j
    return toks


def _starts_operand(t):
    return t is not None and t["t"] in ("WORD", "PHRASE", "LP", "NOT", "PLUS")


def _word_node(tok):
    value, wildcard = tok["value"], False
    if re.search(r"\*+$", value):
        wildcard, value = True, re.sub(r"\*+$", "", value)
    if wildcard:
        prefix = re.sub(r"[^\w-]|_", "", fold(value)).strip("-")
        prefix = re.sub(r"^-+|-+$", "", prefix)
        if not prefix:
            raise QueryError(f"A * wildcard needs at least one letter before it (character {tok['pos'] + 1}).", tok["pos"])
        return {"type": "prefix", "prefix": prefix, "field": tok["field"]}
    parts = tokenize_parts(value)
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "term", "term": parts[0], "field": tok["field"]}
    return {"type": "phrase", "terms": parts, "field": tok["field"]}


def _phrase_node(tok):
    parts = tokenize_parts(tok["value"])
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "term", "term": parts[0], "field": tok.get("field")}
    return {"type": "phrase", "terms": parts, "field": tok.get("field")}


def _parse_tokens(toks, query):
    k = [0]
    end_pos = len(query)

    def peek():
        return toks[k[0]] if k[0] < len(toks) else None

    def unexpected(t, what):
        if t is None:
            return QueryError(f"The search ends too early: I was expecting {what}.", end_pos)
        if t["t"] == "RP":
            return QueryError(f"The closing bracket at character {t['pos'] + 1} has no matching opening bracket.", t["pos"])
        if t["t"] in ("AND", "OR"):
            return QueryError(f"{t['text']} needs a word or phrase before it (character {t['pos'] + 1}).", t["pos"])
        return QueryError(f"I didn't expect that at character {t['pos'] + 1}.", t["pos"])

    def parse_or(depth):
        kids = [parse_and(depth)]
        while peek() and peek()["t"] == "OR":
            op = toks[k[0]]; k[0] += 1
            if not _starts_operand(peek()):
                raise QueryError(f"{op['text']} needs a word or phrase after it (character {op['pos'] + 1}).", op["pos"])
            kids.append(parse_and(depth))
        live = [x for x in kids if x]
        if not live:
            return None
        return live[0] if len(live) == 1 else {"type": "or", "children": live}

    def parse_and(depth):
        if not _starts_operand(peek()):
            raise unexpected(peek(), "a word or phrase")
        items = []
        while True:
            items.append(parse_unary(depth))
            t = peek()
            if t and t["t"] == "AND":
                k[0] += 1
                if not _starts_operand(peek()):
                    raise QueryError(f"{t['text']} needs a word or phrase after it (character {t['pos'] + 1}).", t["pos"])
                continue
            if _starts_operand(t):
                continue
            break
        live = [x for x in items if x]
        if not live:
            return None
        return live[0] if len(live) == 1 else {"type": "and", "children": live}

    def parse_unary(depth):
        t = peek()
        if t["t"] in ("NOT", "PLUS"):
            k[0] += 1
            if not _starts_operand(peek()):
                raise QueryError(f"{t['text']} needs a word or phrase after it (character {t['pos'] + 1}).", t["pos"])
            child = parse_unary(depth)
            return {"type": "not" if t["t"] == "NOT" else "must", "child": child} if child else None
        return parse_primary(depth)

    def parse_primary(depth):
        t = toks[k[0]]
        if t["t"] == "LP":
            if depth >= MAX_DEPTH:
                raise QueryError(f"Brackets are nested too deeply (more than {MAX_DEPTH} levels) at character {t['pos'] + 1}.", t["pos"])
            k[0] += 1
            if peek() and peek()["t"] == "RP":
                raise QueryError(f"There is nothing inside the brackets at character {t['pos'] + 1}.", t["pos"])
            inner = parse_or(depth + 1)
            if not peek() or peek()["t"] != "RP":
                raise QueryError(f"The bracket opened at character {t['pos'] + 1} is never closed. Add a ) or remove the (.", t["pos"])
            k[0] += 1
            return inner
        k[0] += 1
        return _word_node(t) if t["t"] == "WORD" else _phrase_node(t)

    ast = parse_or(0)
    if k[0] < len(toks):
        raise unexpected(toks[k[0]], "the end of the search")
    return ast


def check_raw(query):
    """Reject what must never reach Solr, before any parsing."""
    if not isinstance(query, str):
        raise QueryError("The search must be text.")
    if len(query) > MAX_QUERY_LENGTH:
        raise QueryError(f"That search is longer than {MAX_QUERY_LENGTH} characters. Please shorten it.", MAX_QUERY_LENGTH)
    if "{!" in query or re.search(r"\{\s*!", query):
        raise QueryError("Solr local parameters ({!...}) are not allowed in a search.", query.find("{"))
    if any(ord(c) < 32 and c not in "\t\n\r" for c in query):
        raise QueryError("The search contains control characters.", 0)
    return query


def parse(query):
    """Parse a query string into an AST of plain dicts. Raises QueryError (with .position) like the JS ParseError."""
    q = check_raw(query)
    toks = _lex(q)
    if not toks:
        return {"type": "empty"}
    return _parse_tokens(toks, q) or {"type": "empty"}


# --------------------------------------------------------------------------------------------------------- rendering
MATCH_ALL = "id:[* TO *]"   # "*:*" is NOT match-all inside edismax (it is escaped to a literal); this is (every doc has an id)


def _fielded(fld, body):
    return f"{SOLR_FIELD[fld]}:{body}" if fld else body


def _render(node):
    t = node["type"]
    f = node.get("field")
    if t == "term":
        return _fielded(f, escape_term(node["term"]))
    if t == "phrase":
        return _fielded(f, '"' + " ".join(escape_term(x) for x in node["terms"]) + '"')
    if t == "prefix":
        return _fielded(f, escape_term(node["prefix"]) + "*")
    if t == "must":
        return _render(node["child"])
    if t == "not":
        return f"({MATCH_ALL} -{_render(node['child'])})"
    if t == "or":
        return "(" + " OR ".join(_render(c) for c in node["children"]) + ")"
    if t == "and":
        has_must = any(c["type"] == "must" for c in node["children"])
        parts, positive = [], False
        for c in node["children"]:
            if c["type"] == "not":
                parts.append("-" + _render(c["child"]))
            elif c["type"] == "must":
                parts.append("+" + _render(c["child"])); positive = True
            else:
                parts.append(("" if has_must else "+") + _render(c)); positive = True
        if not positive:
            parts.insert(0, MATCH_ALL)
        return "(" + " ".join(parts) + ")"
    raise QueryError("Unsupported query node.")


def to_lucene(query):
    """Boolean grammar -> Solr/Lucene query text (for the edismax parser with qf). '' for an empty query."""
    ast = parse(query)
    return "" if ast["type"] == "empty" else _render(ast)


# ---------------------------------------------------------------------------------------------------- algorithm requests
@dataclass
class Translation:
    algorithm: str
    params: dict = field(default_factory=dict)   # the Solr /select parameters; the ONLY thing the gateway forwards
    empty: bool = False                           # True: nothing to search for, answer with no results
    terms: list = field(default_factory=list)


def _common(rows, start):
    return {"defType": "edismax", "fl": FL, "rows": str(rows), "start": str(start),
            "sort": "score desc, id asc",          # deterministic ties, like the JS (score, then id)
            "lowercaseOperators": "false", "tie": "1.0", "wt": "json"}


def translate(query, algorithm="bm25", rows=10, start=0):
    """query + algorithm name -> Translation. Raises QueryError for malformed boolean syntax, unknown algorithm or
    refused input (local params). Never returns a parameter that came from the user verbatim."""
    if algorithm not in ALGORITHMS:
        raise QueryError(f"Unknown algorithm {algorithm!r}. Use one of: {', '.join(ALGORITHMS)}.")
    rows, start = int(rows), int(start)
    q = check_raw(query)
    p = _common(rows, start)
    if algorithm == "boolean":
        lucene = to_lucene(q)
        if not lucene:
            return Translation(algorithm, empty=True)
        p.update(q=lucene, qf=QF, mm="100%")
        return Translation(algorithm, p)
    terms = query_terms(q)
    if not terms:
        return Translation(algorithm, empty=True)
    if algorithm == "bm25":
        p.update(q=" ".join(escape_term(t) for t in terms), qf=QF, mm="1")
    elif algorithm == "phrase":
        p.update(q=" ".join(escape_term(t) for t in terms), qf=QF, mm="1", pf=QF, pf2=QF, ps=str(PROXIMITY_WINDOW),
                 ps2=str(PROXIMITY_WINDOW))
    elif algorithm == "fuzzy":
        clauses = []
        for t in terms:
            n = max_edits(stem(t))
            clauses.append(escape_term(t) + (f"~{n}" if n else ""))
        p.update(q=" ".join(clauses), qf=QF_UNSTEMMED, mm="1")
    elif algorithm == "prefix":
        p.update(q=" ".join(escape_term(t) + "*" for t in terms), qf=QF_UNSTEMMED, mm="1")
    return Translation(algorithm, p, terms=terms)
