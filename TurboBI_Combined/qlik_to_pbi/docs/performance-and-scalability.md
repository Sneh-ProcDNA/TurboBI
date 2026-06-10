# Performance, memory & scalability

How the pipeline is structured to stay fast and degrade gracefully on
large apps (many sheets / tables / measures). The data flow is
single-pass with shared indexes computed once; the hot spots that used
to scale super-linearly have been removed. Behaviour and output are
unchanged — these are throughput/memory properties, not semantics.

## Data flow (single pass, typed IR, shared indexes)

```
parse_qlik_output(dir)  ->  QlikIR            (typed; read every input file ONCE)
SemanticModel(ir).build()                      builds, ONCE:
        field_table        field name -> owning table
        measure_by_id      qLibraryId -> measure name
        materialized_vars  Qlik var  -> measure name
        _field_resolver_cache  (memoised closure over field_table)
ir.release_raw_inputs()                        free heavy slots the model
        consumed but nothing reads again (app/script/loadmodel/
        engine_schema/fields/app_props) -> lower peak memory
ReportBuilder(ir, model).build_pages()         consults the model's
        indexes + its own incremental name index (never rebuilds them)
PBIPWriter(...).write(model, pages)            streams files to disk
preflight.run_preflight(out, name)             structural re-scan of disk
```

Nothing re-parses the input JSON; the CLI and the Flask UI both go
through `Converter.convert()`, which calls `parse_qlik_output` exactly
once.

### Typed IR (`ir.py`)

The parser returns a `QlikIR` dataclass (not a bare dict). It documents
the cross-stage contract — exactly which slots flow between
parser→model→report→writer — while keeping the historical mapping
interface (`ir.get(k)`, `ir[k]`, `ir[k] = v`, `k in ir`) so every
existing call site is unchanged. `SemanticModel` / `ReportBuilder` accept
either a `QlikIR` or a plain dict (`QlikIR.from_dict`), so back-compat
callers and the regression beds (which build `SemanticModel({"app":{}})`
directly) work verbatim.

`QlikIR.release_raw_inputs()` is called by `Converter.convert()` right
after `model.build()`. It drops only the slots with **no reader after the
model build** (`app`, `script`, `load_model`, `engine_schema`, `fields`,
`app_props`), lowering the resident set before the heavier report
build + write. Slots with live downstream readers are deliberately kept:
`sheets`/`master_objects`/`dimensions`/`variables` (report builder),
`script_blocks` (writer partition M + conversion report), `bookmarks`
(writer). The release is correctness-neutral — verified by byte-identical
output (`equiv_check.py`, 0 mismatched apps).

## Shared name index in the report builder (was quadratic)

Inline-measure / calc-column synthesis must dedupe each new name
against **every** existing measure and column (PBI rejects a measure
that shadows a column, case-insensitively). The original code rebuilt a
`reserved_ci` set from `model.measures` + every table's columns on
*every* synthesis call — O(measures × columns) per visual, i.e.
quadratic in app size.

Now `ReportBuilder` holds one case-insensitive set built lazily once
(`_reserved_names`) and updated in place at each commit site
(`_register_name`). Dedup is O(1) amortised. Measure-name → home-table
lookups that used to be a linear `next(m for m in model.measures …)`
scan per library measure are now an O(1) dict (`_measure_home` /
`_measure_home_cache`). The model's own `_build_measures` and
`_materialize_variables_as_measures` already used this incremental
idiom; the report now matches it.

Measured on the 73-measure app: per-call resolve cost is flat with size
where it previously grew (≈0.66 ms/call → ≈0.39 ms/call at 800 synthetic
measures), so a very large app no longer pays the quadratic tax.

`_make_field_resolver()` returns a closure over the live `field_table`
dict (not a snapshot), so it is memoised on the model
(`_field_resolver_cache`) instead of re-allocated on every translation.

## I/O layer (`utils.py`) — the dominant wall-clock cost

Writing a PBIP is thousands of small files; for a large app the writer
dominates total time. Two fixes, both behaviour-preserving:

- **`_long_path` uses `os.path.abspath`, not `Path.resolve()`.** The
  `\\?\` long-path prefix only needs an *absolute* path. `Path.resolve()`
  additionally canonicalises symlinks, which on Windows issues a
  `_getfinalpathname` / `realpath` syscall (it opens the target) on
  every file. `os.path.abspath` is pure string normalisation — no
  syscall. This was the single largest cost in the whole pipeline; it is
  now gone from the profile. The output tree is never a symlink, so
  there is no behavioural difference.
- **Process-wide mkdir cache.** `write_json` / `write_text` defensively
  `mkdir`-ed each file's parent on every call; the callers already
  create each target directory once before emitting a batch into it.
  `mkdir_p` / the writers now skip the `os.makedirs` syscall when the
  absolute dir string is already in `_MKDIR_CACHE`. `clear_mkdir_cache()`
  is called by `PBIPWriter.write` right after it `shutil.rmtree`s a
  stale output tree, so re-emitting into the same path in one process
  (the Flask UI / CLI can) re-creates the tree instead of trusting a
  stale "already made" hit.

End-to-end on the largest bundled app this is ~1.7× faster overall
(writer ~2.4×) with byte-identical output (verified by diffing the
emitted TMDL + visual JSON against the pre-change build, normalising
only per-run UUIDs and the machine-specific `RepoPath` default).

## DAX translator: shared single-pass preprocessing

`translate_qlik_to_dax` runs the legacy regex stage first, then the v2
tokenizer ONLY when legacy stubs. Both stages began by independently
running the **same** expensive string preprocessing on the same input:
strip Qlik comments → strip the leading `=` → expand `$(var)` / `$(=var)`
recursively (depth 6) → strip comments again. On every stubbed expression
that work ran **twice** (once per stage).

`_prepare_expr(expr, variable_lookup)` now performs that sequence ONCE in
the public entry; the prepared body is passed to both
`_translate_qlik_to_dax_legacy` and `_try_v2` via an optional `prepared`
parameter. Each stage's internal logic is byte-for-byte unchanged (the
original `expr` is still used for the `BLANK() /* qlik: … */` stub
comment); only the redundant prep is removed. Measured on the
legacy→v2 fallback path: `_expand_variables` drops from 2 calls → 1 and
the top-level `_strip_comments` from 4 → 2 per expression. Verified
behaviour-identical by the corpus/regression beds (`battery`, `battery2`,
`v3`, …) and the all-app `equiv_check.py`.

## God-function decomposition (report builder)

Two report-builder methods had grown past readability and are now split
into named, single-purpose helpers — **without changing emitted JSON**
(byte-identical, `equiv_check.py` clean):

- `_build_chart` (was ~384 lines) →
  `_collect_chart_fields` (category/value/tooltip resolution + sort-chain
  computation), `_build_sort_definition` (the `visual.query.sortDefinition`
  block), `_build_chart_objects` (the `objects` bag: legend / labels /
  axes / reference lines / cardVisual chrome / azureMap settings). The
  remaining `_build_chart` reads as the pipeline it always was: collect →
  project → query state → sort → objects → container styling → frame.
- `_resolve_measure` (was ~178 lines) →
  `_promote_native_agg_to_measure` (force-measure promotion for
  cardVisual / gauge), `_inline_measure_home` (home-table resolution),
  `_synthesise_inline_measure` (composite-expression measure synthesis).
  The top-level method is now the readable dispatch: library-id →
  bare-id safety net → native-agg fast path → inline synthesis.

Each helper is a pure extraction (same logic, same call order, same
data), so per-visual work stays cleanly separable — a prerequisite for
any future parallelisation of per-visual emit.

## What is intentionally NOT changed

- The **legacy DAX translator's per-stage logic** is preserved verbatim
  (the corpus tests are calibrated to its exact output) — only the
  duplicate preprocessing shared with v2 was hoisted out (above). Its
  inline `re.sub` literals rely on the `re` module's internal pattern
  cache; the v2 tokenizer already uses a module-scope precompiled
  `_TOKEN_RE`. Regex is <1 % of runtime, so it was left alone.
- TMDL rendering already uses list-append + `"\n".join(...)` (not
  O(n²) string concatenation in a loop), so the string building is
  already linear.
- `build_pages()` still returns a concrete `List` (not a generator):
  the regression suite and `Converter` depend on the list contract, and
  the realistic visual counts (≤ a few hundred) do not justify the API
  churn. The remaining writer cost is inherent OS file-open / JSON-encode
  work, not reducible without changing the output format.

## Verification harnesses (repo root, outside the package)

- `regression\*.py` — the 12-script, 168-assertion suite (must stay
  green; see `cli-and-pipeline.md`).
- `realapp_gate.py` — runs the full pipeline against every
  `uploads/*/output/unbuilt` and asserts 0 preflight warnings / 0 stub
  measures.
- `equiv_check.py` — builds each app with the current package AND a
  pristine backup, then diffs the content-bearing output (UUID/RepoPath
  normalised) to prove output equivalence after a perf change.
