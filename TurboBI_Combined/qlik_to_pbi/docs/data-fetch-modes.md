# Data fetch modes and partition shapes

## Partition modes

| Mode | Trigger | Partition shape | When to use |
|---|---|---|---|
| Empty stub | default | `Table.FromRows({}, type table [...])` | Validate metadata; visuals load but render no data. |
| Pre-exported files | `--data-dir` | CSV → `Csv.Document(...) + PromoteHeaders + TransformColumnTypes`; **Parquet → single-step `Parquet.Document(File.Contents(...))`** | User already has CSVs/Parquet (e.g. from Qlik Hub or a prior run). Parquet is preferred when both a `.parquet` and `.csv` exist for a table. |
| Cloud fetch + bind | `--fetch-data` (with `--app`) | **Parquet by default**; `--csv` to opt out | One-shot: cloud CLI per data-bearing object. Limited to author's charts + per-chart row cap. |
| Offline Engine fetch | `--fetch-via-engine` (with `--qvf-path`) | **Parquet by default**; `--csv` to opt out | One-shot, offline, **whole-table**: open QVF in running Desktop via localhost WebSocket, synthesise one hypercube per loadmodel table, paginate all rows. Bypasses per-chart cap and cloud-tenant requirement. Best path at large row counts. |

**Default emit format = Parquet (2026-06).** Every fetch entry point defaults `emit_format="parquet"`; the CLI opts out with **`--csv`** (no more `--parquet` opt-in), and the web UI has no format choice. Parquet is typed/columnar (no sniff, no `TransformColumnTypes` cast, smaller, faster for 30M rows). If `pyarrow` is missing, all paths fall back to CSV with a warning — never fail. Parquet files use **zstd** and are written in **large row groups** (`ParquetStreamWriter` buffers pages → ~250k-row groups, env `QLIK_PARQUET_ROW_GROUP`) for fast PBI import.

**Column pruning (DEFAULT ON, 2026-06; opt out `--no-prune-columns`).** Extract only fields the model references, dropping unused source columns. Because the Engine caps `GetHyperCubeData` at 10k cells/call, fewer columns = more rows/call = fewer round-trips, plus smaller files and less VertiPaq memory. Keep-set = `field_usage.collect_keep_fields`: builds the model (+report) and harvests its ACTUAL references (relationship endpoints + measure-DAX columns + calc-column deps, mapped to raw `sourceColumn`) UNION the expression tokens from `collect_used_field_names`. `engine_fetch._prune_table_fields` keeps a field iff in that set OR a cross-table key, never empties a table; `_prune_dangling_relationships` is the final net. The model-derived keep-set is essential because table-qualified join keys (`HCO.HCO_ID` vs `HCP.HCO_ID`) share no name and aren't in any expression — a token-only scan would dangle them. **Validated on 10 real apps: ~57% of columns dropped, zero referenced-column loss** (permanent gate in `regression/column_pruning.py`). Applies to the Engine fetch paths (incl. cloud-context web UI); metadata-only `--input` builds are untouched. The web UI prunes automatically (no toggle).

**Whole-table vs per-visual.** The Engine paths (`--fetch-via-engine` and the cloud-context web-UI path, both via `fetch_via_engine`) extract **one file per loadmodel TABLE**, paginating every row — the "chunks" are just streaming a large table out of the bounded Engine API, NOT scraping visuals. The only **per-visual** extractor is the qlik-CLI `--fetch-data` path (`fetch_data.fetch_object_data`, `qlik app object layout`), which is the sole data path the plain CLI exposes without Engine-API access; it is per-chart and ~500-row capped. Prefer an Engine path (cloud context or Desktop) for complete data.

**Parallel Engine fetch + cell-based split (2026-06).** The Engine fetch is latency-bound (≤10k cells/call, ~0.7s round-trip), so a big table is split into N row-range slices fetched concurrently (`_extract_parallel`, one WebSocket per worker, part files merged by `_merge_parquet_parts`). **Worker count is a FLAT 8 for every cloud app** (`_auto_worker_count` → `min(cap, 8)`; cap 8 cloud / 2 Desktop, since a single localhost engine can't parallelize). Set 2026-06-04 from a benchmark on the 7.5M-row Hospital app — 6 workers → 182s, **8 → 128s**, 12 → 164s with more transient `code 15` contention — so 8 is the sweet spot and more only adds exclusive-`ClearAll` contention without speedup (correctness held at every count). It's size-independent now because the count-anchor removed the heavy over-the-wire fetch of big facts (they return distinct combos + a count, expanded locally), leaving per-table connect/cube overhead that 8 workers cover well regardless of app size. `QLIK_FETCH_WORKERS` overrides exactly (clamped to cap); to go above 8, raise `_CLOUD_WORKER_CAP` and the env together (the benchmark says it doesn't pay on these apps).

**Fetch resilience (2026-06) — never silently emit a partial/0-row table.** `_extract_parallel` delegates orchestration to `_run_units_resilient`: it runs all units in the worker pool, then **serially retries any that failed** (transient dead-WS / pod contention under high concurrency clears once there's no contention), and assembles results so a split table is written **only if all N row-slices are present** — otherwise it is a **hard failure** (no partial merge; part files cleaned; loud `ENGINE FETCH INCOMPLETE` ERROR naming the table). A whole table that was expected to have rows but produced no file is a hard failure too (previously it silently became an empty-stub → 0 rows in Power BI). `_validate_fetched_rows` then re-checks the **expected plan** against what landed (Parquet `num_rows` metadata) and ERRORs on any missing/short table. This was the fix for a heavy (forced-20-worker) run that returned "some tables had 0 rows / data missing": the failures came from sustained-load dead sockets, and the old code silently dropped the failed slices/tables. Lower `QLIK_FETCH_WORKERS` if you see many retries. **Source-level transient-abort retry (2026-06-04):** when parallel workers connect to the same cloud app session they each run the (mandatory) exclusive `Doc.ClearAll`, which can abort another worker's in-flight `CreateSessionObject`/`GetLayout` — `Engine error ... Request aborted (Exclusive/BeginExclusive ... family requests) (code 15)`. This is transient, so `_try_create_cube` now retries it in place (`_is_transient_abort`, `_CUBE_CREATE_ATTEMPTS=4`, 0.5s·attempt backoff) instead of mis-reporting the table as an empty hypercube and leaning on the heavier unit-level serial retry. Measured on Hospital: code-15 surfaced 5→1, "empty hypercube" false-alarms 3→0, fetch 259s→182s, still all 23 tables exact. **Parallelism note:** count-anchor changes *when* row-range splitting is needed — a big fact pruned to its key fetches only its DISTINCT combinations (e.g. `Order Procedure` ~51k) and expands locally, so it no longer needs slicing; splitting still triggers for tables with large distinct-combo counts (`SalesDetails` → 3 slices), and cross-table parallelism is unchanged (Hospital runs 6 workers).

**Count-anchor faithful extract (2026-06-04) — the hypercube must reproduce physical rows.** A dimension-only hypercube returns the DISTINCT combinations of its dimensions, so the per-table extract was wrong in BOTH directions on most tables of every tenant app (proven by the cross-app audit, qcy vs `qNoOfRows`): it **over-counts** (orphan members of a SHARED key come back as NULL-padded phantom rows — Demo/`Observations` 20,841 → 56,579, +171%) AND **under-counts** (exact-duplicate physical rows collapse into one distinct combo — Consumer/`ItemMaster` 7,479 → 2,295). Fix: `_select_table_anchors` picks a **table-scoped count anchor** per table = an *own-only* field (one that appears in exactly ONE engine table, via `_own_only_engine_names`) whose `Count([F])` over the whole app equals the physical `qNoOfRows`. The extract cube then carries `=Count([anchor])` + `qSuppressZero` (`_try_create_cube(anchor=)`), and `_stream_hypercube_rows` **expands each combination by its count** (`_count_from_measure_cell`): SuppressZero drops phantoms (count 0); expansion restores duplicates (count > 1); the total lands exactly on physical. The anchor MUST be own-only — a shared-key `Count(1)`/`Count([key])` is not table-scoped and bleeds across associations (the earlier-reverted bug: Revenue inherited the 6.8M `Order Procedure` count via their shared `PatientEncounterID`). **Best-effort fallback**: when no own field is non-null on every row (e.g. ZipData: one zip has null lat/long so every own field counts 109/110) AND the dim-only cube over-counts, the highest-count own field is used anyway — a count-anchor can never exceed physical, so it strips phantoms at the cost of ≤ the null rows (ZipData 270 → 109, vs +160 fabricated). Pure all-key **bridge tables** (no own field — Consumer/`linkBasket` 134,005 → 130,306) get no anchor → dim-only fallback + loud `_validate_fetched_rows` SHORT warning → remedy = lossless QVD upload. Count-anchor also makes **column pruning row-safe** (count is per kept-combo, expansion → physical regardless of kept columns), so `_guard_row_fidelity` now SKIPS anchored tables (no slow full-width restore). Opt out `QLIK_COUNT_ANCHOR=0`. **Live audit (all 5 tenant apps): Demo / Test / Asset Management / Hospital CLEAN (every table exact, incl. Hospital `Order Procedure` 6,829,836 and `Revenue` 262,683 in 4.3 min — was 25–45 min and wrong); only Consumer has 2 residual small under-counts (the bridge table + the null-coord zip), both flagged.** Pure building blocks gated by `regression/row_fidelity_anchor.py`; the live engine==parquet invariant by the cross-app audit harness.

**Engine-only-table reconciliation (2026-06-04) — every engine table is fetched.** The fetch plan was built from the LOADMODEL (`_extract_table_field_pairs`), which omits disconnected "island" tables; such a table was silently skipped and loaded as 0 rows / absent (the general case of "entire table not fetched" — e.g. a 3-row reference island). `_refresh_field_lists_from_engine` now reconciles the plan with `GetTablesAndKeys` (authoritative per CLAUDE.md): any engine table with fields not matched to a loadmodel table is ADDED to the plan (shared field-builder `_engine_fields_for_table`), with row counts. *(Distinct from a table Qlik's own script loads then DROPs — that one is genuinely absent from the engine model and correctly omitted; its fields live on the tables that survive.)*

**`_guard_row_fidelity` (column-pruning collapse fallback).** Still present for anchorless tables: after pruning it probes each pruned table's `qcy` (`GetLayout`, no data) and restores all columns when `_is_row_collapse` (qcy < physical). Anchored tables skip it. Opt out `QLIK_PRUNE_ROW_GUARD=0`.

**Live database source (2026-06-04) — repoint DB-loaded tables at the source, not the Qlik snapshot.** A Qlik app often loads tables straight from a SQL connection (`LIB CONNECT TO [Databricks];` then `SQL SELECT ... FROM catalog.schema.table;`). The converter used to bind the engine-LOADED snapshot for those too; now, when the user supplies the connection's details, those tables Import live from the source. Flow: `script_parser.parse_db_sources` detects them (connection name + catalog/schema/source-table + raw SQL columns, RENAME-resolved) → `parser` stashes `ir["db_sources"]` → `model._attach_db_source_connections` (gated on user-supplied `db_connections`, keyed by connection name) attaches a `connection` dict and reconciles columns (the source returns RAW names; the Qlik LOAD layer renamed some, so `Table.RenameColumns` maps raw→model, and Qlik-COMPUTED columns the source can't deliver — e.g. an APPLYMAP geopoint — are dropped) → `partition_m.render_partition_m` emits the connector M in **`import`** mode (user choice; the faithful analog of the in-memory app). The DB tables' DATA fetch is SKIPPED but their engine SCHEMA is still captured (`fetch_via_engine(data_skip_tables=…, unbuild_dir=…)` writes the sidecar) so keys + relationships build correctly (verified: DB_test → 5 Databricks Import partitions, 4 qk relationships intact, preflight clean). Secrets are never embedded — the user signs in once in PBI Desktop. CLI: `--db-connections <json>` (name→{class,server,http_path,catalog,schema}); web UI: a "Database connection" panel (auto-shown via `GET /db-connections?app_id=` which `GetScript`-detects the connections + the fields each connector needs) carried on `/convert` as `db_connections`. `_infer_db_class` maps a connection name→connector when class is unset. Gated by `regression/db_source_partition.py`.

The split TRIGGER is **cell-based** (`_plan_split_n`, pure + unit-tested): a table splits when `rows·cols > QLIK_RANGE_SPLIT_MIN_CELLS` (default 1.2M), `N = min(workers, ceil(cells / QLIK_RANGE_CHUNK_CELLS))` (default chunk 1.0M). A row-count gate (the old 300k) missed wide-but-short dominant tables — e.g. a 138k-row × 49-col table is 6.8M cells (often ~80% of an app's whole extract) yet under 300k rows, so it never split and one worker ran it serially while the pool only parallelised the small tables. See [`large-data-strategy.md`](large-data-strategy.md) Phase 0.6.

**IPv4-preferred engine connect (2026-06-04) — fixes chronic flaky/slow connects.** Qlik Cloud tenant hosts publish both A (IPv4) and AAAA (IPv6) DNS records. On a machine with a dead IPv6 route, `websocket-client` tries the IPv6 addresses first and burns the full ~21s TCP connect timeout per address, while REST (`requests`) picks a reachable address so the API key authenticates fine — the connect just "can't reach" / times out intermittently (`getaddrinfo failed`, `ConnectionReset 10054`, slow fetches, "8 slices didn't help", multi-minute connects). `_install_ipv4_preference()` (runs once at import) wraps `socket.getaddrinfo` to sort IPv4 ahead of IPv6 for `*.qlikcloud.com` only (IPv6 kept as fallback). Measured: Hospital connect 21s-timeout → 0.8s. Opt out `QLIK_PREFER_IPV4=0`.

**Uploaded source QVD (fast-path, 2026-06).** When the user has the app's source `.qvd` files, `qvd_ingest.py` transcodes them to typed Parquet **locally** (pyqvd → pyarrow; disk-speed, no Engine round-trips, and *better* typing — leading-zero codes stay string with no `auto` guesswork). `cloud.convert_from_cloud(prefetched_data_dir=…)` (CLI `--prefetched-data-dir`) stages them into `data/` (`_seed_prefetched`) and the Engine fetch **skips** the tables they cover (`fetch_via_engine(skip_tables=…)`); the rest still fetch from the engine. Exposed in the web UI as an optional per-table mapping panel ([`web-ui.md`](web-ui.md)). This is [`large-data-strategy.md`](large-data-strategy.md) Phase 1 — the real step-change for 30M-row apps.

Empty-stub MUST use `Table.FromRows({}, type table [...])` — `Table.FromRecords({[col=null,...]})` is rejected by PBI Desktop with an opaque "Failed to load file" because Power Query can't infer types from null-only records.

**Parquet partition** (`--parquet`, or any `.parquet` in `--data-dir`): a **single step**, no PromoteHeaders, no `TransformColumnTypes` — Parquet carries its column types in the file schema, so `model._columns_for_table` reads them directly (`parquet_io.sniff_parquet_schema`) and the partition is just `Source = Parquet.Document(File.Contents(RepoPath & "/data/<T>.parquet"))`. `File.Contents` on a local path is random-access (no "streamed binary values" error, no size limit beyond Import's model cap); `Parquet.Document` options are left unset (TypeMapping = null) for max type fidelity. This removes the CSV sniff/cast fragility and is the recommended path for large/30M-row data — see [`large-data-strategy.md`](large-data-strategy.md). Needs `pyarrow`; both fetch paths fall back to CSV with a warning if it's missing.

**Engine Parquet column kinds** (`engine_fetch._field_kind_from_tags`): the declared kind for each column comes from the field's engine `qTags` — `$timestamp`→datetime, `$date`→date, `$integer`→int, `$numeric`→double, `$text`/`$ascii`→string (preserve zero-padded codes). An **untagged** field → **`auto`**: `ParquetStreamWriter` value-types it from the clean `qNum`-derived cells (int/double, or string with a leading-zero guard so `00123` codes survive), inferring from the first buffered row group. *Why this matters for KPIs:* the untagged default used to be `string`, so a numeric column the schema didn't tag was stored as **text** — and a measure `SUM(table[col])` over a text column makes a Power BI card render its value in **quotes** (`'37K'`, `'10.6%'`). Critically, an app fetched **without a `GetTablesAndKeys` schema has NO tags at all**, so *every* numeric column would otherwise default to text and every card would quote its value. `auto` makes untagged numeric columns come through as numbers; dates still need the explicit tag (`auto` never date-promotes). Gated by `regression/parquet_emit.py`.

## Object enumeration (cloud fetch, `fetch_data.py`)

`enumerate_data_objects(qlik_input_dir, object_types=None)` walks the local `objects/` folder (no engine round-trip) and picks out objects that carry a hypercube. The whitelist tuple `DEFAULT_DATA_OBJECT_TYPES`:

```python
DEFAULT_DATA_OBJECT_TYPES = (
    "masterobject",
    "table", "pivot-table", "sn-pivot-table",
    "auto-chart", "barchart", "linechart", "piechart",
    "treemap", "scatterplot", "combochart",
    "kpi", "sn-kpi",
    "histogram", "boxplot", "waterfallchart",
)
```

Anything not on this list is skipped (text-image, action-button, container, filterpane, listbox, appprops, loadmodel, plus any new Qlik visual types not yet whitelisted). When debugging "why isn't my chart getting fetched", check this tuple — the skip list is the COMPLEMENT, not an explicit deny list, so new visual types are silently excluded by default.

For each match, the function returns `{id, type, display_name, parent_sheet}`. For sheet cells, `display_name` is `"<sheet title> - <chart title>"`. For top-level master objects, it's `qMetaDef.title`.

## Per-object export

`_export_object_to_csv(app_id, object_id, out_file, qlik_command)` shells out to:

```
qlik app object layout <id> --app <app> --json
```

Captures stdout, parses `qHyperCube` JSON, writes CSV.

- `subprocess.run(..., shell=False)` so parent PATH is honoured (cmd.exe has different PATH semantics than PowerShell/bash).
- Binary resolved up-front via `shutil.which`, which honours `PATHEXT` so a bare `"qlik"` finds `qlik.exe` / `qlik.bat`.

**Why `layout` not `data`:** The CLI's `qlik app object data <id>` emits space-padded fixed-width text for terminal viewing — not parseable when values contain spaces (`"CLEVELAND CLINIC"`). Also returns exit 255 on success, so naive exit-code checks treat it as failure.

`layout` returns the full JSON layout including `qHyperCube.qDataPages[].qMatrix[]`. Each cell is `{qText, qNum, qElemNumber, qState, qIsNull}`. Cell-value rule (`_csv_cell_value`, mirrors `engine_fetch._cell_value`):

- **Measures** → `qNum` (clean number; falls back to `qText` for string-aggregation measures / NaN).
- **Numeric dimensions** → `qNum` too. A dimension counts as numeric via `_dim_is_numeric(qDimensionInfo[i])`: its `qTags` carry `$numeric`/`$integer` (and NOT `$date`/`$timestamp`/`$text`), or its `qNumFormat.qType` is `I`/`R`/`F`/`M`. **Why this matters for data types:** previously *all* dimensions used `qText`, so a Qlik-numeric field formatted `1,234.50` / `$1,000` was written with separators → the CSV sniffer typed it `string` → the field loaded as text in PBI (couldn't aggregate, mismatched the Qlik source) and any numeric cast failed. Emitting `qNum` for numeric dimensions makes them round-trip as numbers and matches Qlik's type. A numeric-dim cell with no finite `qNum` is written empty (keeps the column uniformly numeric/castable).
- **Date / text dimensions** → `qText` (formatted date for the sniffer to detect; text preserved, so zero-padded codes like `00123` keep their leading zeros — `_dim_is_numeric` deliberately returns False for `$text`).

**Row-limit caveat:** `layout` only returns the object's initial data fetch page (typically 500 rows). Charts beyond that are truncated. Fetching more requires WebSocket `GetHyperCubeData` which the CLI doesn't expose. Workarounds: bump source object's `qInitialDataFetch.qHeight` in Qlik before unbuild, or switch to `qlik raw` against the REST data-export endpoint. Currently we accept truncation and log the row count.

## Offline Engine fetch (`engine_fetch.py`, Desktop)

WebSocket: `ws://localhost:4848/app/<URL-encoded-absolute-qvf-path>`. App handle = 1 for path-style Desktop connections (Desktop-specific shortcut). **Cloud is NOT** — see `qlik-engine-cloud-handle.md`.

JSON-RPC shape:
```python
{"jsonrpc":"2.0","id":<n>,"method":"<Name>","handle":<h>,"params":[...]}
```

The engine occasionally pushes change notifications mid-call (`qInvalidated`, `OnAuthenticationInformation`, etc.) — the client loops on `recv()` and discards anything whose `id` doesn't match the current request.

### Extraction loop, per loadmodel table

1. **Per-field probe** (`EngineClient.resolve_fields`) — loadmodel inconsistency means `[X] AS [Y]` lands on `name` vs `alias` randomly. Enumerate every plausible spelling (alias, name, dotted-id leaf, bracket-wrapped variants for names with spaces/dots/hyphens). Use whichever the engine accepts. Fields where no candidate resolves (e.g. a key the load script silently renamed to `[Tbl1.X-Tbl2.X]`) are dropped with an `[ENGINE]` warning. Without this step the engine silently removes failing dimensions from the cube, so the CSV row width ends up smaller than the declared header — every column from the first failure onward gets misaligned by one position.

2. `CreateSessionObject` with:
   ```json
   {"qInfo": {"qType": "qlik2pbi-extract"},
    "qHyperCubeDef": {
      "qDimensions": [{"qDef":{"qFieldDefs":[<engine_name>]},
                       "qNullSuppression": false}, ...],
      "qMeasures": [],
      "qInitialDataFetch": [{"qLeft":0,"qTop":0,"qWidth":N,"qHeight":1}],
      "qSuppressZero": false,
      "qSuppressMissing": false}}
   ```

3. `GetLayout` to read `qHyperCube.qSize.qcy` (rows) and `qcx` (cols). `qcx` should equal `len(resolved_fields)`.

4. Loop `GetHyperCubeData` with `NxPage` rectangles of `min(3000, 10000 // qcx)` rows until `qTop >= qcy`.

`qNullSuppression: false` is critical — default is to skip rows where all measures are null, but we have no measures so a suppressed cube returns *zero rows* for dim-only tables. Same reason `qSuppressZero` and `qSuppressMissing` are explicitly false.

### `Doc.ClearAll` on every connect (selection-state isolation)

`EngineClient.connect()` calls `Doc.ClearAll(qLockedAlso=true, qStateName="")` on every code path (cloud, Desktop OpenDoc, Desktop path-style fallback, mid-extract reconnect) once `app_handle` is resolved. Without this, the engine restores whatever selection state the QVF was saved with — ALWAYS including any active bookmark. Subsequent `GetHyperCubeData` returns only the rows passing that saved filter, so the CSV has partial data.

**Why this matters for bookmarks specifically:** PBI bookmarks are visual-layer state, not data-layer filters. They capture "selections + view state" on top of an unfiltered model. If the data extract is already restricted at the source level, the "no selections" bookmark has nothing to show — it gets the same filtered slice as every other bookmark. Clearing selections at connect time lets each PBI bookmark layer its OWN filter on top of a complete dataset.

`ClearAll` failures are non-fatal — older engines or restricted apps may reject the call; extract continues but bookmark isolation is lost.

### Why hypercube and not `Doc.GetTableData`

`GetTableData(qOffset, qRows, qSyntheticMode, qTableName)` looks cleaner (returns source rows directly, no associative cross-product). **We tried it and reverted.**

Problem: `GetTableData` returns each row's `qValue[]` in the engine's internal **physical table-layout order** (post-JOIN column layout), while `GetTablesAndKeys` reports `qFields` in the engine's **logical field-list order**. The two disagree for any table built from a JOIN — the column labelled `HCP.ZIP` in our header ended up carrying an HCP_ID value, and so on.

Hypercube has caller-controlled column ordering by construction: dimensions are submitted in our `resolved` order; each row's cells come back in that same order. Header and data align without projection logic.

The "extra rows" complaint that originally motivated `GetTableData` is a non-issue for single-table extracts — Qlik's hypercube on N dims of one source table returns exactly that table's rows (no associative cross-product because all dims share the same row).

### Cell value rule (`_cell_value`)

Qlik matrix cells are dual values (`{qText, qNum, qElemNumber}`). Picker:
- Use `qNum` if finite (avoids locale-formatted display strings like `"1,234.56"` for what is really `1234.56`).
- Strip trailing `.0` for integer-valued floats so the downstream `csv_schema` sniffer classifies them as `int64`.
- Fall back to `qText` for text fields (engine signals "not numeric" by setting `qNum` to NaN).

### CSV naming = exact-match binding

Each CSV written as `<tableAlias>.csv`. First tier of `csv_schema.match_csv_for_table` is exact case-insensitive stem match, so these bind with zero manual renaming — the gap the cloud `--fetch-data` flow hits is closed.

### Failure modes

- **Desktop not running**: `ConnectionRefusedError`. Logged with clear hint; converter continues with empty-stub partitions.
- **QVF path not readable by Desktop**: engine returns `App not found` / `Access denied`. Logged per-table.
- **`websocket-client` not installed**: detected at module import; logs `pip install websocket-client` hint.
- **Stale loadmodel**: some fields don't resolve; engine returns empty hypercube; logged and skipped.

### Dependencies

- Qlik Sense Desktop installed and **running** (starts the engine on `localhost:4848` on launch).
- `pip install websocket-client` (the sync `websocket` Python package, **not** `websockets`).

### Privacy

Connection is `ws://` (no TLS, but localhost-only). Desktop runs under current OS user; Qlik permissions match file permissions. Only network call is `127.0.0.1:4848`.

## CSV → table matching tiers (`csv_schema.match_csv_for_table`)

First hit wins:

1. **Exact**: case-insensitive stem (`HCP.csv` → `HCP`).
2. **Normalised**: strip spaces / underscores / hyphens / dots, then compare (`Referral_Edge.csv` → `Referral Edge`).
3. **`Extract.<table>` prefix**: matches federated-data naming from the Tableau-converted PBIP reference (`Extract.HCP.csv` → `HCP`).
4. **Start-anchored prefix**: table name appears at the **start** of the CSV stem (after normalisation), or CSV stem is a prefix of the table name. Tie-broken by shortest stem.

Tier 4 is start-anchored, NOT arbitrary-position substring, because real fetched CSVs carry chart-object titles like `"Overview - HCO by HCP Count.csv"`. An anywhere-substring rule would bind BOTH the HCO and HCP tables to that one file. Start-anchored means `"Customers Master.csv"` still maps to `Customers`, but `"Overview - HCO by HCP Count.csv"` only maps to a hypothetical table whose name appears at position 0.

## Column type fidelity (`csv_schema.sniff_csv_schema`)

The TMDL column `dataType` and the partition's `Table.TransformColumnTypes` cast are derived per column with this precedence: **engine `qTags`** (`type_from_qlik_tags`, authoritative — only present on the `--fetch-via-engine` path's `engine-schema.json`) → **CSV content sniff** → **all-string fallback**.

The sniff **streams the WHOLE file** (`_KindAccumulator` per column; `sample_rows=None` default). It used to sample only the first 200 rows — which typed a column from clean head rows and then failed the partition cast at refresh (*"We couldn't convert to Number/Date. Details: …"*) on a later non-conforming value. Scanning every row makes the declared type provably compatible with every row PBI loads: any cell that doesn't fit latches the column to `string` (safe; the cast can't fail). Streaming keeps memory flat regardless of file size.

This pairs with the fetch-side fix that writes numeric dimensions from `qNum` (above): the converter never *strips* formatting from a fetched CSV (it can't know `1,234.50` is numeric once written with a comma), so correctness depends on the fetch writing clean numbers in the first place. For `--data-dir` (user-supplied CSVs), a numeric column already containing thousands separators will sniff as `string` by design — re-export it without separators (or rely on `--fetch-data`/`--fetch-via-engine`, which write clean) to get a numeric type.

## Cloud Engine API (`cloud.py`, server-friendly)

`wss://<tenant>/app/<app-id>` with `Authorization: Bearer`. All three cloud flags required together; partial credentials rejected at parse time.

API key sent only as Bearer header. Never logged. Tenant URL redacted in logs (`user:pass@` masked) for defence-in-depth. Data flows directly between your machine and your Qlik tenant.

Handle resolution is the gotcha — see `qlik-engine-cloud-handle.md`.

### Saved qlik CLI credentials

If you've already run `qlik context create` or `qlik app unbuild`, tenant + bearer live in `~/.qlik/contexts.yml`. Pass `--use-qlik-context` to read from there:
```bash
python -m qlik_to_pbi --use-qlik-context my-qlik-cloud --cloud-app-id <uuid> --output ./out
```

Loader: `qlik_context.py`. Parses YAML via PyYAML, strips `Bearer ` prefix, returns `QlikContext{name, tenant, api_key}`. API key never reaches a log — only the context name is printed.

### Python API (backend integration)

```python
from qlik_to_pbi.cloud import convert_from_cloud
result = convert_from_cloud(
    tenant="https://acme.us.qlikcloud.com",
    api_key="<bearer-token>",
    app_id="3a4b...",
    output_dir="/var/jobs/abc/out",
    fetch_data=True,
)
# result.pbip_path, result.report_path, result.csv_paths, result.stats
```

Designed for stateless concurrent use — each call opens a fresh WS, authenticates, runs the pipeline, tears down. Safe from multiple workers as long as each uses its own `output_dir`.

## Direct QVF parsing (`qvf_direct.py`)

Primary path for `--qvf-path`. Reads `.qvf` directly — no cloud tenant, no Desktop, no engine. Auto-detects on-disk format:

- **ZIP** — Qlik Sense Cloud / Server exports. JSON entries read straight out.
- **SQLite** — Desktop's on-disk format. Sheet / object / script blobs in SQLite tables; opened as `sqlite3`.
- **Qlik proprietary binary container** (header `FF FF 01 00`) — zlib decompress at every plausible block offset, then union-merge JSON anchored at every `"qType"` marker across UTF-8 / UTF-16-LE / UTF-16-BE / latin-1.
- **Gzip-wrapped** content.
- **Raw bytes JSON scan** — last resort.

After parsing, `qvf_to_unbuild_dir(qvf_path, output_dir)` writes the same JSON folder layout `qlik app unbuild` produces, so the existing parser handles it unchanged.

When direct parse fails (rare), CLI falls back to the Engine API unbuild.

### Diagnostic

```bash
python -m qlik_to_pbi.diagnose path/to/app.qvf
```

Prints what the direct parser saw inside the QVF: sheet count + titles + cell counts, object / measure / dimension / variable counts, first few raw bodies. Run this first when conversion output looks wrong — if a sheet doesn't show here, it's a parser bug; if it does show but doesn't reach the PBIP, it's downstream.

## Offline Engine unbuild (`engine_unbuild.py`)

Counterpart to cloud `qlik app unbuild`, talks to Desktop. Same JSON layout output. When `--qvf-path` is given without `--input` or `--app`, the converter auto-runs `unbuild_via_engine` to a temp folder (or `--keep-unbuild` if specified).

| File | Engine API call(s) |
|---|---|
| `app-properties.json` | `GetAppProperties` (+ `GetAppLayout` for qTitle fallback) |
| `script.qvs` | `GetScript` |
| `dimensions.json` | session-list `qDimensionListDef` → per-id `GetDimension` + `GetProperties` |
| `measures.json` | session-list `qMeasureListDef` → per-id `GetMeasure` + `GetProperties` |
| `variables.json` | session-list `qVariableListDef` (defs inline) → fallback `GetVariableById` |
| `objects/sheet--<slug>-<qid>.json` | session-list `qAppObjectListDef` qType=sheet → per-id `GetObject` + `GetFullPropertyTree` |
| `objects/masterobject-<slug>-<qid>.json` | same with qType=masterobject |
| `objects/loadmodel---loadmodel.json` | `GetObject("LoadModel")` → `GetLayout`; falls back to a synthesised stand-in from `GetTablesAndKeys` |
| `bookmarks.json` | session-list `qBookmarkListDef` → per-id `GetBookmark` + `GetProperties` |

**Why `GetFullPropertyTree` not `GetProperties`:** sheets are recursive — a cell can be a container holding more cells. `GetProperties` returns only the top level. `GetFullPropertyTree` returns the entire `{qProperty, qChildren[{qProperty, qChildren...}]}` tree, byte-for-byte the shape `parser.py` already walks for cloud-unbuild output.

### Loadmodel fallback

Some Desktop versions don't expose `LoadModel` as a named generic object. Fallback synthesises a minimal loadmodel from `GetTablesAndKeys`:
```python
{"tables": [
    {"id": "dsd.<TableAlias>",
     "tableAlias": "<TableAlias>",
     "tableName":  "<TableAlias>",
     "fields": [{"id": "...", "name": "...", "alias": "..."}, ...]},
], "queries": [], "associations": {}}
```

`queries` and `associations` are intentionally empty — converter skips relationship synthesis. Tables and fields still show up so visuals bind. Inference fallback recovers joins from shared field names.

### Bookmark fetch (`_write_bookmarks`)

**The cloud `qlik app unbuild` CLI does NOT export bookmarks at all** — not in JSON output, parser's `_read_json(root / "bookmarks.json", default=[])` silently returns empty list, writer emits no PBI bookmark scaffolds.

`_enumerate_app_lists` declares `BookmarkList` session object via `CreateSessionObject` + `qBookmarkListDef` (`_BOOKMARK_LIST_DEF` at module top). The listdef's `qData` paths copy `qMetaDef.title` / `qMetaDef.description` so surface metadata is enough to scaffold a PBI bookmark without per-item GetProperties.

`_write_bookmarks` iterates each enumerated bookmark and calls `GetBookmark(qid) → GetProperties(handle)` for the full property bag, writing:
```json
[{"qInfo":     {"qId": "<uuid>", "qType": "bookmark"},
  "qMetaDef":  {"title": "<title>", "description": "..."},
  "qBookmark": {<engine selection state, preserved verbatim>}}, ...]
```

Parser picks this up just like a cloud unbuild would have. Writer emits one PBI bookmark scaffold per entry. **Selection state preserved in `qBookmark` but not translated** — Qlik's field IDs don't map 1:1 to PBI filter state, so user finishes selections in Desktop's bookmark pane.

Engines without `BookmarkList` listdef support degrade silently (empty list, no error). Same for individual `GetBookmark` failures — falls back to session-list-item surface so at least the title shows up downstream.
