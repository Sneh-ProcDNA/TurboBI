# Visual emit details (restored sections)

These are the bug-prevention notes for visual JSON emit — each section corresponds to a class of regression we've already had to fix once.

## Visual styling pipeline

`_extract_visual_style(props)` produces a normalised style dict from a Qlik cell `qProperty`. It walks top-level fields (`title`, `fontSize`, `textAlign`, `showTitles`, `background`), `layoutOptions`, and the `components[]` array (Qlik's rich-style records keyed by `general` / `theme`).

Output keys: `title`, `subtitle`, `footnote`, `showTitle`, `textAlign`, `fontSize`, `fontColor`, `fontFamily`, `backgroundColor`, `borderColor`, `borderWidth`, `padding`, `legendShow`, `showDataLabels`.

`_apply_container_styling(visual_block, style)` writes `visualContainerObjects.{background, border, padding, title}`.

**Title placement (corrected):** the visual title goes under **`visualContainerObjects.title`** — NOT `visual.objects.title`. The visualContainer 2.8.0 / visualConfiguration 2.3.0 schemas define `title` (and `subTitle`, `background`, `border`, `padding`, `divider`, `dropShadow`, `visualHeader`, `stylePreset`, …) under `visualContainerObjects`; `objects` is for visual-TYPE formatting (legend, axes, labels, dataPoint). PBI silently IGNORES a title placed under `objects.title`, which is why titles "weren't being picked". The key name is `title` (the earlier rejected attempt used the wrong key `visualTitle`). `_title_properties(style, title)` builds the bag: `show` / `text` / `fontColor` / `fontSize` / `fontFamily` / `alignment` / `background` (title bg), all expr-wrapped. Both `_build_chart` and the other factories now route titles through `_apply_container_styling` (single source of truth).

Per-visual factories (chart / slicer / textbox / action-button / page-navigator) all call the extractor then apply the styling.

## Text-expression evaluation (textbox values, not formulas)

Qlik text objects (`sn-text` Lexical JSON referencing hypercube measures by `cId`; legacy `text-image`) and visual titles routinely embed expressions (`='Top ' & $(vBrokers) & ' Brokers'`, `=Num(Sum(X),'#,##0')`). PBI textboxes are static, so the converter snapshots the **evaluated values** while the engine is connected:

- **Unbuild side** (`text_eval.evaluate_unbuilt_expressions`, called at the end of `engine_unbuild.unbuild_via_engine` — cloud AND Desktop): each text object is evaluated **in object context** via `GetObject → GetLayout` (`qGrandTotalRow` → first data-page row → one explicit `GetHyperCubeData` page), which applies `qNumFormat`, master measures and object-scoped functions exactly as the Qlik client renders them. Leftover title-level expressions (`qStringExpression` bodies, `=`-prefixed `title`/`subtitle`/`footnote`/`label` strings, `qLabelExpression`) go through doc-level `EvaluateEx`. Results land in the **`evaluated-expressions.json`** sidecar: `{"objects": {objectId: {cId: text}}, "expressions": {rawExpr: text}}`. Must run AFTER `_write_bookmarks` (its trailing `ClearAll` guarantees an unfiltered state — a saved selection would skew every aggregate). Never fails the unbuild; unevaluable entries are simply absent.
- **Report side**: the parser loads the sidecar into `ir["evaluated"]`. `ReportBuilder._lookup_text_expr(objId, cId, expr)` resolves Lexical expression nodes (object snapshot → expression snapshot → local static eval); `ReportBuilder._resolve_text(raw)` does the same for title-ish values and is threaded into `_extract_visual_style(props, resolve)`, the slicer header, and the action-button label chain. Final fallback is the historical label behaviour (`qLabel` → `qLabelExpression` → `clean_label(qDef)`) — but no longer truncated at 80 chars (cap 4000).
- **No-engine paths** (old unbuilt folders, `qvf_direct`): `text_eval.eval_static_expression` resolves literals, `&` concats of literals, `chr(n)`, comments, and `$(var)` expansion when the variable's definition is itself statically evaluable. Anything data-driven returns `None` → label fallback. Empty-string evaluation results are legitimate (use explicit `is None` checks, not `or`-chains, when consulting the sidecar).

## Qlik colour matching (registered theme + single-series default)

PBI's default theme colours series with its own palette (#118DFF first), so a default-coloured Qlik app converted without colour work looks wrong everywhere. Two mechanisms fix this (`pbi_theme.py`):

- **Registered report theme** — `converter` resolves the app's palette via `pbi_theme.build_report_theme(ir["qlik_theme"], app theme id)` and `writer._write_report` drops it at `<name>.Report/StaticResources/RegisteredResources/QlikSenseColors.json`, registered in `report.json` as `themeCollection.customTheme {name: "QlikSenseColors.json", type: "RegisteredResources"}` + a `RegisteredResources` entry in `resourcePackages` (item `type: "CustomTheme"`; the `name`/`path` carry the `.json` extension). The theme document is minimal on purpose — `name` + ordered `dataColors` + `tableAccent` — so it overrides only the series palette and inherits fonts/backgrounds from the CY24SU02 base. Palette priority: captured `theme.json` sidecar (custom themes fetched from `/api/v1/themes/{id}/file/theme.json` during the cloud unbuild; pyramid `scale` lists take the longest row) → built-in table by theme id (`horizon` = the 12 colours the converter always used; `sense`/`breeze`/`focus`/`classic` = the muted-rainbow classic 12, primary `#26a0a7`) → horizon default. The app's theme id is captured into `app-properties.json` (`theme` key) by the engine unbuild.
- **Single-series default fill** — Qlik paints a ≤1-dimension, 1-measure cartesian chart with its theme's `dataColors.primaryColor`, NOT data-palette colour 0; PBI would use `dataColors[0]`. So `_build_chart_objects` stamps **`objects.dataPoint[0].defaultColor`** = the palette primary on auto-coloured single-series charts (`lineChart`/bar/column families/`scatterChart`, `_hypercube_counts` ≤1 dim + ==1 measure, and `chartColorMode` not dimension/expression). An author's explicit single colour (`color.auto: false, mode "primary"` → `chartPrimaryColor`) always wins. Dimension-coloured types (pie/donut/treemap/funnel), waterfall (sentiment colours) and multi-series charts are left to the registered theme.
- **`dataPoint` is the real object name (2026-06 fix)**: the earlier emission used `objects.dataColors[].fillColor` — that is NOT a PBI visual-object name, Desktop silently ignored it (the objects bag is an open map), so single-colour stamps never took effect and line/bar charts rendered with theme colour 0. The data-colour card on cartesian/pie/scatter charts is `dataPoint` with `defaultColor` (no selector) for the all-points default and `fill` + a data selector for per-series overrides. Never emit a multi-entry selector-less palette — PBI collapses it to one fill; palettes go through the registered theme only.
- **KPI value colour (`conditionalColoring.paletteSingleColor`)**: with `useConditionalColoring` falsy, Qlik renders the KPI value in `paletteSingleColor` — observed real apps ALL store `singleColor: 3` with the colour there (`{index: 6}` = default dark slate, or explicit `{index:-1, color}`); the old `singleColor == 2` gate matched no real app so KPI values never got their colour. The UI-palette lookup (`_QLIK_PALETTE`) is the full 16-entry Sense `palettes.ui` table — index 6 = `#41555d` is Qlik's default KPI value colour, and the cardVisual value-pane fallback chain is `kpiValueColor → fontColor → #41555d` (not black). Segments-based conditional colouring stays un-replicated.
- **Theme registration convention (verified against Microsoft's semantic-link-labs `set_theme`)**: `customTheme.name` and the RegisteredResources item `name`/`path` all carry the `.json` extension; item type `CustomTheme`; file at `StaticResources/RegisteredResources/<name>.json`; `customTheme.reportVersionAtImport` is REQUIRED (report schema 3.2.0).

## Expression dimensions → field-well population (calculated columns)

Qlik dimensions are frequently EXPRESSIONS, stored in `qDimensions[].qDef.qFieldDefs` (or on a master dimension's `qDim.qFieldDefs`) with a leading `=`. Previously `_resolve_field_name` dropped anything starting with `=`, so the chart's **Category / Legend / Axis well came out blank** even though the measure (Y) populated. Now:

- **`=Field` (simple field with `=` prefix)** — `_resolve_field_name` strips the `=` (and any wrapping parens / trailing dangling `&`+`-` operator from truncated exports) and resolves the bare field directly. Handles `=HCP_Name`, `=Specialty`.
- **`=Expr` (real calculated dimension)** — `_resolve_dimension_expr` synthesises a row-level **calculated column** on the home table (the table of the first token that is a known field) and binds the visual's category to it. The Qlik expression is translated via `translate_qlik_to_dax`; the result is accepted only if it is row-level (rejected if it contains `SUM/COUNT/CALCULATE/...` — those belong in a measure, not a column). Examples that now work: `=class(WeightOuncesQTY/16,25)` → `FORMAT(FLOOR(...),"0") & "-" & ...`, `=YearNBR &'-wk'&WeekOfYearNBR` → `'T'[YearNBR] & "-wk" & 'T'[WeekOfYearNBR]`, `=MonthName(Date)` → `FORMAT('T'[Date], "MMM yyyy")`.

**Master-dimension ordering trap:** a library dimension always carries a friendly `qDim.title` (e.g. "Weight Group") that is NOT a real column. `_resolve_field` must try the expression/calc-column path BEFORE the title fallback — otherwise `return _resolve_field_name(title)` returns `None` and masks a perfectly translatable expression dimension.

Calculated columns are emitted in TMDL as `column 'Name' = <DAX>` (no `sourceColumn`) and are EXCLUDED from every partition's column/type list (CSV, engine, empty-stub) — otherwise the column is defined twice (source + calculated) and the model fails to load. New date-name scalars added to v2: `MonthName` → `FORMAT(x,"MMM yyyy")`, `DayName` → `FORMAT(x,"ddd")`.

Related legacy-translator fix: `_rewrite_field_refs_with_sum` now bails (→ stub → v2) on `&` concatenation, single-quoted literals, or bare unqualified identifiers. A stray `-` inside a literal like `'-wk'` used to trip the numeric-arithmetic path into returning the raw Qlik string as "valid DAX", which blocked v2 from translating string-concat dimension expressions.

## Bar / combo chart subtype resolution

`_resolve_bar_combo_type(ctype, props, default)` picks the PBI visualType for `barchart` AND `combochart` AND `mekko` from Qlik's `barGrouping.grouping` (`grouped` | `stacked`) and `orientation` (`vertical`=columns | `horizontal`=bars). Combochart uses the SAME matrix as barchart — a Qlik combochart styled as grouped/horizontal renders as `clusteredBarChart`, stacked/vertical as `stackedColumnChart`, etc. (the line+column combo PBI types were dropped: real Qlik combocharts are typically styled bar/column groupings without an explicit line series, and the user-set presentation should drive the PBI type):

| Qlik (bar OR combo) | PBI visualType |
|---|---|
| horizontal + grouped | `clusteredBarChart` |
| vertical + grouped | `clusteredColumnChart` |
| horizontal + stacked | `stackedBarChart` |
| vertical + stacked | `stackedColumnChart` |
| mekko | `stackedColumnChart` (closest single-visual) |

`_resolve_pie_type(props, default)` switches `piechart` → `donutChart` when Qlik's `donut.showAsDonut` flag is set (the donut/pie presentation toggle).

Called in `_build_visual` right after the static `VISUAL_TYPE_MAP` lookup. The stacked / column / donut types reuse the default slot families in `_query_state_for_type`, and are included in the legend / data-label / reference-line type lists.

## Slicer default mode

`_build_slicer` emits **only** `objects.data[].properties.mode = 'Dropdown'` so each PBI slicer renders as a dropdown by default — a compact selector that mirrors Qlik's listbox / filterpane UX better than PBI's default long checkbox list.

**Important:** we deliberately do NOT emit `objects.general.orientation`. The earlier version set `orientation = 0` (horizontal pill) or `1` (vertical list) alongside `data.mode = Dropdown`. PBI Desktop applies orientation BEFORE the data.mode hint, so the dropdown setting was effectively ignored and the user saw a vertical checkbox list / pill row instead. Omitting orientation entirely lets dropdown win on first load.

## Visual type resolution (`visualization` over qType, Table default)

`_build_visual` resolves the PBI type in this order:

1. **Master-object viz** — when the cell `qExtendsId`s a master object, the master's `visualization` wins.
2. **Explicit `visualization` beats the cell qType** — the `visualization` property is the authoritative renderer choice and can DIFFER from the cell's `qType`. The key case: a Qlik **straight table** reports `qType = "pivot-table"` but `visualization = "sn-table"`. We prefer `visualization` when it's present, differs from the qType, and is recognised (in the map) — or when the qType is `auto-chart`. Without this a straight table rendered as a `pivotTable` instead of a flat `tableEx`.
3. **`VISUAL_TYPE_MAP`** lookup (`visual_rules.json`).
4. **Table as the default** — when no PBI visual maps to the Qlik type AND the cell carries a data hypercube (`_has_hypercube_data`: dimensions/measures, incl. `gaLayers`), it renders as `tableEx` so an unsupported chart (sankey/radar/mekko variants, custom extensions, …) still shows its figures. Only a data-less cell degrades to a textbox stub.
5. **Zero-dimension `tableEx` → `cardVisual`** (card-not-table rule). After the type resolves, a cell heading for `tableEx` that has **0 dimensions and ≥1 measure** (`_hypercube_counts`) is re-routed to `cardVisual` — a one-row strip of measure values is semantically a card, not a grid. This catches Qlik **KPI / multi-KPI** objects, custom KPI extensions, and KPIs wrapped in an `auto-chart` (which maps to `tableEx`) that previously rendered as a one-row table. `cardVisual` handles one OR many measures (multi-card). An author's **explicit** table (`_EXPLICIT_TABLE_CTYPES`: `table`/`sn-table`/`straight-table`/`grid-chart`/…) is exempt — it stays `tableEx` even with no dimensions. Real `kpi`/`sn-kpi` types map straight to `cardVisual` and never reach this rule. Bed: `regression/card_mapping.py`.

Straight/flat tables (`table`, `sn-table`, `straight-table`) → `tableEx` (everything in the Values well); only `pivot-table` / `sn-pivot-table` → `pivotTable` (Rows/Columns/Values nesting). Dashboard objects: text (`text-image`, `sn-text`, `text`) and images (`sn-image`, `image`) → `_build_textbox_visual`; buttons (`action-button`, `sn-action-button`, `button`) → `_build_action_button` (label + font/fill styling + `goToSheet`→`PageNavigation` action). New chart aliases: `funnel*`→`funnel`, `waterfall*`→`waterfallChart`, `grid-chart`/`sn-grid-chart`→`tableEx`.

## Default font family (Arial)

`_extract_visual_style` ends with `out.setdefault("fontFamily", "Arial")` — Qlik apps that lean on the app theme often carry no per-visual font, and PBI would otherwise fall back to Segoe UI. Arial is applied to titles (`_title_properties`), data labels / axes, and KPI card values; action-button labels default to Arial too. An explicit Qlik font (theme/title `fontFamily`) is always kept.

## Axis + data-label styling from Qlik

`_extract_visual_style` now reads Qlik's per-axis show state from `dimensionAxis` (category axis) and `measureAxis` (value axis):

| Qlik `show` | PBI `<axis>.show` | PBI `<axis>.showAxisTitle` |
|---|---|---|
| `"all"` | `true` | `true` |
| `"labels"` | `true` | `false` |
| `"none"` | `false` | `false` |
| missing | unset (PBI default) | unset (PBI default) |

`_build_chart` emits `objects.categoryAxis` / `objects.valueAxis` blocks with the resolved `show` + `showAxisTitle` properties for all axis-bearing visual types (`lineChart`, `clusteredBarChart`, `clusteredColumnChart`, stacked variants, `columnChart`, `scatterChart`, the combo types, `waterfallChart`).

Data labels (`objects.labels.show`) come from Qlik's `dataPoint.showLabels`. The extractor was hardened: previously `bool(showLabels)` coerced `"auto"` / `"none"` / `None` to `True`, so it force-enabled labels on every visual Qlik hadn't explicitly set. Now only explicit booleans (`True`/`False`, or the string forms `"true"`/`"false"`) forward; `"auto"` and missing values leave PBI's default in place.

### Legend position, axis/label font family, fixed value-axis range

- **Legend position** — Qlik `legend.dock` (`top`/`bottom`/`left`/`right`, or the older `near`/`far`) → PBI `objects.legend.position` (`Top`/`Bottom`/`Left`/`Right`). Emitted alongside `show`.
- **Font family propagation** — the visual's captured `fontFamily` (from the theme/title block) is also stamped onto `objects.labels` (data labels) and `objects.categoryAxis` / `objects.valueAxis` so the whole chart uses the Qlik font consistently. Only font family is shared (unambiguous and safe); per-element size/colour is left to PBI to avoid mixing the title's styling into the data elements.
- **Fixed value-axis range** — when Qlik's `measureAxis.autoMinMax` is explicitly `false`, its `min`/`max` become `objects.valueAxis.start` / `.end`. Auto min/max (the common case) is left to PBI's auto-fit.

## Sort order (`_sort_direction_from_qlik` + `qInterColumnSortOrder`)

Qlik and PBI store sort intent in different places, and dimensions / measures differ AGAIN within Qlik:

- **Dimension** (`NxDimension`): sort lives in `qDef.qSortCriterias[]` (a **list** of `SortCriteria`), with `qDef.qReverseSort` flipping the direction. Dimensions have **no** `qSortBy`.
- **Measure** (`NxMeasure`): a single `SortCriteria` at the block-level `qSortBy` (sibling of `qDef`).
- Each `SortCriteria` carries tri-state flags (`-1` desc / `0` unset / `1` asc): we honour `qSortByNumeric` → `qSortByAscii` → `qSortByExpression`. `qSortByLoadOrder` / `qSortByFrequency` are deliberately NOT mapped (PBI has no equivalent) — they yield `None` and fall to the type-aware Auto fallback below.

**Key fact (verified against real app metadata):** Qlik BAKES fully-resolved, signed criteria into the metadata even when the UI shows "Auto". Real corpus: an Auto dimension serialises `{qSortByNumeric:1, qSortByAscii:1, qSortByLoadOrder:1}` (= **ascending**); an Auto measure serialises `{qSortByNumeric:-1, qSortByLoadOrder:1}` (= **descending by value**). So `_sort_direction_from_qlik` returns the correct direction directly for the vast majority of fields; only a load-order-only / empty criteria returns `None`.

`report._build_visual` collects every resolved column as `(qlik_col_index, table, name, is_measure, explicit_dir|None)`, then orders them by the hypercube's **`qInterColumnSortOrder`** (Qlik's column-priority array; dims are indices `0..D-1`, measures `D..D+M-1`). The **first** entry after ordering is the "topmost" field in Qlik's sorting section = the primary sort.

**"Auto" resolves BY FIELD TYPE on the topmost field.** When the primary field's direction is `None` (Auto with no directional flag), it defaults to:
- **measure → Descending** (Qlik auto-sorts a measure descending-by-value),
- **dimension → Ascending** (Qlik auto-sorts a dimension alphabetically/numerically ascending).

The field's **Qlik column role** drives this — `qlik_col_index >= num_dims` means measure — **NOT** the PBI `is_measure` flag, because a native-aggregation measure like `Sum(X)` binds as a PBI **Column** yet is a Qlik measure that must sort descending. **Lower-priority fields** emit only when they carry an EXPLICIT direction (a deliberate multi-level sort), so naturally-ordered secondary columns aren't force-sorted.

Emitted as a single `visual.query.sortDefinition` block (`sort[]` of `{field:{Column|Measure|Aggregation:{…}}, direction}`, `isDefaultSort:true`) — PBI's schema rejects per-projection `sortDirection`.

**The sort field MUST mirror the projection's field shape.** A `Sum(X)` native aggregation binds as a Column-with-`Aggregation` projection (`{Aggregation:{Expression:{Column:…}, Function:0}}`), NOT a `Measure`. The sort entry has to use the **same `Aggregation` wrapper** — emitting a bare `{Column:{Property:X}}` there leaves PBI unable to match the sort to the projection, so it **silently drops the sort and falls back to alphabetical category order** (the "Sales by Product sorted A→Z instead of by value" bug). `_build_visual` checks `self._column_aggregations` and wraps native-agg sort fields accordingly; real DAX measures still emit `Measure`, explicit dimension sorts still emit a plain `Column`.

**Chronological sort of month/date text labels.** A Qlik month dimension is materialised as a TEXT calc column (`FORMAT('Dim_Date'[Date], "MMM yyyy")`), which PBI sorts alphabetically (Apr, Aug, Dec…). A TMDL `sortByColumn` pointing at the raw `Date` fails PBI's 1:1 rule (many dates → one month label). Fix (`_resolve_dimension_expr` + `_derive_chrono_sort_key`): for a month-grain `FORMAT(<dateref>, "…M…")` label (month token, no day token), synthesise a **hidden int64 sort-key column** at the label's granularity (`YEAR(<ref>)*100 + MONTH(<ref>)`, 1:1 with the label) and emit `sortByColumn: '<key>'` on the label column (`model.py` renders `isHidden` + `sortByColumn` for calc columns). Limitation: only month-within-year / year-less-month `FORMAT(<dateref>,…)` labels are covered; quarter/week/concatenated labels still sort alphabetically.

**History:** (1) the original code read only a singular `qSortBy` (which dimensions don't have) → dimension sort dropped + `qInterColumnSortOrder` ignored. (2) Directional-only handling emitted nothing for "Auto" → most visuals didn't sort. (3) A blanket "Auto → Descending" then mis-sorted Auto dimensions (which should be Ascending). The current type-aware-by-Qlik-column-role rule is correct: verified on a real app — 71 measure-sorts Descending, 34 dimension-sorts Ascending, 2 explicit Column-Descending.

## Action button text + styling

`_build_action_button` reads the button's visible TEXT from any of `style.label` / `style.text` / `props.label` / `props.text` / `props.title` (each may be a `qStringExpression` → `clean_label` unwraps it). Styling is normalised across BOTH shapes Qlik uses: the modern `sn-action-button` nests it in objects (`style.font = {color, size, fontFamily, style:{bold,italic,underline}}`, `style.background = {color}`); older apps use flat fields (`color`, `fontSize`, `font`, `fontWeight`, `backgroundColor`). Emitted to `objects.text[].properties` (`text`, `fontColor`, `textSize`, `fontFamily`, `bold`/`italic`/`underline`, `horizontalAlignment`) and `objects.fill` (button background). Font family defaults to Arial when Qlik named none. (The earlier code read only flat fields and treated `style.font` as a string — when it was an object the family and Arial default were both dropped.)

**Navigation** (`_resolve_button_navigation`): `goToSheet` / `goToSheetById` → PBI `PageNavigation` action targeting the resolved page. **`nextSheet` / `prevSheet`** resolve against `_sheet_order` anchored on `self._current_sheet_id` (set per page in `_build_page`) → the adjacent sheet's page; previously these were left inert because the source-sheet context wasn't threaded through.

## KPI / cardVisual: force measure synthesis

`cardVisual` (KPI) and `gauge` bind a single scalar and REQUIRE a real Measure in their data slot — a Column binding (even with a native aggregation) makes the card render empty / error. `_resolve_measure(meas_block, force_measure=True)` (passed from `_build_chart` when `pbi_type in ("cardVisual","gauge")`) promotes the native-aggregation fast path into a synthesised DAX measure (`SUM`/`COUNT`/… `('T'[col])`) instead of returning a column projection. `_build_chart`'s measure loop sets `force_meas` accordingly.

## Visual coverage map

| Qlik cell type | PBI visualType |
|---|---|
| `auto-chart` (resolved via `properties.visualization`) | family below |
| `linechart` | `lineChart` |
| `barchart` | `clusteredBarChart` |
| `combochart` | `lineClusteredColumnComboChart` |
| `piechart` | `pieChart` |
| `scatterplot` | `scatterChart` |
| `treemap` | `treemap` |
| `kpi` / `sn-kpi` | `cardVisual` |
| `table` | `tableEx` |
| `pivot-table` / `sn-pivot-table` | `pivotTable` |
| `filterpane` / `listbox` | `slicer` |
| `histogram` | `columnChart` |
| `waterfallchart` | `waterfallChart` |
| `gauge` | `gauge` |
| `map` | `azureMap` |
| `text-image` / `sn-nl-insights` | `textbox` |
| `action-button` | `actionButton` |
| `container` / `sn-layout-container` / `sn-tabbed-container` | expanded into child visuals (see below) |
| anything else | `textbox` placeholder |

Full map lives in `config.VISUAL_TYPE_MAP`, loaded from `visual_rules.json` (see below).

## Container expansion (`report._build_container`)

A Qlik container HOLDS other visuals; PBI has no container/tab object, so we expand a container into the visuals it contains rather than dropping them as one placeholder (the old behaviour, which silently lost every chart/filter inside). Two shapes:

- **Layout container** (`sn-layout-container`, free-layout, all children visible): the container props carry `objects[]` = `{childRefId, label, bounds{x,y,width,height} in PERCENT of the container}`; the child definitions are the cell's `child_children` (each with a matching `childRefId`). `_resolve_layout_children` pairs them in author order (children with no `objects[]` entry are kept at full size, never dropped). `_rebase_child_bounds` projects each child's percent-of-container rect onto the container's own percent-of-sheet rect, so the existing `_scale()` maps it to the page exactly like a top-level cell.
- **Tabbed container** (`container` / `sn-tabbed-container`, one tab visible at a time): props carry `children[]` = `{refId(=child qId), label, isMaster}`; the definition is a master object (`master_by_id`) or an inlined child. Tabs carry no bounds, so `_grid_tiles(n)` tiles them in a near-square grid — every tab survives instead of only the active one.

Each child's `qProperty` is a COMPLETE visual definition (qType, hypercube, `components` styling) — identical to a sheet cell — so `_build_container` synthesises a cell dict per child and recurses through `_build_visual`, inheriting every chart/KPI/table/slicer/text builder, master-object resolution, and styling extraction for free. The parser exposes `cell["child_nodes"]` (full `{qProperty,qChildren}` subtrees) alongside the flat `child_children`, so a **filterpane inside a container** is handed its listboxes and expands into real slicers, and a **nested container** expands further. `_MAX_CONTAINER_DEPTH=4` guards a cyclic graph; an empty/unresolvable container still degrades to the historical grey placeholder so the slot is preserved. Verified live: Asset Management's 4 layout containers → 5 slicers + 4 textboxes (was 1 grey box each); Hospital's tabbed container → 2 tiled line charts.

## cardVisual chrome

PBI's modern KPI card validator rejects a `cardVisual` that doesn't declare the value/label/outline/divider/fillCustom bags. We emit minimal versions of each so Desktop's validator passes. Don't strip these "looks like dead code" — they're required for validation.

## azureMap viewport

Maps emit a `mapSettings` + `controls` object bag with a default `UnitedStates` view, custom zoom 5, center 39.5/-104.99, and `autoZoom: false`. The two-bag form (mapSettings AND controls) is required because the visual's runtime auto-fits to the data extent unless `controls.autoZoom: false` is also set.

## Map visuals (azureMap field-well population)

**Qlik map cells carry their lat/long/size bindings in `gaLayers[].locationOrLatitude` / `.locationLongitude` / `.size.expression` — NOT in the visual-level `qHyperCubeDef`.** This trips up anyone treating map cells like other charts.

`_collect_map_fields(props)` walks every layer:

- `_field_from_ga_ref` resolves location blocks (supporting both bare field names and `=expr` prefixes).
- Size measure uses `_resolve_measure` so it benefits from the native-aggregation fast path.

**`size.expression` may be a `libraryItem` reference, not an inline expression** — `{"label": "# of Patients", "key": "<masterMeasureId>", "type": "libraryItem"}`. When `type == "libraryItem"`, resolve `key` via `measure_by_id` (pass it as `qLibraryId` to `_resolve_measure`), NOT as the expression. The old code passed the id (`key`) as an inline expression, so the translator treated the random id as a field and emitted a dangling `'Table'[<id>]` column-ref measure named `Measure <id>` — and because several maps referenced the same library measure, they deduped into `Measure <id> (2)` / `(3)`, all erroring with *"Column '<id>' in table '…' cannot be found."* A general safety net in `_resolve_measure` also catches this: a bare inline expression that exactly matches a `measure_by_id` key binds to that measure instead of dangling (master-measure ids are random tokens, so a real expression never collides).
- Per-layer `qHyperCubeDef.qDimensions` provide category / series.

**Size-slot priority across layers**: an explicit `size.expression` (a point layer's bubble-size binding) wins over any layer's hypercube measure, regardless of layer order (`size_is_explicit` flag). The old "first layer to set `size_proj` wins" let a choropleth layer that happened to come first claim the Size slot, dropping the point layer's real bubble-size binding.

The result populates PBI's azureMap `Latitude` / `Longitude` / `Size` / `Category` / `Series` slots.

## Colour normalisation (`_normalize_hex`)

Every Qlik colour routes through `_normalize_hex`: 3-/4-digit shorthand expands (`#abc` → `#aabbcc`, `#fabc` → `#ffaabbcc`), `#RRGGBB` / `#AARRGGBB` pass through, and anything non-hex is rejected (returns None → keep the theme default). PBI renders a malformed colour literal as black, so dropping it is safer than passing it. The old code passed a `#abc` shorthand through unchanged.

## Bookmark landing page

`writer.write` maps each Qlik bookmark to its OWN sheet's page: pages carry their originating `sheet_id`, the engine-captured bookmark carries `sheetId`, so `explorationState.activeSection` = that page (falling back to page 1 only when the sheet isn't found). The old code anchored every bookmark to page 1. (Selection state itself is still not encoded — bookmarks remain name+page scaffolds.)

## Conditional-visibility notes surfaced

`_extract_show_condition` collects Qlik `qCalcCondition` / `showCondition` expressions into `report._visibility_notes` (they can't be wired as PBI filters without a real measure ref). The converter now emits each as an `info` issue in `conversion_report.md` so the user knows a visibility condition existed and was dropped — previously the notes were collected and silently discarded.

## sn-nav-menu → PBI pageNavigator

Qlik's `sn-nav-menu` master object becomes PBI's built-in `pageNavigator` visual. PBI auto-populates one button per page in the report; we supply orientation from Qlik's `layoutOptions.orientation` (with cell-aspect fallback) and pass through `general.bgColor` / `theme.highlightColor` to fill + border.

**Bug fix to remember**: the master-object → visualization resolution lives at `master_by_id[mref].get("visualization")`. Earlier versions stored the wrapper instead of `qProperty` so this key was always empty. If you see nav menus rendering blank or as placeholders, check this first.

## Filterpane orientation / slicer layout

`_build_filterpane_slicers` reads `props.layoutOptions.orientation` (Qlik's explicit `"horizontal"` / `"vertical"`) with cell-aspect fallback (`w/h >= 2` → horizontal). Children are then tiled along that axis (horizontal = width-shares side-by-side, vertical = height-shares stacked).

`_build_slicer` accepts the orientation flag and sets PBI's `general.orientation` to `"0"` (horizontal pill row) or `"1"` (vertical list).

Standalone listboxes (not inside a filterpane) get their orientation inferred from their own cell bounds.

## Visual JSON shape gotchas

PBI's visual schema rejected several plausible-but-wrong shapes during bring-up. The valid ones now in use:

- **Projections live under `visual.query.queryState.<slot>.projections`**, NOT `visual.projections`. Empty slots are omitted entirely (the validator rejects `{projections: []}`).
- **Per-visual-type slot names** come from `_query_state_for_type`: pieChart uses `Category` + `Y`; treemap uses `Category` + `Values`; cardVisual uses `Data`; pivotTable uses `Rows` / `Columns` / `Values`; scatter uses `X` / `Y` / `Details`; combo uses `Y` + `Y2`; default is `Category` + `Y`.
- **`partition <Name> = m`** is the correct TMDL line — NOT `partition Partition source = m`. The `source =` is an indented child property, not a same-line modifier.
- **Visual containers under `visualContainerObjects`** (background, border) — putting those under `objects` is silently ignored.
- **Sort goes under `visual.query.sortDefinition`**, NOT on each projection. We previously emitted `proj["sortDirection"]` per projection; PBI's schema rejects that with `"An additional property 'sortDirection' was included in the /visual/query/queryState/<slot>/projections/<n> property"`. Canonical form:
  ```json
  "query": {
    "queryState": {...},
    "sortDefinition": {
      "sort": [{"field": {"Column"|"Measure": {...}}, "direction": "Ascending"|"Descending"}],
      "isDefaultSort": true
    }
  }
  ```
  Per-visual sort specs are collected in `self._current_sort_specs` (fresh list per `_build_chart` call) and emitted only for projections that made it into the query state.

## Visual rules JSON (`visual_rules.json`)

The Qlik-type → PBI-type lookup table lives in `qlik_to_pbi/visual_rules.json` instead of being hard-coded in `config.py`. Users can extend coverage for custom / uncommon Qlik visuals without editing Python. `_load_visual_type_map()` reads it once at import time and normalises keys to lower-case.

## Script-derived per-table field rename map

`script_parser.parse_field_renames(script)` walks every `[Table]: LOAD ... RESIDENT|FROM ...;` block plus `RENAME TABLE [old] to [new];` and returns `{final_table: {engine_field: original_field}}`.

Two consumers:

1. **Friendly CSV / TMDL column names.** When the script renamed `[From_HCP_ID]` → `[From_HCP_ID-HCP_ID]`, we still write the column to the CSV under the original `From_HCP_ID` and the TMDL column carries that same friendly name. The cube still queries the engine field, but the user-visible name matches the Qlik original. Ambiguous reverts (two engine fields collapsing to the same original, e.g. both `HCO.City` and `HCP.City` would revert to `City`) are detected and skipped to avoid phantom shared-name relationships.

2. **Join-key translation.** `model._extract_relationships_from_engine` uses the rename map to translate `qk` key records into per-table relationship endpoints. So a join keyed on the engine field `From_HCP_ID-HCP_ID` resolves to `Referral Edge[From_HCP_ID]` ↔ `HCP[HCP_ID]` — not the synthetic shared name.

## Script-derived partitions (`script_to_m.py`)

`ScriptTranslator(variables=...).parse_blocks(script)` walks the script's LOAD statements and emits ready-to-use Power Query M expressions per table. Source-type detection covers:

| Qlik source | M emission |
|---|---|
| `.csv` | `Csv.Document(File.Contents(...), [Delimiter=..., Encoding=..., QuoteStyle=...])` + `PromoteHeaders` |
| `.xlsx` / `.xls` | `Excel.Workbook(File.Contents(...))` + `Source{[Name=<sheet>]}[Data]` + `PromoteHeaders` |
| `.json` | `Json.Document(File.Contents(...))` + `Table.FromList(...)` |
| `.xml` | `Xml.Tables(File.Contents(...))` |
| ODBC / OLEDB | `Odbc.DataSource(...)` / `OleDb.DataSource(...)` |
| `.parquet` | `Parquet.Document(File.Contents(...))` |
| `.qvd` | placeholder + note (PBI's QVD connector is preview-only) |

Resident / inline / unknown sources are skipped and fall back to the empty stub.

`model._script_partition_m(table)` picks the matching block by table name (case-insensitive) and emits its M expression in TMDL. The partition cascade priority: **live DB credentials → CSV match → script-derived M → empty stub**.

## What-If parameters from Qlik variables

`model._build_what_if_parameters` materialises a numeric Qlik variable as a PBI What-If parameter (synthetic table + measure).

**The default build does NOT call it** — the previous auto-synthesis over-generated, turning every internal counter variable (`index = 0`, `matches = 0`, etc.) into a phantom table. Qlik's loadmodel doesn't tag user-facing variables vs. internal ones, so without a reliable signal we err on the side of not creating parameters the source dashboard never exposed.

The function is still defined for callers who want to opt in. Users who want a What-If parameter create one via PBI Desktop > Modeling > New Parameter; the TMDL shape is identical to what the function emits, so the manual workflow is the same.

**Do not "re-enable" this without first solving the user-facing vs. internal variable classification problem.** Deleting the function as dead code is also wrong — it's the canonical reference for the synthetic-table TMDL shape.

When invoked, it emits:
- A synthetic table `<Var> Parameter` with one column matching the variable name.
- A partition using `List.Numbers(min, count, step)` → a one-column table.
- A measure `<Var> Value` = `SELECTEDVALUE('<Var> Parameter'[<Var>], <default>)`.
- `annotation PBI_Parameter = True` on the table so PBI Desktop's Modeling > Parameters pane recognises it.

## Engine schema sidecar (`engine-schema.json`)

The engine-current data model is the **authoritative** source of truth for table layout, field-to-table mapping, and key-based relationships. When `--fetch-via-engine` runs, `engine_fetch._refresh_field_lists_from_engine` calls `GetTablesAndKeys(qSyntheticMode=false)` and writes the result to `objects/engine-schema.json`:

```json
{
  "tables": {"<name>": {"fields": [{"name", "key_type", "tags",
                                    "is_hidden", "is_system", ...}],
                        "row_count": <int>}},
  "keys":   [{"key_fields": [...], "tables": [...]}, ...],
  "field_renames": {"<table>": {"<engine_field>": "<original_field>"}}
}
```

- `parser.py` reads this into `ir['engine_schema']`.
- `model._build_from_engine_schema` consumes it in preference to the loadmodel. Tables / fields come directly from the engine, system tables are filtered, relationships are seeded from `qk` records.
- `loadmodel---loadmodel.json` is the fallback for non-engine paths (`--data-dir` alone, qvf-direct, ...).

The sidecar also bundles `field_renames`, the per-table `{engine_field: original_field}` map recovered from the script's autogenerated section.

## Engine field tags drive column types

Engine schema fields carry a `tags` array with semantic markers the Qlik engine attached during the load script: `$integer`, `$numeric`, `$date`, `$timestamp`, `$text`, `$ascii`, `$key`, `$hidden`. `csv_schema.type_from_qlik_tags(tags)` maps these to a PBI type descriptor:

| Tag (strictest first) | TMDL `dataType` | M `type` | `formatString` |
|---|---|---|---|
| `$timestamp` | `dateTime` | `type datetime` | `yyyy-MM-dd HH:mm:ss` |
| `$date` (no `$timestamp`) | `dateTime` | `type date` | `yyyy-MM-dd` |
| `$integer` | `int64` | `Int64.Type` | — |
| `$numeric` (no `$integer`) | `double` | `type number` | — |
| `$text` / `$ascii` | `string` | `type text` | — |
| (no relevant tag) | `None` → caller falls back | — | — |

`model._build_from_engine_schema` plumbs each field's tags into `_columns_for_table(..., field_tags=...)`. Per-column type priority:

1. **Engine tag** (when present) — authoritative, survives CSV misalignment and absent data files.
2. **CSV sniffer** (when CSV is present) — best-effort fallback.
3. **All-string stub** — when nothing else available.

Why this matters: the CSV sniffer reads cells from the data file to guess types. If the data file is missing, all columns become text. If the file is present but headers misalign with data rows, the sniffer detects wrong types per column. Engine tags bypass both failure modes — the model has correct types even when no CSV exists.
