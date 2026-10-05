# nmbambo.github.io

Portfolio of Nkosinathi Mbambo, Meaning Coherence Assurer — scientist, engineer, analyst, modeller and architect. Built in the Ingqiqo Executables identity.

Plain HTML and CSS, no build step. Served by GitHub Pages from `main`.

## Structure

- `index.html` — the portfolio home
- `work/<slug>/index.html` — one page per project
- `assets/projects.js` — the cards under Selected work; each `url` points at a project page
- `assets/site.css`, `assets/rose.svg` — the Ingqiqo identity

When a project's public repository opens, add a link to it on that project's page.


## Pages (October 2026)

- `engage/` — Working together: what you receive, where to start, who decides what, warranties, commitments, sectors, data protection, partners.
- `notes/` — From the work: short pieces and three long arguments (illustrative samples only; no prices).
- `resources/` — The Ingqiqo library: online readers for five booklets (The Price of Better shows only its outline and Part One, pages 1–10; the rest is sold as the PDF edition, and nothing after page 10 leaves `build_readers.py`) (`resources/read/`, page images built by `scripts/build_readers.py` from the printed PDFs), free public PDF editions (`resources/pdf/`), and the Decision Dojo (`resources/dojo/`, self-contained; progress stays in the visitor's browser). Pages carrying prices, capacity figures, tender particulars, unfinished fields or rand amounts are held back in `build_readers.py`. Member sign-in shows "opening soon" until the member hosts are live. Check: `python3 tests/resources/e2e_resources.py`.
- `assets/rose.svg`, `assets/rose-small.svg` — Ingqiqo rose, revision 2 (Isibheqe geometry, amber sigil).

Public-site rule: no prices, rate cards, financials, capacity figures, client data or company particulars.

## Event-sourced search (October 2026)

- `search/` — Boolean search strings over the site and the public pages of four booklets, with an agent that proposes the search method (Thompson sampling over query shapes) and a person who decides.
- `assets/es/` — CQRS and event sourcing in the browser: append-only IndexedDB event store (idempotent by event id), mitt event bus (`assets/vendor/mitt.mjs`), idempotent projectors with checkpoints, adapters for Kafka REST, Solace PubSub+ REST and the reference-stack gateway (off by default in `config.json`).
- `assets/search/` — tokenizer, field-aware inverted index, Boolean parser, five algorithms (boolean, BM25, fuzzy, prefix, phrase), the agent.
- `scripts/build_corpus.py`, `scripts/cdc.py`, `.github/workflows/cdc.yml` — build-time change data capture into `data/events.ndjson` (append-only; ids are content hashes).
- `reference-stack/` — Docker Compose reference, light stack by default (about 0.5 GB): Postgres lakebase, NATS JetStream, a command service, Debezium Server, a query service (SQLite FTS5) and a KrakenD edge gateway on `127.0.0.1:8092`. Opt-in profiles: `heavy` (Kafka, REST proxy, Debezium Connect, KurrentDB, Message DB, gateway, projector), `solr`, `olake`, `solace`; plus the CDC head-to-head harness. The light stack and edge were verified live on 2026-10-04; the heavy profile on 2026-10-02; the CDC comparison is not yet run.
- Tests: `node --test "tests/es/*.test.mjs" "tests/search/*.test.mjs"`; `python3 -m unittest discover -s tests/cdc`; `python3 -m unittest` in `reference-stack/{gateway,projector,cdc/harness}`; live checks in `reference-stack/tests/live/` (`10_light.py`, `12_edge.py`).
