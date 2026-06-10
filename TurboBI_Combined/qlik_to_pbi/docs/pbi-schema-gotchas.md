# PBI schema gotchas

PBI Desktop's TMDL + visual JSON schema is strict and reports validation failures only as "Cannot resolve all the paths..." with no per-file context. Schema rules that cost real time to find:

- **`visualContainerObjects`** only accepts: `background`, `border`, `padding`, `visualHeader`, `stylePreset`, `divider`, `outspacePane`, `title`, `general`, `shadow`, `lockAspect`. `visualTitle` is NOT valid here (we tried; Desktop rejects). Title text belongs under `visual.objects.title[]`.

- **Relationships dangle** when their `fromColumn` / `toColumn` references a column that no longer exists on the table — Desktop fails the entire project load. Always run `_prune_dangling_relationships` and fall back to `_infer_relationships_from_shared_fields` if pruning emptied the list.

- **Measure / column name collisions are case-insensitive within a table.** PBI rejects load when a measure shares a name with any column. Dedupe with `(Measure)` / `Sum of X` suffix.

- **A table named `Measures` is REJECTED** — it collides with MDX's special `[Measures]` dimension, so the AS model-schema validator aborts the *whole file* at load: `Unsupported Table name "Measures" has been found in data model schema` (`ModelSchemaValidationFailed`; the Feb-2025 PBI Desktop validation, also catches the leading-space `[ Measures]` variant). A Qlik app can legitimately have a table called "Measures", so `_sanitize_table_name` remaps reserved names (`model._RESERVED_TABLE_NAMES`, case-insensitive) → `"<name> Table"` (e.g. `Measures` → `Measures Table`). Because that function is the single deterministic chokepoint for every table-name production (table build, both relationship paths, csv map; report.py reads the sanitised `model.tables`), the rename propagates everywhere automatically. Backstop: `preflight._check_reserved_table_names` flags any reserved `table` header that slips through.

- **Measure names with DAX-forbidden characters silently fail** — `(`, `)`, `[`, `]`, `{`, `}`, `;`, `:`, `/`, `\`, `*`, `|`, `?`, `&`, `%`, `$`, `!`, `+`, `=`, `<`, `>`, `'`, `"`, `,`. Visuals referencing such a measure render empty with no error. Apply `_sanitize_measure_name` to ANY incoming label (Qlik default labels are often the raw expression `Count(distinct [Field])`). Hyphens are OK.

- **Empty `Table.FromRows({})` partitions need `type table [<col>=<type>]`** literal — never `Table.FromRecords({[col=null,...]})` which Desktop can't infer types from.

- **`partition <Name> = m`** is the correct TMDL form. `partition Partition source = m` does NOT work despite some online examples suggesting it.

- **`Entity` is the canonical key** inside `field.Column.Expression.SourceRef.Entity`. `Source` is reserved for query aliases and causes silent visual load failures.

- **`PBI_NavigationStepName` and `PBI_ResultType` go at TABLE scope** (one tab, sibling of `column` / `partition`), AFTER the partition body, with blank lines between. Inside the partition block is syntactically accepted but breaks PQ Editor round-trip and triggers "pending changes" warnings.

- **`Table.TransformColumnTypes`' optional 3rd arg is `culture`, not `MissingField`.** Passing `MissingField.UseNull` raises "The value for option 'Culture' is invalid." Use the 2-arg form; sniff the CSV ahead of time to ensure type-list names match `Table.PromoteHeaders` output.

- **Sort lives under `visual.query.sortDefinition`**, NOT on each projection. Per-projection `sortDirection` is rejected with "An additional property 'sortDirection' was included in /visual/query/queryState/…/projections/N".

- **`Aggregation.Function` has NO DistinctCount slot.** PBI's `IQueryAggregateFunction` enum: Sum=0, Avg=1, Count=2, Min=3, Max=4, CountNonNull=5, StandardDeviation=6, Variance=7, Median=8. Slot 5 is CountNonNull, NOT distinct. Route `Count(distinct X)` to DAX measure synthesis (`DISTINCTCOUNT(...)`).

- **Don't stamp `formatString: '#,##0'` on numeric columns by default.** When the M cast doesn't deliver the matching storage type, PBI renders the format pattern as the cell content. Only emit `formatString` on date / dateTime columns or What-If parameter slicer columns.

**Why:** Each of these caused at least one Desktop load error.

**How to apply:** When adding any new visual JSON or TMDL emit, validate via `preflight.run_preflight` before assuming it works. The pre-flight allowlist (`_ALLOWED_CONTAINER_KEYS`) is the canonical source — extend it only after confirming Desktop accepts the new key.
