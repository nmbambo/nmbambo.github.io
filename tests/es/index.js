// Lets `node --test tests/es/` work on Node 22 (a bare directory argument is resolved as a module).
// Prefer `node --test "tests/es/*.test.mjs"` if you want per-file reporting.
const files = ['store', 'projector', 'adapters'];
Promise.all(files.map((f) => import(`./${f}.test.mjs`)));
