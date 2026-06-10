# DAX translator architecture

## Two-stage architecture

`translate_qlik_to_dax(expr, table_name, variable_lookup, measure_lookup, field_resolver)` is the public entry. Order matters:

1. **Legacy regex translator** (`dax_translator.py`) — corpus-calibrated, narrow scope: bare aggregations, simple single-key set analysis, `$(varName)` recursive inline (depth 6), bare arithmetic with field refs wrapped in `SUM(...)`, literals passed through.
2. **v2 tokenizer + recursive-descent** (`dax_translator_v2.py`) — runs ONLY when legacy emits a stub. Handles multi-key set analysis (incl. multi-value `IN`, `<>` negation, `{"*"}` wildcard, `distinct`/`total` *after* the set block, and `>a <b` range/date search strings), `If` chains, date / string / numeric functions, `Alt` / `Coalesce`, `Pick`, `Match`, `dual`. v2 success replaces the stub; v2 failure leaves the legacy stub in place.

Both stages strip Qlik comments via `_strip_comments` (`//` line + `/* */` block, quote-aware) **before** the leading `=` is removed — a body may lead with a `//` line comment whose newline precedes the `=` that introduces the expression, and if the `=` were stripped first the comment would leave the `=` stranded in the DAX body. A `//` runs to end of line; the newline is preserved so the expression resumes on the next line. Comments are stripped again after `_expand_variables` (matches `$(var)` AND `$(=var)`) to catch comments inside expanded variable bodies, and from each variable body *before* the expansion wrapper's parens are added so a trailing `//` can't swallow the closing paren. The same comment strip runs in `report._extract_first_field` (home-table detection) and `model._is_materialisation_candidate` (the `startswith("=")` gate) so a leading comment can't misdirect home-table selection or block a variable from materialising.

### Variable materialisation (`model._materialize_variables_as_measures`)

Runs **before** `_build_measures`. For each Qlik variable defined as an expression (`=Yearstart(Max(Date))`, `=Sum({<...>} Admitted) / Sum(...)`, ...) that's safe to materialise (skip plain config values, `$1`/`$2` parameterised macros, pure set-analysis fragments, and **search/range-string macros** — see below), the body is translated to DAX and added to `self.measures` keyed under the variable's name. The mapping is recorded in `self.materialized_vars` (Qlik name -> DAX measure name).

**Search/range-string variables are NOT materialised** (`model._is_search_string_body`, 2026-06). A variable like `vD_YTD = ='>=$(=MakeDate($(vD_InputYear),1,1))<=$(=MonthEnd(...))'` is a Qlik TEXT MACRO meant to be substituted verbatim inside a set modifier (`Date={"$(vD_YTD)"}`), not a value. Materialising it made `$(vD_YTD)` resolve to a measure ref `[vD_YTD]` and the filter come out as `'Facts'[Date] = "([vD_YTD])"` — a dateTime column compared to TEXT, which **errors at query time and breaks every visual using it** (the FinSight financial app's P&L / cash-flow / liquidity KPIs). Excluded from materialisation, the variable stays a macro and `_var_lookup` hands its raw body to the translator, which expands it inline (next paragraph) so v2's range-search path emits a real `Date >= DATE(...) && Date <= EOMONTH(...)` filter. Detection: after stripping a leading `=` and one layer of surrounding quotes, the body starts with `<`/`>`.

After materialisation, the variable lookup hooks in `model._build_measures` and `report._var_lookup` return `[varName]` (a bare DAX measure ref) instead of the raw Qlik body. So a master measure `$(fHRRPReadmissionRateCHFYTD)` becomes a single `[fHRRPReadmissionRateCHFYTD]` reference rather than re-inlining the variable body in every consumer measure.

Topological sort via `$(other)` references ensures a variable that depends on other variables sees them already materialised first (e.g. `vMinDate` becomes a measure before `fHRRPReadmissionRateCHFYTD` references it).

### measure_lookup resolver hook

Both the legacy `_qualify_field` / `_rewrite_field_refs_with_sum` / `_rewrite_bare_identifiers` and the v2 `ExpressionTranslator.resolve` accept an optional `measure_lookup(name) -> Optional[str]`. When the lookup recognises a name as an existing DAX measure (materialised variable or library measure), the resolver emits the bare `[Name]` form instead of `'Table'[Name]`. Without this, a variable-materialised reference would be re-qualified as a column ref and produce broken DAX (`'PE Hospital'[fHRRPReadmissionRateCHFYTD]`).

`report._measure_lookup` (the report-side exposure) **excludes any name that is also a real column name** (case-insensitive). Measures are often auto-named after fields, so without this exclusion a bare/bracketed reference to a column whose name coincides with a measure would be mis-emitted as a measure ref `[Name]` instead of `'Table'[col]`. Measure-vs-column dedup normally prevents the overlap, but the exclusion is a defensive guarantee: when a name resolves to both, the column wins.

### field_resolver hook (table→column mapping)

`translate_qlik_to_dax` takes an optional `field_resolver(field_name) -> Optional[str]` that returns the fully-qualified DAX ref of the column on the field's **owning** table (e.g. `"Region"` → `"'DimGeography'[Region]"`), or `None` for unknown fields. It threads through the same resolution points as `measure_lookup` (`_qualify_field`, the v2 `resolve`, and `_translate_set_analysis`). Resolution order in `_qualify_field`: measure ref `[Name]` → `field_resolver(name)` → fallback `'table_name'[name]` (the measure's home table).

`model._make_field_resolver()` builds it from `self.field_table` (Qlik field → owning table, seeded with both raw and sanitised keys), returning `'<owner>'[<_sanitize_column_name(field)>]`. It's passed at every call site (`model._build_measures`, `model._materialize_variables_as_measures`, and the inline-measure / calc-column paths in `report.py`).

**Bare variable references.** Qlik also permits a bare `varName` (not just `$(varName)`) inside an expression. When `field_resolver` can't resolve a name as a column it consults `model._resolve_bare_variable(name)`: a materialised variable → its measure ref `[var]`; a plain scalar config variable (`var_lat_offset = 0`) → the inline literal (`0`); an `=expression` body → `None` (left for the normal `$(var)` path, not inlined raw). Without this, a bare variable fell through to a bogus `'home'[varName]` column that fails at refresh with *"Column 'varName' … cannot be found"* — e.g. `=min(latitude)+(1+var_lat_offset)*…` shipped `'ZipData'[var_lat_offset]`.

**Why it matters:** before this, every field in an expression was pinned to the measure's single home table. A measure like `Sum([Sales]) / Sum([Budget])` whose operands live on different tables emitted `'Fact'[Budget]` for a column that isn't on `Fact`, breaking the measure. Now each field binds to the table that actually holds it: `SUM('Fact'[Sales]) / SUM('BudgetFact'[Budget])`. Set-analysis filter fields resolve independently too — `Sum({<DimRegion={'EU'}>} Sales)` → `CALCULATE(SUM('Fact'[Sales]), 'DimGeo'[Region] = "EU")`.

**New function coverage goes into v2** via `SCALAR_1_MAP` / `SCALAR_N_MAP` / `AGG_MAP` — not legacy.

Full catalogue of what is and isn't translated lives in `feature-catalogue.md`.

### What the legacy regex pipeline translates

| Qlik pattern | DAX |
|---|---|
| `Sum(Field)` | `SUM('T'[Field])` |
| `Count(Field)` | `COUNTA('T'[Field])` |
| `Count(distinct Field)` | `DISTINCTCOUNT('T'[Field])` |
| `Avg(Field/16)` | `AVERAGEX('T', 'T'[Field]/16)` (iterator) |
| `$(varName)` | inlined recursively (depth 6) |
| `Sum({<F={'v'}, G={1}>} X)` | `CALCULATE(SUM('T'[X]), 'T'[F]="v", 'T'[G]=1)` (per-field boolean filters, each field resolved to its owning table) |
| Bare arithmetic with field refs | each field wrapped in `SUM(...)` |
| Numeric / string literals | passed through |

**Legacy set analysis handles a LONE aggregation only.** `_translate_set_analysis` emits exactly one `CALCULATE(<agg>(<field>), <per-field boolean filters>)` — the measured field and every filter field resolved to its owning table via `field_resolver`. The boolean-filter form (`CALCULATE(agg, 'T'[F]=v)`) replaced the older `FILTER(ALL('T'), ...)` wrapper: it matches Qlik's set semantics more closely (overrides only the named field's selection rather than wiping every filter on the table) and lets filters on different tables resolve correctly. It can faithfully represent only a single `Agg({<set>} field)` with nothing around it. It *bails to a stub* (→ v2) when the matched aggregation does not span the whole expression — a ratio of two set aggregations (`Sum({s1} A)/Sum({s2} B)`), a sum of set aggregations, trailing arithmetic (`Sum({s} A) * 100`), or a `DISTINCT`/`TOTAL` qualifier (which it can neither encode nor parse past). v2's recursive-descent parser walks the full arithmetic tree and emits **every** operand. Before this guard, a compound expression silently collapsed to just its first aggregation, dropping the denominator entirely — e.g. `Sum({<DateType={'AdmitDT'}>} Admitted)/Count(DISTINCT{<DateType={'AdmitDT'}>} PatientID)` produced only the numerator's `CALCULATE`. Do **not** "fix" the guard by teaching legacy to handle compound sets — that is v2's job by design.

**Legacy claims an expression only when its OUTERMOST call is an aggregation.** A Qlik wrapper like `Date(Max(Date), 'MM/DD/YYYY')` or `If(Sum(x)>0, ...)` contains an `Agg(` token, but the legacy simple-agg path would keep the outer `Date(` / `If(` as a literal DAX call and emit broken DAX — `DATE(<date>, "MM/DD/YYYY")` makes PBI report *"Too few arguments were passed to the DATE function. The minimum argument count for the function is 3."* So before taking the simple-agg branch we check `re.match(r"\s*([A-Za-z_]\w*)\s*\(", src)` and bail to v2 when that leading function name isn't in `_LEGACY_AGG_NAMES`. v2 strips Qlik's `Date`/`Num`/`Time`/`Timestamp`/`Interval` formatting wrappers to their inner expression (the format belongs on the measure's `formatString`, not the expression) and parses `If`/`Pick`/`Alt` properly.

### Qlik date pivots are SCALAR (`_DATE_PIVOT_SCALAR`)

Qlik's `Yearstart` / `Yearend` / `Monthstart` / `Monthend` / `Quarterstart` / `Quarterend` take a **scalar** date and return a scalar date (the first/last day of that date's period). DAX's `STARTOFYEAR` / `STARTOFMONTH` / … are **column-only time-intelligence** functions — passing a scalar (e.g. `EDATE(MAX('Date'[Date]), -12)`) errors *"The first argument to 'STARTOFYEAR' must specify a column."* So `_parse_function` resolves these via `_DATE_PIVOT_SCALAR` to the scalar form, NOT the time-intel mapping:

| Qlik | DAX |
|---|---|
| `Yearstart(x)` | `DATE(YEAR(x), 1, 1)` |
| `Yearend(x)` | `DATE(YEAR(x), 12, 31)` |
| `Monthstart(x)` | `DATE(YEAR(x), MONTH(x), 1)` |
| `Monthend(x)` | `EOMONTH(x, 0)` |
| `Quarterstart(x)` | `DATE(YEAR(x), (QUARTER(x)-1)*3+1, 1)` |
| `Quarterend(x)` | `EOMONTH(DATE(YEAR(x), QUARTER(x)*3, 1), 0)` |

Only the date arg is used; any extra Qlik args (`period_no` shift, `first_month_of_year`) are approximated away rather than crashing. So `vMinDateLY = Yearstart(Addmonths(Max(Date),-12))` → `DATE(YEAR(EDATE(MAX('Calendar Link Table'[Date]), -12)), 1, 1)`.

### Set-analysis range search (v2)

`_search_string_filter` / `_translate_bound` handle a set value that is a comparison search string, e.g. `Date={">a <b"}` (after variable expansion `Date={">(Yearstart(Max(Date))) <(Date(Max(Date),'MM/DD/YYYY'))"}`, or after materialisation `Date={">$(vMinDate) <$(=vMaxDate)"}` → bounds `[vMinDate]` / `[vMaxDate]`). `_split_search_bounds` splits the string on `>=|<=|<>|>|<`; each bound is translated through a sub-parser and emitted as `FILTER(ALL(field), field op bound && ...)`. Since the date pivots now emit scalar `DATE(...)` directly (above), the bounds are already scalar. `_scalarize_date_pivots` remains as a defensive fallback for any residual `STARTOFYEAR`-family token.

**Two upstream fixes make this path actually fire on real financial apps (2026-06):**
- **`$(=<expr>)` evaluation form** (`dax_translator._strip_dollar_eval`): Qlik's immediate-eval `$(=MakeDate($(vD_InputYear),1,1))` is rewritten to `(MakeDate($(vD_InputYear),1,1))` so the inner Qlik expression survives to the translator (the bare-identifier form `$(=var)` is left for the variable expander). Previously the surviving `$(` stubbed the whole measure. Combined with NOT paren-wrapping a search-string variable value on expansion (`_is_search_string_value` → substitute the unquoted body verbatim), a date-range macro expands to a clean `>=MakeDate(...)<=MonthEnd(...)` string `_split_search_bounds` can parse.
- **Binary-minus tokenisation** (`dax_translator_v2._tokenize`): the `NUM` pattern greedily ate a leading `-`, so `MakeDate(([vD_InputYear])-1,1,1)` (last-year bounds) lexed `-1` as a negative literal and the sub-parser raised, dropping the whole range back to the broken `Date = "text"` form. A `-`number directly after a VALUE token (`NUM`/`ID`/`STR`/`RPAR`/`RBKT`/`BRACKETID`) is now split into `OP(-)` + `NUM`, so `(expr)-1` subtracts. Verified on all 4 FinSight apps: 59/59 date-range measures emit valid range DAX (was 0; 59 erroring `Date = "text"`).

### Lexer note: bracketed field names

The v2 lexer emits a single `BRACKETID` token for `[Field With Spaces]` (regex alternative placed *before* `LBKT`). Previously the parser joined the inner word tokens with no separator, collapsing `[HRRP Condition]` → `HRRPCondition`. `_parse_primary` and `_parse_set_field_filter` consume `BRACKETID` and strip the brackets; the bare `LBKT`/`RBKT` path remains only as a fallback for malformed input.

### What we do NOT translate (falls back to stub)

- `AGGR(...)`, `Above`, `Below`, `Peek`, `RangeSum`, `RangeMin`, `RangeMax`, `Aggr`-driven calc patterns.
- `Class(...)`, `WildMatch`, `Replace`, `FirstSortedValue`; quarter-pivot range bounds.
- `Colormix1`, `RGB`, `White`, color expressions.
- Anything still containing `{<`, `>}`, `$(`, or any known-Qlik-only function name AFTER translation — `_looks_like_valid_dax` rejects these and the caller stubs them. **Brace rule**: it strips `IN {…}` table constructors (the only legit DAX brace we emit — multi-value set analysis / Match), then rejects ANY surviving `{`/`}`. This catches a Qlik set block the legacy path failed to translate — `{$}` (current selection), `{1}` (all-data), or a `{<…>}` remnant — which previously slipped through as garbage like `SUMX('T', {$} 'T'[X])` (balanced braces passed the old count check). Rejecting routes them to v2, which renders `{1}`→`CALCULATE(…, ALL())`, `{$}`→no added filter. `DATE` was removed from the forbidden-function list since it is valid DAX and v2 strips Qlik's `Date()` to a passthrough.

In the v2 set parser, `{$<…>}` (current-selection + modifier) is equivalent to `{<…>}` since `$` is Qlik's default set identifier, so it just consumes the `$` and lets the `<…>` modifier produce the filters; `{$}` alone → no filters (respect current context).

## Stub format

```
BLANK() /* qlik: <original expression, flattened to one line, up to 240 chars> */
```

Legal DAX (block comment). Loads cleanly in PBI Desktop and shows the user the original formula in the measure's expression view for hand rewrite.

## AGGX promotion + aggregation finalisation (`_finalize_dax`)

DAX `SUM` / `AVERAGE` / `MIN` / `MAX` / `DISTINCTCOUNT` only accept a **bare column reference**; they reject an expression in the body (*"The SUM function only accepts a column reference as an argument"* at query/refresh time). Two passes guard this, bundled into **`_finalize_dax(dax, table_name)`** which the public entry runs on the chosen result of BOTH stages (legacy and v2) before returning:

- **`_promote_agg_to_iterator`** rewrites `AGG(<expr>)` → the iterator form `AGGX('<table>', <expr>)` (`SUM`→`SUMX`, `AVERAGE`→`AVERAGEX`, `MIN`→`MINX`, `MAX`→`MAXX`, `COUNTA`→`COUNTAX`) when the body is anything other than a single column. `AGG(<col>)` is left alone (valid as-is; the iterator form would change the storage-engine optimisation envelope). The iterator table is the body's first `'Table'[Col]` ref (falls back to `table_name`) so a v2 set-analysis body that references the fact table iterates that table, not the measure's home. The legacy stage already calls this internally; re-running in `_finalize_dax` is idempotent and is what finally fixed the **v2 gap** — set-analysis output `CALCULATE(SUM(a * b), …)` never passed through the legacy promotion, so `Sum({set} Qty*Price)` shipped as invalid `CALCULATE(SUM('T'[Qty] * 'T'[Price]), …)`.
- **`_fix_count_of_if`** rewrites `COUNT`/`DISTINCTCOUNT(IF(cond, col[, else]))` → `CALCULATE(<count>(col), FILTER('table', cond))`. Qlik's `Count(distinct If(cond, Field))` (count Field on rows where cond) literally translated to the column-only-rejected `DISTINCTCOUNT(IF(...))`. Only the `IF`-with-a-bare-column-value shape is rewritten; any other non-column count arg is left untouched (still loads, just not auto-fixed).

`preflight._check_measure_aggregations` is the safety net: it flags any surviving `SUM`/`AVERAGE`/`DISTINCTCOUNT(<non-column>)` in a written measure / calc-column at convert time. Regression bed: `regression/agg_iterator.py`.

## Inline measure synthesis (anonymous chart measures)

Qlik's auto-charts and KPI cards carry their numbers as anonymous inline measures — `qHyperCubeDef.qMeasures[].qDef.qDef` holds a Qlik expression with no library binding. PBI cannot represent these as column references (a column ref cannot hold `Sum(X)`), so we **synthesise a real DAX measure into the model on demand**.

The synthesised measure:

- **Name** — `qLabel` if present, else label-expression text, else a synthetic name like `"Sum Patients_Diagnosed"` from `_derive_inline_measure_label`. Deduped against all existing measure AND column names with a `(2)` / `(3)` suffix.
- **Home table** — the table that *actually owns the first referenced field*. If `Sum(Patients_Diagnosed)` references a field that lives on `HCP`, the measure homes on `HCP`, NOT on the first table in the model. The bare-field qualifier in the translated DAX is rewritten to match (`'Data'[Patients_Diagnosed]` → `'HCP'[Patients_Diagnosed]`).
- **Expression** — the result of `translate_qlik_to_dax`, which is either valid DAX or the `BLANK()` stub.

`_extract_first_field` is the helper that decides the home table. It strips comments + set-analysis blocks, skips Qlik aggregation keywords (`sum`, `count`, `avg`, …), and collects field-name candidates (bracketed first, then bare). It takes an optional **`is_known(name) -> bool`**: when supplied (all three home-resolution call sites pass `lambda n: n in field_table or _sanitize_column_name(n) in field_table`), it returns the first candidate that is an ACTUAL known field, falling back to the first candidate only if none resolve. This stops a leading token that looks field-like but isn't a real column (a dotted `Table.Field` qualifier, or a token the home-table map doesn't carry) from homing the measure on the wrong table — the H3 fix.

Same first-field rule applies to library measures in `_build_measures` so e.g. `Sum([Sales Margin Amount])` doesn't end up as a phantom column on `tables[0]`. The library-measure resolver (`report._resolve_measure`) homes a `qLibraryId` hit on the measure's own table, falling back to **`tables[0]["name"]` (a real table), never the literal `"Data"`** which may not exist as a table and would point the projection's `Entity` at nothing — the H2 fix.

## Measure name sanitisation (`_sanitize_measure_name`)

Qlik routinely auto-labels measures with the literal expression — `Count(distinct [Field])`, `Sum([Margin]) / Sum([Revenue])`, `Avg(X) * 1.05`. Those characters (`(`, `)`, `[`, `]`, `*`, `/`, `%`, `&`, etc.) are **forbidden in DAX identifiers** per the DAX syntax reference. PBI silently rejects a measure whose NAME contains any of them, and every visual referencing that measure renders empty.

`_DAX_FORBIDDEN_RE = [.,;:/\\*|?&%$!+=()[]{}<>'"@#`~^]`. Strip these, collapse runs of whitespace, fall back to `"Measure"` if everything was stripped. **Hyphens are preserved** — valid in DAX and needed to round-trip compound IDs like `From_HCP_ID-HCP_ID`.

**Symbol prefixes are mapped to words BEFORE the strip**, otherwise the symbol is silently dropped and the name reads badly: `#` (Qlik's "number of" count idiom) → `# of X`/`#X` becomes `Number of X` (not the stranded `of X`); a percent-indicator `%` (`% Unemployed`, `Margin %`) → `Percent`. A digit-adjacent `%` (a format/value like `0%`, `100%`) is left for the strip so a label that embeds a format string doesn't gain a stray "Percent". Materialised-variable measures keep their variable name verbatim (`vMaxDate` stays `vMaxDate`) since clean names pass through unchanged.

Examples:
- `Count(distinct [From_HCP_ID-HCP_ID])` → `Count distinct From_HCP_ID-HCP_ID`
- `Sum([Margin]) / Sum([Revenue])` → `Sum Margin Sum Revenue`
- `Profit & Loss` → `Profit Loss`

Applied in:
- `model._build_measures` — library (master) measures from `measures.json`.
- `report._resolve_measure` — inline (per-chart) measures synthesised on the fly.

The visual JSON uses the sanitised name as its `Property` and `queryRef`, so the visual's measure binding resolves to the model's sanitised measure name automatically.

## Measure-vs-column name collisions

PBI rejects model load when a measure shares a name with ANY column on ANY table. Dedupe measures against the union of every column name across every table, plus all earlier measures — both for library measures (`SemanticModel._build_measures`) and for inline measures (`ReportBuilder._resolve_measure`). Suffix: `" (Measure)"` → `" (Measure 2)"` → ...

## Native column aggregation (no synthesised measure)

`_native_aggregation_projection(expr)` recognises simple `Sum(X)` / `Count(X)` / `Avg(X)` / `Min(X)` / `Max(X)` expressions and binds PBI's built-in column aggregation function directly instead of creating a DAX measure. The projection emitter wraps the field in `field.Aggregation.{Expression: {Column:...}, Function: <enum>}` and sets a friendly `displayName` like `"Sum of Patients_Count"`.

PBI's `IQueryAggregateFunction` enum: `Sum=0`, `Avg=1`, `Count=2`, `Min=3`, `Max=4`, `CountNonNull=5`, `StandardDeviation=6`, `Variance=7`, `Median=8`. **There is intentionally NO `CountDistinct` entry** — distinct count has no portable numeric slot in this enum across PBI versions (slot 5 is CountNonNull, not distinct). `Count(distinct X)` therefore returns `None` from `_native_aggregation_projection` and falls through to inline-measure synthesis, which produces an unambiguous DAX measure:

```dax
DISTINCTCOUNT('TableName'[ColumnName])
```

Composite expressions (multi-field, set analysis, ratios) still synthesise DAX measures via the existing path.
