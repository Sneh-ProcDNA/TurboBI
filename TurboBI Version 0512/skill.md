# Tableau-to-Power-BI Skill Catalog

What the converter knows how to translate, with concrete patterns. Each entry shows: Tableau input → DAX/TMDL output, plus the location in code.

## DAX translation skills (`dax_translator.py`)

### Aggregations
| Tableau | DAX | Notes |
|---|---|---|
| `SUM([X])` | `SUM('T'[X])` | Direct |
| `AVG([X])` / `AVERAGE([X])` | `AVERAGE('T'[X])` |  |
| `COUNT([X])` | `COUNT('T'[X])` |  |
| `COUNTD([X])` / `CNTD([X])` | `DISTINCTCOUNT('T'[X])` |  |
| `MIN/MAX/MEDIAN/STDEV/VAR` | Direct passthrough rename |  |

### Aggregations with measure-aware unwrapping
- `SUM([SomeOtherCalc])` where `SomeOtherCalc` resolves to a measure → drops the SUM wrapper, emits the measure ref bare. PBI rejects `SUM('T'[Measure])` with "The SUM function only accepts a column reference".

### Level of Detail (LOD)
| Tableau | DAX |
|---|---|
| `{ FIXED : agg(X) }` | `CALCULATE(<dax-agg>, ALL('T'))` |
| `{ FIXED [Dim] : agg(X) }` | `CALCULATE(<dax-agg>, ALLEXCEPT('T', 'T'[Dim]))` |
| `{ FIXED [D1], [D2] : agg(X) }` | `CALCULATE(<dax-agg>, ALLEXCEPT('T', 'T'[D1], 'T'[D2]))` |
| `{ FIXED YEAR([Date]), MONTH([Date]) : agg }` | maps to synthesized `Year of Date` + `Month Number of Date` cols |
| `{ INCLUDE / EXCLUDE ... }` | **NOT translated** (drops with `[DAX-DROP]`) |

Implementation: placeholder substitution (`__LOD_BLOCK_N__`) so LOD-translated DAX doesn't get re-tokenized.

### Date / Time
| Tableau | DAX |
|---|---|
| `DATEDIFF('day', A, B)` | `DATEDIFF(A, B, DAY)` (arg-order swap, unquoted enum) |
| `DATEPART('year', X)` | `YEAR(X)` (and `MONTH`/`QUARTER`/`DAY`/`WEEKNUM`/`HOUR`/...) |
| `DATETRUNC('month', X)` | `DATE(YEAR(X), MONTH(X), 1)` |
| `DATETRUNC('quarter', X)` | `DATE(YEAR(X), (QUARTER(X) - 1) * 3 + 1, 1)` |
| `DATETRUNC('year', X)` | `DATE(YEAR(X), 1, 1)` |
| `DATETRUNC('week', X)` | `(X) - (WEEKDAY(X, 2) - 1)` (Monday-start week) |
| `DATETRUNC('day', X)` | `INT(X)` |
| `DATENAME('month', X)` | `FORMAT(X, "MMMM")` |
| `DATENAME('year', X)` | `FORMAT(X, "YYYY")` |
| `DATENAME('quarter', X)` | `"Q" & QUARTER(X)` |
| `DATENAME('weekday', X)` | `FORMAT(X, "dddd")` |
| `DATE(X)` cast | `DATEVALUE(X)` (works on date AND string inputs) |
| `MAKEDATE(y, m, d)` | `DATE(y, m, d)` |
| `MAKETIME(h, m, s)` | `TIME(h, m, s)` |
| `MAKEDATETIME(date, time)` | `DATE(...)` (degraded — no DAX combine) |
| `DATEPARSE('format', str)` | `DATEVALUE(str)` (drops format arg) |
| `PARSEDATE(str)` | `DATEVALUE(str)` |
| `TODAY()` / `NOW()` | `TODAY()` / `NOW()` |
| `DATEADD('month', -1, X)` | passthrough rename to DAX `DATEADD` |

### Conditionals
| Tableau | DAX |
|---|---|
| `IF [cond] THEN x ELSE y END` | `SWITCH(TRUE(), <cond>, x, y)` |
| `IF(cond, x, y)` | `IF(cond, x, y)` |
| `IIF(cond, x, y)` | `IF(cond, x, y)` |
| `IIF(cond, x, y, unknown)` | `IF(ISBLANK(cond), unknown, IF(cond, x, y))` |
| `CASE x WHEN a THEN p WHEN b THEN q ELSE r END` | `SWITCH(x, a, p, b, q, r)` |

### Strings
| Tableau | DAX |
|---|---|
| `LEN/UPPER/LOWER/TRIM/LTRIM/RTRIM` | direct passthrough |
| `LEFT/RIGHT/MID` | direct passthrough |
| `FIND/REPLACE/CONTAINS` | passthrough rename to `FIND` / `SUBSTITUTE` / `CONTAINSSTRING` |
| `STR(x)` | `FORMAT(x, "")` |
| `SPLIT(s, delim, n)` | `PATHITEM(SUBSTITUTE(s, delim, "|"), n)` (since DAX has no native split) |
| `REGEXP_EXTRACT` / `REGEXP_REPLACE` / `REGEXP_MATCH` | **NOT translated** (DAX has no regex) |

### Table calculations
| Tableau | DAX | Notes |
|---|---|---|
| `TOTAL(expr)` | `CALCULATE(expr, ALLSELECTED())` | Percent-of-total pattern |
| `ATTR([X])` | `SELECTEDVALUE('T'[X])` | Single-value-or-blank semantic |
| `INDEX()` | NOT translated | Needs PBI Visual Calculations |
| `RANK_UNIQUE` / `RANK_DENSE` | NOT translated | Needs `RANKX` rewrite |
| `RUNNING_SUM` / `WINDOW_AVG` | NOT translated | Needs DAX window functions or visual calcs |
| `LOOKUP(expr, n)` / `PREVIOUS_VALUE` | NOT translated | Visual calcs |

### Misc
| Tableau | DAX |
|---|---|
| `ZN(x)` | `IFERROR(x, 0)` |
| `ISNULL(x)` | `ISBLANK(x)` |
| `AND` / `OR` / `NOT` | `&&` / `\|\|` / `NOT` |
| `<>` / `!=` | `<>` |
| `==` | `=` |
| `TRUE` / `FALSE` / `NULL` | `TRUE()` / `FALSE()` / `BLANK()` |
| `// line comment` | stripped (DAX has no inline comments) |
| `/* block comment */` | stripped (multi-line OK) |

### Tableau internals
| Tableau | DAX |
|---|---|
| `COUNT([__tableau_internal_object_id__].[<table-id>])` | `COUNTROWS('Table')` (Tableau's row counter) |
| `Calculation_<bigint>` measure name | hidden in field pane (`isHidden: true`) but DAX still callable |

## Model-building skills (`model.py`)

### Trivial-alias calc-field resolution
Tableau auto-generates calc fields with cryptic names like `Calculation_4313392949811654658` whose formula is just `// caption\n[X]` (a pure rename). The converter now handles these inherently:
- `parser._merge_worksheet_calc_fields` picks up worksheet-local `<view>/<datasource-dependencies>/<column>` entries that the top-level datasource walk misses, and appends them to the owning datasource's column list (logs `[CALC-INDEX] merged <n>`).
- `model._register_calc_alias_resolutions` then walks every calc field, detects formulas matching `[X]` / `[X (TableHint)]` / `// caption\n[X]` (with `group`/`bin`/`parameter`/`calculation` excluded as non-table suffixes), resolves the inner ref to a real column, and registers the calc-field name + caption in `col_locator` against that column. Logs `[CALC-ALIAS] '<calc_name>' -> <table>.<column>`.

Corpus impact: across the smoke-test corpus the standalone converter went from ~100 `[RESOLVE]` warnings (16 of which the agent's formula resolver was patching via hint sidecars) down to ~27 warnings and **0 hints needed** — the agent's `calc_index.py` + `formula_resolver.py` are now a redundant safety net rather than a required pass.

### Calculated columns vs measures
- **Calc field with `role='measure'` + formula** → DAX measure.
- **Calc field with `role='dimension' + datatype='boolean'` + formula** → DAX calculated COLUMN (row-level eval). Same-table `MIN(...)` wrapping is stripped so it works in row context. Cross-table param refs keep `MIN()` (1-row tables).
- **Tableau group / categorical-bin / bin block** (`<calculation class='group'|'categorical-bin'|'bin' column='[Src]'>` with `<bin value='"Label"'><value>...</value></bin>` children, no `formula` attribute) → DAX calculated column with `SWITCH(TRUE(), [src] IN {...}, "Bucket", [src])`. The calc column is re-parented to the SOURCE column's TMDL table so the bare `[src]` ref resolves in row context. Per-group log line: `[GROUP] '<name>' -> DAX calc column on table='<tbl>' (source='<col>')`. Filter-on-group-label is also expanded via `report._expand_group_filter_members` so a Tableau filter that picks a bucket label is rewritten as `[Src] IN { members }` — works whether or not the calc column emit succeeded.

### Date hierarchies (auto-synthesized per dateTime column)
For every `dateTime` column on data tables (not parameter tables), the writer emits:

```tmdl
column 'Year of <X>' = YEAR([<X>])           // int64
column 'Quarter of <X>' = "Qtr " & ROUNDUP(MONTH([<X>]) / 3, 0)  // string
column 'Month Number of <X>' = MONTH([<X>])  // int64, hidden, sortByColumn for Month
column 'Month of <X>' = FORMAT([<X>], "MMMM") // string
column 'Day of <X>' = DAY([<X>])              // int64
column 'Year-Trunc of <X>' = DATE(YEAR([<X>]), 1, 1)
column 'Year-Quarter of <X>' = DATE(YEAR([<X>]), (QUARTER([<X>])-1)*3+1, 1)
column 'Year-Month of <X>' = DATE(YEAR([<X>]), MONTH([<X>]), 1)

hierarchy '<X> Hierarchy'
    level Year     column: 'Year of <X>'
    level Quarter  column: 'Quarter of <X>'
    level Month    column: 'Month of <X>'
    level Day      column: 'Day of <X>'
```

### Parameter tables
- One table per parameter (not lumped).
- List params: two columns (`Value`, `Label`), one row per `<member>`.
- Any/range params: single column `Value`, one row with current/default value.
- Date hierarchies skipped (single-row tables don't need them).

### Blend relationships (Phase C)
- Synthesized many-to-many `bothDirections` rels between tables that share a column name across worksheets that bind to multiple datasources (`_synthesize_blend_relationships`).
- Blend keys taken from `worksheet.datasourceDeps[*].columns` intersection (case-insensitive); fallback to `col_locator` overlap scoped to the names the worksheet actually mentioned.
- Measure-typed `col_locator` entries are filtered out so a measure name never becomes a relationship endpoint.
- BOTH-sides-duplicate keys: logged as `[BLEND-WARN] using TREATAS fallback` and skipped (no auto TREATAS emit).
- Cycle-breaker `_deactivate_ambiguous_paths` walks rels in insertion order; the unordered-pair seen-set catches parallel keys, union-find catches triangle cycles; redundant rels get `isActive: false` but stay in the model so DAX can invoke them via `USERELATIONSHIP(...)`.
- Relationship name: deterministic `blend_<8-char-hash>` of `(fromTable, toTable, key)` — stable across re-runs.

### Multi-datasource visual binding (Phase D)
`_resolve_visual_field` (report.py) is the unified resolver for visual / filter / sort fields. When a field carries a `binding_ds` (Tableau encoded a non-primary datasource on the encoding / filter), it looks up `(binding_ds, fname)` through the secondary's `col_locator` first and logs `[BLEND] binding routed: '<field>' -> ds=<secondary> table=<tbl> (primary ds=<primary>)`. Single-ds worksheets bypass this path entirely. The Phase C relationship is what makes the cross-ds projection actually join at query time.

### Connection classes (partition M emit)
- `hyper` / `excel-direct` / `csv` / `textscan` / `json` / `federated` / `""` → CSV-from-Hyper extract path. `mode: import`.
- `sqlserver` live → `Sql.Database(server, db)` + `[Schema=..., Item=...]` nav. `mode: directQuery`.
- `postgres` live → `PostgreSQL.Database("host:port", db)` + `[Schema=..., Item=...]` nav (default schema `"public"`, default port 5432). `mode: directQuery`.
- `snowflake` live → `Snowflake.Databases(server, warehouse, [Implementation="2.0"])` + DB / Schema / Name nav. Default schema `"PUBLIC"`. `mode: directQuery`.
- `databricks` / `azure-databricks` / `azuredatabricks` / `databricks-sql` / `spark-sql` / `spark` live → `DatabricksMultiCloud.Catalogs(server, http_path, [Catalog="", Database="", QueryTags=null, EnableAutomaticProxyDiscovery=null, Implementation="2.0"])` + Catalog / Schema / Name nav. Default catalog `"hive_metastore"`, default schema `"default"`. Connector function overridable via `connector_function` in the credentials file. `mode: import`.
- Custom SQL on sqlserver / postgres / snowflake / databricks → `Value.NativeQuery(Source, "<escaped-sql>", null, [EnableFolding=true])`. SQL escaped via `utils.escape_m_string` (`"` → `""`, `\n` → `#(lf)`, `\t` → `#(tab)`).
  - **Tableau-embedded** custom SQL (`<relation type='text'>`) now wins over a bound Hyper/CSV extract when the inner connection class is live — previously the extract always won and the SQL was silently ignored. Logs `[CONN] '<table>' has customSql + live class '<cls>' — dropping extract path and emitting Value.NativeQuery instead of CSV bind`.
  - **Credentials-file override**: a `query` / `custom_sql` / `sql` field on a `CredentialEntry` stamps `customSql` + `force_live_for_custom_sql=True` onto the matched connection. The override always beats Tableau-embedded SQL.
- `redshift` and other live classes (oracle / mysql / bigquery / …) → placeholder `Table.FromRows({}, type table [...])` + `// TODO: live connection class '<cls>'` comment + `[CONN] live connection class '<cls>' — emitting placeholder` log. `mode: import`.

### Credentials-driven connection overrides
- `--credentials <file.json|.xlsx>` (CLI) or `credentials_path=` (Python API) loads a `CredentialStore` of class / server / database / port / schema / warehouse / http_path / catalog / connector_function / username / password / token entries.
- Match priority: `datasource`/`caption` exact → `class` + `server` → `class` only → safe fallback (single Databricks entry catches Tableau extract / federated workbooks).
- Overrides are applied in `write_tmdl` before partition-M emit, so a credentials file can redirect a workbook from dev → prod, or repoint a Tableau Hyper-extract workbook at a live Databricks warehouse (via `prefer_live_over_extract`, default `True`, plus `_is_databricks_live_connection` recognising server + http_path).
- Passwords / PATs are **never** written to the M expression. They surface only in `credentials_manifest.json` as `has_password` / `has_personal_access_token` flags — the manifest is the input for PBI Desktop "Data source settings" or the Power BI REST API.

## Visual picker skills (`visual_picker.py` + `visual_rules.json`)

### Mark → visualType
Direct lookup table. Notable mappings (after fixes):
- `bar` → `barChart` (with orientation override below)
- `circle` → `scatterChart` (was `pieChart` — fixed)
- `pie` / `donut` → `pieChart` / `donutChart`
- `point` / `polygon` / `filled-map` / `multipolygon` / `map` → `azureMap` (was `map` / `filledMap`; the legacy slot entries are kept as no-op fall-throughs)
- `automatic` → falls through to auto rules

### Map default viewport (North America)
Every map visualType the converter emits (`azureMap` plus the legacy `map` / `filledMap` / `shapeMap` bags) gets a `mapSettings` bag with `view: 'UnitedStates'` + `customZoom: 3` + `customCenterLat/Lon: 39.5, -98.35`, plus a `controls` bag with `autoZoom: false`. Alternate property names (`zoom`, `centerLat`, `centerLong`, `predefinedView`) are emitted alongside the canonical ones because Azure Maps' property keys vary across Desktop builds — PBI silently drops unknown keys.

### Table & matrix header styling
`tableEx` and `pivotTable` always emit a `columnHeaders` bag (and `pivotTable` also `rowHeaders`) — never empty. Source precedence:
1. `parser._parse_worksheet_header_style` — picks up `<style-rule element='column-header'|'row-header'|'header'>` attrs (`color`, `font-*`, `background-color`, `text-align`); scoped/subtotal overrides skipped.
2. Worksheet `titleStyle` — same-font-as-title heuristic.
3. Worksheet `labelStyle` — final font fallback.
4. Tableau-like defaults — Arial 10pt, color `#1b1b1b`, bold, center-aligned.

Properties emitted: `fontFamily`, `fontSize`, `fontColor` (solid envelope), `bold`, `italic`, `underline`, `alignment` (Left/Center/Right capitalised), `backColor`.

### Bar orientation (override)
- Continuous on COLS, dim on ROWS → `barChart` (horizontal bars)
- Continuous on ROWS, dim on COLS → `columnChart` (vertical bars)

`_CONTINUOUS_AGGS` includes measure aggregations PLUS `usr:` (user calc), table-calc prefixes, and forecast prefixes — so a calc-field reference on rows correctly triggers vertical orientation.

### Scatter safety
- 2 measures + 0 dims → `scatterChart` (fine)
- 1 measure + 1 dim → `columnChart` or `barChart` based on which shelf has the measure
- 0 measures → `tableEx` (fall back, scatter would show nothing)

### Auto rules (mark='Automatic')
Evaluated in priority order on the rows/cols shelf shape:
1. measure_count=0, dim_count=0 → `cardVisual` (text-only worksheet)
2. measure_count>=2, dim_count=0 → `scatterChart` (two measures, no dims)
3. measure_count=1, dim_count=0 → `cardVisual` (single measure)
4. has_date + measure_count>=1 → `lineChart` (date axis + measure)
5. measure_count>=1, dim_count>=3 → `tableEx`
6. measure_count>=1, dim_count>=2 → `pivotTable`
7. measure_count=1, dim_count=1 → `columnChart`
8. dim_count>=2, measure_count=0 → `tableEx`
9. fallback → `tableEx`

### Card upgrade
Both direct mapping and auto rule paths upgrade `cardVisual` → `multiRowCard` when `len(labelFields) >= 2` (multiple text encodings, e.g. KPI card with "Total Territories" + "Sum of Patients").

## Filter binding skills (`report.py`)

### Literal value typing (filter `Where` clause)
- Boolean column → `{"Literal": {"Value": "true"}}` (bare keyword, no quotes)
- Integer → `{"Literal": {"Value": "5L"}}` (L suffix)
- Number → `{"Literal": {"Value": "5.5D"}}` (D suffix)
- String → `{"Literal": {"Value": "'foo'"}}` (single-quote wrapped)

Wrong typing produces "field is not available" errors at PBI load.

### Date-part agg redirect (resolver)
- `yr:Date Added` → `Year of Date Added` calc column
- `qr:Date` → `Quarter of Date`
- `mn:Date` → `Month of Date`
- `dy:Date` → `Day of Date`
- `tmn:Date of Visit` → `Year-Month of Date of Visit` (truncate-month, dateTime)
- `tqr:Date of Visit` → `Year-Quarter of Date of Visit`
- `ty:Date of Visit` → `Year-Trunc of Date of Visit`
- `tmd:Date of Visit` / `td:Date of Visit` → bind to Date of Visit directly

### Top N filter
Subquery shape (NOT `VisualTopN.ItemCount`). Container type `TopN` so user can edit; inner Condition discriminator is `VisualTopN`. Cross-table case adds the measure's table to the subquery `From` with alias `m`.

## Text & encoding skills

### Tableau text artifact stripping
`_clean_text` (parser.py) removes characters Tableau injects as RTF leftovers:
- C0 / DEL controls (`0x00-0x1f`, `0x7f`)
- C1 controls (`0x80-0x9f`)
- U+00C6 (Æ run separator)
- U+200B–U+200F, U+202A–U+202E, U+2060 (zero-width / bidi marks)
- U+FEFF (BOM)
- U+FFF9–U+FFFD (interlinear annotation, replacement chars)

Applied to: worksheet titles, dashboard textbox runs, captions.

### Font fallback
`_safe_font_family` (report.py) substitutes Arial only when the Tableau font isn't in PBI's shipped-fonts allow-list. Calibri / Segoe UI / Times New Roman / etc. pass through unchanged. Color and size are always pulled from twb, never substituted.

## Output artifacts (per-run)

- `<name>.pbip` + `<name>.Report/` + `<name>.SemanticModel/` — standard PBIP layout.
- `_ir.json` — JSON view of the parser IR (debug aid, gated on `debug_ir=True`).
- `datasource_mapping.xlsx` — four sheets (`Datasources`, `Columns`, `Relationships`, `Skipped Relationships`) wiring Tableau column refs to PBI tables/columns/CSV headers. The file to open when a visual references a missing column.
- `credentials_manifest.json` — one entry per live datasource with effective connection (post-override), username, `has_password` / `has_personal_access_token` flags, `credentials_matched` boolean. Skipped classes: `federated` / `hyper` / `extract` / empty.
- Stale `*_pbip` output is removed by `_clean_generated_output` at the start of each run; falls back to renaming the locked folder when PBI Desktop holds it open.

## What's NOT yet implemented

- `INCLUDE` / `EXCLUDE` LODs
- `INDEX()` / `RANK_UNIQUE` / `RANK_DENSE`
- `RUNNING_SUM` / `WINDOW_AVG` / `LOOKUP` (need PBI Visual Calculations)
- `REGEXP_EXTRACT` / `REGEXP_REPLACE` / `REGEXP_MATCH`
- Multi-line strings with embedded `\r CHAR(10)` (could substitute `UNICHAR(10)`)
- Forecast / "Forecast Indicator" output column from Tableau forecast
- `:Measure Names` (Tableau's pivoted column header)
- Live connectors beyond sqlserver / postgres / snowflake / databricks (oracle / mysql / redshift / bigquery / …)
- Tableau `<aliases>` value substitution (display labels for column values)
- Tableau set / group / hierarchy definitions
- TREATAS rewrite for blend pairs with duplicate keys on both sides (logged as `[BLEND-WARN]`, user fixes by hand)
- USERELATIONSHIP wrappers on measures that span multiple blend rels (filter context propagates through the active path, but the alternative path can only be invoked manually)
- LOD calcs that span blend boundaries (need DAX-level rewriting beyond the model relationship)
