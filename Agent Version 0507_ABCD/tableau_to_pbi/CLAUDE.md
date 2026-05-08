# Tableau → Power BI Converter

Converts Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files. Operates in two modes — full TWBX (data model + visuals) or stub TWB (visuals only). The companion package `tableau_to_pbi_agent/` is a thin wrapper that calls into this base module after running an LLM-assisted field-resolution pass; it has no parallel parser/report/model files.

## Pipeline

1. `parser.py` → reads `.twb` XML into IR dicts (datasources, worksheets, dashboards, parameters)
2. `model.py` → builds the TMDL semantic model (tables, columns, relationships, measures)
3. `report.py` → builds pages and visuals from the IR; routes worksheet zones to chart visuals
4. `writer.py` → drops everything onto disk in PBIP layout
5. `visual_picker.py` + `visual_rules.json` → picks the PBI `visualType` from a worksheet's mark + shelf shape
6. `dax_translator.py` → translates Tableau calc formulas to DAX
7. `hyper.py` → enriches the model with Hyper extract column types/data

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

### Map

- Detection by Tableau's `<column semantic-role='[Geographical].[Latitude]'>` first, then name match (`lat`/`lng`/`latitude`/etc.).
- Lat/lon columns get `dataCategory: Latitude` / `Longitude` and `summarizeBy: none` in TMDL.
- Map binders use bare Column refs (no aggregation) so coordinates aren't collapsed.
- **Auto-fills**: PBI Desktop auto-stamps X/Y/Series mirrors and per-binding filters on save. The converter pre-emits these for `map` / `filledMap` / `shapeMap` / `cardVisual` / `multiRowCard` so the open/save round-trip doesn't mutate the file. Bar/pie/table currently skip auto-stamping.

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
- **Categorical-bin calc columns** (`_compile_categorical_bin` in `parser.py`): Tableau's `<calculation class='categorical-bin' column='[X]'>` blocks emit as DAX calculated columns via `daxColumnExpr` — the column TMDL is `column 'Y' = SWITCH(TRUE(), [X] IN { ... }, "Bucket", [X])`. `_render_table_tmdl` skips `sourceColumn:` for columns with `daxColumnExpr` so PBI computes the value instead of looking for a CSV header that doesn't exist.
- **Boolean-dim calcs → calculated columns, not measures**: Tableau row-level boolean predicates like `[Date] >= [Start] AND [Date] <= [End]` get tagged `role='dimension' datatype='boolean'`. Emitted as a measure they break visual-level filtering — the scalar wrapper MIN-wraps the fact-table column, which collapses to the global min when the visual doesn't include that column on an axis (filter then either always passes or always fails). Fix in `_build_measures`: detect `role==dimension AND tmdlType==boolean` and emit as a calc column (`daxColumnExpr`) with same-table MIN-wraps stripped (`MIN('SameTable'[Col])` → `'SameTable'[Col]`). Cross-table refs (params) keep the MIN since each parameter table has 1 row. **Tradeoff**: calc columns evaluate at refresh time, so slicer changes to the param values don't re-trigger evaluation. Acceptable for converted Tableau models — the right PBI pattern is a slicer on the date column directly.
- **Tableau internal-object-id row counter**: `COUNT([__tableau_internal_object_id__].[<table-id>])` is Tableau's idiom for row count. Pre-translate to `COUNTROWS('<table>')` in `translate_tableau_to_dax` BEFORE tokenization (placeholder pattern, like LOD), so the COUNT wrapper around an unresolvable internal-table-id ref doesn't reach the main token loop and emit invalid DAX.
- **Auto-generated calc-field measures hidden**: Tableau anonymous calc fields get internal IDs like `Calculation_4313392949811654658`. These are noise in PBI's field pane — they're internal references between measures, not user-facing picks. `_build_measures` sets `isHidden=true` when the measure name matches `^Calculation[_ ]\d+$` so the field pane shows only named measures while DAX measures still cross-reference each other.

## Data blending → relationships

- **Tableau worksheet-level blends** (a worksheet pulls dims from one datasource + measures from another) get translated to PBI model relationships in `relationships.tmdl`. Detection runs in `_synthesize_blend_relationships` (model.py) over each worksheet's `datasourceDeps` list (parsed from `<datasource-dependencies>` in the twb XML).
- **Blend-key inference**: Tableau's TWB XML doesn't expose explicit blend keys, only an `<aliases enabled='yes'/>` flag. We fall back to "shared column names": dims that appear in BOTH the primary and secondary worksheet declarations become blend-key candidates. The candidate's `(table, column)` resolution must point to a real column on its TMDL table — measure-typed `col_locator` entries are filtered out so a measure name like `Total Distinct Patients` doesn't accidentally become a relationship endpoint.
- **Cardinality**: emitted as **many-to-many bothDirections**. Tableau blends don't enforce uniqueness or non-null on either side; many-to-one would reject duplicate or blank keys at load time. `bothDirections` is the closest semantic match because PBI's many-to-many with `oneDirection` is effectively passive (USERELATIONSHIP-only).
- **Cycle/parallel-edge avoidance** (`_deactivate_ambiguous_paths`): PBI requires exactly ONE active relationship path between any pair of tables. Synthesized blend rels can produce (a) parallel rels on different keys between the same pair, and (b) triangle cycles when three tables blend pairwise. Both raise `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` on load. Walk relationships in insertion order; use union-find to detect cycle-closing edges; mark second-and-later parallel rels and cycle-closers `isActive: false`. Inactive rels stay in the model — DAX can invoke them via `USERELATIONSHIP(...)`.

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
