"""Offline tests for solr_query.py: grammar, Solr rendering, algorithm parameters, injection, and a cross-check of the
Python parser against the real JS parser (assets/search/boolean.mjs) when `node` is available."""
import json
import pathlib
import shutil
import subprocess
import unittest

from solr_query import (QF, QF_UNSTEMMED, QueryError, escape_term, max_edits, parse, query_terms, stem, to_lucene,
                        tokenize_parts, translate)

REPO = pathlib.Path(__file__).resolve().parents[3]


class GrammarToLucene(unittest.TestCase):
    def test_operators_and_implicit_and(self):
        self.assertEqual(to_lucene("decision AND (warranty OR canvas)"), "(+decision +(warranty OR canvas))")
        self.assertEqual(to_lucene("meaning coherence"), "(+meaning +coherence)")
        self.assertEqual(to_lucene("a | b || c"), "(a OR b OR c)")
        self.assertEqual(to_lucene("a & b && c"), "(+a +b +c)")

    def test_precedence_or_below_and(self):
        self.assertEqual(to_lucene("a b OR c"), "((+a +b) OR c)")
        self.assertEqual(to_lucene("a OR b c"), "(a OR (+b +c))")

    def test_not_forms(self):
        self.assertEqual(to_lucene("solver -quantum"), "(+solver -quantum)")
        self.assertEqual(to_lucene("solver NOT quantum"), "(+solver -quantum)")
        self.assertEqual(to_lucene("solver !quantum"), "(+solver -quantum)")
        self.assertEqual(to_lucene("NOT quantum"), "(id:[* TO *] -quantum)")
        self.assertEqual(to_lucene("a OR -b"), "(a OR (id:[* TO *] -b))")
        self.assertEqual(to_lucene("-a -b"), "(id:[* TO *] -a -b)")

    def test_required_marks_make_the_rest_optional(self):
        self.assertEqual(to_lucene("+moloto framework"), "(+moloto framework)")   # framework only ranks, as in boolean.mjs
        self.assertEqual(to_lucene("+moloto"), "moloto")

    def test_phrases_fields_prefix(self):
        self.assertEqual(to_lucene('"human in the loop"'), '"human in the loop"')
        self.assertEqual(to_lucene("title:spine"), "title:spine")
        self.assertEqual(to_lucene('title:"decision ir"'), 'title:"decision ir"')
        self.assertEqual(to_lucene("tags:booklet tag:page"), "(+tags:booklet +tags:page)")
        self.assertEqual(to_lucene("type:booklet AND pedigree"), "(+type:booklet +pedigree)")
        self.assertEqual(to_lucene("warrant*"), "warrant*")
        self.assertEqual(to_lucene("title:dec*"), "title:dec*")

    def test_lowercase_operators_are_words_and_unknown_fields_are_phrases(self):
        self.assertEqual(to_lucene("cats and dogs"), "(+cats +and +dogs)")
        self.assertEqual(to_lucene("foo:bar"), '"foo bar"')
        self.assertEqual(to_lucene("state-of-art"), '"state of art"')
        self.assertEqual(to_lucene("Café"), "cafe")

    def test_empty(self):
        self.assertEqual(to_lucene("   "), "")
        self.assertEqual(to_lucene("()"[:0]), "")

    def test_malformed_raises_with_position(self):
        for q in ['"open', "a AND", "(a", "a)", "OR a", "title:", '""', "*", "()", "a OR OR b"]:
            with self.subTest(q=q), self.assertRaises(QueryError) as cm:
                to_lucene(q)
            self.assertIsInstance(cm.exception.position, int)

    def test_depth_and_length_limits(self):
        with self.assertRaises(QueryError):
            to_lucene("(" * 41 + "a" + ")" * 41)
        with self.assertRaises(QueryError):
            to_lucene("a " * 600)


class Injection(unittest.TestCase):
    def test_local_params_rejected_everywhere(self):
        for alg in ("boolean", "bm25", "fuzzy", "prefix", "phrase"):
            for q in ["{!dismax qf=text}foo", "foo {!lucene}bar", "{! type=edismax}x", "a { !func}b"[:0] + "{!func}1"]:
                with self.subTest(alg=alg, q=q), self.assertRaises(QueryError):
                    translate(q, alg)

    def test_special_characters_never_reach_solr_as_syntax(self):
        nasty = ['foo" OR title:secret', "a\\b", "x ^9", "q~9", "(a OR b", "_query_:\"{!dismax}x\"", "$x", "foo]", "[a TO b]",
                 "id:[* TO *]", "text:*", "a/b/", "x -", "!", "&&", "|| a", "foo:*", "a? b*"]
        for q in nasty:
            for alg in ("bm25", "fuzzy", "prefix", "phrase"):
                with self.subTest(q=q, alg=alg):
                    try:
                        tr = translate(q, alg)
                    except QueryError:       # refused outright (local params): also safe
                        self.assertIn("{!", q)
                        continue
                    if tr.empty:
                        continue
                    # only letters, digits and the syntax we add ourselves: ~1 ~2 trailing *, spaces
                    for tok in tr.params["q"].split(" "):
                        core = tok.rstrip("*")
                        if "~" in core:
                            core, n = core.split("~")
                            self.assertIn(n, ("1", "2"))
                        self.assertTrue(core.isalnum(), (q, tok))
            try:
                out = to_lucene(q)
            except QueryError:
                continue
            out = out.replace("id:[* TO *]", "")      # the one bracketed construct we emit ourselves (match-all)
            for bad in ("{", "}", "[", "]", "^", "~", "$", "\\", "_query_", "_val_"):
                self.assertNotIn(bad, out, (q, out))

    def test_boolean_output_only_uses_the_allowed_fields(self):
        out = to_lucene("title:a section:b text:c tag:d tags:e type:f source:g id:h secret:i")
        self.assertNotIn("id:", out.replace("tags:", ""))   # id:h and secret:i are plain phrases, not fields
        self.assertNotIn("secret:", out)

    def test_escape_term(self):
        self.assertEqual(escape_term("a+b"), "a\\+b")
        self.assertEqual(escape_term('"x"'), '\\"x\\"')
        self.assertEqual(escape_term("{!f}"), "\\{\\!f\\}")
        self.assertEqual(escape_term("a b"), "a\\ b")
        self.assertEqual(escape_term("plain"), "plain")


class Algorithms(unittest.TestCase):
    def test_bm25(self):
        t = translate("decision warranty", "bm25", 10, 0)
        self.assertEqual(t.params["q"], "decision warranty")
        self.assertEqual(t.params["qf"], QF)
        self.assertEqual((t.params["defType"], t.params["mm"], t.params["rows"], t.params["start"]), ("edismax", "1", "10", "0"))
        self.assertEqual(t.params["sort"], "score desc, id asc")
        self.assertEqual(QF, "title^3 section^2 tags^2 text")

    def test_bm25_drops_stopwords_like_the_js(self):
        self.assertEqual(translate("what is the spine", "bm25").terms, ["spine"])
        self.assertEqual(translate("the of", "bm25").terms, ["the", "of"])   # nothing else left: keep them
        self.assertEqual(translate("Spine spines", "bm25").terms, ["spine"])  # de-duplicated by stem

    def test_fuzzy_thresholds_match_maxedits(self):
        t = translate("cohrence warrnaty cat quantum", "fuzzy")
        self.assertEqual(t.params["q"], "cohrence~2 warrnaty~2 cat quantum~1")
        self.assertEqual(translate("architecture", "fuzzy").params["q"], "architecture~2")
        self.assertEqual(t.params["qf"], QF_UNSTEMMED)
        self.assertEqual([max_edits(x) for x in ("abc", "abcd", "abcdefg", "abcdefgh")], [0, 1, 1, 2])
        # thresholds are measured on the JS stem: "decisions" (9) -> "decision" (8) -> 2 edits; "spines" -> "spine" (5) -> 1
        self.assertEqual(stem("decisions"), "decision")
        self.assertEqual(translate("decisions spines", "fuzzy").params["q"], "decisions~2 spines~1")

    def test_prefix(self):
        t = translate("deci mean", "prefix")
        self.assertEqual(t.params["q"], "deci* mean*")
        self.assertEqual(t.params["qf"], QF_UNSTEMMED)

    def test_phrase_uses_pf_and_ps8(self):
        t = translate('"human in the loop"', "phrase")
        self.assertEqual(t.params["q"], "human loop")
        self.assertEqual((t.params["pf"], t.params["ps"], t.params["pf2"], t.params["ps2"]), (QF, "8", QF, "8"))

    def test_boolean_params(self):
        t = translate("decision AND (warranty OR canvas)", "boolean", 5, 20)
        self.assertEqual(t.params["q"], "(+decision +(warranty OR canvas))")
        self.assertEqual((t.params["mm"], t.params["tie"], t.params["rows"], t.params["start"]), ("100%", "1.0", "5", "20"))

    def test_empty_and_unknown(self):
        self.assertTrue(translate("", "bm25").empty)
        self.assertTrue(translate("   ", "boolean").empty)
        self.assertTrue(translate("!!!", "fuzzy").empty)
        with self.assertRaises(QueryError):
            translate("x", "semantic")

    def test_no_user_controlled_parameter_names(self):
        t = translate("x y", "phrase")
        self.assertLessEqual(set(t.params), {"defType", "fl", "rows", "start", "sort", "lowercaseOperators", "tie", "wt",
                                             "q", "qf", "mm", "pf", "pf2", "ps", "ps2"})

    def test_tokenizer_matches_js_rules(self):
        self.assertEqual(tokenize_parts("Human-in-the-loop, Zoë's"), ["human", "in", "the", "loop", "zoe", "s"])
        self.assertEqual(query_terms("Meaning Coherence"), ["meaning", "coherence"])


JS_DUMP = """
import { parse, ParseError } from %r;
const qs = JSON.parse(process.argv.at(-1));
const strip = (n) => { if (!n) return n; const o = { type: n.type }; if (n.field) o.field = n.field;
  if (n.children) o.children = n.children.map(strip); if (n.child) o.child = strip(n.child);
  if (n.type === 'prefix') o.prefix = n.prefix; if (n.type === 'term') o.n = 1; if (n.type === 'phrase') o.n = n.terms.length; return o; };
console.log(JSON.stringify(qs.map((q) => { try { return { ok: strip(parse(q)) }; } catch (e) { return { err: e.message, pos: e.position }; } })));
"""


def shape(n):
    if n is None:
        return None
    o = {"type": n["type"]}
    if n.get("field"):
        o["field"] = n["field"]
    if "children" in n:
        o["children"] = [shape(c) for c in n["children"]]
    if "child" in n:
        o["child"] = shape(n["child"])
    if n["type"] == "prefix":
        o["prefix"] = n["prefix"]
    if n["type"] == "term":
        o["n"] = 1
    if n["type"] == "phrase":
        o["n"] = len(n["terms"])
    return o


@unittest.skipUnless(shutil.which("node") and (REPO / "assets/search/boolean.mjs").exists(), "node or boolean.mjs not available")
class PythonParserEqualsJsParser(unittest.TestCase):
    QUERIES = [
        "decision AND (warranty OR canvas)", '"human in the loop"', "title:spine", "solver -quantum", "cohrence warrnaty", "deci",
        "meaning coherence", "+moloto framework", "warrant*", "type:booklet AND pedigree", "a | b || c & d && e", "NOT a", "!a b",
        "a OR -b", "-a -b", '"open', "a AND", "(a", "a)", "OR a", "title:", '""', "*", "()", "a OR OR b", "foo:bar", "state-of-art",
        "Café AND naïve", 'title:"decision ir" OR tag:page', "tags:x source:y.html text:z", "a - b", "a + b", "+ a", "(a (b (c OR d)))",
        "a b OR c d", "NOT NOT a", "+-a", "-+a", "x*y", "title:dec* section:me*", "it's", "1.5 AND v2", "e-mail*", "((a))", "a AND NOT b",
        "a NOT", "(a OR)", "and or not", "title:\"\"", "title:*", "ΑΒΓ delta*", "a\tb\nc", "\"a\" \"b\"", "\"a b\"c",
    ]

    def test_same_ast_shape_and_same_errors(self):
        script = JS_DUMP % str(REPO / "assets/search/boolean.mjs")
        out = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(self.QUERIES)], capture_output=True,
                             text=True, timeout=60, check=True).stdout
        js = json.loads(out)
        for q, expect in zip(self.QUERIES, js):
            with self.subTest(q=q):
                try:
                    got = {"ok": shape(parse(q))}
                except QueryError as e:
                    got = {"err": str(e), "pos": e.position}
                self.assertEqual(got, expect)


if __name__ == "__main__":
    unittest.main()
