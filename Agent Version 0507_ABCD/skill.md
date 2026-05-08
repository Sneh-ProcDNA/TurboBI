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

### Calculated columns vs measures
- **Calc field with `role='measure'` + formula** → DAX measure.
- **Calc field with `role='dimension' + datatype='boolean'` + formula** → DAX calculated COLUMN (row-level eval). Same-table `MIN(...)` wrapping is stripped so it works in row context. Cross-table param refs keep `MIN()` (1-row tables).
- **Tableau categorical-bin block** (no formula attribute, has `<bin>` children) → DAX calculated column with `SWITCH(TRUE(), [src] IN {...}, "bucket", [src])`.

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

### Blend relationships
Synthesized many-to-many `bothDirections` rels between tables that share a column name across worksheets that bind to multiple datasources. Cycle-breaker marks redundant rels `isActive: false` to satisfy PBI's "one active path per pair" rule.

### Connection classes (partition M emit)
- `hyper` / `excel-direct` / `csv` / `textscan` / `federated` / `""` → CSV-from-Hyper extract path.
- `sqlserver` live → `Sql.Database(server, db)` + Schema/Item nav, `mode: directQuery`.
- `postgres` live → `PostgreSQL.Database("host:port", db)` + Schema/Item nav.
- `snowflake` live → `Snowflake.Databases(account, wh, [Implementation="2.0"])` + DB/Schema/Name nav.
- Custom SQL on sqlserver/postgres → `Value.NativeQuery(Source, "<sql>", null, [EnableFolding=true])`.
- Other live (oracle/mysql/redshift/...) → placeholder `Table.FromRows({})` + TODO comment + `[CONN] UNSUPPORTED` log.

## Visual picker skills (`visual_picker.py` + `visual_rules.json`)

### Mark → visualType
Direct lookup table. Notable mappings (after fixes):
- `bar` → `barChart` (with orientation override below)
- `circle` → `scatterChart` (was `pieChart` — fixed)
- `pie` / `donut` → `pieChart` / `donutChart`
- `point` → `map`, `polygon` → `filledMap`
- `automatic` → falls through to auto rules

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

## What's NOT yet implemented

- `INCLUDE` / `EXCLUDE` LODs
- `INDEX()` / `RANK_UNIQUE` / `RANK_DENSE`
- `RUNNING_SUM` / `WINDOW_AVG` / `LOOKUP` (need PBI Visual Calculations)
- `REGEXP_EXTRACT` / `REGEXP_REPLACE` / `REGEXP_MATCH`
- Multi-line strings with embedded `\r CHAR(10)` (could substitute `UNICHAR(10)`)
- Forecast / "Forecast Indicator" output column from Tableau forecast
- `:Measure Names` (Tableau's pivoted column header)
- Live connectors beyond sqlserver/postgres/snowflake (oracle/mysql/redshift/bigquery/...)
- Tableau `<aliases>` value substitution (display labels for column values)
- Tableau set / group / hierarchy definitions
