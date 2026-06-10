# Qlik (QVF) → Power BI Project (PBIP) Converter

Python package at `qlik_to_pbi/` (~12k LOC). Entry: `python -m qlik_to_pbi`.
Converts a Qlik Sense app (an unbuilt JSON IR folder, or a `.qvf` directly)
into a `.pbip` that opens in Power BI Desktop. Pipeline: parser → model →
DAX translator (legacy regex, then v2 tokenizer) → report builder → writer,
orchestrated by `converter.py` / `__main__.py`. A UI-less copy lives at
`qlik_to_pbi_cli/`; the optional Flask web UI is `python -m qlik_to_pbi.app`.

## How to work here

- **Simplicity first.** The minimum change that solves the task — no
  speculative features, abstractions, or configurability that wasn't asked for.
- **Surgical.** Touch only what the request needs; match surrounding style;
  don't refactor unrelated code. Flag pre-existing dead code, don't delete it.
- **Verify, don't assume.** Every change is gated by the regression suite + a
  real-app build (below). State assumptions; if the ask is ambiguous or a
  simpler path exists, say so before coding.
- **Behavior-preserving by default.** This tool's value is its accumulated
  edge-case correctness — carry logic forward, never silently re-derive it.

## Non-negotiable invariants (deeper context in `qlik_to_pbi/docs/`)

- Engine schema (`GetTablesAndKeys`) is authoritative; loadmodel is fallback only.
- DAX translator order: legacy regex pipeline first, then the v2 tokenizer for stubs.
- Data extract uses the hypercube only (never `Doc.GetTableData` — column-order mismatch).
- Always `GetActiveDoc`/`OpenDoc` before any cloud Engine API call — never assume handle 1.
- `Doc.ClearAll` on every engine connect, or saved selection state filters the extract.
- Sanitize every measure name via `_sanitize_measure_name` — DAX-forbidden chars silently fail visuals.
- Prune dangling relationships before write — one missing endpoint fails the whole load with no per-file context.
- Don't stamp `formatString` on generic numeric columns — PBI renders it as cell text on a storage-type mismatch.
- The parser returns a typed `QlikIR` dataclass (dict-compatible); the model releases the raw IR after build.
- Text-expression evaluation (`text_eval.evaluate_unbuilt_expressions`) runs AFTER `_write_bookmarks` — its trailing ClearAll guarantees unfiltered snapshots; consult the sidecar with `is None` checks (empty-string results are legitimate).
- `themeCollection.customTheme` requires `reportVersionAtImport` (report schema 3.2.0, additionalProperties false) — omit it and the report fails schema validation.
- Per-visual chart colour = `objects.dataPoint[].defaultColor` — `dataColors` is NOT a real object name (Desktop silently ignores it); palettes go through the registered report theme (`pbi_theme.py`), never multi-entry selector-less fills.
- KPI value colour lives in `conditionalColoring.paletteSingleColor` whenever `useConditionalColoring` is falsy — do NOT gate on the `singleColor` enum (real apps store 3, not 2).
- SUM/AVERAGE over a string column on a BOUND table must be coerced to `SUMX/AVERAGEX(IFERROR(VALUE(...)))` (model pass 3) — the raw form returns TEXT and PBI renders cards as `'21'`; never "fix" it by promoting the bound column's dataType (load fails).
- Web-UI progress stages key on module log prefixes (`[UNBUILD]`, `Engine extract`, `[1-4/4]`, `[WRITE]`, `[UI] Zipping`) — generic keywords (writ|output|done) appear in every stage and mis-jump the bar.

## Verify every change (hard gate)

- **Regression:** run each bed in `regression/*.py` (`python regression/<bed>.py`); each must exit 0 (~153 assertions).
- **Real-app build** must stay preflight-clean at fixed anchors:
  `python -m qlik_to_pbi --input qlik_to_pbi/uploads/<app>/output/unbuilt --output <tmp>`
  → prints `Pre-flight: no structural issues found`. Anchors:
  `3027f737…` = 23 tables / 230 cols / 67 measures / 11 rels / 0 stubs; `9599dcee…` = 6 / 54 / 0 / 4 / 0.
- After any package change, re-sync the UI-less copy `qlik_to_pbi_cli/` (mirror every `*.py` except `app.py` / `templates/`).

## Where everything lives

Detailed mechanics, schema gotchas, the feature catalogue, partition shapes, and
verification commands live in `qlik_to_pbi/docs/` — start at
`qlik_to_pbi/docs/INDEX.md`. Read on demand; none are pre-loaded.
