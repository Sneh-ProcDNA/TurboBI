# Tableau-to-Power-BI Conversion Agent

A multi-stage agent that converts Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files. Two cooperating Python packages:

- **`tableau_to_pbi/`** — the deterministic core. XML → IR → TMDL/visual.json → PBIP folder.
- **`tableau_to_pbi_agent/`** — thin LLM-assisted wrapper. Pre-resolves ambiguous field references using Claude before delegating to the core. Has no parallel parser/model/report files.

This branch (Agent Version 0511_ABCD) carries Phase A+B+C+D changes on top of the base converter:
- **A+B** — live partitions (`Sql.Database` / `PostgreSQL.Database` / `Snowflake.Databases` / `DatabricksMultiCloud.Catalogs`), custom-SQL relations (`Value.NativeQuery` with `EnableFolding`), federated-aware connection parsing, `escape_m_string` helper.
- **C** — Tableau worksheet-level blends → PBI relationships (`_synthesize_blend_relationships`), with cycle/parallel-edge breaker (`_deactivate_ambiguous_paths`).
- **D** — multi-datasource visual binding (`_resolve_visual_field` falls back across `datasourceDeps` when a field isn't on the primary).
- **Credentials workflow** — `--credentials` JSON / XLSX overrides connection fields and emits `credentials_manifest.json`. Passwords / PATs never embed in the PBIP.

See `CHANGES_ABCD.md` in the repo root for the file-by-file diff.

## When to use which

- **`twbx` (full mode)** — packaged workbook with embedded Hyper extract. Builds a real TMDL semantic model from `<object-graph>` metadata, enriches columns with Hyper schema, exports CSVs, wires visuals to typed columns. Run `python script.py path/to/file.twbx --output ./out_pbip`.
- **`twb` (stub mode)** — XML-only workbook. Single placeholder data table; visuals are wired but query against empty rows. Useful when only the layout/visual structure matters. Run `python script.py path/to/file.twb`.
- **Credentials override** — append `--credentials creds.json` (or `.xlsx`) to either mode to promote dev → prod servers or repoint an extract-mode workbook at a live Databricks warehouse. See `credentials.json` in the repo root for the schema.

## Input shape

```
*.twb / *.twbx       (Tableau workbook)            creds.json / .xlsx (optional)
     |                                                     |
     v                                                     v
parser.py:           reads XML  →  IR dicts        credentials.py:
                     (datasources w/ connection +     CredentialStore (class/server/
                      customSql + extracts,           db/schema/http_path/catalog/
                      worksheets w/ datasourceDeps,   username/password/token)
                      dashboards, parameters)               |
     |                                                     |
     v                                                     |
hyper.py:            (twbx only) loads .hyper              |
                     extract, exports DataFrames           |
                     per table                             |
     |                                                     |
     v                                                     v
model.py:            IR + credentials  →  SemanticModel
                     - tables/columns/measures/params
                     - blend rels (Phase C) + cycle-breaker
                     - per-table partition mode (import / directQuery)
                     - credential overrides applied in write_tmdl
     |
     v
report.py:           IR + model  →  pages + visuals
                     - multi-DS binding (Phase D) via datasourceDeps
                     - visual.json per worksheet zone
     |
     v
writer.py:           drops everything to PBIP folder layout
     |
     v
*.pbip / .Report / .SemanticModel + _ir.json
                                  + datasource_mapping.xlsx
                                  + credentials_manifest.json (when live ds present)
```

## Pipeline stages and decision points

### 1. Parse (`parser.py`)
- Reads twb XML into IR dicts. Captures:
  - **Datasources**: name, caption, columns (with role/type/formula), `connection` (via `_parse_connection_metadata` — federated-aware; picks the first non-federated/non-hyper inner `<connection>` and pulls class / server / dbname / port / schema / authentication / service / warehouse / db / role / sslmode), `customSql` list (`<relation type='text'>` / `'query'>` fragments captured as `{name, sql}` dicts), `extracts` (hyper dbnames), color encodings.
  - **Worksheets**: shelves (`rowFields` / `colFields`), encodings (color / size / label / tooltip / detail), filters, mark class, title, `datasourceRef` (primary), and `datasourceDeps` (FULL list of `<datasource-dependencies>` blocks, ordered, primary first, with the declared column refs per entry) — this is the input to Phase C blend detection and Phase D multi-DS binding.
  - **Dashboards**: layout zones, sizing, dashboard-level filters.
  - **Parameters**: each `<column>` with `param-domain-type` becomes its own scalar.

### 2. Build semantic model (`model.py`)
- **Per-datasource tables**: groups columns by `parentTable` from `<object-graph>`. Each group becomes one TMDL table.
- **Hyper enrichment**: matches TMDL tables to Hyper extract tables by name; overrides column types from Hyper catalog; adds Hyper-only columns missing from TWB XML.
- **Calc fields → measures or columns**:
  - `role='measure'` + formula → DAX measure (via `dax_translator`).
  - `role='dimension' + datatype='boolean'` + formula → DAX calculated column (row-level evaluation, supports filter use).
  - `<calculation class='categorical-bin'>` (no formula attribute) → DAX calculated column with `SWITCH(TRUE(), [src] IN {...}, "bucket", [src])`.
- **Globally-unique measure names**: rename non-canonical duplicates to `<name> (<table>)` and rewrite cross-table DAX refs.
- **Auto-generated `Calculation_xxx` measures hidden** so the field pane stays clean.
- **Parameter tables**: one table per parameter (no shared lumped table). List params get `Value`/`Label` columns; any/range params get a single `Value` column with one row.
- **Blend relationships (Phase C)**: `_synthesize_blend_relationships` walks each worksheet's `datasourceDeps`, pairs primary against each secondary, infers blend keys from declared-column intersection (case-insensitive), filters out measure-typed candidates, and emits `blend_<hash>` many-to-many `bothDirections` relationships. Pairs with both-side duplicate keys are skipped with `[BLEND-WARN]`. `_deactivate_ambiguous_paths` then marks parallel and cycle-closing rels `isActive: false`; they remain in the model and are reachable via `USERELATIONSHIP(...)`.
- **Partition M emit (`_render_table_tmdl` + `_render_partition_m`)**: per-table branch on `connection.class` — sqlserver / postgres / snowflake / databricks all emit live sources; sqlserver / postgres / snowflake stamp `mode: directQuery`; databricks uses `mode: import`. Hyper/CSV-extract path wins for `federated` / `hyper` / `extract` / file-based classes. When a credentials file marks a Databricks entry and `prefer_live_over_extract` is true (default), the converter discards the bound Hyper/CSV path and emits the live Databricks source instead.

### 3. Build report (`report.py`)
- **Visual type picking** (`visual_picker.py`): mark → visual lookup, with overrides for orientation (rows-vs-cols measure detection) and scatter safety.
- **Field resolution**: Tableau field refs go through `_resolve_visual_field` → `col_locator` → PBI `(table, column)`. Measures emit as `Measure` ref; columns wrapped in appropriate `Aggregation` or bare `Column`.
- **Multi-datasource binding (Phase D)**: when a field carries a `binding_ds` different from the worksheet's primary (Tableau data blend), the resolver consults the secondary's `col_locator` first and logs `[BLEND] binding routed: '<field>' -> ds=<secondary> table=<tbl>`. The Phase C relationship is what makes the cross-ds projection actually join at query time.
- **Date-part agg redirects**: `tmn:Date of Visit` (truncate-month) → bind to `Year-Month of Date of Visit` calc column. Same for `tqr:`/`ty:`.
- **Filter literals**: typed correctly per column type (booleans `true` not `'true'`, numerics with `L`/`D` suffix).

### 4. Write (`writer.py`)
- Drops files in PBIP layout: `.pbip` manifest, `.Report/definition/...`, `.SemanticModel/definition/...`, `data/<ds>/*.csv`.
- Long path names truncated to 80 chars + 8-char hash to stay under Windows MAX_PATH (260).
- Orchestrator (`converter.py`) cleans any pre-existing `*_pbip` folder via `_clean_generated_output` (with rename-fallback for Windows handle locks), then writes `_ir.json`, `datasource_mapping.xlsx`, and `credentials_manifest.json` (only when live ds present) alongside the PBIP folder.

## Error handling and reporting

The converter emits structured log lines for diagnostics:

| Prefix | Meaning |
|---|---|
| `[CONN]` | Connection class detection (extract / DirectQuery / unsupported); also logs the live-class placeholder fallback |
| `[BLEND]` | Synthesized blend relationship; multi-DS binding routed; cycle-breaker deactivation |
| `[BLEND-WARN]` | Blend rel skipped due to duplicate keys on both sides (TREATAS fallback hint) |
| `[MEAS-DEDUP]` | Measure renamed to avoid global collision |
| `[CALC-COL]` | Boolean dim emitted as calc column instead of measure |
| `[GROUP]` | Tableau group / categorical-bin materialised as DAX calc column (or alias-fallback when bin XML is malformed); filter-on-group-label expansion |
| `[CALC-INDEX]` | Worksheet-local `Calculation_<id>` fields (`<view>/<datasource-dependencies>/<column>`) merged into the owning datasource's column list during parse |
| `[CALC-ALIAS]` | Trivial-alias calc field (formula is `[X]` or `// caption\n[X]`) registered in `col_locator` against its underlying column — visuals referencing `Calculation_<id>` resolve directly without agent assistance |
| `[DAX-DROP]` | Untranslatable Tableau formula; emits `BLANK()` placeholder |
| `[RESOLVE]` | Field ref couldn't be bound → field dropped from visual |
| `[FILTER]` | Filter ref couldn't be bound → filter dropped |
| `[VPICK]` | Visual type chosen for a worksheet (mark + shelf shape + result) |
| `[HYPER]` | Hyper extract enrichment events |
| `[CREDS]` | Credentials file load + per-datasource match status in the manifest |
| `[MAP]` | `datasource_mapping.xlsx` write status (or skip when openpyxl missing) |
| `[CLEAN]` | Stale `*_pbip` output removal / rename-on-lock fallback |

Validate before opening in PBI Desktop:

```bash
python "C:/Users/ShrikantPansare/_validate_pbip.py" "<output_pbip_dir>"
```

Reports: tables/measures/relationship counts, broken DAX refs, duplicate-name violations.

For column-level mismatches, open `datasource_mapping.xlsx` in the output folder: the **Columns** sheet maps each PBI column to its Tableau column reference AND the Hyper / CSV header it actually binds to.

## Smoke test corpus

After material changes, convert the corpus and confirm visual counts haven't shifted:

```bash
cd "C:/Users/ShrikantPansare" && for f in \
  "UseCase.twbx" \
  "UseCase2.twbx" \
  "Sample Dashboards/Netflix Movies and TV Shows Dashboard.twbx" \
  "Sample Dashboards/Merchandise Sales Dashboard.twbx" \
  "Sample Dashboards/Superstore Performance Dashboard _ #VOTD.twbx"; do
  echo "=== $f ==="
  python -m tableau_to_pbi_agent "$f" --skip-llm 2>&1 | grep -E "types=|warnings:" | head -3
done
```

Validated dashboards (in `C:/Users/ShrikantPansare/`):
- `Site Monitoring.twbx` — 11 tables, 73 measures, 4 DAX drops
- `Quality Checks.twbx` — 15 tables, 41 measures, 4 DAX drops
- `Sprint Output.twbx` — 11 tables, 37 measures, 2 DAX drops
- `Production Report.twbx` — 14 tables, 116 measures, 9 DAX drops
- `Sample Dashboards/Healthcare Resources Analysis ...twbx` — 21 tables, 64 measures, 6 DAX drops (all `INDEX()`)
