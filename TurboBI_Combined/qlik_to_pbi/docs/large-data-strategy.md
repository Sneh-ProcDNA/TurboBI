# Large-data strategy: beyond per-chart CSV (QVD / Parquet / Import vs DirectQuery)

> Status: **Phase 0 + Phase 1 IMPLEMENTED (2026-06)** — typed Parquet is the
> **default** emit format from every fetch path, bound with a single-step
> `Parquet.Document` partition (no cast). The CLI opts out with `--csv`; the web
> UI has no format choice. **Phase 1 (direct QVD→Parquet) is now LIVE** as an
> *upload* path: `qvd_ingest.py` transcodes user-supplied source QVDs to typed
> Parquet locally (pyqvd → pyarrow), and the cloud fetch **skips** the tables a
> QVD covers (`cloud._seed_prefetched` + `fetch_via_engine(skip_tables=…)`,
> `--prefetched-data-dir`). The web UI exposes it as an optional per-table mapping
> panel. Also (2026-06) the parallel-split trigger is now **cell-based**
> (`_plan_split_n`), not row-based — see Phase 0.6. Read alongside
> [`data-fetch-modes.md`](data-fetch-modes.md) for the fetch + bind mechanics. The
> research below (2025–2026 sources) is what Phases 0/1 were built on.

## Why this exists — the three problems with today's CSV path

The converter currently populates data by exporting each Qlik **chart hypercube**
to a CSV (`fetch_data.py` cloud path, `engine_fetch.py` Desktop path) and binding
each TMDL partition with `Csv.Document(...) + PromoteHeaders + TransformColumnTypes`.
That has three hard ceilings:

1. **Slow.** One CLI/WebSocket round-trip *per data-bearing object*, paginated.
2. **Row-capped in cloud.** `qlik app object layout` returns only the object's
   *initial data fetch* page (~500 rows); the rest is silently truncated
   (`fetch_data.py` docstring; `data-fetch-modes.md` "Row-limit caveat").
3. **Won't scale.** The real workstream reaches **~30 million rows**. CSV is a
   text format with **no typed schema**, so the converter has to *sniff* every
   column (whole-file scan in `csv_schema.sniff_csv_schema`) and emit a
   `Table.TransformColumnTypes` cast that can still fail at refresh. At 30M rows
   CSV is large, slow to parse, and fragile on types.

The structural insight: **Qlik has no per-table data API** — data is script-loaded
and exposed only through objects (chart hypercubes). But the *source* of that data
is almost always a set of **QVD files** produced by the load script. The strategy
below pivots from "scrape charts" to "carry the underlying tables", and from CSV
to a typed columnar format.

---

## 1. Can Power BI read QVD directly?  **No.**

**There is no native QVD connector in Power BI Desktop — not GA, not preview.**
The built-in connector list (Data sources in Power BI Desktop) contains no QVD
entry, and the Qlik/Fabric community threads in 2024–2026 consistently answer
"Power BI cannot read the native `.qvd` binary; export it first."

- QVD is Qlik's proprietary columnar binary: an **XML header** plus two binary
  blocks (a symbol table and a bit-stuffed index table). Power Query has no
  reader for it.
- The only way to get QVD into Power BI **as Microsoft ships it** is to convert
  it to something Power BI *can* read (CSV/Parquet/text), or stand up a database.
- **Third-party commercial option — Innovoco "BI Connector."** This is *not*
  Power BI reading QVD; it's an external ETL that reads your QVDs (associations
  + transforms intact) and **loads them into a target** — Azure/Snowflake/
  BigQuery/SQL Server, or auto-creates a Power BI dataset, or writes CSV/JSON
  flat files. Paid, gateway/Azure-Marketplace product. Useful as an
  enterprise alternative, but it's a separate platform, not a local file read,
  and it requires its own infrastructure/credentials.

**Verdict:** we cannot bind a TMDL partition straight to a local `.qvd`. QVD has
to be *transcoded* by us. The good news: QVD's format is documented well enough
that **open-source Python libraries read it with no Qlik install** (see §4),
which lets *this converter* do the transcode itself — offline, uncapped, and
fast.

---

## 2. Intermediate format comparison — CSV vs Parquet vs "leave as QVD"

| Criterion | **CSV** (today) | **Parquet** (recommended) | **Leave as QVD** |
|---|---|---|---|
| Power BI reads it natively? | Yes — `Csv.Document` | **Yes — `Parquet.Document`, connector is GA** | **No** (no connector at all) |
| Typed schema in the file? | No — must sniff + cast | **Yes** — column types travel in the Parquet schema; no `TransformColumnTypes` needed | Yes, but unreadable by PBI |
| Type fidelity | Fragile: one stray formatted value latches a column to `string`; numeric-dim handling is a workaround | **High**: int64→Whole Number, double→Decimal, timestamp→Date/Time map directly from logical types | n/a |
| File size @ scale | Largest (uncompressed text) | **Smallest** — columnar + compression (Snappy/Zstd); commonly a fraction of CSV | Compact (but moot) |
| Python write speed | `csv.writer` row-by-row | **Fast** — `pyarrow.parquet.write_table` is columnar/vectorised, faster than CSV at millions of rows | n/a (would need a *reader*, then re-encode anyway) |
| 30M rows in Import mode | Poor (slow parse, fragile types, big file) | **Good** — designed for analytics-scale columnar load | Not possible |

Notes that matter for our partition code:

- **Parquet connector is General Availability** (Power Query "Parquet connector"
  summary table lists *Release state: General Availability*; supported in Power BI
  semantic models & dataflows, Fabric, Power Apps). It is built in — no add-on.
- **M function:** `Parquet.Document(binary as binary, optional options as record) as table`.
  The optional `options` record accepts a `TypeMapping` (null = preserve maximum
  type fidelity; `"Sql"` = SQL-compatible). We want the **default (null)** to keep
  Qlik-derived types.
- **Local files do NOT hit the "streamed binary values" error.** That error
  (`Parquet.Document cannot be used with streamed binary values`) only occurs on
  sources that can't do random file access — SharePoint, Google Cloud Storage,
  Web/REST — which then need `Binary.Buffer` (feasible only for small files). The
  **local filesystem and ADLS Gen2 support random access**, so
  `Parquet.Document(File.Contents("C:\...\T.parquet"))` reads large local files
  with **no buffering and no size error** (Chris Webb / Microsoft Learn).
  Since this converter writes files next to the PBIP on the local disk, we are
  squarely in the safe case.

**Why not "leave as QVD":** Power BI can't open it; we'd still need a reader and a
re-encode step, so we'd write Parquet anyway. The only value of the raw QVD is as
*our input* (see §4), not as the bound artifact.

---

## 3. 30 million rows: Import vs DirectQuery, and the export-shape question

**Import is still the right default at 30M rows.** Microsoft's own guidance is
"use Import by default; move off it only for latency/size/governance reasons."
Practical points from current sources:

- **VertiPaq compression is the whole point.** Import compresses columnar data
  ~10x (often more for low-cardinality columns); a multi-GB source frequently
  lands at a few hundred MB in-model. 30M rows of a typical star-schema fact is a
  routine Import workload, not an exotic one, *provided the model is a star*
  (narrow fact + dimensions), not 30M-row wide flat tables.
- **DirectQuery is the wrong fit here.** DQ needs a live relational source behind
  it (SQL/Snowflake/etc.). We are producing files, not a database; DQ over files
  isn't a thing. DQ also loses VertiPaq's in-RAM speed and restricts DAX. Only
  consider DQ/Direct Lake if the org later lands these tables in Fabric/a
  warehouse — out of scope for a file-emitting converter.
- **Memory ceiling:** Import is fine while *uncompressed* source < ~100 GB (Pro
  has a model-size cap; Premium/Fabric capacities raise it). 30M rows is well
  inside this for normal column widths.

**Per-chart hypercube export is not viable at 30M rows — and shouldn't be the
model.** A chart hypercube is an *aggregated, GROUP BY-shaped* cross-tab over a
few dimensions; it is neither the source table nor row-complete, and the cloud
path caps it at ~500 rows anyway. To carry 30M rows you must **extract whole
tables once**, not scrape charts:

- The **offline Engine path** (`engine_fetch.py`) already does the right thing in
  spirit — it synthesises a hypercube **per loadmodel table** and paginates *all*
  rows (bypassing the per-chart cap). That is "whole-table extract" via the
  engine. Its cost at 30M rows is the WebSocket pagination throughput and JSON
  overhead.
- The **faster, offline, dependency-light** alternative is to read the **source
  QVDs directly** (the script's `STORE ... INTO x.qvd`) with a Python QVD reader
  and stream them to Parquet — no engine round-trips, no row cap, native types
  (§4).

---

## 4. Recommendation for THIS converter

**Adopt Parquet as the bound intermediate format, and prefer whole-table extracts
over per-chart hypercube scraping. Reach the source data the cheapest way
available, transcode to Parquet with `pyarrow`, and bind partitions with
`Parquet.Document(File.Contents(...))`.**

This kills all three problems at once: no per-chart round-trips (speed), no
~500-row cloud cap (extract whole tables), and a typed columnar file that Import
handles at 30M rows without a fragile sniff/cast (scale + correctness).

### Phased plan

**Phase 0 — Quick win (low risk, biggest immediate payoff): emit Parquet instead
of CSV, reuse existing extract paths. — ✅ IMPLEMENTED 2026-06.**
- Keep the current data sources (cloud `layout`, Desktop `engine_fetch`,
  user-supplied files) but **write `.parquet` instead of `.csv`** via `pyarrow`.
- Drop the whole-file CSV sniffer for the Parquet path: types come from the
  extracted Qlik metadata (`qTags`/`qNumFormat`) baked into the Arrow schema, so
  the TMDL `dataType` is derived once and **no `Table.TransformColumnTypes` is
  emitted**. Smaller files, faster load, no cast-at-refresh failures.
- New partition shape (below). This alone makes the **engine/whole-table path**
  scale to large row counts and removes the per-column sniff fragility.

*As built:* new module `qlik_to_pbi/parquet_io.py` — `PYARROW_AVAILABLE` guard,
`write_parquet_columns` (auto-typed, buffered, for small column-wise data),
`ParquetStreamWriter` (declared-schema, **row-group-batched** — buffers pages
and flushes a Parquet row group every `QLIK_PARQUET_ROW_GROUP` rows, default
250k — for whole-table extracts up to ~30M rows), `sniff_parquet_schema` (reads the file's
typed schema into the same descriptor shape `csv_schema.sniff_csv_schema`
returns). Parquet is the **default** `emit_format` on every entry point
(`fetch_object_data`, `fetch_via_engine`, `convert_from_cloud`); the CLI forces
CSV with **`--csv`**. Both fetch paths emit it:
`fetch_data._write_hypercube_parquet` types each column from `qDimensionInfo`
qTags (numeric dims + measures from `qNum`; a date dim's `qNum` is its Qlik
serial, converted back to a datetime) and `engine_fetch._write_parquet` types
each column from the resolved field's qTags (`$integer`→int64, `$numeric`→double,
`$date`/`$timestamp`→dateTime, untagged→string so a cast can never fail). Both
**degrade to CSV with a warning when pyarrow is absent** (never fail the fetch).
`model._columns_for_table` reads a `.parquet` file's schema directly (no content
scan) and `model._render_table_tmdl` emits the single-step partition below.
`--data-dir` binds whatever `.parquet`/`.csv` files it finds (Parquet preferred).
The CSV path is byte-for-byte unchanged. Bed: `regression/parquet_emit.py`.

**Phase 0.5 — output quality + column pruning (2026-06).**
- *Row-group batching* (`ParquetStreamWriter`): the extract hands us small pages
  (≤10k-cell engine pages, or one cloud data-page), and writing one row group per
  page produced many tiny row groups — slow for Power BI to scan, poor
  compression. The writer now buffers pages and flushes a row group only at
  `QLIK_PARQUET_ROW_GROUP` rows (default 250k), giving large row groups while peak
  memory stays at one row-group's worth. *Codec* switched snappy → **zstd**
  (smaller files, negligible CPU, read natively by PBI).
- *Column pruning* (**DEFAULT ON**; opt out with `--no-prune-columns`): extract
  only the columns the model references. Because the Engine API caps
  `GetHyperCubeData` at **10,000 cells per call** (a hard limit — *not* tunable;
  raising `QLIK_MAX_CELLS_PER_CALL` past it errors), fewer columns is the only way
  to fit more ROWS per round-trip — so pruning cuts engine round-trips *and* file
  size *and* VertiPaq memory at once. **Keep-set = `field_usage.collect_keep_fields`**:
  it builds the model (+ report, to materialise inline measures / calc columns) and
  harvests the model's ACTUAL references — every relationship endpoint, every
  measure-DAX column, every calc-column dependency — mapped back to each column's
  raw `sourceColumn`, UNION the Qlik expression tokens from
  `collect_used_field_names`. `engine_fetch._prune_table_fields` keeps a field iff
  it's in that set OR is a cross-table key, and never empties a table (keep-all
  fallback); the model's `_prune_dangling_relationships` is the final net.
  **Why model-derived, not just token scan**: a join key can be *table-qualified*
  (`HCO.HCO_ID` on one table, `HCP.HCO_ID` on another) — it shares no name and is in
  no expression, so a pure token scan would drop it and dangle the relationship.
  Harvesting the built model's relationship endpoints closes that gap.
  **Validated on 10 real apps** (Consumer Sales 44 tbl, Hospital 23 tbl, …): drops
  **~57% of columns** with **zero referenced-column loss** — that real-app safety
  invariant is a permanent gate in `regression/column_pruning.py`. Pruning runs
  only on the fetch paths, so metadata-only (`--input`, no fetch) anchor builds are
  unaffected.

- *Row-fidelity guard* (**DEFAULT ON**; opt out `QLIK_PRUNE_ROW_GUARD=0`) — the
  critical correctness companion to pruning. A Qlik hypercube returns the
  **distinct combinations** of its dimensions, so fetching only a SUBSET of a
  table's columns can **collapse physical rows** that happen to match on the kept
  columns — silently undercounting any row-grain measure. **Measured**: Hospital's
  `Order Procedure` is 6,829,836 physical rows; a hypercube over **all 12 columns**
  returns ~6.83M (100% — no loss), but pruned to its single key column it returns
  **51,160** (0.7%), and pruned to a few columns including the high-cardinality
  `ParentOrderId` it returns ~5.7M (the user-reported "5.7M of 6.8M" loss). The
  fix: after pruning, `_guard_row_fidelity` cheaply probes each pruned table's
  hypercube `qcy` (a `GetLayout` — **no row data pulled**); when it falls below the
  physical `qNoOfRows` (`_is_row_collapse`, 0.5% tolerance for null noise), it
  **restores all columns for that table** — the only set proven to preserve every
  physical row, because the distinct combination of a table's *full* column set is
  its own row set, independent of associations. Surgical: only collapsing tables
  are restored (`Order Procedure` 1→12, `Revenue` 1→4 on Hospital); tables whose
  kept columns already preserve their rows (`Accounts` 5/13, `PE Hospital` 20/35, …)
  stay pruned. A post-fetch backstop (`_validate_fetched_rows`, reads Parquet
  `num_rows`) ERRORs on any residual shortfall — genuine full-row duplicates even
  all-columns can't separate; remedy there is the lossless **QVD upload**. Opt out
  via `QLIK_PRUNE_ROW_GUARD=0`. Gated by `regression/qvd_ingest.py`. **Net invariant:
  column pruning can never silently drop rows.**

  *(A faster `Count(1)` count-expansion — fetch distinct groups + a per-group count,
  expand on write — was implemented and REVERTED 2026-06: in a multi-table model
  `Count(1)` grouped by a SHARED key is not table-scoped, so a small table sharing a
  key with a big one got the big table's count (Hospital `Revenue` came out at
  Order Procedure's 6.83M instead of 262k). Restoring all columns is correct but
  slower; the fast + correct path for big tables is the **QVD upload**.)*

**Phase 0.6 — row-range parallel fetch (2026-06).** The engine extract is
**latency-bound**, not bandwidth-bound: `GetHyperCubeData` is hard-capped at 10k
cells/call and each cloud round-trip is ~0.7s, so serial throughput is **~1,220
rows/s ≈ 14.6k cells/s** (measured) regardless of column count — a 6.83M-row table
takes **~93 min serial**. The old parallel path split work across TABLES only
(cap 4), so a single dominant fact table got no parallelism at all.

`engine_fetch._extract_parallel` now builds a flat list of **units**: a small
table is one unit; a big table is split into N contiguous **row-range slices**,
each fetched on its own WebSocket via `extract_table(part=k, nparts=N)` — slice k =
rows `[k·qcy//N, (k+1)·qcy//N)`. Row order is immaterial for an Import model, so
the slices' part files stream-merge (`_merge_parquet_parts`, row-group copy, no
re-encode) into one `T.parquet`. **8-way ≈ 12 min, 16-way ≈ 6 min** for that
6.83M-row table.

*Resilience — never a silent partial (2026-06).* High concurrency on one engine
pod causes occasional dead sockets **during** the long data pull (a connect probe
shows 20 simultaneous sessions open fine — the failures are sustained-load, mid-
stream). `_run_units_resilient` (the orchestration `_extract_parallel` delegates
to) runs all units in the pool, then **serially retries** any that failed (no
contention → the transient dead-WS clears), and assembles so a split table is
written **only if all N slices are present** — otherwise it is a **hard failure**
(no partial merge; part files deleted; loud `ENGINE FETCH INCOMPLETE`). A whole
table expected to have rows but missing is a hard failure too, instead of
silently degrading to an empty-stub (= 0 rows in Power BI). `_validate_fetched_rows`
then re-checks the expected plan and ERRORs on anything missing/short. This fixed
a forced-20-worker run that returned "some tables had 0 rows / data missing": the
old merge silently concatenated whatever slices survived. Unit-tested with an
injected fake `run_fn` in `regression/qvd_ingest.py`. **Worker count is auto-sized (2026-06)** by `_auto_worker_count(total_records, cap)`, tiered on the app's TOTAL RECORDS: **Basic ≤5M → 4, Medium ≤20M → 6, High >20M → 8** — deliberately CONSERVATIVE. Higher counts (an earlier 10/14/20) overloaded the engine pod on sustained multi-million-row pulls (sockets died mid-stream, tables failed); the connect step tolerates 20+ but the sustained data pull does not. Count-expansion (below) removes the heavy full-width fetch, so high concurrency is no longer needed. Unknown record count → fallback **6**. An explicit `QLIK_FETCH_WORKERS` overrides exactly (still clamped). Cap **8 cloud** / 2 Desktop
(localhost engine is single-threaded). `_refresh_field_lists_from_engine` returns
`(tables, row_counts)` to size the split.

*Split TRIGGER is cell-based, not row-based (2026-06 fix).* Fetch cost is
**cells** (rows × columns): each `GetHyperCubeData` call moves ≤10k cells whatever
the table's shape. The original row-count gate (>300k rows) missed the very common
shape where the dominant table is "only" ~140k rows but **wide** — e.g. Consumer's
`SalesDetails` (138k rows × 49 cols = **6.77M cells = 78% of that app's whole
extract**) sat *under* the 300k-row gate, so it never split and one worker ground
through it serially while the 8-way pool only parallelised the tiny tables ("8
slices didn't help"). `_plan_split_n(rows, cols, workers)` (a pure, unit-tested
helper — `regression/qvd_ingest.py`) now gates on cells: split when
`rows·cols > QLIK_RANGE_SPLIT_MIN_CELLS` (default **1.2M**), with
`N = min(workers, ceil(cells / QLIK_RANGE_CHUNK_CELLS))` (default chunk **1.0M**),
never more slices than rows. So `SalesDetails` → 7 slices, `Order Procedure`
(82M cells) → 8, while narrow tables (`linkBasket` 134k×2) stay single.

*Schema agreement across slices.* Untagged columns are typed `auto` (inferred
from the first row group), so independent slices could disagree and the merge
would fail. A split table is therefore **probed once** (`_probe_split`: resolve
fields + read the real `qcy` + sample head rows) to resolve every `auto` column
to a **concrete** kind; all slices then write that identical explicit schema
(`_write_parquet(kinds=…)`), so the merge is a plain row-group copy. The probe's
resolved field list is shared to all slices (no per-slice re-probe). Any probe
failure falls back to a single self-resolving unit — never worse than before.
CSV emit never splits (the merge is Parquet-only).

*VertiPaq-friendly ordering (modest).* `ParquetStreamWriter` sorts each LARGE row
group (≥50k rows) by its lowest-cardinality column (`QLIK_PARQUET_SORT=0` to
disable) → longer runs → smaller Parquet + some VertiPaq RLE. Value-preserving
(row order is immaterial for Import) and skipped for small fixtures. Payoff is
modest: VertiPaq re-encodes on import (min-bit value encoding + its own
dictionaries), so Parquet-side type/dictionary tweaks mostly don't propagate, and
the one real lever — a *global* sort — is memory-unsafe at 30M rows, so only a
per-row-group sort is done. Memory stays bounded by the existing cell-budget cap.

**Phase 1 — direct QVD → Parquet (IMPLEMENTED 2026-06, as an upload path).**
`qvd_ingest.py` reads a source **QVD directly** with `pyqvd` and writes typed
Parquet — no Qlik engine, no cloud, no per-chart cap, no truncation. This is the
real step-change for large apps: a local columnar read at disk speed instead of
the engine's latency-bound ~14.6k cells/s.

- **Entry points.** `qvd_to_parquet(qvd, out)` transcodes one file;
  `transcode_qvd_map({table: qvd}, out_dir)` does a batch (one bad file is
  recorded, not fatal — that table just falls back to the engine);
  `transcoded_table_names(results)` is the skip set.
- **Typing comes for free + is *better* than the engine's.** `pyqvd`'s
  `to_pandas()` already distinguishes int64 / float64 / datetime / string and —
  crucially — keeps **leading-zero codes (`"007"`) as string**, so no `auto`
  sniffing or text-vs-number guesswork is needed. The Arrow schema is fixed once
  from the full frame (no per-row-group drift) and written with the same zstd /
  250k-row-group conventions as the engine Parquet.
- **Wiring.** `cloud.convert_from_cloud(prefetched_data_dir=…)` (CLI
  `--prefetched-data-dir`) stages the transcoded Parquet into the run's `data/`
  (`_seed_prefetched`) and passes the covered table names to
  `fetch_via_engine(skip_tables=…)`, which drops them from the plan (incl. the
  split probe). With every table supplied → **zero engine data calls**; partial →
  engine fetches only the remainder. Then bound exactly as in Phase 0.
- **Web UI.** Optional per-table mapping panel (`/tables` lists the app's tables +
  sizes + a `qvd_supported` flag; multipart `/convert` carries the files; the
  background job transcodes via `qvd_ingest`, streaming `[QVD]` progress). Empty
  mapping = skip = engine fetch as today. See [`web-ui.md`](web-ui.md).
- **Optional dependency.** `pyqvd` (+ `pyarrow`) are lazy imports, exactly like
  `pyarrow` alone; absent → the panel hides / the upload is ignored and the run
  falls back to the engine. Never required for a conversion to succeed.
- **Memory.** `pyqvd` reads the whole QVD into RAM (it is not a streaming reader),
  so peak ≈ the pandas frame; the *output* is row-group-chunked. For multi-GB
  QVDs ensure adequate RAM (a future streaming reader would lift this).
- Fallback chain stays intact: uploaded-QVD → engine extract (split) → empty stub.

### Required Python libraries

- **`pyarrow`** — write Parquet (`pyarrow.parquet.write_table`, or
  `ParquetWriter` for incremental row groups). Columnar/vectorised; faster than
  `csv.writer` at millions of rows and produces far smaller files.
  *(Optional `pandas` only if convenient; not required — Arrow tables can be built
  directly from columns.)*
- **A QVD reader (Phase 1), pick one):**
  - **`qvd`** (PyPI, Rust core, Apache-2.0) — `from qvd import qvd_reader; df = qvd_reader.read('t.qvd')`. Fast; reads to DataFrame/dict; no Qlik needed.
  - **`PyQvd`** (pure-Python, zero-dependency) — `QvdTable.from_qvd("t.qvd")`,
    `to_pandas()`, supports `chunk_size=` chunked reads for big files.
  - (A Rust `qvdrs` with built-in Parquet/DuckDB export is also referenced in the
    ecosystem; evaluate if it is published/maintained when implementing.)
  - **Type fidelity caveat:** verify dual-value handling on real QVDs — map Qlik
    numeric symbols to int64/double and keep text symbols as strings, the same
    distinction `_cell_value` / `_dim_is_numeric` already encode. Bake the chosen
    type into the Arrow schema so Parquet carries it.

### Exact M partition shape Power BI will use

Replace the CSV partition (`Csv.Document(...) + PromoteHeaders + TransformColumnTypes`)
with a **single-step** Parquet read. Because Parquet carries the schema, there is
**no PromoteHeaders and no type-cast step**:

```m
let
    Source = Parquet.Document(
        File.Contents("C:\...\data_exports\<TableAlias>.parquet")
    )
in
    Source
```

Notes:
- `File.Contents` on a **local path** gives random access → no `Binary.Buffer`,
  no "streamed binary values" error, no size limit beyond Import's normal model
  cap. (For an ADLS Gen2 deployment, swap `File.Contents(...)` for
  `AzureStorage.DataLake(...)`/`AzureStorage.BlobContents(...)` — also
  random-access-safe.)
- Leave the `Parquet.Document` **`options` argument unset (TypeMapping = null)** to
  preserve maximum type fidelity (don't pass `"Sql"`).
- The empty-stub mode is unchanged (`Table.FromRows({}, type table [...])`).

---

## Bottom line

- **Power BI cannot read QVD** (no native connector, no preview); reading QVD is
  *our* job, not Power BI's.
- **Parquet is the correct intermediate**: GA native connector
  (`Parquet.Document`), typed schema (no sniff/cast), smallest files, fast
  `pyarrow` writes, ideal for 30M-row **Import** (DirectQuery is N/A for a
  file-emitting tool).
- **Stop scraping per-chart hypercubes for bulk data; extract whole tables once**
  — ideally straight from the source QVDs — and bind with
  `Parquet.Document(File.Contents(...))`.

---

## Sources

- [Data sources in Power BI Desktop — Microsoft Learn](https://learn.microsoft.com/en-us/power-bi/connect-data/desktop-data-sources) (no QVD connector listed)
- [Power Query Parquet connector — Microsoft Learn](https://learn.microsoft.com/en-us/power-query/connectors/parquet) (Release state: General Availability; local/Blob/ADLS Gen2; streamed-binary note)
- [Parquet.Document — powerquery.io](https://powerquery.io/accessing-data/parquet/parquet.document) (signature + optional `TypeMapping` options record)
- [Chris Webb — Parquet Files In Power BI And The "Streamed Binary Values" Error](https://blog.crossjoin.co.uk/2021/03/07/parquet-files-in-power-bi-power-query-and-the-streamed-binary-values-error/) (local & ADLS Gen2 are safe; only SharePoint/GCS/web need `Binary.Buffer`)
- [Binary.Buffer — Power Query M](https://learn.microsoft.com/en-us/powerquery-m/binary-buffer)
- [Data types in Power BI Desktop — Microsoft Learn](https://github.com/MicrosoftDocs/powerbi-docs/blob/main/powerbi-docs/connect-data/desktop-data-types.md) (Whole Number = Int64; date/time mapping)
- [DirectQuery in Power BI: when to use, limitations — Microsoft Learn](https://learn.microsoft.com/en-us/power-bi/connect-data/desktop-directquery-about)
- [Optimize Power BI data models for large datasets — b-eye](https://b-eye.com/blog/optimize-power-bi-data-models-large-datasets/) (VertiPaq compression; Import < ~100 GB; aggregations for DQ)
- [Power BI reading Parquet from a Data Lake — Redgate Simple Talk](https://www.red-gate.com/simple-talk/blogs/power-bi-reading-parquet-from-a-data-lake/)
- [Reading and Writing the Apache Parquet Format — Apache Arrow (pyarrow)](https://arrow.apache.org/docs/python/parquet.html) (`write_table` / `ParquetWriter`)
- [`qvd` — PyPI](https://pypi.org/project/qvd/) (Rust-core QVD reader, Apache-2.0); [SBentley/qvd-utils](https://github.com/SBentley/qvd-utils)
- [PyQvd — docs](https://pyqvd.readthedocs.io/stable/guide/introduction.html) and [GitHub](https://github.com/MuellerConstantin/PyQvd) (pure-Python; `from_qvd`, `to_pandas`, `chunk_size`)
- [Innovoco BI Connector — migration](https://innovoco.com/migration/bi-connector/) and [Azure Marketplace listing](https://marketplace.microsoft.com/en-us/product/web-apps/innovocoinc1600178369558.qlik_to_powerbi_connector) (commercial QVD→target ETL; not a native PBI read)
