# Tableau → Power BI Converter

Converts Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files. Operates in two modes — full TWBX (data model + visuals) or stub TWB (visuals only). The companion package `tableau_to_pbi_agent/` is a thin wrapper that calls into this base module after running an LLM-assisted field-resolution pass; it has no parallel parser/report/model files.

This branch (Agent Version 0511_ABCD) carries Phase A+B+C+D changes on top of the original converter — live partitions, custom SQL, Tableau-blend → PBI relationships, multi-datasource visual binding, and a credentials file workflow. See `CHANGES_ABCD.md` for the file-by-file diff.

## Pipeline

1. `parser.py` → reads `.twb` XML into IR dicts (datasources, worksheets, dashboards, parameters). Captures `connection`, `customSql`, `extracts` per datasource and `datasourceDeps` per worksheet.
2. `model.py` → builds the TMDL semantic model (tables, columns, relationships, measures). Synthesises blend relationships from worksheet `datasourceDeps`; applies credential overrides; chooses extract-CSV vs live-DirectQuery vs live-Databricks per table.
3. `report.py` → builds pages and visuals from the IR; routes worksheet zones to chart visuals. Resolves bindings via the worksheet's primary ds first, then falls back to other `datasourceDeps` for blended fields (Phase D).
4. `writer.py` → drops everything onto disk in PBIP layout.
5. `visual_picker.py` + `visual_rules.json` → picks the PBI `visualType` from a worksheet's mark + shelf shape.
6. `dax_translator.py` → translates Tableau calc formulas to DAX.
7. `hyper.py` → enriches the model with Hyper extract column types/data.
8. `credentials.py` → optional JSON / XLSX credentials store. Overrides server / database / port / schema / warehouse / http_path / catalog before partition-M emit, and feeds `credentials_manifest.json`.
9. `converter.py` → orchestrator. Also emits `datasource_mapping.xlsx` (TWB → PBI wiring) and `credentials_manifest.json`, and cleans stale `*_pbip` output before each run.

## Visual types and slot conventions

### Card vs. multiRowCard

- **Single value (1 text encoding)** → `cardVisual` (modern KPI card). Slots: `Data`, `Tooltips`. Format bags: `value` (font/size/color/horizontalAlignment) + `label` / `outline` / `divider` / `fillCustom` all `show: false`. Each format bag carries `selector: {id: "default"}`. Height forced to 50.
- **Multi-value (2+ text encodings)** → `multiRowCard`. Slot: `Values` (every encoding lands here). Format bag: `dataLabels` carries `color`/`fontFamily`/`fontSize`/`bold`/`italic`/`underline` from twb (NOT just color — without `fontFamily`, PBI falls back to Segoe UI). `categoryLabels.show: false`. Keeps Tableau zone height (no 50px override).
- The picker upgrades cardVisual → multiRowCard when `len(labelFields) >= 2` at both the direct-mark mapping and the auto-rule paths.
- Card field emitter wraps bare columns in Aggregation: `Min` (Function 3) for strings, `CountNonNull` (Function 5) for numerics. DAX measures emit `Measure` ref directly.

### Slicer

- Height forced to 60. Title always `enabled=False`.
- `objects.general[0].properties.singleSelect = true|false`
- `objects.data[0].properties.mode = 'Dropdown'|'Basic'`
- Surveyed Tableau corpus tokens: `compact` / `dropdown` → single-select dropdown; `checkdropdown` → multi-select dropdown; `radiolist` → single-select list; `vscroll` / `checklist` → multi-select list.

### Top N filter

- Subquery shape (NOT `VisualTopN.ItemCount`). Dimension column lives in the outer `Where In`. Ranking measure lives in the subquery's `OrderBy.Aggregation`.
- `Direction: 2` = Descending (Top), `1` = Ascending (Bottom).
- **Cross-table**: when the ranking measure's table differs from the dimension's, add the measure's table to the subquery `From` with alias `m` and use `Source: "m"` in OrderBy. Single-table case keeps alias `c`.

### Charts

- **Pie / donut** default: `legend.show: true, position: 'Right'`.
- **Area / line / stackedArea**: `markers.show: true, shape: 'circle'` plus `dataPoint.showAllDataPoints: true`. Marker color from twb mark-color when present.
- **Tableau `<encoding attr='color'>` palette**: parsed at the datasource level into `datasource.colorMaps[field]: {bucket: hex}`. Applied as per-category PBI `dataPoint` selectors via `scopeId.Comparison` (Equality / `ComparisonKind: 0`). Wrong shape would be `dataViewWildcard.matchingConditions` — that fails schema validation. `scopeId` is a `QueryExpressionContainer`.

### Map (Azure Maps)

- Every Tableau geo mark (`map`, `point`, `polygon`, `filled-map`, `multipolygon`) routes to PBI's `azureMap` visualType. The legacy `map` / `filledMap` / `shapeMap` slot entries in `config.VISUAL_SLOTS` are kept as no-op fall-throughs so any caller still referencing them by name doesn't break; the picker no longer emits them.
- Detection by Tableau's `<column semantic-role='[Geographical].[Latitude]'>` first, then name match (`lat`/`lng`/`latitude`/etc.).
- Lat/lon columns get `dataCategory: Latitude` / `Longitude` and `summarizeBy: none` in TMDL.
- Map binders use bare Column refs (no aggregation) so coordinates aren't collapsed.
- **Auto-fills**: PBI Desktop auto-stamps X/Y/Series mirrors and per-binding filters on save. The converter pre-emits these for `azureMap` / `map` / `filledMap` / `shapeMap` / `cardVisual` / `multiRowCard` so the open/save round-trip doesn't mutate the file. Bar/pie/table currently skip auto-stamping.
- **Default viewport — North America**: Every map visual the converter emits (`azureMap` + the legacy `map` / `filledMap` / `shapeMap`) gets a `mapSettings` bag with `view: 'UnitedStates'`, `customZoom: 5`, `customCenterLat: 39.5`, `customCenterLon: -104.99`, plus alternate property names (`zoom`, `centerLat`, `centerLong`, `predefinedView`) for older Desktop builds and `autoZoom: false`. A second `controls` bag with `autoZoom: false` blocks the visual's default auto-zoom-to-data behaviour which would otherwise override the initial view. Property names come from Microsoft's Azure Maps visual `capabilities.json` (`mapSettings.view` enum: Auto / World / UnitedStates / Custom; `customZoom` numeric; `customCenterLat`/`customCenterLon` doubles). Legacy `map` / `filledMap` / `shapeMap` visuals auto-fit to the data extent and don't honour `mapSettings` — they're emitted defensively so a manual visualType swap preserves intent. Users who want a different region override in the Format pane after open.

### Table & matrix header styling

- `tableEx` and `pivotTable` always emit a `columnHeaders` bag (and `pivotTable` also emits `rowHeaders`) with **Tableau-like defaults layered with TWB-specific overrides** — PBI's default Segoe UI 9pt left-aligned headers are too far from Tableau's look to feel familiar, so the converter forces the bag to be present regardless of what the workbook supplied.
- Style sources, highest-precedence first:
  1. `worksheet.columnHeaderStyle` / `rowHeaderStyle` — parsed from `<style-rule element>` blocks by `parser._parse_worksheet_header_style`. The element list (lowest-precedence first inside a single bag): `header` → `field-labels` → `field-labels-decoration` → `row-header` / `column-header`. The `field-labels-decoration` rule is where Tableau stores the **column-header background color** (a band painted behind the field labels) — checking only `header` would miss it. Picks up `color` → fontColor, `font-family`, `font-size`, `font-weight=bold`, `font-style=italic`, `font-underline` / `text-decoration=underline`, `background-color` (transparent skipped), and `text-align`. Scoped variants — `data-class='subtotal'`/`'total'`, `scope='rows'`/`'cols'`/`'totals'`, or `field='...'` — are skipped entirely. They describe per-row/per-column overrides for totals/subtotals, NOT the general header style, so letting one through (as the previous version did when no base value existed) produced pale-grey headers in workbooks that intended dark blue.
  2. `worksheet.titleStyle` — Tableau workbooks usually use the same font for title and headers, so this is the next-best signal when no `<style-rule element='column-header'>` is present.
  3. `worksheet.labelStyle` — finally falls through to the worksheet's label style for fontFamily / fontSize only.
  4. **Tableau-like defaults** — Arial 10pt, color `#1b1b1b`, bold, center-aligned. Backgrounds default to the worksheet's `backgroundColor` when set, otherwise omitted (PBI fills white).
- PBI property bag uses `fontFamily` / `fontSize` / `fontColor` / `bold` / `italic` / `underline` / `backColor` / `alignment`. Alignment values are capitalised (`Left` / `Center` / `Right`) per PBI's string-enum convention. `fontColor` and `backColor` are wrapped in `{solid: {color: <expr>}}` envelopes (PBI's color shape).
- `pivotTable.rowHeaders` mirrors `columnHeaders` when the workbook only supplied one set of header rules — a Tableau crosstab styles both axes identically by default.

### Textbox

- **Container chrome (background, border) goes under `visualContainerObjects`, NOT `objects`.** PBI silently ignores `objects.background` for textbox visuals — the colored tile won't render. Chart visuals route the same way.
- `containerStyle` is now stashed on every zone dict by the parser (regardless of `type-v2`). The routing passes it to `_build_textbox` for color/legend/bitmap/fallback zones — previously only `text`/`title` zones had styling carried through.

## Model conventions

- **Date hierarchies**: every `dateTime` column gets synthesized `Year of <X>` / `Quarter of <X>` / `Month of <X>` / `Day of <X>` calculated columns plus a `<Date> Hierarchy` block in TMDL. DAX expressions use bracket notation (`[Date Added]`) — single-quote (`'Date Added'`) is parsed as a *table name* in DAX.
- **Tableau date-part agg redirect**: `yr:date_added` on a shelf rewrites the binding to `Year of Date Added` (the synthesized hierarchy column) since PBI has no date-part aggregation function.
- **Cols-map authoritative binding**: `<cols><map key='[Region]' value='[Dim_HCP].[Region]'>` blocks beat all heuristic resolution for un-suffixed field references.
- **`PBI_TimeIntelligenceEnabled = 1`** annotation in `model.tmdl` so PBI Desktop creates auto date hierarchies on first open.
- **Globally-unique measure names**: PBI rejects a model where two tables both define a measure with the same name (case-insensitive), failing with `PFE_TM_OBJECT_NAME_ALREADY_EXISTS`. Tableau allows it because each datasource is its own namespace. `_enforce_global_measure_uniqueness` in `model.py` runs after `_build_all_measures` and renames non-canonical duplicates to `<name> (<table>)`, rewriting any `'Table'[OldName]` DAX refs and updating `col_locator` so the report builder picks the post-rename names.

### Parameter tables

- **One table per parameter** (no shared lumped table). List parameters get a `Value`/`Label` two-column table with one row per `<member>`. Any/range parameters (including dateTime params like Start Date / End Date) each get their OWN single-column single-row table named after the parameter caption with column `Value`. This matches PBI's "What If" pattern and avoids mixing types in one row. The legacy lumped `Parameters` table caused (a) the date-hierarchy synthesizer to emit useless Year/Quarter/Month/Day columns on parameter date fields, and (b) confusion in PBI's slicer UX because it expects one parameter per table.
- **dateTime parameters** must emit M's `#datetime(yyyy, m, d, h, mi, s)` literal in the partition row, NOT a quoted string. PBI rejects mismatched types with "Expression.Error: The type of the value does not match the type of the column" — the error message echoes the offending value.
- `_m_literal` strips Tableau's `#YYYY-MM-DD#` markers and any surrounding quotes, parses ISO date or datetime, and falls back to `null` when unparseable.
- Date-hierarchy synthesis (Year of X / Month of X / etc.) is **skipped on parameter tables** — single-row tables don't benefit from hierarchies and emitting 8+ calc columns per dateTime parameter clutters the field pane.

### DAX translator (`dax_translator.py`)

- **Scalar wrapping**: bare column refs in measure context get wrapped in `MIN()` automatically. PBI measures must return scalars; Tableau's `IF [bool] THEN [col] END` would otherwise emit `'Table'[Col]` directly which fails. The post-processor skips wrapping inside aggregation calls (SUM/AVG/COUNT/SELECTEDVALUE/CALCULATE/etc.) and inside known DAX measure refs (tracked in `_ACTIVE_MEASURE_REFS`).
- **`SUM` on measures**: when a Tableau calc wraps another calc in `SUM(...)` and the inner ref resolves to a model measure (registered in `measure_refs` passed from `model.py`), the translator drops the wrapper. PBI rejects `SUM('Table'[SomeMeasure])` with "The SUM function only accepts a column reference as an argument."
- **Field-ref parsing**: `_parse_field_ref` strips Tableau type-suffix tokens (`nk`/`qk`/`ok`/`ck`/`ik`) and any duplicate-index integer (`:3`) before deciding role-vs-field. Only known agg/role tokens (`sum`/`avg`/`yr`/`usr`/etc.) are treated as the role; otherwise the whole spec stays in the field name. Without this, `Calculation_xxx:qk:3` was misparsed and calc fields couldn't be resolved.
- **LOD FIXED translation** (`_translate_lod_fixed`): `{ FIXED [Dim] : agg(X) }` becomes `CALCULATE(<dax-agg>, ALLEXCEPT('Table', 'Table'[Dim]))`; the global form `{ FIXED : agg }` becomes `CALCULATE(..., ALL('Table'))`. Implementation uses placeholder substitution (`__LOD_BLOCK_N__`) so the LOD-translated DAX doesn't get re-tokenised by the Tableau-syntax token loop. Multi-dim LODs and dims wrapped in `YEAR()`/`MONTH()`/`QUARTER()`/`DAY()` redirect to the synthesized hierarchy columns (`Year of X`, `Month Number of X` integer, etc.). `INCLUDE` / `EXCLUDE` LODs still drop.
- **Date construction**: `MAKEDATE(y, m, d)` → `DATE(y, m, d)`. `MAKETIME(h, m, s)` → `TIME(h, m, s)`. `MAKEDATETIME(date, time)` → `DATE(...)` (degraded — DAX has no native combine). `DATEPARSE('format', str)` / `PARSEDATE(str)` → `DATEVALUE(str)` (drops format arg; DAX has no format-string parser, so custom formats degrade to BLANK on unrecognised inputs).
- **Tableau formula comments**: tokenizer skips `// line comment` and `/* block comment */` (multi-line). Both are stripped before tokenization rather than emitted as DAX, since DAX has no equivalent comment syntax inside expressions.
- **Date function translations**: `DATEDIFF('part', A, B)` → `DATEDIFF(A, B, PART)` (arg-order swap, unquoted enum). `DATEPART('year', X)` → `YEAR(X)` (and friends). `DATETRUNC('month', X)` → `DATE(YEAR(X), MONTH(X), 1)`. `DATENAME('month', X)` → `FORMAT(X, "MMMM")`. `DATE(X)` cast → `DATEVALUE(X)` (works on date AND string inputs).
- **TOTAL / ATTR / SPLIT / TRIM**: `TOTAL(expr)` → `CALCULATE(expr, ALLSELECTED())` (percent-of-total pattern). `ATTR([X])` → `SELECTEDVALUE('T'[X])` for column refs, `MIN(...)` fallback. `SPLIT(s, d, n)` → `PATHITEM(SUBSTITUTE(s, d, "|"), n)` since DAX has no native split. TRIM/LTRIM/RTRIM are direct passthroughs.
- **Tableau groups & categorical-bin calc columns** (`_compile_categorical_bin` in `parser.py`): Tableau's `<calculation class='group' | 'categorical-bin' | 'bin' column='[X]'>` blocks emit as DAX calculated columns via `daxColumnExpr` — the column TMDL is `column 'Y' = SWITCH(TRUE(), [X] IN { ... }, "Bucket", [X])`. `_parse_columns` calls `_compile_categorical_bin(calc_el)` for every group/bin column and re-parents the new calc column to the SOURCE column's TMDL table (resolved through `cols_map` → `col_parent` → `objects`), so the bare `[X]` ref inside the DAX resolves in row context. `_render_table_tmdl` skips `sourceColumn:` for columns with `daxColumnExpr` so PBI computes the value instead of looking for a CSV header that doesn't exist. Logs `[GROUP] '<name>' in ds='<ds>' -> DAX calc column on table='<tbl>' (source='<col>')` per materialised group. If the bin XML is malformed and the compile returns `BLANK()`, the path falls back to the legacy alias-to-source so visuals at least bind to the underlying values. `model._register_group_aliases` then stashes the `{baseField, membersByGroup}` registry into `_group_aliases` for `group_info()` filter expansion (Tableau-style filter on a group label translates to `[Source] IN { members }`); the alias-injection backstop in `col_locator` only fires when the group field has NO calc column entry yet, so the calc-column path always wins.
- **Context-aware calc-field classification** (calc column vs measure): `_build_measures` decides per calc field whether to emit as a calculated column or a DAX measure. Calc columns evaluate row-by-row at refresh time — the only path that supports member-list filtering and row-level predicates; measures evaluate at query time and can't be the target of an `IN (a,b,c)` filter. Decision tree (first match wins):
  1. **Formula contains aggregation token** (`SUM`/`AVG`/`COUNT`/`COUNTD`/`MIN`/`MAX`/`MEDIAN`/`STDEV`/`VAR`/`ATTR`/`TOTAL`/`RUNNING_*`/`WINDOW_*`/`LOOKUP`/`PREVIOUS_VALUE`/`CORR`/`COVAR`/`PERCENTILE`/`FIRST`/`LAST`/...) → **measure**. Detected by `_formula_has_aggregation` (strips `//` and `/* */` comments first to avoid false positives on commented-out aggregations).
  2. **`role='dimension'` + `tmdlType='boolean'`** → **calc column**. Tableau row-level boolean predicates like `[Date] >= [Start] AND [Date] <= [End]`. As a measure the scalar wrapper MIN-wraps the fact-table column, which collapses to the global min when the visual doesn't include that column on an axis (the filter then always passes or always fails). As a calc column the comparison runs row-by-row against each fact-table row.
  3. **`role='dimension'` + no aggregation** → **calc column**. Tableau-declared dimensions (`IIF([Status]="Open", "Y", "N")`, `IF [Region]="US" THEN "Domestic" ELSE "International" END`) emit row-by-row so they can drive slicers and axes.
  4. **Used as a member-list filter target** (`worksheet.filters[*].members` non-empty) → **calc column**, regardless of role. Member filters need a row-level value to compare against; binding them to a measure silently fails at load. The `_calc_field_member_filter_keys` index is built once per build pass and consulted for every calc field.
  5. Otherwise → **measure**.
- Same-table `MIN(...)` wraps emitted by the translator's scalar guard are stripped from calc-column DAX (`MIN('SameTable'[Col])` → `'SameTable'[Col]`) so the comparison runs in row context. Cross-table refs (especially parameter tables, which are 1-row) keep the MIN — `MIN` of a 1-row table IS the current parameter value.
- Logged per emission: `[CALC-COL] '<name>' on table '<tbl>' emitted as calculated column (<reason>)` where reason is one of `boolean predicate, row-level filter` / `member-list filter target` / `dimension role, no aggregation`. Pre-fix only the boolean-dim path emitted calc columns; every other dimension calc became a measure and the user's filters silently dropped at load.
- **Tradeoff**: calc columns evaluate at refresh time, so slicer changes to *parameter values* don't re-trigger evaluation. Acceptable for converted Tableau models — the right PBI pattern is a slicer on the underlying column directly.
- **Tableau internal-object-id row counter**: `COUNT([__tableau_internal_object_id__].[<table-id>])` is Tableau's idiom for row count. Pre-translate to `COUNTROWS('<table>')` in `translate_tableau_to_dax` BEFORE tokenization (placeholder pattern, like LOD), so the COUNT wrapper around an unresolvable internal-table-id ref doesn't reach the main token loop and emit invalid DAX.
- **Auto-generated calc-field measures hidden**: Tableau anonymous calc fields get internal IDs like `Calculation_4313392949811654658`. These are noise in PBI's field pane — they're internal references between measures, not user-facing picks. `_build_measures` sets `isHidden=true` when the measure name matches `^Calculation[_ ]\d+$` so the field pane shows only named measures while DAX measures still cross-reference each other.
- **Trivial-alias calc resolution** (`_register_calc_alias_resolutions` in `model.py`): Tableau auto-generates calc fields whose formula is a single column reference (`[X]`, `[X (TableHint)]`, `// caption\n[X]`). These are pure renames — no arithmetic, no predicate, no function call. Visuals reference them by their cryptic `Calculation_<big_id>` identifier rather than the underlying column. The resolver detects the trivial-alias shape with the same regex as `tableau_to_pbi_agent/resolvers/formula_resolver.py` (which was an LLM-free Pass-2 fallback in the agent), then registers the calc-field name AND its caption in `col_locator` against the underlying column. Logs `[CALC-ALIAS] '<calc_name>' (ds='<ds>') -> <table>.<column>`. Suffixes that are Tableau-internal markers (`group`, `bin`, `parameter`, `calculation`) are NOT treated as table hints. Without this pass, worksheets that bind `Calculation_xxx` to a shelf emit `[RESOLVE] not found` and the field gets silently dropped.
- **Worksheet-local calc-field merge** (`_merge_worksheet_calc_fields` in `parser.py`): Tableau emits per-worksheet calc fields under `<view>/<datasource-dependencies datasource='X'>/<column>` rather than the top-level `<datasources>/<datasource>/<column>` block, so `_parse_datasources` never saw them. After parsing worksheets, the new pass walks every `<worksheet>/<view>/<datasource-dependencies>/<column>` with a `Calculation_` name + `<calculation formula='...'>` child and appends a synthetic column dict to the matching datasource. First definition wins (Tableau repeats the same calc element verbatim in every worksheet that uses it). Logs `[CALC-INDEX] merged <n> worksheet-local calc field(s)`. Combined with the trivial-alias resolver, the standalone converter now handles `Calculation_xxx` references inherently — the agent's `calc_index.py` + `formula_resolver.py` path is retained as a safety net but no longer needs to fire for the corpus.
- **Self-reference guard for measure DAX** (`_field_to_pbi_for_ds(..., exclude_self=)`): `_build_all_measures` pre-registers every calc field's caption AT THE FRONT of `col_locator[(ds, caption)]` so the calc shadows a same-named column — matching Tableau's "calc shadows column" semantics for visual binding. Without intervention this also means that when the calc's OWN DAX references `[Caption]`, the translator's `field_to_pbi` resolves the bare ref back to the measure being defined, emitting `'Table'[Caption] = '...' & 'Table'[Caption] & ...` which PBI rejects at load with `PFE_XL_CALCCOLUMN_CIRCULAR_DEPENDENCIES: A circular dependency was detected: Measure: 'Table'[Caption], Measure: 'Table'[Caption]`. Fix: `_field_to_pbi_for_ds` accepts an `exclude_self=(table, pbi_name)` argument that skips that pair when picking each bucket's first candidate, and `_build_measures` rebuilds `field_to_pbi` per-measure with the current measure as the exclusion. The shadow behaviour is preserved for visual-binding lookups (which call `_field_to_pbi_for_ds(ds_name)` without exclusion); only the measure-translation pass sees the underlying column. Repro: Medical Affairs Dashboard's `Response Date` calc field (a `DATENAME + STR + DAY + YEAR` chain) shares its caption with the `Response Date` column on `MIR` — pre-fix, it emitted `FORMAT('HCP Info (2)'[Response Date], "dddd") & ...` (self-ref); post-fix it emits `FORMAT(MIN('MIR'[Response Date]), "dddd") & ...` and loads cleanly.

## Data blending → relationships (Phase C)

- **Tableau worksheet-level blends** (a worksheet pulls dims from one datasource + measures from another) get translated to PBI model relationships in `relationships.tmdl`. Detection runs in `_synthesize_blend_relationships` (model.py) over each worksheet's `datasourceDeps` list (parsed from `<datasource-dependencies>` in the twb XML).
- **Blend-key inference**: Tableau's TWB XML doesn't expose explicit blend keys, only an `<aliases enabled='yes'/>` flag. We fall back to "shared column names": dims that appear in BOTH the primary and secondary worksheet declarations become blend-key candidates. The candidate's `(table, column)` resolution must point to a real column on its TMDL table — measure-typed `col_locator` entries are filtered out so a measure name like `Total Distinct Patients` doesn't accidentally become a relationship endpoint. When the worksheet only declares one side's columns, the fallback intersects the two datasources' `col_locator` keys, scoped to names the worksheet actually mentioned, so arbitrary shared names across large datasources don't emit dozens of garbage rels.
- **Cardinality**: emitted as **many-to-many bothDirections**. Tableau blends don't enforce uniqueness or non-null on either side; many-to-one would reject duplicate or blank keys at load time. `bothDirections` is the closest semantic match because PBI's many-to-many with `oneDirection` is effectively passive (USERELATIONSHIP-only). Each blend rel is flagged `isBlend: True` so `write_tmdl` emits the explicit `crossFilteringBehavior` / `fromCardinality` / `toCardinality` lines; non-blend rels keep the legacy many-to-many shape.
- **Skipped pairs**: when BOTH sides have the same column name appearing on multiple TMDL tables (`_has_duplicate_column`), the converter logs `[BLEND-WARN] using TREATAS fallback ...` and skips the relationship. TREATAS is not auto-emitted today — the warning is the user's hook to rewrite the measure manually.
- **Cycle/parallel-edge avoidance** (`_deactivate_ambiguous_paths`): PBI requires exactly ONE active relationship path between any pair of tables. Synthesized blend rels can produce (a) parallel rels on different keys between the same pair, and (b) triangle cycles when three tables blend pairwise. Both raise `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` on load. Walk relationships in insertion order; the unordered-pair seen-set catches parallel keys; union-find detects cycle-closing edges; mark all but the first `isActive: false`. Inactive rels stay in the model — DAX can invoke them via `USERELATIONSHIP(...)`.
- **Relationship names** are a deterministic 8-char `blend_<hash>` of `(fromTable, toTable, key)` so re-runs are stable across conversions.

## Card / multi-row card titles

`cardVisual` and `multiRowCard` always emit an explicit `visualContainerObjects.title.show = false`, regardless of the workbook's `titleEnabled` flag. Two reasons:

1. The PBI KPI / card pattern relies on the headline value (`value` / `dataLabels` bags) as the visual's primary text. A worksheet title on top duplicates it and steals vertical space (cards have a 50px-tall constraint where every pixel matters).
2. Tableau worksheets that author a "text" card mark almost always have a worksheet title that's identical to the value the user is highlighting — leaving the title enabled produces a redundant header.

Users who explicitly want the title back can flip it in the Format pane after open. The explicit `show=false` (as opposed to omitting the title block) is what actually hides the bar; PBI Desktop's default-when-absent behaviour is to render the worksheet name as a title.

## Multi-datasource visual binding (Phase D)

- `_resolve_visual_field` in `report.py` is the single resolution path for visual fields, filters, and sort. When the encoding / filter carries a `binding_ds` that differs from the worksheet's primary datasource (Tableau data blend), the resolver looks up `(binding_ds, fname)` through the **secondary's** `col_locator` first. On hit, logs `[BLEND] binding routed: '<field>' -> ds=<secondary> table=<tbl> (primary ds=<primary>)`.
- Single-ds worksheets (the common case) bypass the override and use the primary resolver path unchanged.
- The synthesised Phase C relationships are what make this work at query time — without an active relationship between primary and secondary, the cross-ds projection would not join.

## Connection classes & partition M

`_render_partition_m` in `model.py` chooses a partition-M shape per datasource based on `connection.class`. The function is called from `_render_table_tmdl` when no Hyper/CSV path is available; with a Hyper extract the CSV branch wins (because the data is already baked) unless `_prefer_live_over_extract` is true AND the live class is Databricks.

| `connection.class` | Branch | mode |
|---|---|---|
| `""` / `federated` / `hyper` / `extract` | Returns `None` — caller emits `Table.FromRows({}, type table [...])` placeholder | import |
| `excel-direct` / `textscan` / `csv` / `json` | Returns `None` — caller uses CSV-from-Hyper path | import |
| `sqlserver` | `Sql.Database(server, dbname)` + `[Schema=..., Item=...]` nav | directQuery |
| `postgres` | `PostgreSQL.Database("server:port", dbname)` + `[Schema=..., Item=...]` nav | directQuery |
| `snowflake` | `Snowflake.Databases(server, warehouse, [Implementation="2.0"])` + DB/Schema/Name nav | directQuery |
| `databricks` / `azure-databricks` / `databricks-sql` / `spark-sql` / `spark` | `DatabricksMultiCloud.Catalogs(server, http_path, [Catalog="",Database="",QueryTags=null,EnableAutomaticProxyDiscovery=null,Implementation="2.0"])` + Catalog/Schema/Name nav | import |
| `redshift` and other live classes | `_render_unsupported_partition` — Table.FromRows placeholder with `// TODO` comment, logs `[CONN] live connection class '<cls>' — emitting placeholder Table.FromRows` | import |

- **Custom SQL** (`<relation type='text'>` / `'query'>` captured as `datasource.customSql`): when present, sqlserver / postgres / snowflake / databricks branches emit `Value.NativeQuery(Source, "<escaped-sql>", null, [EnableFolding=true])` instead of the navigation-record form. The SQL string is escaped via `utils.escape_m_string` (doubles `"`, converts newlines to `#(lf)`, tabs to `#(tab)`).
- **Snowflake `Implementation="2.0"`** is mandatory — without it PBI emits the legacy ODBC connector which doesn't honour Schema/Name navigation correctly for many Snowflake account shapes.
- **Databricks PAT**: the partition M never embeds the token. The token shows up only in `credentials_manifest.json` as `"authentication": "PersonalAccessToken", "has_personal_access_token": true` so a deployment script knows it must configure PAT auth via PBI Desktop or the REST API.
- **Databricks "live over extract"** (`_is_databricks_live_connection` + `_prefer_live_over_extract`): when the effective connection class is Databricks AND `prefer_live_over_extract` is truthy (default), `write_tmdl` discards the bound Hyper/CSV path and emits the live `DatabricksMultiCloud.Catalogs` source instead. This is the path that lets a credentials file repoint an extract-mode Tableau workbook at a live Databricks warehouse without editing the .twb.

## Custom SQL as datasource

Two paths bring custom SQL into the partition-M expression. Both target the same emission path (`Value.NativeQuery(Source, "<escaped-sql>", null, [EnableFolding=true])`), differing only in where the SQL originates.

- **Tableau-embedded custom SQL** (`<relation type='text'>` / `<relation type='query'>` captured by `parser._parse_custom_sql` into `datasource.customSql`): When the workbook is extract-mode (Hyper CSV bound) BUT the inner connection class is live (sqlserver / postgres / snowflake / databricks), `write_tmdl` now drops the CSV path and emits the live native-query partition instead. Previously the extract always won, which left workbooks with `<relation type='text'>` blocks effectively static; the user could see the SQL in the Tableau workbook's data source pane but the converted PBIP just served the baked-in extract data. Logs `[CONN] '<table>' has customSql + live class '<cls>' — dropping extract path and emitting Value.NativeQuery instead of CSV bind`.
- **Credentials-file `query` override**: `CredentialEntry` now accepts `query` / `custom_sql` / `sql` as header names. When set, `apply_to()` stamps `customSql=[{"name":"credentials override","sql":"<sql>"}]` AND `force_live_for_custom_sql=True` onto the connection dict, so the override wins regardless of whether Tableau embedded SQL of its own. Combined with `prefer_live_over_extract=True` (the default), this is the path that lets a credentials file repoint an extract-mode workbook at an arbitrary live query without editing the .twb XML.

The credentials override is the override layer: when both a Tableau-embedded SQL and a credentials `query` are present, the credentials version wins because `apply_overrides()` replaces `connection.customSql` and the model picks up the merged connection's value.

## Credentials file

- Loaded by `tableau_to_pbi.credentials.load_credentials(path)` from `--credentials` (CLI) or `credentials_path=` (API). Supported formats: `.json` (preferred — accepts `{"connections": [...]}` or a bare list) and `.xlsx` (sheet named `Credentials` or active sheet, header row + data rows). The loader prints `[CREDS] Loaded <n> credential entries from <file>`.
- `CredentialStore.match(conn, datasource=, caption=)` priorities (first hit wins):
  1. Exact `datasource` or `caption` match (datasource-aware override for workbooks that hit the same server with different logical names).
  2. `class` + `server` (case-insensitive, server lower-cased).
  3. `class` only (server omitted — useful for single-server workbooks).
  4. Safe fallback: a Tableau `federated` / `hyper` / `extract` / empty-class connection routes to a **single** live Databricks entry when exactly one exists in the store.
- `CredentialEntry.apply_to(conn)` returns a copy of the parsed connection dict with `class` / `server` / `dbname` / `port` / `schema` / `warehouse` / `http_path` / `catalog` / `connector_function` / `prefer_live_over_extract` overridden when set, plus any unknown columns from the XLSX header passed through via `extra`. **Passwords and PATs are NEVER written into the M expression** — the manifest is the only hand-off.
- `credentials_manifest.json` (written by `converter._write_credentials_manifest`) lists every live datasource with its **effective** connection (post-override), `username`, `has_password` boolean, and for Databricks an `authentication: "PersonalAccessToken"` + `has_personal_access_token` flag. Datasources whose class is empty / federated / hyper / extract are skipped (no live credentials to manage).
- The legacy lumped `Parameters` connection class is treated as `extract`, so the manifest never lists it.

## Windows long-path support

Every directory creation and file write in `writer.py`, `model.py:write_tmdl`, and `hyper.py:write_csv` routes through a `_long_path` helper (in `utils.py`) that prepends `\\?\` to the absolute path on Windows. The prefix opts the call into the extended-length Win32 API (32 767-char limit), which is the only reliable way to write the deeper PBIP paths once the user's output root + workbook name + page-id + visual-id chain crosses 260 chars (the legacy MAX_PATH). Without this, dashboards with long workbook names (e.g. `Healthcare Resources Analysis for National Healthcare Group.twbx`) fail mid-write with `[WinError 3]` or `[Errno 2]` errors that look like missing files. The `safe_filename(..., max_len=30)` ds-dir / `max_len=40` csv-file caps still apply — long-path is the safety net for the cases the truncation can't save (the workbook name itself is the major contributor and we don't truncate it because it's the user-facing identifier).

## Stdout / stderr UTF-8

`script.py` reconfigures `sys.stdout` and `sys.stderr` to UTF-8 with `errors="replace"` on entry. Tableau workbooks regularly carry calc-field names with `◀ ▲ ▼ ▶` arrow glyphs and other Unicode symbols for navigation widgets; printing these to a Windows cp1252 console raised `UnicodeEncodeError: 'charmap' codec can't encode character '◀'` and aborted the run mid-`[VPICK]` log. `errors="replace"` substitutes any unencodable char with `?` rather than crashing — the actual TMDL writes already use explicit `encoding="utf-8"`, so output correctness is unaffected.

## CSV partition path is relative to SemanticModel

`write_tmdl` resolves each Hyper-CSV path to a `data/<ds>/<csv>` form **relative to the SemanticModel directory** before handing it to `_render_table_tmdl`. The emitted partition M is:

```m
Source = Csv.Document(File.Contents("data/<ds_dir>/<csv>"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv])
```

`File.Contents` resolves relative paths against the SemanticModel directory, so the PBIP stays portable when the folder is moved/copied. Before this, the partition embedded the fully-resolved absolute path; PBI Desktop then failed to load on a moved PBIP and the deep absolute path tripped Windows MAX_PATH at write time on certain users' folders.

Two extra MAX_PATH safety belts now help the deepest-nested layouts stay under 260 chars:

- `safe_filename(ds_name, max_len=30)` for the `data/<ds_dir>` segment — Tableau's `federated.<22-char-id>` form is 32 chars on its own, so the truncated form (`federated.<14-char-prefix>_<8-char-hash>` ≈ 28 chars) prevents the parent path from chewing through the budget.
- `safe_filename(key, max_len=40)` for the CSV file name — Tableau extract keys like `Extract.MATILLION_X (COMM_DEV.MATILLION_X)_<32-char-GUID>` are ~120 chars on their own; the truncated form is ~40 chars so a ~50-char file name (incl. `.csv`) fits next to the ~100-char SemanticModel prefix.

When a CSV happens to live outside the SemanticModel folder (rare — extract-mode workbooks emit inside it by construction), the absolute path is kept as a last resort with `relative_to()` falling back via `ValueError`.

## Output artifacts

In addition to the standard PBIP layout (`<name>.pbip`, `<name>.Report/`, `<name>.SemanticModel/`, `data/`), the converter writes:

- `_ir.json` — JSON snapshot of the parser IR (datasources, worksheets, dashboards, parameters), gated on `debug_ir=True` (default).
- `datasource_mapping.xlsx` — four-sheet TWB → PBIP wiring report:
  - **Datasources** — caption, bound hyper file, declared extracts, list of TMDL tables produced per datasource.
  - **Columns** — every PBI column with its TMDL table, the original Tableau column reference, the Hyper/CSV header, dataType, and hidden flag. The file to open when a visual seems to reference the wrong column.
  - **Relationships** — every emitted rel (`<object-graph><relationship>` + synthesized blend rels). Includes the active flag so deactivated cycle-closers are visible.
  - **Skipped Relationships** — TWB-declared rels the converter could not emit (missing endpoint, self-join, etc.) with the reason.
- `credentials_manifest.json` — see Credentials section above. Only written when at least one live datasource exists.
- `_clean_generated_output` deletes any previous `*_pbip` folder at the start of each run; falls back to renaming the stale folder to `<name>.__stale_<ts>` when Windows holds a lock (PBI Desktop or Explorer keeping handles open).

## Visual picker conventions

- **Bar orientation** depends on which Tableau shelf has the continuous (measure-like) field. `_CONTINUOUS_AGGS` extends `_MEASURE_AGGS` to include `usr` (user calc field refs), `attr`, table-calc prefixes (`cum`, `pcto`, ...), and forecast prefixes. Without `usr`, calc-field measures wouldn't be recognized as continuous and the orientation override would never fire.
  - Continuous on COLS, dim on ROWS → horizontal `barChart`.
  - Continuous on ROWS, dim on COLS → vertical `columnChart` (override fires).
- **Scatter chart safety**: Tableau `mark='Circle'` maps to `scatterChart`, but PBI's scatter requires both X and Y to be numeric. When the `Circle` worksheet has a dimension on one axis (categorical dot plot) and only one measure, route to `columnChart`/`barChart` instead. With zero measures, fall back to `tableEx`.
- **Categorical-bin aware**: previous broken default mapped Tableau `circle` mark to `pieChart`. Now `circle` → `scatterChart` (and the orientation/dim-aware fallback above).

## Parser conventions

- **labelFields (plural)**: `<text>`/`<label>` encodings produce a list of all encodings, deduped by field name. `labelField` (singular) keeps the LAST one for backward compat with the visual_picker's `encoding: "label"` rule.
- **Worksheet labelStyle layers** (in precedence order, highest wins):
  1. `<style-rule element='worksheet'>` — bare `font-family` / `font-size` / `color`
  2. `<style-rule element='cell'>` — same bare attrs
  3. `<style-rule element='mark'>` — `mark-labels-*` attrs (chart data labels)
  4. `<customized-label>/<formatted-text>/<run>` — `fontname` / `fontsize` / `fontcolor` / `bold` / `italic` / `underline` (the marks-card label editor's choices)

## Font fallback

- **`_safe_font_family`** in `report.py` substitutes Arial only when the Tableau font isn't in PBI's shipped-fonts allow-list (e.g. `Tableau Medium` / `Tableau Bold` → Arial). Calibri / Segoe UI / Times New Roman / etc. pass through unchanged. **Color and size are always pulled from twb**, never substituted.

## Schema sources

- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/visualContainer/2.7.0/schema.json` — visual container shape
- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/formattingObjectDefinitions/1.5.0/schema.json` — `Selector`, `DataRepetitionSelector`, `DataViewWildcard`
- `https://raw.githubusercontent.com/microsoft/json-schemas/main/fabric/item/report/definition/semanticQuery/1.4.0/schema.json` — `QueryExpressionContainer` (Comparison, In, Aggregation, Subquery, ...)

## Smoke test

Quick corpus check after material changes:

```bash
cd C:/Users/ShrikantPansare && for f in \
  "UseCase.twbx" \
  "UseCase2.twbx" \
  "Sample Dashboards/Netflix Movies and TV Shows Dashboard.twbx" \
  "Sample Dashboards/Merchandise Sales Dashboard.twbx" \
  "Sample Dashboards/Superstore Performance Dashboard _ #VOTD.twbx"; do
  echo "=== $f ==="
  python -m tableau_to_pbi_agent "$f" --skip-llm 2>&1 | grep -E "types=|warnings:" | head -3
done
```

Each workbook should build with the same visual counts as before the change and zero new warnings. The `[VPICK]` diagnostic line printed per worksheet shows which visual type the picker assigned (mark / row count / col count / label field).

## Common gotchas

- **`objects` vs. `visualContainerObjects`**: data formatting goes under `objects`; container chrome (title, background, border, divider) goes under `visualContainerObjects`. Wrong bag = silently ignored.
- **`scopeId` for per-category formatting**: not `dataViewWildcard.matchingConditions`. `dataViewWildcard` only accepts `matchingOption` per the schema.
- **DAX column quoting**: `[Column]` inside expressions, `'Table'` for table refs. Mixing them produces "Cannot find column" errors.
- **M datetime literals**: `#datetime(...)`, not quoted strings. Type-mismatch errors surface as the verbatim offending value in PBI's error message.
- **PBI Desktop fallback fonts**: when `fontFamily` is absent on a format bag, PBI falls back to Segoe UI even if the parser captured a different family elsewhere. Always emit `fontFamily` explicitly when twb supplies one.
- **Snowflake without `Implementation="2.0"`**: PBI silently picks the legacy ODBC connector and the Schema/Name nav records mis-resolve. Always include the option record on `Snowflake.Databases(...)`.
- **Blend keys that resolve to a measure**: `col_locator` carries both columns and measures. A measure name like `Total Distinct Patients` getting picked as a blend-key endpoint produces a relationship PBI refuses to load. `_synthesize_blend_relationships` filters these out by checking the candidate against `cols_per_table` first.
- **Custom-SQL SQL escaping**: M strings need `""` for embedded quotes, `#(lf)` for newlines, `#(tab)` for tabs. Use `utils.escape_m_string` — raw Python string concat will produce M-parse errors that only surface when PBI opens the partition.
- **Plaintext passwords**: never embed credentials in the partition M. PBIP / TMDL has no secure in-file store; anything written to disk lands in version control. The manifest is the authorised hand-off.
- **`Value.NativeQuery` needs a Database, not a Table**: `Snowflake.Databases(...)` and `DatabricksMultiCloud.Catalogs(...)` both return a catalog *listing* (a Table), not a database. Calling `Value.NativeQuery` on those directly fails with `Expression.Error: Native queries aren't supported by this value. Details: [Table]`. The custom-SQL emit therefore navigates one level into the named database/catalog (`Database = Source{[Name="<db>"]}[Data]`, `Catalog = Source{[Name="<catalog>", Kind="Database"]}[Data]`) before passing the result to NativeQuery. `Sql.Database` and `PostgreSQL.Database` return a Database directly, so no extra hop is needed for sqlserver / postgres.
