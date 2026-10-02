"""The site's Boolean search grammar -> SQLite FTS5.

Grammar and error messages are those of assets/search/boolean.mjs (CONTRACT.md section 7); the lexer and parser below are a
line-by-line port, so both engines accept and reject the same strings, with the same ParseError text and position.

    expr   := and ( OR and )*            OR also | ||
    and    := unary ( [AND] unary )*     AND also & && ; adjacency is an implicit AND
    unary  := (NOT | ! | -) unary | + unary | primary
    primary:= ( expr ) | "phrase" | field:term | field:"phrase" | term | term*
    fields : title section text tag(s) type source        operators only when UPPERCASE

Two renderings of the same AST:

  to_match(ast)  one FTS5 MATCH string, when the query never needs "every document except ...":
        A AND B              ->  ("a" AND "b")                 (every leaf is a double-quoted token or phrase)
        A OR B               ->  ("a" OR "b")
        A -B  /  A NOT B     ->  ("a") NOT ("b")               FTS5's NOT is binary, so exactly the shape `A NOT B`
        +A B                 ->  ("a")                         B only ranks, as in boolean.mjs
        "a b" / f:"a b"      ->  {title section text tags} : "a b"  /  title : "a b"
        term*                ->  {title section text tags} : "pre"*
        field:term           ->  title : "term"                (tag/tags -> tags)
    Un-fielded leaves are confined to title, section, text and tags with a column filter, because the index also carries the
    `type` and `source` columns, which only an explicit type: / source: reaches (as in the JS index).

  to_sql(ast)    for the shapes FTS5 cannot say: a bare NOT, a group made only of negations, a negation under OR.
        A compound SELECT over the documents table:  universe EXCEPT (...)  /  UNION  /  INTERSECT, each universe-free
        subtree still being ONE MATCH.  Same set semantics as boolean.mjs evaluate().

Injection: nothing the user typed reaches FTS5 except as the characters [letters digits] of an analysed token, wrapped in
double quotes (any quote is doubled anyway); column names come from a fixed map. FTS5 syntax characters, the keywords
AND/OR/NOT/NEAR, a `col:` filter, `^`, `*` and `-` are therefore either consumed by the grammar above or inert.
"""
import re
from dataclasses import dataclass, field

from analysis import fold, stem, tokenize_parts

MAX_QUERY_LENGTH = 1000
MAX_DEPTH = 40
FIELD_NAMES = {"title", "section", "text", "tag", "tags", "type", "source"}
COLUMN = {"title": "title", "section": "section", "text": "text", "tag": "tags", "tags": "tags", "type": "type", "source": "source"}
SCORE_COLUMNS = ("title", "section", "text", "tags")            # un-fielded terms only ever look here
SCORE_FILTER = "{" + " ".join(SCORE_COLUMNS) + "}"
_TOKEN = re.compile(r"^[^\W_]+$", re.UNICODE)


class QueryError(ValueError):
    """Malformed Boolean syntax, or something refused. `.position` is a character offset (the JS ParseError.position)."""

    def __init__(self, message, position=0):
        super().__init__(message)
        self.position = position


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
        prefix = re.sub(r"[^\w-]|_", "", fold(value))
        prefix = re.sub(r"^-+|-+$", "", prefix)
        if not prefix:
            raise QueryError(f"A * wildcard needs at least one letter before it (character {tok['pos'] + 1}).", tok["pos"])
        return {"type": "prefix", "prefix": prefix, "field": tok["field"]}
    parts = tokenize_parts(value)
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "term", "term": stem(parts[0]), "field": tok["field"]}
    return {"type": "phrase", "terms": [stem(p) for p in parts], "field": tok["field"]}


def _phrase_node(tok):
    parts = tokenize_parts(tok["value"])
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "term", "term": stem(parts[0]), "field": tok.get("field")}
    return {"type": "phrase", "terms": [stem(p) for p in parts], "field": tok.get("field")}


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
    if not isinstance(query, str):
        raise QueryError("The search must be text.")
    if len(query) > MAX_QUERY_LENGTH:
        raise QueryError(f"That search is longer than {MAX_QUERY_LENGTH} characters. Please shorten it.", MAX_QUERY_LENGTH)
    if any(ord(c) < 32 and c not in "\t\n\r" for c in query):
        raise QueryError("The search contains control characters.", 0)
    return query


def parse(query):
    """Query string -> AST (dicts). Raises QueryError with the JS ParseError message and position."""
    q = check_raw(query)
    toks = _lex(q)
    if not toks:
        return {"type": "empty"}
    return _parse_tokens(toks, q) or {"type": "empty"}


# ------------------------------------------------------------------------------------------------------------- leaves
def quote(token):
    """One analysed token as an FTS5 string. Tokens are letters/digits only by construction; the check is the second layer."""
    if not _TOKEN.match(token):
        raise QueryError("Internal: refusing a token with unexpected characters.")
    return '"' + token.replace('"', '""') + '"'


def _colspec(fld):
    return f"{COLUMN[fld]} :" if fld else f"{SCORE_FILTER} :"


def leaf(node):
    """term / phrase / prefix node -> one FTS5 expression (a single column-filtered string or prefix query)."""
    t, fld = node["type"], node.get("field")
    if t == "term":
        return f"{_colspec(fld)} {quote(node['term'])}"
    if t == "phrase":
        return f"{_colspec(fld)} \"{' '.join(_bare(x) for x in node['terms'])}\""
    if t == "prefix":
        parts = [p for p in node["prefix"].split("-") if p]
        body = " ".join(_bare(x) for x in parts)
        return f"{_colspec(fld)} \"{body}\" *"
    raise QueryError("Unsupported query node.")


def _bare(token):
    quote(token)                      # validates; returns nothing we need
    return token


# ---------------------------------------------------------------------------------------------------- MATCH rendering
def _split_and(node):
    musts, pos, negs = [], [], []
    for c in node["children"]:
        if c["type"] == "not":
            negs.append(c["child"])
        elif c["type"] == "must":
            musts.append(c["child"])
        else:
            pos.append(c)
    return musts, pos, negs


def to_match(node):
    """AST -> one FTS5 MATCH string, or None when the set needs a complement (see to_sql)."""
    t = node["type"]
    if t in ("term", "phrase", "prefix"):
        return leaf(node)
    if t == "must":
        return to_match(node["child"])
    if t == "not":
        return None
    if t == "or":
        kids = [to_match(c) for c in node["children"]]
        return None if any(k is None for k in kids) else "(" + " OR ".join(kids) + ")"
    if t == "and":
        musts, pos, negs = _split_and(node)
        base = musts or pos            # with a +term present the unmarked terms only affect ranking
        if not base:
            return None
        kids = [to_match(c) for c in base]
        if any(k is None for k in kids):
            return None
        out = kids[0] if len(kids) == 1 else "(" + " AND ".join(kids) + ")"
        for n in negs:
            m = to_match(n)
            if m is None:
                return None
            out = f"({out}) NOT ({m})"
        return out
    return None


# ------------------------------------------------------------------------------------------------------- SQL rendering
UNIVERSE = "SELECT id FROM docs WHERE deleted = 0"


def to_sql(node, params):
    """AST -> a self-contained `SELECT id FROM ...` returning the matching document ids; MATCH strings go to `params`."""
    m = to_match(node)
    if m is not None:
        params.append(m)
        return "SELECT rowid AS id FROM fts WHERE fts MATCH ?"
    t = node["type"]
    if t == "not":
        return f"{UNIVERSE} EXCEPT SELECT id FROM ({to_sql(node['child'], params)})"
    if t == "must":
        return to_sql(node["child"], params)
    if t == "or":
        return " UNION ".join(f"SELECT id FROM ({to_sql(c, params)})" for c in node["children"])
    if t == "and":
        musts, pos, negs = _split_and(node)
        base = musts or pos
        sql = (" INTERSECT ".join(f"SELECT id FROM ({to_sql(c, params)})" for c in base)) if base else UNIVERSE
        for n in negs:
            sql = f"SELECT id FROM ({sql}) EXCEPT SELECT id FROM ({to_sql(n, params)})"
        return sql
    raise QueryError("Unsupported query node.")


# -------------------------------------------------------------------------------------------------------- ranking terms
def collect_terms(node):
    """Positive (non-negated) terms of the AST, [(token, field|None)], de-duplicated in order. Phrase words are scored one by
    one and a prefix is returned as ("prefix", text, field) for the caller to expand against the vocabulary
    (boolean.mjs collectTerms)."""
    seen, out = set(), []

    def add(kind, value, fld):
        key = (kind, value, fld)
        if key not in seen:
            seen.add(key)
            out.append(key)

    def walk(n):
        if not n:
            return
        t = n["type"]
        if t == "term":
            add("term", n["term"], n.get("field"))
        elif t == "phrase":
            for x in n["terms"]:
                add("term", x, n.get("field"))
        elif t == "prefix":
            add("prefix", n["prefix"], n.get("field"))
        elif t in ("and", "or"):
            for c in n["children"]:
                walk(c)
        elif t == "must":
            walk(n["child"])
    walk(node)
    return out


@dataclass
class BooleanPlan:
    ast: dict
    match: str = None                    # set when one MATCH string says it all
    sql: str = None                      # set otherwise (compound SELECT of document ids)
    params: list = field(default_factory=list)
    empty: bool = False


def plan_boolean(query):
    """Query -> BooleanPlan (parses, validates, renders). Raises QueryError."""
    ast = parse(query)
    if ast["type"] == "empty":
        return BooleanPlan(ast, empty=True)
    m = to_match(ast)
    if m is not None:
        return BooleanPlan(ast, match=m, params=[m])
    params = []
    return BooleanPlan(ast, sql=to_sql(ast, params), params=params)
