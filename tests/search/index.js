// Lets `node --test tests/search/` work on Node 22 (a bare directory argument is resolved as a module).
// Prefer `node --test "tests/search/*.test.mjs"` for per-file reporting.
const files = ['tokenize', 'index', 'boolean', 'algorithms', 'agent', 'search', 'bench'];
Promise.all(files.map((f) => import(`./${f}.test.mjs`)));
