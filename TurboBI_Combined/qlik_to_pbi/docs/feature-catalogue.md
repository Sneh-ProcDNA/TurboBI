# Feature catalogue

Catalogue of converted features as of conversation #7 (May 22 2026). Quick lookup for "does the tool already do X?"

## Visual types

(from `config.VISUAL_TYPE_MAP`, loaded from `visual_rules.json`)

linechart, barchart, combochart, piechart, scatterplot, treemap, kpi/sn-kpi, table, pivot-table/sn-pivot-table, filterpane (expanded into per-listbox slicers), listbox, histogram, boxplot, waterfallchart, gauge, map (azureMap with lat/long/size from `gaLayers`), action-button (with `goToSheet` → pageNavigation), text-image (with markdown → PBI paragraphs), container / sn-layout-container / sn-tabbed-container (expanded into the child visuals they hold — layout containers keep each child's percent position; tabbed containers tile the tabs in a grid; see `visual-and-emit-details.md`), sn-nav-menu → pageNavigator, sn-funnel-chart, sn-bullet-chart, sn-radar-chart, sn-sankey-chart, sn-grid-chart, sn-org-chart, sn-distribution-plot, sn-heatmap, sn-dot-plot, sn-network-chart, sn-word-cloud, mekko-chart.

## DAX translator coverage

- **Aggregations**: Sum / Avg / Min / Max / Count / Count(distinct) / Only / First / Last / Median / Stdev / StdevP / Fractile (→ PERCENTILE.INC) / Concat.
- **Date**: Year / Month / Day / Hour / Minute / Second / Week (→ WEEKNUM) / Weekday / Quarter, Yearstart/end, Monthstart/end, Quarterstart/end, Addmonths (→ EDATE) / Addyears / Adddays, MonthsBetween / Age, MakeDate, Today / Now.
- **String**: Len / Upper / Lower / Trim / Ltrim / Rtrim / Capitalize (→ PROPER), Left / Right / Mid, Replace (→ SUBSTITUTE), SubField (→ PATHITEM + SUBSTITUTE), Index (→ FIND), Text (→ FORMAT), SubstringCount.
- **Numeric**: Abs / Ceil / Floor / Round / Sqrt / Sqr (= x*x) / Log (LN) / Log10 / Exp / Sign / Frac (= x - INT(x)) / Pow (→ POWER) / Div (→ DIVIDE with safe zero) / Mod / Fmod / Even / Odd, Max / Min variadic.
- **Logic / type tests**: If, Pick (→ nested IFs), Match / WildMatch (→ IN {...}), Alt / Coalesce (→ nested IF/ISBLANK), Class (numeric bucketing via FLOOR), IsNull (ISBLANK), IsNum (ISNUMBER), IsText (ISTEXT), Null, Dual, Evaluate, Num/Date/Time/Timestamp/Interval (passthrough).
- **Color**: RGB (→ hex via DEC2HEX), ARGB, Color, White / Black / Red / Green / Blue / Yellow / Magenta / Cyan / Darkgray / Lightgray, ColorMix1 / ColorMix2.
- **Set analysis** (v2 tokenizer): single- AND multi-key `{<F={'v'}, G={1,2}>}` → `CALCULATE(core, f1, f2, ...)`; multi-value → `IN {...}`; `<>` → `NOT(... IN {...})`; `{"*"}` wildcard → `ALL(field)`; `Count({set} distinct field)` (distinct after the set block) → `DISTINCTCOUNT`; range/date search strings `Date={">a <b"}` → `FILTER(ALL(field), field > a && field < b)` with scalar date-pivot mapping (`Yearstart`→`DATE(YEAR(x),1,1)`, etc.).
- **Variables**: `$(var)` and the `$(=var)` evaluation form both expand to the variable body before translation; Qlik `//` and `/* */` comments are stripped (incl. trailing comments in variable bodies). Expression-defined variables are **materialised as real DAX measures** *before* master measures/dimensions are built (`model._materialize_variables_as_measures`, topological by `$(other)` deps); consumers reference `[varX]` instead of re-inlining the body. See `dax-translator-architecture.md`.
- **`dual(displayText, sortValue)`** → display arg (status arrows / custom-sort labels).
- **Still stubbed**: AGGR(...), `Peek` / `Above` / `Below`, `FirstSortedValue`, quarter-pivot range bounds (`Startofquarter`/`Endofquarter`), and search-string bounds whose inner expression doesn't fully translate.

## Formatting

- `qFmt` → DAX `formatString` translator handles currency (`$#,##0.00`), percent, scientific, date/time patterns. Expression formats fall back to type defaults.
- Column-level `formatString` defaults per data type (dates → `yyyy-MM-dd`, doubles → `#,##0.00`).
- Native-aggregation projections inherit column format.
- Visual styling: title text + color/size/font/alignment + title background, visual background, border, padding, legend show, data labels show, KPI value pane styling. From Qlik `components[]` array + `layoutOptions`. **Title is emitted under `visualContainerObjects.title`** (the PBIR-correct location — `objects.title` is ignored by PBI). See `visual-and-emit-details.md`.
- **Text-expression evaluation**: textbox expressions (`sn-text` Lexical nodes) and `=`-expression titles/labels are evaluated to their VALUES during the engine unbuild (object-context `GetLayout` + doc-level `EvaluateEx` → `evaluated-expressions.json` sidecar → `ir["evaluated"]`); no-engine paths fall back to a local static evaluator (literals, `&` concats, `chr()`, `$(var)`), then the measure label (no longer truncated at 80 chars). See `visual-and-emit-details.md` + `text_eval.py`.
- **Qlik colour matching**: a registered report theme (`StaticResources/RegisteredResources/QlikSenseColors.json`, `themeCollection.customTheme`) carries the app's Qlik data palette (captured custom `theme.json` → built-in horizon/classic tables → horizon default), and auto-coloured single-series cartesian charts are stamped with Qlik's `primaryColor` per-visual (PBI would otherwise use theme colour 0). See `pbi_theme.py` + `visual-and-emit-details.md`.
- **Bar / combo subtype** chosen from Qlik `barGrouping.grouping` + `orientation`: clustered/stacked × bar/column, plus line+clustered/stacked-column combo. See `visual-and-emit-details.md`.
- **Expression dimensions** (`=Field`, `=class(...)`, `=MonthName(Date)`, `=A &'-'& B`, incl. master/library dims) now populate the Category/Legend/Axis well: simple `=Field` resolves directly; real expressions synthesise a row-level **calculated column** (`column 'Name' = <DAX>`) and bind to it. Previously these blanked the field well. See `visual-and-emit-details.md`.
- **KPI / gauge** always bind a synthesised DAX **measure** (never a raw/aggregated column) so the card renders. Simple `Sum(X)` KPIs become `SUM('T'[X])` measures.
- **Yes/No flag columns stay TEXT**, not logical — Power Query can't convert "Yes"/"No" to `type logical` (it threw `Expression.Error: We couldn't convert to Logical`). Only literal `true`/`false` sniff as logical. (`csv_schema._BOOL_VALUES`).
- Qlik string literals `'x'` → DAX `"x"` in the legacy agg path (single quotes are table refs in DAX); `_rewrite_bare_identifiers` skips double-quoted literals.

## Data layer (4 modes)

1. Empty stub partition (default).
2. `--data-dir` CSV binding with schema sniffing.
3. `--fetch-data` (cloud Qlik CLI per chart).
4. `--fetch-via-engine` (offline Desktop WebSocket; writes `engine-schema.json` sidecar with full schema + qk keys).

Plus:
- Script-derived partitions (Csv / Excel / Json / Xml / Odbc / Parquet / Folder from `script.qvs` LOAD blocks).
- Live-DB partitions via credentials.json (SQL Server, Postgres, Redshift, Snowflake, Databricks).

## Other features

- What-If parameters from Qlik variables (function exists; auto-synthesis disabled by default — see `visual-and-emit-details.md`).
- Qlik bookmarks → PBI bookmark scaffolds (engine-recovered). Field selection state IS now captured (ApplyBookmark + selection object, with explicit-value resolution for engine-summarised "N of M" selections) and listed in `conversion_report.md`'s **Bookmarks** section. The PBI bookmark file itself stays a name+page scaffold — encoding the captured selections into PBI bookmark filter-state is deferred (high blast radius, can't validate without Desktop).
- Page navigation (sn-nav-menu + action-button `goToSheet`).
- Filterpane orientation honouring.
- Reference lines on charts.
- Tooltip-only measure routing.
- Sort direction from `qSortBy`.
- Pre-flight structural validator.
- Enriched `conversion_report.md`.
- CLI flags `--dry-run` / `--quiet` / `--report-only`.

**How to apply:** When asked "can the tool do X?" — check this list first. If listed, find the responsible module via the keyword (e.g. "sort" → `_sort_direction_from_qlik` in `report.py`).
