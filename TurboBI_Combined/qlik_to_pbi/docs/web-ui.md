# Web UI (Flask)

Browser-based wrapper around `python -m qlik_to_pbi`. Files:

- `qlik_to_pbi/app.py` — Flask server + per-job background worker.
- `qlik_to_pbi/templates/index.html` — single-page UI (theme toggle, App-ID input, progress steps, live log, download button). **Two** option pills inside the App-ID card on the title row: a **"Fetch data"** switch and a **"Prune columns"** switch (both on by default). Fetched data is emitted as **typed Parquet by default** (converter-side), so there is no format choice in the UI. Below the App-ID box, an **extract-time estimate line** (a range, prune-aware) appears once a valid App ID is entered with Fetch on (cloud-context only). Below that, an optional **"Map source QVD files"** panel lets the user upload the app's source `.qvd` files per table for a local fast-path (see *QVD upload fast-path* below).

## Launch

```
python -m qlik_to_pbi.app                    # http://0.0.0.0:5000
python -m qlik_to_pbi.app --port 8080        # custom port
python -m qlik_to_pbi.app --host 127.0.0.1   # local-only
```

Dependency: `pip install Flask`. The websocket-client dep used by the cloud-mode subprocess is already required by the converter.

## Unbuild-mode auto-selection

`_run_job` picks the subprocess command per job:

| Condition | Subprocess command |
|---|---|
| `~/.qlik/contexts.yml` has a usable context (`load_qlik_context()` succeeds) | `python -m qlik_to_pbi --use-qlik-context --cloud-app-id <id> --output …` |
| No context, but the `qlik` CLI binary resolves (`resolve_qlik_command()`) | `python -m qlik_to_pbi --app <id> --output … --qlik-cmd qlik [--fetch-data]` |
| Neither | Job fails as an **api**-category error telling the user to install the qlik CLI or run `qlik context create`. |

So **the UI works without a local qlik.exe** as long as the user has a cloud context — the realistic case on most dev machines.

Two toggles, compact pills inside the App-ID card on the title row (the UI POSTs `{app_id, fetch_data, prune_columns}`; `qlik_cmd` defaults to `"qlik"` server-side for the CLI-fallback path):

**"Fetch data"** (on by default):
- **Cloud-context mode** (primary): `fetch_data=True` extracts **whole tables** (one Parquet file per loadmodel table) through the Engine API via `convert_from_cloud` → `fetch_via_engine`; unticking forces metadata-only by passing `--data-dir <empty>` so the converter binds empty-stub partitions.
- **CLI-fallback mode** (only when no cloud context exists): appends `--fetch-data`, which uses the qlik CLI's per-object `layout` (the one path the plain CLI exposes — per-chart, ~500-row capped).

**"Prune columns"** (on by default): keeps only the columns the model references (smaller/faster). Unticking appends **`--no-prune-columns`** (only when fetching), so every source column is pulled and each table keeps its full row-level grain. Pruning a table down to few columns + the distinct-combination hypercube extract collapses a fact table's rows (a 6.8M-row table pruned to one key column loads as ~50k distinct keys) — turning pruning OFF restores both the columns and the row count. The pill is visually disabled when Fetch data is off (pruning is moot with no fetch).

**Extract-time estimate line** (below the App-ID box, cloud-context only): on a valid App ID + Fetch on, the SPA calls `GET /estimate?app_id=…&prune=<0|1>`, which reads the app's `…/api/v1/apps/<id>/data/metadata` (a lightweight REST call — no engine WebSocket, no row data) and returns `{tables, rows, bytes, est_full_seconds, est_low_seconds, est_high_seconds, pruned}`. The shown time is a **range** (`fmtDur2(est_low, est_high)`) that reflects what actually runs: **pruning** (`prune=1` ⇒ ~0.6× cells, the default) and the **parallel** fetch (best/worst effective workers, `_PARALLEL_BEST=8` / `_PARALLEL_WORST=2`). A single number was misleading — the old `est_full_seconds` (full, unpruned, serial) over-stated the real time several-fold because it ignored both pruning and parallelism (this was the "estimate seems incorrect" report). With pruning **off** the line turns into a **warning** variant. The estimate is keyed on `app_id|prune` client-side so flipping the prune pill re-fetches. The API key is used only as a request header and is never returned to the client.

## QVD upload fast-path

The Engine fetch is latency-bound (≤10k cells/call, ~0.7s round-trip). When the user **has the source QVDs**, reading them locally is the real step-change — disk-speed columnar read, no engine ceiling (`docs/large-data-strategy.md` Phase 1). The optional **"Map source QVD files"** panel (below the estimate, shown once a valid App ID is entered) implements it:

- On first expand, the SPA calls `GET /tables?app_id=…` → `{ok, tables:[{name,rows,cols}], qvd_supported}` (same cheap `data/metadata` call, sorted biggest-first; big tables — `rows·cols > 1.2M`, matching the server split threshold — are flagged "slow via engine — map this one"). `qvd_supported=false` (pyqvd/pyarrow absent server-side) hides the upload and the panel says so.
- Each row has a `.qvd` file picker. Picking a file stores it client-side keyed by table name; the panel is **fully optional** — map none, some, or all.
- On submit, if any QVDs are mapped the SPA POSTs **multipart** `/convert` (each file under key `qvdfile:<TableName>`); otherwise it POSTs JSON as before. `_run_job` saves the uploads, transcodes them via `qvd_ingest.transcode_qvd_map` (streaming `[QVD]` progress lines), and appends **`--prefetched-data-dir <dir>`** so `convert_from_cloud` stages them into `data/` and the Engine fetch **skips** those tables. Tables left unmapped still come from the engine (Fetch on) or stay empty stubs (Fetch off). A transcode failure for one file is logged and that table falls back to the engine — never fatal.

**Data format = typed Parquet, by default, no UI choice.** The converter emits Parquet from every fetch path (schema-typed, no cast, faster for large data — see `large-data-strategy.md`); `_run_job` passes **no format flag** because Parquet is the converter default. If `pyarrow` is missing server-side it degrades to CSV with a log warning. (CLI users can force CSV with `--csv`; the UI doesn't expose it.) **Memory safety on a full (unpruned) fetch:** the extract streams page-by-page and `ParquetStreamWriter` caps each row group by a **cell budget** (`QLIK_PARQUET_CELL_BUDGET`, default 4M cells), so a wide table can't balloon the in-memory buffer regardless of width.

## Routes

| Route | Purpose |
|---|---|
| `GET /` | Render the SPA (`templates/index.html`). |
| `POST /convert` | JSON `{app_id, fetch_data, prune_columns}` **or** multipart (same scalars as form fields + one `qvdfile:<Table>` per uploaded QVD) → spawns a job, returns `{job_id}`. (Data is always emitted as Parquet; no format field. `prune_columns=false` → `--no-prune-columns`; mapped QVDs → `--prefetched-data-dir`.) |
| `GET /estimate?app_id=<id>&prune=<0\|1>` | `{ok, tables, rows, bytes, est_full_seconds, est_low_seconds, est_high_seconds, pruned}` from the app's `data/metadata` REST endpoint — feeds the extract-time range. `{ok:false}` (HTTP 200) when no cloud context (so the SPA just hides the line). Validates the App ID is a UUID first. |
| `GET /tables?app_id=<id>` | `{ok, tables:[{name,rows,cols}], qvd_supported}` from `data/metadata` — feeds the QVD-mapping panel (one row per table). `qvd_supported` = `qvd_ingest.qvd_available()` (pyqvd + pyarrow present). |
| `GET /stream/<job_id>` | Server-Sent Events: each converter stdout/stderr line is one `data:` event; a terminal `event: done` carries the final status. |
| `GET /status/<job_id>` | `{status, zip_name, error, error_category, error_title, error_detail}`. Polled by the SPA after the SSE channel closes. |
| `GET /log/<job_id>` | `{lines:[...]}` — the full captured raw output, so the error stage always has it even if the live stream dropped. |
| `GET /download/<job_id>` | Zip of the whole output tree. 404 until the job has zipped. |

## Per-job lifecycle

```
POST /convert
  -> _JOBS[id] created
  -> Thread(_run_job)
       runs the subprocess, streams every line through _push_log
       on success: zip the whole OUT tree into <work_dir>/qlik_<short>_pbip.zip
       on failure: _classify_failure(log_lines) -> status="error" + category/title/detail
       in either case: Timer(_JOB_TTL_SECONDS=1800) cleans up work_dir
```

**No TMDL post-processing.** The Flask UI runs on the user's machine, so the converter bakes a valid absolute `RepoPath` for THIS machine and opening the PBIP from the build location just works. (An earlier inline gzip+base64 CSV-embed / `setup.bat` RepoPath-fixer experiment was explored and reverted — the zip simply ships the whole tree instead; see "Zip contents".)

## Failure categorisation

So a failed conversion shows a real, actionable error rather than a generic "check raw output". On failure `_run_job` calls `_classify_failure(log_lines)` → one of five buckets, surfaced via `/status`:

| Category | Title shown | Typical trigger |
|---|---|---|
| `api` | Qlik connection / API error | auth/connection — key missing/wrong/expired, app not found |
| `semantic_model` | Semantic model creation failed | model build (tables / relationships / measures / DAX) |
| `data_source` | Data source issue | data fetch / CSV binding / partition build |
| `visual` | Visual conversion issue | sheet / visual / page build |
| `technical` | Technical failure | anything else (catch-all) |

Decision order in `app.py`: scan the **error region** only (the Python traceback if present, else error-keyword lines — benign `[CONVERT]/[MODEL]` progress markers are stripped so their text doesn't trip a signature) → (1) auth signatures first, (2) deepest in-package traceback frame (`_TB_MODULE_CATEGORY`: `qlik_context`/`cloud`→api, `model`→semantic_model, `report`→visual, `fetch_data`/`csv_schema`/`partition_m`→data_source), (3) keyword signatures, (4) stage-progress.

The error stage in `index.html` shows the category title + message + an **always-present "View raw output" toggle** (auto-expanded only for `technical`/unexpected; collapsed for the four known categories), fed by a JS `rawLines[]` accumulator with the `/log` endpoint as fallback.

## Zip contents

`_zip_directory(output_dir, zip_path, arcname_root=f"qlik_{short_id}")` zips the **entire output tree**, not just the pbip:

```
qlik_<short>/
  pbip/      <Name>.pbip + <Name>.Report/ + <Name>.SemanticModel/{data,definition,...}
  data/      cloud-fetched CSVs (or .parquet when Parquet is on; only when fetch_data=True)
  unbuilt/   JSON IR (sheets, dimensions, measures, variables, bookmarks)
```

Shipping the full tree means: (a) the inner `<Name>.SemanticModel/data/` is intact for the default `RepoPath`, (b) the outer `data/` is a fallback for users who repoint elsewhere, and (c) the JSON IR lets them re-run the converter offline with `--input`.

## Known caveats

- The Flask dev server is single-threaded per request, but `_run_job` runs in its own thread, so concurrent jobs work as long as each gets its own UUID-keyed `work_dir`.
- `_JOB_TTL_SECONDS=1800` (30 min) — after that the worker dir + zip are wiped. Long-lived downloads need a different store.
- 401 from `wss://...qlikcloud.com/app/...` during the Engine-API connect = expired bearer token in `~/.qlik/contexts.yml`; re-run `qlik context create` or refresh the API key (surfaces as the **api** category).
