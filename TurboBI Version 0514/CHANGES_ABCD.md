# Phase A+B+C+D changes — live partitions, custom SQL, blending, multi-DS visuals

This is the FULL-SCOPE branch. The companion AB-only branch lives at
`C:\Users\ShrikantPansare\Agent Version 0507_AB\` and contains only the
A+B subset.

## What changed (file:line list)

### A+B (same as the AB branch — see CHANGES_AB.md for the full A+B detail)
- `tableau_to_pbi/utils.py` — `escape_m_string` helper for M string literal
  escaping.
- `tableau_to_pbi/parser.py` — `_parse_connection` (federated-aware,
  captures port/schema/authentication/etc.) and
  `_parse_custom_sql_relations`. IR carries `customSqls`.
- `tableau_to_pbi/model.py` — `_LIVE_DIRECTQUERY_CLASSES` /
  `_EXTRACT_LIKE_CLASSES`; `_render_table_tmdl` branches on connection
  class; partition writes `mode: {partition_mode}`.
- `tableau_to_pbi/converter.py` — IR debug payload includes `customSqls`.

### C — Tableau data blending → Power BI relationships

#### `tableau_to_pbi/parser.py`
- Worksheet IR now carries a `datasourceDeps` list (`{datasource, columns}`
  per declared dependency), capturing every datasource the worksheet
  binds to. The legacy single-ds primary path still selects via
  `_pick_worksheet_datasource` for backwards compatibility, but
  `datasourceDeps` is the input to blend detection.

#### `tableau_to_pbi/model.py`
- `_synthesize_blend_relationships` (called from `build_model` after
  `_build_all_measures`). Walks every worksheet's `datasourceDeps`. For
  worksheets with 2+ data datasources (Parameters excluded), pairs the
  primary against each secondary and infers blend keys via the
  intersection of declared columns (case-insensitive). Falls back to
  the intersection of `col_locator` keys when one side declares no
  columns. **The blend-key candidate must be a real column on its TMDL
  table — measure-typed entries in `col_locator` are filtered out**
  (previous bug: `Total Distinct Patients` got picked as a blend key
  because the calc field was registered on `col_locator`).
- Cardinality decisioning:
  - Both sides have duplicate-named columns across multiple TMDL tables
    → log `[BLEND-WARN] using TREATAS fallback ...` and skip the
    relationship.
  - Primary side has duplicates → emit many-to-many + `[BLEND-WARN]`.
  - Otherwise → many-to-one from secondary to primary
    (`crossFilteringBehavior: oneDirection`, mirroring Tableau's blend
    semantics).
- `_has_duplicate_column` is a small helper that checks whether a column
  name appears on >1 TMDL table within a datasource.
- Relationship name is a deterministic 8-char hash of
  `(fromTable, toTable, key)` so re-runs are stable across conversions.

#### `tableau_to_pbi/model.py` — relationship serialization
- `write_tmdl` emits per-relationship `crossFilteringBehavior`,
  `fromCardinality`, `toCardinality` fields (previously hard-coded
  many-to-many).

### D — Multi-datasource visual binding

#### `tableau_to_pbi/report.py`
- Visual binding lookup walks the worksheet's full `datasourceDeps` when
  the primary's resolver doesn't find a field, instead of returning
  empty. Routes the binding to the secondary's TMDL table when the
  secondary owns the field. Logs `[BLEND] binding routed: '<field>' ->
  <ds>` so the routing is auditable per visual.
- Single-ds worksheets unchanged — the legacy path is the default and
  the multi-ds branch is opt-in based on `len(datasourceDeps) >= 2`.

### Cycle-breaking pass

PBI requires exactly **one active path between any two tables**. The blend
synthesis can produce two conflict shapes:

  - **Parallel keys** — two blend rels on different keys between the same
    table pair (e.g. `Extract (3) <-> Extract` on both `Date of Visit`
    and `Encounter ID`).
  - **Triangle** — three relationships forming a cycle (e.g.
    `Extract (3) -> Extract -> Extract (10)` plus
    `Extract (3) -> Extract (10)` direct).

Both raise `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` on load.
`_deactivate_ambiguous_paths` (called at the tail of
`_synthesize_blend_relationships`) walks the relationship list in
insertion order and:
  - Marks the second-and-later relationship in any unordered table-pair
    as `isActive: false`.
  - Uses union-find to detect cycle-closing relationships and marks
    them `isActive: false` too.

Inactive relationships are kept in the model — they can be invoked
explicitly via `USERELATIONSHIP(...)` in DAX, mirroring Tableau's
ability to use multiple blend keys in different visuals.

## Deferred / known issues

- **TREATAS DAX rewriting is logged, not emitted.** When both blend
  sides have duplicate keys, we skip the relationship and log a warning;
  we do NOT rewrite individual measures to use TREATAS. The user can
  fix these by hand, or upgrade C to emit a calculated table that
  materialises the join.
- **Snowflake blend-key inference is column-name-based.** Tableau
  workbooks don't expose explicit blend keys in the XML beyond the
  `<aliases enabled='yes'/>` flag, so we use shared column names. The
  inference is deterministic but won't match user-customised blend
  relationships set up via Tableau's "Edit Relationships" dialog.
- **D path for cross-ds aggregations** is naive — a measure from
  secondary on a primary visual works because the relationship
  propagates filter context, but the user may need to wrap it in
  `CALCULATE(..., USERELATIONSHIP(...))` if multiple blend rels share a
  primary table. Not currently emitted automatically.

## What's been verified on the Healthcare workbook

- 14 datasources (`Extract` through `Extract (14)`), 14 worksheets each
  with 2+ datasource dependencies → blending is the default mode.
- After the bug fix (filtering measures from `col_locator` blend-key
  candidates) the synthesized relationships use only real columns:
  shared dimensions like `Date of Visit`, `Reason for visit`,
  `Age category`. The pre-fix bug emitted relationships joining on
  measures like `Total Distinct Patients`, which PBI rejects.
- `[MEAS-DEDUP]` pass renames same-named measures across tables (e.g.
  `Total Visits`, `Race Percentage`, `Date Range`) to
  `<name> (<table>)`, preserving the canonical instance and updating
  cross-table DAX refs (e.g. `Metric - reason`'s SWITCH expression).
- Smoke-test corpus (UseCase / UseCase2 / Netflix / Merchandise /
  Superstore) — visual counts unchanged for non-blended workbooks.

## What I did NOT do that the user might expect

- See AB-only deliverable for non-goals on connection metadata,
  credentials, unsupported live connectors.
- **Did not auto-disambiguate USERELATIONSHIP for measures that span
  multiple blend relationships** — see "deferred" above.
- **Did not rewrite Tableau LOD calcs** that span blend boundaries —
  these need DAX-level rewriting beyond a simple model relationship.
- **Did not add `Implementation="2.0"` to Snowflake source** by default
  in this branch — the AB branch added it; this branch should match.
  TODO: fold A+B's Snowflake `Implementation="2.0"` change in if it's
  not already there.
