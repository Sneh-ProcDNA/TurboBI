# TurboBI — Tableau to Power BI Converter

> **Convert Tableau `.twb` / `.twbx` workbooks into Power BI Project (`.pbip`) files — deterministically, with optional Claude AI assistance.**

TurboBI is a Python-based conversion pipeline that translates Tableau workbooks into Power BI's open PBIP format. It reconstructs your Tableau semantic model (tables, columns, measures, relationships) into TMDL files, translates Tableau formula syntax into DAX, maps every worksheet visual to a corresponding Power BI visual type, and writes the complete PBIP folder structure ready to open in Power BI Desktop.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Pipeline Stages](#pipeline-stages)
- [Packages](#packages)
  - [tableau_to_pbi — Core Converter](#tableau_to_pbi--core-converter)
  - [tableau_to_pbi_agent — AI-Assisted Wrapper](#tableau_to_pbi_agent--ai-assisted-wrapper)
  - [Web UI — app.py](#web-ui--apppy)
- [Supported Features](#supported-features)
  - [DAX Translation](#dax-translation)
  - [Visual Mapping](#visual-mapping)
  - [Data Sources](#data-sources)
  - [Credentials & Connection Overrides](#credentials--connection-overrides)
- [Phase A+B+C+D Enhancements](#phase-abcd-enhancements)
- [Installation](#installation)
- [Usage](#usage)
  - [CLI (script.py)](#cli-scriptpy)
  - [Agent CLI](#agent-cli)
  - [Web UI](#web-ui)
  - [Python API](#python-api)
- [Output Artifacts](#output-artifacts)
- [Diagnostic Log Prefixes](#diagnostic-log-prefixes)
- [Known Limitations](#known-limitations)
- [Running Tests](#running-tests)

---

## Overview

TurboBI is a two-layer system:

| Layer | Package | Purpose |
|---|---|---|
| **Core** | `tableau_to_pbi` | Fully deterministic XML → IR → TMDL/visual.json pipeline |
| **Agent** | `tableau_to_pbi_agent` | LLM-assisted pre-pass that resolves ambiguous field references before handing off to the core |
| **Web UI** | `app.py` | Flask server wrapping the agent CLI with SSE log streaming and one-click download |

The core converter runs without any network access or API keys. The agent layer is optional — when `ANTHROPIC_API_KEY` is set it calls Claude Haiku to resolve unrecognized Tableau field references; when skipped (`--skip-llm`) the converter still runs twice but without any LLM calls.

---

## Architecture

```
User input: *.twb / *.twbx
         |
         v
  ┌──────────────────────────────────────────────────────────┐
  │   tableau_to_pbi_agent  (optional LLM pre-pass)          │
  │                                                          │
  │  orchestrator.py                                         │
  │    ├── parser.py (pass 1, build model snapshot)          │
  │    ├── run converter pass 1 → capture warnings           │
  │    ├── warnings_parser.py → structured [RESOLVE] records │
  │    ├── calc_index.py + formula_resolver.py (zero-token)  │
  │    ├── ClaudeClient → field_resolver.py (parallel async) │
  │    ├── hints.py (persist / load sidecar)                 │
  │    └── run converter pass 2 with hints applied           │
  └──────────────────────────────────────────────────────────┘
         |
         v
  ┌──────────────────────────────────────────────────────────┐
  │   tableau_to_pbi  (core converter)                       │
  │                                                          │
  │  parser.py      — .twb XML → IR dicts                    │
  │       |                                                  │
  │  hyper.py       — .hyper extract → typed DataFrames      │
  │       |                                                  │
  │  model.py       — IR → SemanticModel (TMDL tables,       │
  │                   measures, columns, relationships)      │
  │       |                                                  │
  │  report.py      — IR + model → pages + visual.json       │
  │       |                                                  │
  │  writer.py      — writes PBIP folder to disk             │
  └──────────────────────────────────────────────────────────┘
         |
         v
  Output: <name>_pbip/
    ├── <name>.pbip
    ├── <name>.Report/definition/...
    ├── <name>.SemanticModel/definition/...
    ├── _ir.json
    ├── datasource_mapping.xlsx
    └── credentials_manifest.json  (live sources only)
```

---

## Project Structure

```
TurboBI Version 0514/
│
├── app.py                          # Flask web UI server
├── script.py                       # CLI entry point (direct converter)
├── credentials.json                # Example credentials schema
├── agent.md                        # Agent architecture documentation
├── skill.md                        # Translation skills reference catalog
├── CHANGES_ABCD.md                 # Phase A+B+C+D change log
│
├── templates/
│   └── index.html                  # Web UI frontend (drag-and-drop upload)
│
├── uploads/                        # Per-job work directories (auto-managed)
│
├── tableau_to_pbi/                 # ── Core converter package ──
│   ├── __init__.py                 # Exposes Converter, run()
│   ├── converter.py                # High-level orchestrator (Converter class)
│   ├── parser.py                   # .twb XML → IR dicts (TWBParser)
│   ├── hyper.py                    # .hyper extract reader (pantab + HyperAPI)
│   ├── model.py                    # IR → SemanticModel / TMDL
│   ├── report.py                   # IR + model → page/visual JSON
│   ├── writer.py                   # PBIP folder writer (PBIPWriter)
│   ├── dax_translator.py           # Tableau formula → DAX translation
│   ├── visual_picker.py            # Mark-type → PBI visualType logic
│   ├── config.py                   # Canvas sizes, slot maps, PBIP schema URLs
│   ├── utils.py                    # Shared helpers (hex_id, safe_filename, …)
│   ├── credentials.py              # JSON / XLSX credentials loader
│   ├── visual_rules.json           # Editable mark → visualType lookup table
│   ├── visual_rules_clean.json     # Cleaned variant (no deprecated entries)
│   ├── visual_rules_fixed.json     # Fixed variant (circle → scatterChart)
│   └── validators/
│       ├── __init__.py
│       └── slot_validator.py       # Validates visual slot bindings
│
├── tableau_to_pbi_agent/           # ── Agent / LLM wrapper package ──
│   ├── __init__.py
│   ├── __main__.py                 # python -m tableau_to_pbi_agent
│   ├── cli.py                      # argparse CLI (run_with_agent)
│   ├── orchestrator.py             # Two-pass pipeline + hint management
│   ├── claude_client.py            # Async Anthropic client with prompt caching
│   ├── context_builder.py          # Builds compact model snapshot for Claude
│   ├── calc_index.py               # Indexes worksheet-local Calculation_xxx fields
│   ├── canonical.py                # Strips Tableau (Object!Suffix) from names
│   ├── warnings_parser.py          # Parses [RESOLVE] / [FILTER] warnings
│   ├── hints.py                    # Hint sidecar I/O (load / save / merge)
│   ├── resolvers/
│   │   ├── field_resolver.py       # LLM-based (table, column) resolver
│   │   └── formula_resolver.py     # Zero-token deterministic alias resolver
│   ├── evals/                      # Evaluation harness
│   └── TODO.md                     # Development notes
│
└── uploads/<job_id>/               # Web UI job temp directories
    ├── <workbook>.twbx
    ├── credentials.json            # (optional, user-supplied)
    └── <workbook>_pbip.zip         # Final download artifact
```

---

## Pipeline Stages

### Stage 1 — Parse (`parser.py`)

`TWBParser` reads the `.twb` XML file and emits three flat lists of plain dicts:

**Datasources** — Each entry captures:
- `name`, `caption`, `columns` (with role, type, formula, parentTable)
- `connection` metadata: class (sqlserver / postgres / snowflake / databricks / hyper / …), server, database, port, schema, warehouse, authentication, http_path
- `customSqls` — list of `{name, sql}` dicts from `<relation type='text'>` / `type='query'` elements
- `extracts` — Hyper `.hyper` dbnames bound to this datasource
- Color encodings (datasource-level palette overrides)

**Worksheets** — Each entry captures:
- Row/col shelf fields, encodings (color / size / label / tooltip / detail)
- Filters, mark class, title, titleStyle, labelStyle
- `datasourceRef` (primary datasource name)
- `datasourceDeps` — full ordered list of `{datasource, columns}` from `<datasource-dependencies>` blocks (input for Phase C blend detection and Phase D multi-datasource binding)

**Dashboards** — Each entry captures:
- Canvas size, layout zones (deduped and scaled), dashboard-level filters

After parsing, `_merge_worksheet_calc_fields` picks up `<view>/<datasource-dependencies>/<column>` entries that live only inside worksheet scopes and appends them to the owning datasource's column list (logs `[CALC-INDEX]`).

---

### Stage 2 — Build Semantic Model (`model.py`)

`SemanticModel` takes the parsed IR and builds a complete TMDL-ready data model:

- **Table grouping**: columns are grouped by `parentTable` from `<object-graph>` metadata. Each group becomes one TMDL table.
- **Hyper enrichment**: column types are overridden with accurate types from the `.hyper` extract catalog; Hyper-only columns not present in the TWB XML are added.
- **Calculated fields classification**: each calc field is examined by `_formula_has_aggregation` to decide between DAX measure, DAX calculated column, or categorical-bin column.
- **Trivial alias resolution**: `_register_calc_alias_resolutions` detects calc fields whose formula is just `[X]` and registers them in `col_locator` to avoid `[RESOLVE]` warnings (logs `[CALC-ALIAS]`).
- **Measure deduplication**: globally-unique names enforced; duplicates renamed to `<name> (<table>)` and DAX cross-references rewritten (logs `[MEAS-DEDUP]`).
- **Parameter tables**: one table per Tableau parameter; list params get `Value`/`Label` columns.
- **Date hierarchies**: auto-synthesized Year/Quarter/Month/Day columns + hierarchy TMDL block for every `dateTime` column.
- **Blend relationships (Phase C)**: `_synthesize_blend_relationships` infers many-to-many `bothDirections` Power BI relationships from worksheets that bind to multiple datasources. `_deactivate_ambiguous_paths` then marks cycle-closing or parallel-edge rels `isActive: false`.
- **Partition M expressions**: per-table M code for live sources (SQL Server, PostgreSQL, Snowflake, Databricks), CSV-from-Hyper for extract sources, and `Value.NativeQuery` for custom SQL.

---

### Stage 3 — Build Report (`report.py`)

`ReportBuilder` translates worksheets and dashboards into PBIP report JSON:

- **Visual type selection** (`visual_picker.py`): mark class → PBI visualType, with shelf-shape overrides for bar orientation, scatter safety, auto-rules for mark='Automatic', and card-to-multiRowCard upgrade.
- **Field resolution**: `_resolve_visual_field` maps Tableau field references to `(table, column)` pairs via `col_locator`. Multi-datasource binding (Phase D) consults secondary datasources when the primary doesn't own the field.
- **Date-part redirects**: `yr:Date`, `tmn:Date of Visit`, etc. → synthesized date-hierarchy column names.
- **Filter binding**: literal value typing (boolean / integer / float / string), Top N filter shape, and member-list filter expansion for group fields.
- **Header styling**: table/matrix column and row header styles parsed from `<style-rule>` elements in the TWB.
- **Map defaults**: all geo visuals get a North America default viewport (`view: 'UnitedStates'`, `customZoom: 3`).

---

### Stage 4 — Write (`writer.py`)

`PBIPWriter` drops the complete PBIP layout to disk:

```
<stem>_pbip/
├── <stem>.pbip                            # project manifest
├── <stem>.Report/
│   ├── definition.pbir
│   └── definition/
│       ├── report.json
│       ├── pages/pages.json
│       └── pages/<page_id>/
│           ├── page.json
│           └── visuals/<visual_id>/visual.json
└── <stem>.SemanticModel/
    ├── definition.pbism
    ├── definition/
    │   ├── database.tmdl
    │   ├── model.tmdl
    │   ├── relationships.tmdl
    │   ├── tables/<TableName>.tmdl
    │   └── cultures/en-US.tmdl
    └── data/<ds_dir>/<table>.csv          # Hyper-extract CSVs
```

Long path names are truncated to 80 characters + 8-character deterministic hash suffix to stay within Windows MAX_PATH (260).

---

## Packages

### `tableau_to_pbi` — Core Converter

The deterministic core — no network calls, no AI, fully reproducible.

| File | Responsibility | Lines |
|---|---|---|
| `converter.py` | `Converter` class orchestrating all four stages; `run()` public API; `extract_twbx()` unzip helper | ~600 |
| `parser.py` | `TWBParser` class; full XML walk for datasources, worksheets, dashboards | ~2,976 |
| `hyper.py` | `HyperData` + `HyperRegistry`; pandas/pantab/tableauhyperapi integration; graceful degradation when libraries absent | ~426 |
| `model.py` | `SemanticModel`; table building, measure/column classification, blend rels, date hierarchies, partition-M emit | ~4,157 |
| `report.py` | `ReportBuilder`; visual binding, filter construction, page layout | ~3,379 |
| `writer.py` | `PBIPWriter`; PBIP folder layout, TMDL file formatting | ~355 |
| `dax_translator.py` | `translate()` function; Tableau formula → DAX with LOD handling, date functions, conditional expressions | ~1,573 |
| `visual_picker.py` | `pick_visual_type()`; mark → visualType lookup + auto rules | ~393 |
| `config.py` | `VISUAL_SLOTS`, `DEFAULT_PAGE_WIDTH/HEIGHT`, aggregation tables, PBIP schema URLs | — |
| `utils.py` | `hex_id`, `lineage_tag`, `safe_filename`, `tmdl_quote`, `escape_m_string`, `_long_path` | — |
| `credentials.py` | `CredentialStore`; JSON/XLSX loader; per-datasource match + override logic | — |
| `visual_rules.json` | Editable mark → visualType lookup table (no Python recompile needed to change visual mappings) | — |

---

### `tableau_to_pbi_agent` — AI-Assisted Wrapper

An optional two-pass wrapper around the core converter.

**How it works:**
1. Run the converter once and capture stdout.
2. Parse all `[RESOLVE]` and `[FILTER]` warnings into structured records.
3. For each warning, first try the zero-token `formula_resolver` (handles trivial `[X]` alias calc-fields deterministically).
4. Send remaining ambiguous warnings to Claude Haiku in parallel — all share one cached system prompt (the model snapshot), dramatically reducing token cost.
5. Persist the resulting `(table, column)` hints to a `.hints.json` sidecar file next to the workbook.
6. Re-run the converter with hints injected — unresolved fields that triggered `[RESOLVE]` now bind correctly.

| File | Responsibility |
|---|---|
| `orchestrator.py` | `run_with_agent()` — full two-pass pipeline, async hint resolution, before/after warning counts |
| `claude_client.py` | `ClaudeClient`; async Anthropic SDK wrapper with `cache_control` on the system prompt |
| `context_builder.py` | Builds compact `{ds: {tables: {table: [columns]}}}` snapshot for Claude; `warning_context()` per-warning slice |
| `warnings_parser.py` | Regex-based parser for `[RESOLVE]`, `[FILTER]`, `[DS]` log lines → typed dicts |
| `hints.py` | Atomic load/save of `.hints.json` sidecar; merge-and-persist |
| `calc_index.py` | `build_calc_index()` — indexes all `Calculation_<id>` fields from the .twb for caption-based resolution |
| `canonical.py` | Strips Tableau `(Object!Suffix)` disambiguation tokens from field names |
| `resolvers/field_resolver.py` | `FieldResolver` class — async LLM-based `(table, column)` resolver |
| `resolvers/formula_resolver.py` | `resolve_via_formula()` — deterministic resolver for `[X]`-pattern calc aliases |
| `cli.py` | `argparse` CLI; exposes `--skip-llm`, `--model`, `--credentials` flags |

---

### Web UI — `app.py`

A Flask server providing a drag-and-drop browser interface for non-developer users.

**Endpoints:**

| Route | Method | Description |
|---|---|---|
| `/` | GET | Serve `templates/index.html` |
| `/convert` | POST | Accept `.twbx`/`.twb` upload + optional `credentials.json`, spawn background job, return `job_id` |
| `/stream/<job_id>` | GET | Server-Sent Events stream; replays buffered log lines + live output |
| `/status/<job_id>` | GET | JSON status (`pending/running/done/error`), report summary, zip filename |
| `/download/<job_id>` | GET | Serve the completed `<stem>_pbip.zip` as an attachment |

**Job lifecycle:**
- Each upload gets a UUID-based work directory under `./uploads/`.
- Conversion runs in a daemon background thread; stdout is captured line-by-line into a `queue.Queue` and a persistent log buffer.
- SSE clients that join late replay all buffered lines before receiving live output.
- A `threading.Timer` cleans up the work directory 30 minutes after completion.
- Max upload size: 500 MB.

---

## Supported Features

### DAX Translation

| Category | Tableau → DAX |
|---|---|
| **Aggregations** | `SUM`, `AVG`/`AVERAGE`, `COUNT`, `COUNTD` → `DISTINCTCOUNT`, `MIN`, `MAX`, `MEDIAN`, `STDEV`, `VAR` |
| **LOD (Fixed)** | `{ FIXED : agg }` → `CALCULATE(agg, ALL('T'))`, `{ FIXED [D] : agg }` → `CALCULATE(agg, ALLEXCEPT('T','T'[D]))` |
| **Date functions** | `DATEDIFF`, `DATEPART`, `DATETRUNC`, `DATENAME`, `MAKEDATE`, `DATEADD`, `TODAY`, `NOW`, and more |
| **Conditionals** | `IF/THEN/ELSEIF/ELSE/END`, `IIF`, `CASE/WHEN`, function-form `IF(c, x, y)` |
| **Strings** | `LEN`, `UPPER`, `LOWER`, `TRIM`, `LEFT`, `RIGHT`, `MID`, `FIND`, `REPLACE`, `CONTAINS`, `STR`, `SPLIT` |
| **Table calcs** | `TOTAL` → `CALCULATE(..., ALLSELECTED())`, `ATTR` → `SELECTEDVALUE` |
| **Null / logic** | `ZN`, `ISNULL`, `AND`, `OR`, `NOT`, `TRUE`, `FALSE`, `NULL` → DAX equivalents |
| **Tableau internals** | Row-count calc → `COUNTROWS`, `Calculation_xxx` auto-hidden in field pane |

**Not translated (drops to `BLANK()` placeholder):** `INCLUDE`/`EXCLUDE` LODs, `INDEX()`, `RANK_UNIQUE`, `RANK_DENSE`, `RUNNING_SUM`, `WINDOW_AVG`, `LOOKUP`, `REGEXP_*`.

### Visual Mapping

| Tableau Mark | Power BI Visual |
|---|---|
| `bar` | `barChart` / `columnChart` (orientation-aware) |
| `line` | `lineChart` |
| `area` | `areaChart` |
| `circle` | `scatterChart` |
| `pie` / `donut` | `pieChart` / `donutChart` |
| `map` / `point` / `polygon` / `filled-map` | `azureMap` (North America default viewport) |
| `text` | `tableEx` |
| `square` | `treemap` |
| `Automatic` | Auto-rules based on shelf shape (see Visual Picker) |

### Data Sources

| Connection Class | Output Mode | M Expression |
|---|---|---|
| `sqlserver` | DirectQuery | `Sql.Database(server, db)` + schema nav |
| `postgres` | DirectQuery | `PostgreSQL.Database("host:port", db)` + schema nav |
| `snowflake` | DirectQuery | `Snowflake.Databases(server, warehouse, [Implementation="2.0"])` |
| `databricks` / `spark` | Import | `DatabricksMultiCloud.Catalogs(server, http_path, ...)` |
| `hyper` / `excel-direct` / `csv` / `federated` | Import | `Csv.Document(File.Contents("data/..."))` (portable relative path) |
| Custom SQL (any live class) | Inherits class mode | `Value.NativeQuery(Source, "<sql>", null, [EnableFolding=true])` |

### Credentials & Connection Overrides

Pass a `--credentials` file (JSON or XLSX) to:
- Override `server`, `database`, `port`, `schema` in the emitted M expressions (dev → prod promotion without touching the Tableau file)
- Generate a `credentials_manifest.json` documenting effective connection parameters and `has_password` / `has_personal_access_token` flags for Power BI Desktop setup
- Passwords and tokens are **never** written to M expressions

---

## Phase A+B+C+D Enhancements

This release includes four enhancement phases on top of the base converter:

**Phase A+B — Live Partitions & Custom SQL**
- `Sql.Database`, `PostgreSQL.Database`, `Snowflake.Databases`, `DatabricksMultiCloud.Catalogs` live source M expressions
- `Value.NativeQuery` for custom SQL relations with `EnableFolding=true`
- Federated-aware connection parsing; `escape_m_string` helper

**Phase C — Tableau Data Blending → Power BI Relationships**
- `_synthesize_blend_relationships` infers many-to-many `bothDirections` relationships from worksheets binding to multiple datasources
- Blend keys inferred from declared-column intersection (case-insensitive); measure-typed entries excluded
- `_deactivate_ambiguous_paths` marks cycle-closing and parallel-edge rels `isActive: false`

**Phase D — Multi-Datasource Visual Binding**
- `_resolve_visual_field` falls back across `datasourceDeps` when a field isn't on the primary datasource
- Routes binding to the secondary's TMDL table; logs `[BLEND] binding routed` for auditability

---

## Installation

```bash
# Clone and install dependencies
pip install flask anthropic pandas openpyxl

# Optional: Hyper extract support
pip install pantab tableauhyperapi

# Optional: agent LLM support
export ANTHROPIC_API_KEY=your_key_here
```

---

## Usage

### CLI (`script.py`)

```bash
# Full conversion from a packaged workbook
python script.py workbook.twbx

# With output directory
python script.py workbook.twbx --output ./output_pbip

# With credentials override (JSON or XLSX)
python script.py workbook.twbx --credentials credentials.json

# .twb only (visual layout, stub data model)
python script.py workbook.twb
```

### Agent CLI

```bash
# With LLM field resolution (requires ANTHROPIC_API_KEY)
python -m tableau_to_pbi_agent workbook.twbx

# Without LLM (re-applies existing hints sidecar only)
python -m tableau_to_pbi_agent workbook.twbx --skip-llm

# With credentials + custom model
python -m tableau_to_pbi_agent workbook.twbx --credentials creds.json --model claude-haiku-4-5-20251001
```

### Web UI

```bash
python app.py                  # http://localhost:5000
python app.py --port 8080
python app.py --host 0.0.0.0 --debug
```

Then open the browser, drag-and-drop a `.twbx` file, and watch the live conversion log stream. When done, click **Download** to get the PBIP zip.

### Python API

```python
from tableau_to_pbi import run

run(
    input_path="workbook.twbx",
    output="./output_pbip",
    credentials_path="credentials.json",
)
```

---

## Output Artifacts

| File | Description |
|---|---|
| `<name>_pbip/` | Standard PBIP folder — open in Power BI Desktop |
| `<name>_pbip/<name>.pbip` | Project manifest |
| `<name>_pbip/<name>.Report/` | Page and visual definitions (JSON) |
| `<name>_pbip/<name>.SemanticModel/` | TMDL tables, measures, relationships, columns |
| `<name>_pbip/<name>.SemanticModel/data/` | CSV files from Hyper extract (portable, relative path) |
| `_ir.json` | Parser IR dump (debug; gated on `debug_ir=True`) |
| `datasource_mapping.xlsx` | Four-sheet Excel: Datasources / Columns / Relationships / Skipped Relationships |
| `credentials_manifest.json` | Effective connection params + credential flags (written only when live data sources present) |
| `<name>.hints.json` | Agent field-resolution hints sidecar (persisted for re-runs) |

---



| Prefix | Meaning |
|---|---|
| `[CONN]` | Connection class detection; live-class placeholder fallback |
| `[BLEND]` | Synthesized blend relationship; multi-DS binding routed; cycle-breaker deactivation |
| `[BLEND-WARN]` | Blend rel skipped (duplicate keys on both sides) |
| `[MEAS-DEDUP]` | Measure renamed to avoid global collision |
| `[CALC-COL]` | Boolean dim emitted as calculated column |
| `[GROUP]` | Categorical-bin / group emitted as DAX SWITCH calc column |
| `[CALC-INDEX]` | Worksheet-local `Calculation_<id>` fields merged into datasource |
| `[CALC-ALIAS]` | Trivial-alias calc field resolved to underlying column |
| `[DAX-DROP]` | Untranslatable formula; `BLANK()` placeholder emitted |
| `[RESOLVE]` | Field ref could not be bound; field dropped from visual |
| `[FILTER]` | Filter ref could not be bound; filter dropped |
| `[VPICK]` | Visual type chosen for a worksheet |
| `[HYPER]` | Hyper extract enrichment events |
| `[CREDS]` | Credentials file load + per-datasource match status |
| `[MAP]` | `datasource_mapping.xlsx` write status |
| `[CLEAN]` | Stale `*_pbip` output removal / rename-on-lock fallback |

---





