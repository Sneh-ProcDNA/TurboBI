# Qlik→PBIP Converter — Documentation Index

In-repo source of truth for converter mechanics. Read on-demand from `CLAUDE.md`.

## Core
- [project-overview.md](project-overview.md) — engine-schema-as-truth, legacy-then-v2 translator, hypercube-only extract, ClearAll-on-connect invariants
- [cli-and-pipeline.md](cli-and-pipeline.md) — CLI flags, input/output layout, parser→model→DAX→report→writer stages, verification commands
- [feature-catalogue.md](feature-catalogue.md) — what visual types / DAX functions / formatting features ship today; quick lookup for "does it handle X?"
- [web-ui.md](web-ui.md) — Flask web UI (`python -m qlik_to_pbi.app`): routes, per-job lifecycle, CLI-vs-cloud-context auto-selection, categorised failure errors + always-on raw-output view

## Mechanics
- [data-fetch-modes.md](data-fetch-modes.md) — four partition modes, cloud/offline fetch flows, CSV matching tiers, direct QVF parse, bookmark fetch
- [dax-translator-architecture.md](dax-translator-architecture.md) — legacy→v2 two-stage, stub format, AGGX promotion, inline-measure synthesis, name sanitisation, variable materialisation, measure_lookup hook
- [tmdl-emit-rules.md](tmdl-emit-rules.md) — partition shapes, table-level annotations, formatString policy, summarizeBy, pre-flight validator
- [visual-and-emit-details.md](visual-and-emit-details.md) — visual styling pipeline (title in `visualContainerObjects`), text-expression evaluation (engine snapshot sidecar + static fallback → textbox VALUES not formulas), Qlik colour matching (registered theme + single-series primary), expression-dim calc columns, bar/combo subtype matrix, pie↔donut, slicer dropdown default, axis show/title from Qlik, KPI force_measure, map field-wells, sn-nav-menu, cardVisual chrome, azureMap viewport, sheet title resolution, script-derived renames/partitions, What-If params, visual_rules.json, engine-schema sidecar, engine field tags

## Gotchas & limitations
- [pbi-schema-gotchas.md](pbi-schema-gotchas.md) — visualContainerObjects allowlist, dangling-relationship trap, measure/column collisions, partition shapes
- [qlik-engine-cloud-handle.md](qlik-engine-cloud-handle.md) — never assume auto-open at handle 1; always GetActiveDoc/OpenDoc
- [known-limitations.md](known-limitations.md) — AGGR, QVD direct read, pivot-cell formatting, drill hierarchies, alternate states, conditional-visibility wiring (composite set analysis and bookmark capture are now resolved)

## Performance
- [performance-and-scalability.md](performance-and-scalability.md) — single-pass data flow, shared name index (was quadratic), `_long_path`/mkdir-cache I/O fixes, what was deliberately left unchanged, verification harnesses
- [large-data-strategy.md](large-data-strategy.md) — handling up to ~30M rows: why Power BI can't read QVD natively, why **Parquet** (not CSV) is the recommended emit format, Import-vs-DirectQuery, and a phased plan (Parquet emit → direct QVD→Parquet extraction)

## Working style
- [user-preferences.md](user-preferences.md) — prefers wide feature batches over automated testing
