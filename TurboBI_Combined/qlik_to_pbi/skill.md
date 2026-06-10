# Qlik → PBIP Converter — Skill Guide

Quick-reference for changing this converter. Pair with `CLAUDE.md` (the
comprehensive reference) — this file is the index of *rules you must not
break* and *places to look first*.

## Architecture in one diagram

```
.qvf / unbuilt JSON ──► parser.py ──► ir dict ──► model.py ──► writer.py ──► .pbip
                          │                          │
                          │                          ├──► report.py (visuals)
                          │                          └──► preflight.py (validation)
                          │
                          └──► engine_fetch.py (data extract via WebSocket)
                          └──► engine_unbuild.py (engine-based JSON unbuild)
```

## Source-of-truth chain (CRITICAL)

Always honour this priority order. Breaking it produces wrong data
types, misaligned columns, or dangling refs.

| Concern | Source 1 (authoritative) | Source 2 (fallback) | Source 3 (last resort) |
|---|---|---|---|
| Table list + field list | `engine-schema.json` sidecar | `loadmodel---loadmodel.json` | — |
| Field types (int / double / date / text) | Engine qTags via `type_from_qlik_tags` | CSV sniffer (`csv_schema.py`) | All-string stub |
| Column ORDER in TMDL | CSV header order (if CSV present) | Engine field order | Loadmodel order |
| Relationships | `qk` records in engine schema | Shared-name inference | None |
| Field rename recovery | `script_parser.parse_field_renames` | Engine `name`/`alias` | None |
| Bookmarks | `bookmarks.json` (engine-extracted) | None (cloud CLI omits them) | — |
| DAX measure body | Legacy regex translator | v2 tokenizer | `BLANK() /* qlik: ... */` stub |

## Non-negotiable rules

### TMDL emission
- `dataType` valid values: `string`, `int64`, `double`, `decimal`, `dateTime`, `boolean`, `binary`. Never invent new ones.
- `Entity` is the canonical key inside `field.Column.Expression.SourceRef.Entity`. `Source` is reserved for query aliases and causes silent visual load failures.
- `annotation PBI_NavigationStepName` and `annotation PBI_ResultType = Table` go at **table scope** (one tab), AFTER the partition body, with blank lines between. NOT inside the partition block.
- `annotation PBI_Parameter = True` at table scope marks a What-If parameter table for PBI Desktop's Modeling > Parameters pane.
- Measure-vs-column name collisions are CASE-INSENSITIVE within a table. PBI rejects load on collision. Apply `(Measure)` suffix.
- Relationship `fromColumn` / `toColumn` MUST reference columns that exist on the named table — `_prune_dangling_relationships` enforces this.

### `visualContainerObjects` allowlist
Only these keys are accepted by PBI's schema. Adding any other key
makes Desktop refuse to load the entire report with "Cannot resolve
all paths…":
```
background, border, padding, visualHeader, stylePreset, divider,
outspacePane, title, general, shadow, lockAspect
```
Title text belongs under `visual.objects.title[]`, NOT
`visualContainerObjects.visualTitle` (we tried; Desktop rejects).

### Visual JSON schema
- Sort: `visual.query.sortDefinition.sort[]`, NOT per-projection `sortDirection`. Per-projection emits cause PBI to reject the file.
- Empty `queryState` slots get omitted entirely. `{projections: []}` is rejected by the validator.

### Format strings
- **Do NOT stamp a per-type default like `'#,##0'`** on numeric columns. PBI renders the format pattern literally when the M cast doesn't deliver the matching storage type. Numeric / string columns get NO `formatString`. Dates do.
- Numeric Qlik patterns must be normalised via `_normalise_numeric_format` (`###,#` → `#,##0`, etc.) before emission. DAX rejects patterns without a terminating `0`.
- For measures with no explicit Qlik `qFmt`, emit `""` and let the underlying column drive the format.

### Measure name sanitization (`_sanitize_measure_name`)
DAX forbids these characters in identifiers: `. , ; : / \ * | ? & % $ ! + = ( ) [ ] { } < > ' " @ # ` ~ ^`. PBI silently rejects measures with any of them. Apply the sanitizer in BOTH places:
- `model._build_measures` (library measures)
- `report._resolve_measure` (inline measures)
Hyphens are KEPT (valid in DAX, needed for compound IDs).

### Count(distinct X)
Has **no portable native column-aggregation slot** in PBI's
`IQueryAggregateFunction` enum (Function: 5 means CountNonNull, not
distinct). `_native_aggregation_projection` returns `None` for
`Count(distinct …)`; the inline-measure synthesis path produces an
unambiguous `DISTINCTCOUNT('Table'[Col])` DAX measure.

### CSV column alignment
- Header and data row positions MUST match. The CSV writer in `engine_fetch._write_csv` writes both from the same `resolved` list, in the same order.
- The hypercube extract path (`extract_table`) submits dimensions in `resolved` order; engine returns row cells in that same order. **Caller-controlled column ordering is the only thing we trust.**
- `Doc.GetTableData` was tried and rejected — its `qValue[]` order disagrees with `GetTablesAndKeys.qFields` order for joined tables. Stay on hypercube.

## Where to look first

| Symptom | Likely file / function |
|---|---|
| `'1'` / `'5'` quoted numbers in data view | CSV column has wrong type → check `type_from_qlik_tags` + sniffer + M cast |
| `#,##0` shown literally in cells | `formatString` stamped on a numeric column → `_render_table_tmdl` |
| "Field was deleted from the model" warning | Measure name has DAX-forbidden chars → `_sanitize_measure_name` |
| "Cannot resolve all paths…" Desktop error | Run `preflight.run_preflight` to localise; likely `visualContainerObjects` key, dangling relationship, or `Entity` typo |
| "An additional property 'X' was included in /…" | Visual JSON has a PBI-rejected key. Check `visual.query` (sortDefinition not sortDirection), `visualContainerObjects` keys |
| "The value for option 'Culture' is invalid." | `Table.TransformColumnTypes` got an extra positional arg — the 3rd param is `culture`, not `MissingField`. Use the 2-arg form. |
| "Pending changes that haven't been applied" | Missing `annotation PBI_NavigationStepName` on the partition table |
| Empty visual referencing a measure | Measure name has `(`, `)`, `[`, `]` → sanitize. Or visual references stale measure name from older build. |
| Extra rows in extracted CSV | Hypercube cross-product across multiple tables → check `_extract_table_field_pairs` groups fields per table |
| Wrong column data in CSV | CSV from a previous broken extraction. Re-run `--fetch-via-engine`. |
| Misaligned CSV header vs data | Same — header and data come from same source; if misaligned, the CSV is stale. |
| Bookmarks missing in PBIP | Cloud CLI doesn't export them. Use `--fetch-via-engine` (calls `_write_bookmarks`). |
| `Count(distinct X)` returns wrong value | Native-agg routing bug. Confirm `_native_aggregation_projection` returns `None` for distinct count. |

## How to verify any change

```bash
# Smoke test the metadata layer (no data needed)
python -m qlik_to_pbi --input ./output --output ./out

# With CSV bindings
python -m qlik_to_pbi --input ./output --data-dir ./csv_exports --output ./out

# Full offline extract + convert
python -m qlik_to_pbi --qvf-path "<path/to/app.qvf>" --fetch-via-engine --output ./out

# Pre-flight only (skip writes, just lint an existing PBIP)
python -m qlik_to_pbi --input ./output --output ./out --report-only
```

Open the PBIP in Power BI Desktop after each run; the `preflight.run_preflight` step also catches schema-violating emissions before they reach Desktop.

## Don'ts (collected from past mistakes)

1. **Don't put `PBI_ResultType` or `PBI_NavigationStepName` inside the partition block.** They go at table scope (1 tab).
2. **Don't pass `MissingField.UseNull` as the 3rd arg to `Table.TransformColumnTypes`.** That position is `culture`, not a MissingField option. The cast errors as "The value for option 'Culture' is invalid."
3. **Don't trust `GetTableData`'s column order.** Use hypercube with caller-controlled dim order.
4. **Don't synthesise What-If parameters from arbitrary numeric Qlik variables.** Most are internal counters. The function exists for opt-in callers; don't enable it by default.
5. **Don't map `CountDistinct` to `Function: 5`.** That value means `CountNonNull`. Route distinct count to DAX measure synthesis.
6. **Don't sanitise hyphens out of column names.** They're valid in DAX and break compound IDs (`From_HCP_ID-HCP_ID`).
7. **Don't stamp `formatString: '#,##0'` on every numeric column by default.** PBI renders the format string as text when the underlying cast fails.
8. **Don't assume the CSV header order matches the loadmodel field order.** It doesn't — header order comes from the CSV file itself (sniffer-determined).
9. **Don't add a key to `visualContainerObjects` without checking the allowlist.** PBI rejects the whole report load.
10. **Don't bypass `_sanitize_measure_name`.** Raw Qlik measure labels often contain DAX-forbidden chars.

## File map (only the load-bearing modules)

| Module | Owns |
|---|---|
| `parser.py` | Reads unbuilt JSON → IR dict. Knows nothing about PBI. |
| `model.py` (`SemanticModel`) | TMDL emission. Owns column / measure / relationship lists, partition shapes, format strings. |
| `report.py` (`Report`) | Per-visual JSON. Owns the projection / queryState / sortDefinition / visual styling logic. |
| `writer.py` (`Writer`) | Filesystem layout — page folders, bookmarks, .pbip wrapper. |
| `engine_fetch.py` | WebSocket → engine extracts to CSV. Hypercube path; `_refresh_field_lists_from_engine` writes the sidecar. |
| `engine_unbuild.py` | WebSocket → unbuilt JSON (replaces cloud CLI). `_write_bookmarks` is the only bookmark source. |
| `csv_schema.py` | CSV sniffer + `type_from_qlik_tags` mapper. |
| `script_parser.py` | Recovers `field_renames` from autogenerated load script. |
| `script_to_m.py` | Builds Power Query M from script LOAD blocks. |
| `dax_translator.py` / `dax_translator_v2.py` | Qlik expr → DAX. Legacy regex then v2 tokenizer. |
| `preflight.py` | Post-emit structural validator. Adds warnings to conversion report. |
| `visual_rules.json` | External map: Qlik visualization type → PBI visualType. Edit this to add a new visual, not Python. |

See `CLAUDE.md` for the comprehensive reference — every section above has a corresponding detailed write-up there.
