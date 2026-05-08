# Tableau-to-Power-BI Conversion Agent

A multi-stage agent that converts Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files. Two cooperating Python packages:

- **`tableau_to_pbi/`** — the deterministic core. XML → IR → TMDL/visual.json → PBIP folder.
- **`tableau_to_pbi_agent/`** — thin LLM-assisted wrapper. Pre-resolves ambiguous field references using Claude before delegating to the core. Has no parallel parser/model/report files.

## When to use which

- **`twbx` (full mode)** — packaged workbook with embedded Hyper extract. Builds a real TMDL semantic model from `<object-graph>` metadata, enriches columns with Hyper schema, exports CSVs, wires visuals to typed columns. Run `python script.py path/to/file.twbx --output ./out_pbip`.
- **`twb` (stub mode)** — XML-only workbook. Single placeholder data table; visuals are wired but query against empty rows. Useful when only the layout/visual structure matters. Run `python script.py path/to/file.twb`.

## Input shape

```
*.twb / *.twbx       (Tableau workbook)
     |
     v
parser.py:           reads XML  →  IR dicts
                                    (datasources, worksheets, dashboards, parameters)
     |
     v
hyper.py:            (twbx only) loads .hyper extract, exports DataFrames per table
     |
     v
model.py:            IR  →  SemanticModel
                            (tables, columns, relationships, measures, parameter tables)
     |
     v
report.py:           IR + model  →  pages + visuals
                                    (visual.json per worksheet zone)
     |
     v
writer.py:           drops everything to PBIP folder layout
     |
     v
*.pbip / .Report / .SemanticModel
```

## Pipeline stages and decision points

### 1. Parse (`parser.py`)
- Reads twb XML into IR dicts. Captures:
  - **Datasources**: name, caption, columns (with role/type/formula), connections, custom SQL relations, color encodings.
  - **Worksheets**: shelves (rowFields/colFields), encodings (color/size/label/tooltip/detail), filters, mark class, title, datasource dependencies for blend detection.
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
- **Blend relationships**: synthesizes many-to-many `bothDirections` rels between tables that share a column name across worksheet datasource dependencies. Cycle-breaker marks redundant rels `isActive: false`.

### 3. Build report (`report.py`)
- **Visual type picking** (`visual_picker.py`): mark → visual lookup, with overrides for orientation (rows-vs-cols measure detection) and scatter safety.
- **Field resolution**: Tableau field refs go through `col_locator` → PBI `(table, column)`. Measures emit as `Measure` ref; columns wrapped in appropriate `Aggregation` or bare `Column`.
- **Date-part agg redirects**: `tmn:Date of Visit` (truncate-month) → bind to `Year-Month of Date of Visit` calc column. Same for `tqr:`/`ty:`.
- **Filter literals**: typed correctly per column type (booleans `true` not `'true'`, numerics with `L`/`D` suffix).

### 4. Write (`writer.py`)
- Drops files in PBIP layout: `.pbip` manifest, `.Report/definition/...`, `.SemanticModel/definition/...`, `data/<ds>/*.csv`.
- Long path names truncated to 80 chars + 8-char hash to stay under Windows MAX_PATH (260).

## Error handling and reporting

The converter emits structured log lines for diagnostics:

| Prefix | Meaning |
|---|---|
| `[CONN]` | Connection class detection (extract / DirectQuery / unsupported) |
| `[BLEND]` | Synthesized blend relationship |
| `[BLEND-WARN]` | Blend rel skipped due to duplicate keys |
| `[MEAS-DEDUP]` | Measure renamed to avoid global collision |
| `[CALC-COL]` | Boolean dim emitted as calc column instead of measure |
| `[DAX-DROP]` | Untranslatable Tableau formula; emits `BLANK()` placeholder |
| `[RESOLVE]` | Field ref couldn't be bound → field dropped from visual |
| `[FILTER]` | Filter ref couldn't be bound → filter dropped |
| `[VPICK]` | Visual type chosen for a worksheet (mark + shelf shape + result) |
| `[HYPER]` | Hyper extract enrichment events |

Validate before opening in PBI Desktop:

```bash
python "C:/Users/ShrikantPansare/_validate_pbip.py" "<output_pbip_dir>"
```

Reports: tables/measures/relationship counts, broken DAX refs, duplicate-name violations.

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
