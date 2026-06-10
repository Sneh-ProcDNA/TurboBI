# CLI, input/output, and pipeline

## Input contract — what `qlik app unbuild` produces

```
<dir>/app-properties.json   (qTitle is the report name)
      config.yml             (ignored)
      connections.yml        (ignored)
      script.qvs             (load script body)
      dimensions.json        (master dimensions)
      measures.json          (master measures)
      variables.json         (script + UI variables)
      objects/
        sheet--<slug>-<guid>.json
        masterobject-<slug>-<guid>.json
        appprops---<guid>.json
        loadmodel---loadmodel.json
```

## CLI flags

| Flag | Purpose |
|---|---|
| `--input` / `-i` | Existing `qlik app unbuild` folder. |
| `--app` / `-a` | App id (drives unbuild when `--input` absent, or feeds `--fetch-data`). |
| `--output` / `-o` | PBIP output folder. Default `<input>_pbip`. |
| `--name` | Override report name (default: `app-properties.qTitle`). |
| `--keep-unbuild <dir>` | Preserve temp unbuild dir. |
| `--data-dir <dir>` | Pre-existing CSVs; bind via `Csv.Document`. |
| `--fetch-data` | Shell to qlik CLI per data-bearing object; needs `--app`. |
| `--fetch-data-dir <dir>` | Where fetched CSVs land. Default `./csv_exports`. |
| `--qlik-cmd <path>` | Override qlik binary. |
| `--fetch-via-engine` | Offline extract via Desktop's local Engine API. Requires `--qvf-path`. Mutually exclusive with `--fetch-data`. |
| `--qvf-path <path>` | Absolute path to `.qvf` Desktop should open. |
| `--engine-url <url>` | Default `ws://localhost:4848`. |
| `--cloud-tenant`, `--cloud-api-key`, `--cloud-app-id` | Cloud Engine API (all three required together; partial credentials rejected at parse time). |
| `--use-qlik-context [name]` | Read tenant + bearer from `~/.qlik/contexts.yml`. |
| `--dry-run` | Parse + plan + summarise, no PBIP write. |
| `--quiet` | Sets `QLIK_LOG_LEVEL=WARNING`. |
| `--report-only` | Write `conversion_report.md` only. |

`--input` and `--app` are no longer mutually exclusive — supplying both reuses the existing unbuild while still letting `--fetch-data` use the app id for the export step. Cloud flags short-circuit input-source resolution; when cloud mode is active, `--input`, `--qvf-path`, `--app` are all ignored.

If `--cloud-app-id` isn't passed but `--app` is, the latter is promoted automatically — so `qlik app unbuild --app <uuid>` maps 1:1 to `python -m qlik_to_pbi --use-qlik-context --app <uuid> --output ./out`.

## Output layout

```
OUT/
  unbuilt/   IR (skipped if --input already a folder)
  data/      CSVs from fetch (user convenience copy)
  pbip/      <Name>.pbip + <Name>.Report/ + <Name>.SemanticModel/
                (the SemanticModel carries its own data/ copy
                 so the PBIP is portable on its own)
```

Overrides:
- `--keep-unbuild PATH` → JSON IR goes to `PATH` instead of `OUT/unbuilt/`.
- `--fetch-data-dir PATH` → fetched CSVs go to `PATH` instead of `OUT/data/`.
- `--data-dir PATH` → bind to pre-existing CSVs at `PATH` (skip fetch entirely).

The PBIP under `OUT/pbip/` is always self-contained. The top-level `OUT/data/` folder is for the user's convenience (inspection, re-use in other tools); deleting it does not break the PBIP.

## Pipeline (orchestrated by `converter.py`)

1. **`fetch_data.py` / `engine_fetch.py`** — optional pre-step. `--fetch-data` shells to cloud CLI per object; `--fetch-via-engine` talks JSON-RPC over localhost WebSocket to Desktop. Both drop CSVs in `--fetch-data-dir`.
2. **`parser.py`** — walks unbuild dir into `{app, script, dimensions, measures, variables, sheets, master_objects, load_model, app_props, fields, engine_schema, bookmarks}`. The `fields` slot is a deduped list of every field name referenced anywhere — used to backfill missing columns into the model.
3. **`csv_schema.py`** (optional) — when `--data-dir` is given, sniffs per-column types from each CSV's header + first 200 rows and matches CSVs to model tables via four-tier name matching.
4. **`model.py`** — builds TMDL semantic model. Prefers `engine_schema` (`_build_from_engine_schema`) when present; falls back to loadmodel + `_prune_dangling_relationships` + `_infer_relationships_from_shared_fields`. When a CSV is matched to a table, the schema sniffed by `csv_schema` overrides loadmodel columns.
5. **`dax_translator.py`** — Qlik expression → DAX. Two-stage; see `dax-translator-architecture.md`.
6. **`report.py`** — each Qlik sheet → one PBI page; each cell → one PBI visual. Inline measures are synthesised into the model on demand so the chart has a real binding (column refs can't hold a `Sum(X)` expression). Page title falls back to the slug embedded in the sheet filename if `qMetaDef.title` is empty.
7. **`writer.py`** — drops the canonical PBIP layout. Copies any matched CSVs into `<name>.SemanticModel/data/`.
8. **`preflight.py`** — structural validator after write; warnings appended to `conversion_report.md`'s "Pre-flight" section.

The pipeline is single-pass with shared indexes (`field_table`, `measure_by_id`, the report's incremental name set) computed once and threaded through — see [performance-and-scalability.md](performance-and-scalability.md) for the data-flow and the I/O optimisations in `utils.py`.

## Logging

`_logging.get_logger("TAG")` returns a per-tag logger; output format is `[<TAG>] <message>` to stderr at INFO level. Tags in use: `PARSE`, `MODEL`, `DAX`, `REPORT`, `CONVERT`, `WRITE`, `FETCH`, `CSV-SCHEMA`, `ENGINE`.

Set `QLIK_LOG_LEVEL=DEBUG` (or `WARNING`) in the environment to change the global level. `_logging.configure_default()` is idempotent and is called from the CLI entry.

## Verification

```bash
python -m qlik_to_pbi --input ./output --output ./out                              # empty-stub smoke test
python -m qlik_to_pbi --input ./output --data-dir ./csv_exports --output ./out    # CSV-bound
python -m qlik_to_pbi --app <APP_ID> --fetch-data --output ./out                  # cloud end-to-end
python -m qlik_to_pbi --qvf-path "...MyApp.qvf" --fetch-via-engine --output ./out # offline E2E
```

PowerShell users: forward slashes in paths work; the above forms are portable.

Inspect generated TMDL:
```
type "out\<Name>.SemanticModel\definition\tables\<TableName>.tmdl"
type "out\<Name>.SemanticModel\definition\model.tmdl"
start "out\<Name>.pbip"
```

No automated tests. When Desktop rejects a generated report, it names the file path + property — start there and work backwards through these docs.

For `--fetch-data` failures, debug in isolation:
```bash
python -c "from pathlib import Path; from qlik_to_pbi.fetch_data import fetch_object_data; from qlik_to_pbi._logging import configure_default; configure_default(); fetch_object_data('<APP_ID>', Path('./output'), Path('./csv_exports'))"
```

The log will tell you whether the CLI was found, how many candidate objects were enumerated, and per-object what failed.

## Conversion report (`conversion_report.md`)

Sections emitted under `<OUT>/pbip/conversion_report.md`:
- **Summary** — table count, measures (translated vs stubbed), pages, visuals, variables, bookmarks, native aggregations, what-if params, script-derived partitions.
- **Pre-flight Warnings** — structural issues found after emit.
- **Visual Coverage** — per-PBI-type counts.
- **Script-derived Partitions** — table → source type → original Qlik source path.
- **What-If Parameters** — list of synthesised parameters.
- **Detailed Issues** by severity / component.
- **Measures Requiring Manual Review** — stubbed formulas with original Qlik expression preserved.

The report is purely diagnostic — the converter does not consume it.
