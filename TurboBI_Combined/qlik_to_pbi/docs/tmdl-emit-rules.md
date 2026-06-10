# TMDL emit rules

## TMDL layout

```
<name>.SemanticModel/
  .platform
  definition.pbism
  data/                                    (only when CSVs bound)
    <table-or-object-name>.csv             copies of --data-dir or fetched CSVs
  definition/
    database.tmdl          "database\n\tcompatibilityLevel: 1600\n"
    model.tmdl             model header + `ref table` per table
                              + `expression RepoPath = "<abs>"` (when any
                                CSV partition exists)
                              + `ref cultureInfo en-US`
    relationships.tmdl     one `relationship <lineage_tag>` per join
    cultures/en-US.tmdl
    tables/<safe_table_name>.tmdl   columns + measures + partition
```

`RepoPath` expression in model.tmdl only when at least one CSV partition exists. `SemanticModel._uses_repo_path` is flipped by `_render_table_tmdl` whenever a CSV partition is rendered; the model.tmdl emit reads the flag afterwards.

## Partition shapes

**Empty stub** (no CSV match):

```m
partition <Table> = m
    mode: import
    source =
        let
            Source = Table.FromRows({}, type table [#"col1" = text, ...])
        in
            Source
```

**CSV-backed**:

```m
partition <Table> = m
    mode: import
    source =
        let
            Source = Csv.Document(File.Contents(RepoPath & "/data/<file>.csv"),
                                  [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
            PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
            ChangedTypes = Table.TransformColumnTypes(PromotedHeaders,
                                  {{"col1", Int64.Type}, {"col2", type text}, ...})
        in
            ChangedTypes

    annotation PBI_NavigationStepName = <final-step-name>

    annotation PBI_ResultType = Table
```

**Script-derived** (a `LOAD … FROM …` block recovered by `script_to_m`, when no CSV / live-DB match): same `let … in` shape, M built by `ScriptTranslator` (`Csv.Document` / `Excel.Workbook` / `Qvd.Tables` stub / etc.) with a `// TODO: Update FilePath` comment for `lib://` sources. `_script_partition_m` matches `ir["script_blocks"]` by table name and skips `resident` / `inline` / `unknown` blocks.

### Expression-block indentation (load-blocking invariant)

**Every** partition `source =` expression line MUST be indented *deeper* than the `source =` property (2 tabs): `let` / `in` at **3 tabs**, body steps at **4 tabs**. TMDL aborts the whole project load with `TMDL Format Error: Invalid indentation was detected!` (pointing at the first expression line, usually `let`) and no per-file context if any line is shallower. `script_to_m` returns *canonical* M (`let`/`in` at column 0, steps at 2 spaces); `_render_table_tmdl` re-indents it per-line at the emit boundary — it does NOT carry TMDL depth itself. The other four branches (what-if / live / CSV / empty-stub) bake the 3-/4-tab depth into their literal `"\t\t\t…"` strings directly. (Bug fixed 2026-06: the script branch appended the column-0 M verbatim → `let` at column 0 → every script-derived table failed to open. The new pre-flight check below guards against any recurrence in any branch.)

Calc-column (`column 'X' = <DAX>`) and measure (`measure 'X' = <DAX>`) expressions are flattened to a single line via `_flatten_expr` (newlines/tabs → spaces; DAX is whitespace-insensitive) so a multi-line DAX expression can never leak a continuation line to column 0 the same way.

## Table-level annotations

`PBI_NavigationStepName` and `PBI_ResultType` are emitted at **table scope** (one tab, sibling of `column` / `partition`), AFTER the partition body, with a blank line between each. This matches what PBI Desktop itself emits when it saves a TMDL project — putting them inside the `partition` block is syntactically accepted but breaks PBI's PQ Editor round-trip: PQ can't map back to a "current step", the M can't be edited cleanly, and PBI Desktop reports "pending changes that haven't been applied" on every open.

`PBI_NavigationStepName`'s value is the M expression's final step name (`ChangedTypes`, `Typed`, `Source`).

`PBI_Parameter = True` at table scope only for synthesised What-If parameter tables.

## formatString policy ("format-shown-as-text" trap)

`_render_table_tmdl` is selective about emitting `formatString` on columns:

- **date / dateTime columns** keep their format pattern (`yyyy-MM-dd`, `yyyy-MM-dd HH:mm:ss`) — without it, PBI prints the long ISO timestamp.
- **What-If parameter columns** keep `#,##0.00` so the slicer renders nicely.
- **Every other numeric / string column gets NO `formatString`.**

Reason: PBI Desktop applies `formatString` to the column's *declared* data type. If the M cast doesn't deliver the matching numeric storage type (because the data file's column contains text where the type list said int64, for instance), PBI falls back to rendering the format pattern itself as the cell's text — the user sees `#,##0` literally in the data view. Leaving the format unset means PBI renders the value as-is for any storage type; the user can apply a format via the UI if desired.

**Note on What-If**: auto-synthesis of What-If parameter tables is DISABLED by default — see `visual-and-emit-details.md`. The formatString rule above applies when a user opts in or manually creates one via PBI Desktop > Modeling > New Parameter.

## summarizeBy heuristics

`_render_table_tmdl` picks `summarizeBy` per column:
- **What-If parameter column** → `none` + `SummarizationSetBy = User` (slicer's selected value must not be re-aggregated).
- **`string` / `dateTime` / `date` / `boolean` / `time` / `binary`** → `none` (PBI hides Sum for these anyway).
- **`int64` / `decimal` whose name ends in `id` or `_id`** → `none` (summing a PK is meaningless and clutters the field-well menu with "Sum of HCO_ID").
- **Everything else numeric** → `sum`.

## Column data-type resolution (`_guess_type` + `_reconcile_column_types_with_dax`)

Type-signal priority when building columns (`_columns_for_table`):
1. **Engine `qTags`** (`$integer` / `$numeric` / `$date` / `$timestamp` / `$text`) — strongest.
2. **CSV content sniffing** (`csv_schema.sniff_csv_schema`) — when a data file matches.
3. **`_guess_type` fallback** — column-NAME conventions for the loadmodel / empty-stub path (no engine tags, no CSV). Qlik's loadmodel carries no per-field type, but field names follow strong conventions: a `...DT` / `...Date` / `...Timestamp` suffix → `dateTime`; `...AMT` / `...QTY` / `...NBR` / `...CNT` / `...PCT` and camel words `Amount`/`Count`/`Rate`/`Charge`/`Balance`/`Total`/`Cost`/`Price`/`Weight`/`Percent` → `double`; everything else → `string`. Case-sensitive so "Update"/"Discount" don't read as date/count. Safe because this only types an EMPTY `Table.FromRows({})` partition (no rows to fail a cast); IDs/codes stay `string`.

The all-`string` fallback is then corrected by **`_reconcile_column_types_with_dax`**, run at the top of `write_tmdl` (after the report builder has appended its inline-chart measures, so every measure is in scope). It scans the *generated measure DAX* for how each column is used:

- **Promotes a `string` column to `int64` / `double`** when a measure `SUM`/`AVERAGE`s it, uses it in `*` / `/` arithmetic, or compares it to a bare numeric literal (`'T'[F] = 1`). This is what removes the runtime errors *"SUM cannot work with values of type String"* and *"comparison operations do not support comparing values of type Text with values of type Integer"*. SUM/arithmetic → `double`; integer comparison → `int64`. **Promotion is restricted to NO-DATA STUB tables** (`stub_tables` — tables where the partition is neither what-if, live (`connection`), CSV (`table.get("csv")` or in `table_csv`), nor a script partition that resolves to a **real** data source). The stub partition derives its M type from `dataType` (`_m_type_for`), so promoting keeps TMDL ↔ partition-M consistent (`double` ↔ `number`, `int64` ↔ `Int64.Type`) and there are no rows to fail a cast. **Why not CSV/live/real-script**: their column types come from an external source (sniffed `mType`, the DB schema, or `Table.PromoteHeaders` with no cast) — promoting `dataType` there would declare a numeric type the partition actually delivers as text, and the column would fail to load. **QVD-stub tables ARE promoted (2026-06 fix):** a `LOAD … FROM x.qvd` table has a script partition, but PBI can't read QVD so `script_to_m` emits an **empty `Source = #table({}, {})` stub** (no rows) — it is a stub in disguise. The gate now excludes a script table only when its M does NOT contain `#table({}, {})` (i.e. a genuine `Csv.Document`/`Excel.Workbook`/`Odbc` source); a `#table({}, {})` script stub is promoted like the no-source stub. **This is the common case** — most real apps load fact tables from QVD, so before the fix every numeric fact column (e.g. `Facts[Value]`, summed by `SUMX('Facts', 'Facts'[Value] * 'Accounts'[Weight])`) stayed `string`, showing as a non-numeric field with no Σ and mis-evaluating once data is bound (user report, app `2f9b36be`). Bed `regression/qvd_stub_type_promotion.py`. In Qlik set analysis an unquoted value `{<F={1}>}` already means F holds the number 1, so the promotion matches Qlik semantics.
- **Quotes the literal** for a column that must stay text but is compared to a number (e.g. a CSV-sniffed text column): `'T'[F] = 1` → `'T'[F] = "1"` — a valid text comparison instead of the failing text-vs-integer one.
- **Set-analysis `IN {…}` lists** are scanned too (`in_re`): `'T'[Col] IN {2020, 2021}` with an all-numeric value list marks `Col` numeric (promote on stub tables) the same way a scalar comparison does; on a still-text column the values are quoted (`IN {"2020","2021"}`). A list containing any quoted/string value is left alone. (Without this, multi-value set filters on a string stub column produced an uncaught text-vs-integer error — the scalar `cmp_a`/`cmp_b` regexes only see single comparisons.)

`DISTINCTCOUNT` / `COUNT` and `MIN` / `MAX` do NOT trigger promotion — they are valid over text, so `Count(distinct PatientID)` leaves `PatientID` as `string`.

- **Coerces `SUM`/`AVERAGE` over a text column on a BOUND table (pass 3, 2026-06 fix)**: when the table is NOT a stub (parquet/CSV/live partition — promotion forbidden, see above), `SUM('T'[X])` over a `string` column evaluates to TEXT, which Power BI renders wrapped in quotes (a KPI card showing `'21'` instead of `21` — user report). The pass rewrites the aggregation to the iterator form with a numeric coercion: `SUM('T'[X])` → `SUMX('T', IFERROR(VALUE('T'[X]), BLANK()))` (and `AVERAGE` → `AVERAGEX`). Non-numeric rows blank out, matching Qlik's `Sum()`/`Avg()` semantics (text ignored). MIN/MAX are deliberately excluded — `MinString`/`MaxString` translate to legitimate text `MIN`/`MAX` (same exclusion as the pre-flight check, whose `SUM\s*\(` regex also no longer fires on the rewritten `SUMX(` form).

## Hidden columns (`isHidden`)

Engine-schema fields tagged `$hidden` (`is_hidden` in the sidecar) emit `isHidden` in their TMDL column block. The column STAYS in the model (relationships / measure refs that target it still resolve) but is hidden from PBI's field list — mirroring Qlik's intent instead of cluttering the field well with engine-internal helper fields. Loadmodel-built models carry no hidden info, so this only applies to engine-schema builds.

## Column name sanitisation (`_sanitize_column_name`)

PBI's TMDL parser uses `.` as the table.column separator inside qualified refs. A loadmodel column called `HCP.City` would break relationships pointing at `HCP.'HCP.City'` because the parser reads that as a three-segment reference. `_sanitize_column_name` rewrites `.` and other separator characters to `_` before emission. The original name is kept on the column's `sourceColumn` property so the M binding still resolves to the actual on-disk column.

## Format string translation (`_qlik_format_to_pbi`)

Translates Qlik's `qNumFormat` into DAX-compatible format strings:

| Qlik qType | Output |
|---|---|
| `M` (money) | `"$"#,##0.00;"$"-#,##0.00` (precision from `qnDec`) |
| `D` (date) | `yyyy-MM-dd` (default; explicit `qFmt` normalised) |
| `T` (time) | `HH:mm:ss` |
| `TS` | `yyyy-MM-dd HH:mm:ss` |
| `IV` | `[h]:mm:ss` |
| `U` / `R` / `F` (no `qFmt`) | `""` — let the column-level format drive |

Explicit Qlik patterns pass through with case normalisation (`YYYY` → `yyyy`, `HH24` → `HH`). Expression formats (`=Date(...)`) are dropped to the type default — DAX rejects expressions as format strings and would fail load.

**No-default-stamp policy for measures**: for `qType = U` / `R` / `F` without an explicit `qFmt`, the function returns `""` rather than synthesising a default. The underlying column's data-type format (`#,##0` for int64, `#,##0.00` for double, `yyyy-MM-dd` for dateTime) already drives the measure's display — re-translating Qlik's `qnDec` hint (often a wild value like 10) would produce garbage like `#,##0.0000000000`.

### Numeric pattern normalisation (`_normalise_numeric_format`)

Qlik commonly emits malformed numeric patterns that DAX rejects:

| Qlik input | DAX output |
|---|---|
| `###,#` | `#,##0` (DAX requires terminating `0`) |
| `###,###` | `#,##0` |
| `###,###.##` | `#,##0.##` |
| `# ##0` | `#,##0` (space → comma thousand sep) |
| `##0` | `0` |
| `####` (no separator) | `0` (NOT `#,##0`) |
| `0000` (zero-pad code) | `0` (NOT `#,##0`) |
| `$#,##0.00` | passed through (currency prefix preserved) |
| `0%`, `0.0%` | passed through |
| `0.00;-0.00` (sign template) | each segment normalised independently |

Invoked from `_qlik_format_to_pbi` for non-date `qType`s when an explicit `qFmt` is present. Date / datetime patterns pass through untouched (token replacement handled separately).

**Thousands grouping is added ONLY when the Qlik mask carried a separator** (`,` or space). The old "4+ digits → group" rule mangled zero-padded codes: `0000` (a 4-digit year / store code) became `#,##0`, rendering `2024` as `2,024`. A wide all-`#` mask without a separator becomes plain `0` — showing `1000000` instead of `1,000,000` is cosmetic; corrupting a code is not.

## Heuristic relationship inference (`_infer_relationships_from_shared_fields`)

Runs only when the loadmodel's `queries` / `associations` are empty (common for direct-parsed QVFs and stale unbuild snapshots):

- For each column name that appears in **exactly two** tables, emit a many-to-one relationship.
- Skip names shared by 3+ tables (key fields used everywhere) to avoid fan-out routing.
- Pick the smaller table (fewer columns) as the "one" side — gets cardinality right for ~80% of star schemas.

Always runs as a fallback only — if `_extract_relationships` already produced anything from the loadmodel's `associations`, the inference is skipped.

## Relationship cardinality (`_assign_relationship_cardinality` + `_render_relationships`)

**Every relationship is emitted many-to-MANY, single-direction. The converter NEVER auto-emits a many-to-one.** Relationship records are built with `from`/`fromColumn` = the "many" (fact) side and `to`/`toColumn` = the dimension side by both `_extract_relationships_from_engine` and `_infer_relationships_from_shared_fields`. `_assign_relationship_cardinality` (end of `build()`) sets every `toCardinality = "many"`; `_render_relationships` adds **`crossFilteringBehavior: oneDirection`** on each so the filter still propagates dimension → fact exactly like a many-to-one would for ordinary visuals, and PBI's `automatic` resolution can't pick bidirectional (which risks an ambiguous-path load rejection). NO `bothDirections`, ever. (`_render_relationships` keeps a defensive `to_card == "one"` branch — plain single-direction, no crossFiltering line — for a relationship set to one by hand, but the automatic pipeline never sets it.)

**Why unconditional many-to-many (policy settled 2026-06 after repeated regressions).** Qlik's associative model is many-to-many by nature: a shared field name associates two tables where *either* side may repeat — composite/synthetic keys (`AggSales[AggKey] = '103-10015824-roadway'` on many rows), bridge/link tables, and blank keys (`MasterPlanning[Master Planning Family] = '' ×2`) are all routine. PBI's many-to-one imposes a one-side uniqueness-and-non-blank constraint Qlik never had. A wrong many-to-one is **load-fatal**: PBI aborts the entire refresh with *"Column 'X' contains a duplicate value '…' … not allowed on the one side of a many-to-one relationship"*, cascading as opaque `OLE DB or ODBC error: 0x80040E4E` / "Load was cancelled by an error in loading a previous table" across every other table.

Crucially, **the converter cannot reliably prove one-side uniqueness at convert time**, so it must not try. An earlier attempt read the bound CSV and upgraded to many-to-one when the key looked unique — but cloud fetch is **row-capped (~500 rows; see `data-fetch-modes`)**, so a sample that looks unique does NOT mean the full column is, and the upgrade then failed at refresh against the complete data (exactly how `AggSales[AggKey]` kept recurring). Engine extracts are sampled/deduped, stubs have no data, and Python's `csv` vs PBI's `Csv.Document` can disagree on parsing/locale. The consequences are **asymmetric**: a false "unique" breaks the whole report; staying many-to-many merely makes the relationship "limited" (slightly slower; a few advanced DAX patterns differ) but it always loads. So we always emit many-to-many and let the user tighten specific relationships to many-to-one in Desktop, where PBI validates the actually-loaded full data and reports if it's safe. Single-direction M:M filters dimension→fact identically to M:1 for normal fact-by-dimension visuals (no over-counting). The original (working) converter used blanket many-to-many for the same reason; the interim many-to-one "optimization" regressed real apps five times (ItemMaster → ShipToAddress2 → SalesRep → MasterPlanning → AggSales) before being abandoned. **Do not reintroduce an automatic many-to-one path.** Pairs with `_prune_dangling_relationships` (drops relationships whose key column isn't on the table at all). Bed: `regression/cardinality_dupkey.py`; anchors show 11/11 and 4/4 many-to-many, preflight-clean.

## Sheet → page title resolution

Priority order:
1. `qProperty.qMetaDef.title` from the sheet JSON.
2. The slug embedded in the filename `sheet--<title-kebab>-<guid>.json`. `_title_from_filename` matches the guid suffix and title-cases the slug (`sheet--patient-details-...json` → "Patient Details").
3. Literal `"Untitled"`.

## Pre-flight validator (`preflight.run_preflight`)

After `writer.write()`, checks:

- `visualContainerObjects` keys are in the whitelist (`background`, `border`, `padding`, `visualHeader`, `stylePreset`, `divider`, `outspacePane`, `title`, `general`, `shadow`, `lockAspect`).
- Every relationship's `fromColumn` / `toColumn` exists on the named table. Parsed **block-by-block** (`_REL_HEADER_RE` splits on each `relationship` line; `_FROMCOL_RE` / `_TOCOL_RE` extract the columns independently within the block) so it's robust to property order / extra lines (`fromCardinality`, `toCardinality`, `crossFilteringBehavior`) — the old single regex required `fromColumn` immediately followed by `toColumn` and silently stopped validating if anything was inserted between them.
- Every `ref table` in `model.tmdl` has a corresponding `tables/<name>.tmdl` file.
- Every page id in `pages.json` has a matching folder + `page.json`.
- Each `visual.json` has `visualType` and a valid `position`.
- **Partition expression indentation** (`_check_partition_indent`): for every `tables/*.tmdl`, the first non-blank line after a bare `source =` (matched by `_SOURCE_PROP_RE`) must have *more* leading tabs than the `source =` line itself. Catches the load-blocking "Invalid indentation was detected!" class before Desktop does — across all five partition branches.
- **Column-only aggregations** (`_check_measure_aggregations`): a measure / calc-column `… = <expr>` must not apply `SUM` / `AVERAGE` / `DISTINCTCOUNT` to anything but a bare column (`'T'[C]` / `[C]`). Catches the refresh-time *"The SUM function only accepts a column reference as an argument"* class. (The translator's `_finalize_dax` should already have promoted these to `SUMX` / `CALCULATE`; this is the net.)
- **Numeric aggregation over a TEXT column** (`_check_numeric_agg_on_text_column`): a measure `SUM('T'[C])` / `AVERAGE('T'[C])` where `C`'s declared `dataType` is `string`. A numeric aggregation of text returns TEXT, which PBI renders **wrapped in single quotes** (`'37K'`, `'10.6%'`) on cards, axes and data labels — the classic "numbers shown as string" report. Root cause is data fetched with that column typed text (untagged engine field → string before the parquet `auto` fix); the build can't retype it (Parquet schema is authoritative, single-step partition has no cast), so the warning's remedy is **re-fetch/re-convert**. MIN/MAX are excluded (text MIN/MAX is legitimate); COUNT/DISTINCTCOUNT return numbers regardless. Fires only on data-bound builds — metadata-only `--input` builds reconcile such columns to numeric (`_reconcile_column_types_with_dax`), so the real-app anchors stay clean. Bed: `regression/kpi_text_agg.py`. See also `data-fetch-modes.md` (engine Parquet column kinds → `auto`).

Warnings appended to `conversion_report.md`'s "Pre-flight" section. Surfaces what PBI Desktop reports as generic "Cannot resolve all paths…" with no per-file context.

## Post-write artifact verification (`writer._verify_artifacts`)

Runs at the END of `writer.write()` (after all files are on disk). Raises a `RuntimeError` if any **required** PBIP/PBIR artifact is missing, empty/whitespace, legally-empty JSON (`{}` / `[]` / `null`), or invalid JSON: the root `<name>.pbip`, `<name>.Report/definition.pbir`, `definition/report.json`, `definition/version.json`, `<name>.SemanticModel/definition.pbism`, and every `definition/pages/**/page.json` + `**/visual.json`. Mirrors PBI's own `IsJsonLegallyEmpty` check.

**Why:** PBI reports an empty `definition.pbir` only at OPEN time, as the opaque *"ReportDefinition: Required artifact is missing … IsJsonLegallyEmpty"*. The converter always writes valid content (the `.pbir` is a static, spec-correct dict — `definitionProperties/2.0.0` + `version 4.0` + `byPath`, byte-identical to the MS Learn example), so an empty file means the WRITE was truncated (disk full, antivirus quarantine, locked/re-opened file, interrupted run) — or the user opened the `.pbip` from *inside* a zip and Windows extracted a 0-byte entry (fix: extract the whole zip to a real folder first). This check converts that silent, open-time failure into a clear convert-time error so a broken PBIP never ships. Do NOT "fix" the `.pbir` format in response to this error — the format is correct. Bed: `regression/artifact_verify.py`.
