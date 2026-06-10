"""Semantic model builder for the Qlik -> PBIP pipeline.

Qlik's load model is script-driven; the unbuilt JSON does not carry
column types or relationships. So we synthesise a single placeholder
table containing every field referenced anywhere in the app, plus one
DAX measure per Qlik library measure. That gives PBI valid columns to
bind to so the report opens, with the data layer left to the user.

If a `loadmodel---loadmodel.json` is present (Qlik's data-model snapshot
with tables and field-to-table mapping), we use it to split the columns
across real tables instead of stuffing them in one. The relationships are
emitted with `*:*` cardinality since Qlik's associative model does not
declare a many-side.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ._logging import get_logger
from .config import DEFAULT_TABLE_NAME
from .credentials import CredentialStore
from .csv_schema import match_csv_for_table, sniff_csv_schema
from .dax_translator import translate_qlik_to_dax, _strip_comments
from .ir import QlikIR
from .partition_m import is_live_connection, manifest_entry_for, render_partition_m
from .utils import (
    clean_label,
    lineage_tag,
    safe_filename,
    tmdl_quote,
    write_text,
)

_log = get_logger("MODEL")


class SemanticModel:
    def __init__(
        self,
        ir: "QlikIR | Dict[str, Any]",
        data_dir: Optional[Path] = None,
        credentials: Optional[CredentialStore] = None,
        live_mode: bool = False,
        default_connection_class: str = "",
        db_connections: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        # Accept either the typed QlikIR or a raw dict (back-compat for
        # callers / tests that still build a plain dict). QlikIR exposes
        # the same ``.get()`` / ``[]`` interface, so the rest of the
        # class is agnostic to which one it holds.
        self.ir = QlikIR.from_dict(ir) if isinstance(ir, dict) else ir
        self.app_title: str = (ir.get("app", {}) or {}).get("qTitle", "Report")
        self.tables: List[Dict[str, Any]] = []
        self.measures: List[Dict[str, Any]] = []
        self.relationships: List[Dict[str, Any]] = []
        # `qLibraryId` -> (table_name, measure_name) for measure lookup
        # during visual emit.
        self.measure_by_id: Dict[str, str] = {}
        # Qlik variable name -> measure name for variables that were
        # successfully materialised as DAX measures by
        # ``_materialize_variables_as_measures``. Used by the report
        # builder's ``_var_lookup`` (and ``_build_measures`` below) to
        # redirect ``$(varX)`` references to ``[varX]`` measure refs.
        self.materialized_vars: Dict[str, str] = {}
        # `Field name` -> table_name for visual emit.
        self.field_table: Dict[str, str] = {}
        # Optional directory of CSV exports. When provided, each table
        # is matched against a CSV by name and its partition is bound
        # to that file via Csv.Document. See csv_schema.match_csv_for_table.
        self.data_dir: Optional[Path] = Path(data_dir) if data_dir else None
        self._csv_files: List[Path] = []
        if self.data_dir and self.data_dir.is_dir():
            # Both CSV and Parquet are bindable. Parquet (preferred for
            # large/typed data -- see docs/large-data-strategy.md) carries
            # its schema, so it binds with no sniff and no cast. The
            # matcher works on the file STEM, so it's format-agnostic; the
            # per-file branch in ``_columns_for_table`` picks the reader.
            self._csv_files = (
                sorted(self.data_dir.glob("*.parquet"))
                + sorted(self.data_dir.glob("*.csv"))
            )
            n_pq = sum(1 for p in self._csv_files if p.suffix.lower() == ".parquet")
            _log.info(
                f"Data dir: {self.data_dir} ({len(self._csv_files)} data files"
                f"{f', {n_pq} parquet' if n_pq else ''})"
            )
        # Table name -> Path (absolute) of CSV that backs that table.
        # Populated when columns are built. The writer copies these
        # into the SemanticModel/data folder.
        self.table_csv: Dict[str, Path] = {}

        # Live database connections from credentials.json. When
        # ``live_mode`` is True and the credential store matches a
        # table, the partition M targets the live DB instead of the
        # CSV / empty-stub path. Manifest entries are accumulated here
        # for ``write_credentials_manifest`` to consume.
        self.credentials: Optional[CredentialStore] = credentials
        self.live_mode: bool = bool(live_mode)
        self.default_connection_class: str = (default_connection_class or "").strip()
        self.manifest_entries: List[Dict[str, Any]] = []

        # Script-detected DB sources (LIB CONNECT TO ... + SQL SELECT). When the
        # user supplies a matching connection's details here (keyed by the Qlik
        # connection NAME, lowercased -> {class, server, http_path, catalog,
        # schema, ...}), those tables are repointed at the live source via a
        # DB Import partition instead of the engine-loaded snapshot. Independent
        # of ``live_mode`` / credentials.json -- this is the auto-detect path.
        self.db_connections: Dict[str, Dict[str, Any]] = {
            (k or "").strip().lower(): v
            for k, v in (db_connections or {}).items()
        }

    # ------------------------------------------------------------------
    def build(self) -> None:
        # Preferred source of truth: the engine-current schema sidecar
        # written by engine_fetch (GetTablesAndKeys, qSyntheticMode
        # false). It reflects the data model AFTER the autogenerated
        # section's renames / DROPs, including synthesised keys and
        # script-added columns. Fall back to loadmodel for non-engine
        # paths (--data-dir without --fetch-via-engine, qvf-direct
        # parses, etc.) and ultimately to a stub when neither is
        # available.
        engine_schema = self.ir.get("engine_schema")
        load_model = self.ir.get("load_model")
        if isinstance(engine_schema, dict) and engine_schema.get("tables"):
            self._build_from_engine_schema(engine_schema)
        elif isinstance(load_model, dict) and load_model.get("tables"):
            self._build_from_load_model(load_model)
        else:
            self._build_stub_table()

        # Repoint script-detected DB-source tables at their live source
        # (e.g. Databricks Import partition) when the user supplied the
        # connection's details. Done BEFORE relationship pruning so any column
        # dropped here (a Qlik-computed column the source can't deliver) is
        # reflected when dangling relationships are pruned below.
        if self.db_connections:
            self._attach_db_source_connections()

        # Materialise Qlik variables as DAX measures BEFORE
        # _build_measures so master measures/dimensions that reference
        # ``$(varX)`` resolve to ``[varX]`` (a real measure ref) rather
        # than inlining the same body N times. Isolates per-variable
        # translation failure from every consumer measure.
        self._materialize_variables_as_measures()

        self._build_measures()

        # Drop any relationship whose endpoints no longer exist on the
        # actual tables. Relationships emitted by `_extract_relationships`
        # come from the loadmodel snapshot, which is routinely out of
        # sync with the engine: a key field may have been renamed by
        # the load script (e.g. `[HCP_ID] AS [From_HCP_ID-HCP_ID]`), in
        # which case the loadmodel association still names the pre-
        # rename column. PBI Desktop fails the whole project to load
        # with "FromColumn/ToColumn refers to an object which cannot
        # be found" when a relationship dangles, so prune dangling
        # refs before TMDL emit.
        had_relationships = bool(self.relationships)
        self._prune_dangling_relationships()
        # If pruning emptied the list (every association was stale),
        # fall back to inferring relationships from shared column names.
        # Qlik's associative model joins on every shared field name
        # implicitly, so this recovers joins on script-renamed keys
        # like `From_HCP_ID-HCP_ID` that appear in both tables.
        if had_relationships and not self.relationships:
            _log.info(
                "All loadmodel relationships were stale; running "
                "shared-field-name inference as fallback."
            )
            self._infer_relationships_from_shared_fields()

        # Set every relationship to many-to-many, single-direction. Qlik's
        # associative model is M:M by nature (composite/bridge keys,
        # blanks, repeated dimension keys are all routine), and the
        # converter cannot reliably prove one-side uniqueness at convert
        # time (cloud fetch is row-capped, extracts are sampled, stubs
        # have no data). A wrong many-to-one is LOAD-FATAL -- "Column 'X'
        # contains a duplicate value ... not allowed on the one side of a
        # many-to-one relationship", cascading as opaque 0x80040E4E across
        # every table. Emitting only M:M makes that error impossible; the
        # user can tighten specific relationships to M:1 in Desktop where
        # PBI validates the real loaded data. See the method docstring.
        self._assign_relationship_cardinality()

        # Attach live-DB connections from the credentials store before
        # we render TMDL. When live mode is on, this replaces the CSV /
        # empty-stub partition path with a real Sql.Database / etc M
        # expression. When off (or no credentials supplied), tables are
        # left alone and the existing partition shapes apply.
        if self.live_mode and self.credentials and not self.credentials.is_empty():
            self._attach_live_connections()

        # NOTE: we used to synthesise PBI What-If parameters from
        # every numeric Qlik variable. That over-generated -- most
        # numeric Qlik variables (e.g. ``index = 0``) are internal
        # counters / placeholders, not user-facing controls. Without a
        # reliable signal from the engine (Qlik doesn't tag user-
        # facing parameters), auto-synthesis creates phantom tables in
        # the model that the original dashboard never exposed.
        #
        # PBI users who want a What-If parameter can add it via
        # Modeling -> New Parameter; that path produces exactly the
        # same TMDL shape we used to emit. We keep
        # ``_build_what_if_parameters`` for callers who want to opt
        # in, but do not invoke it during the default build.

        _log.info(
            f"Model built: {len(self.tables)} tables, "
            f"{sum(len(t['columns']) for t in self.tables)} columns, "
            f"{len(self.measures)} measures, "
            f"{len(self.relationships)} relationships, "
            f"{len(self.table_csv)} CSV-backed tables, "
            f"{len([t for t in self.tables if t.get('connection')])} live-DB tables."
        )

    # ------------------------------------------------------------------
    def _attach_live_connections(self) -> None:
        """For each table, look up a credentials entry and attach a
        ``connection`` dict that ``_render_table_tmdl`` consumes to
        emit a live partition.

        Tables tagged as ``Extras`` (synthesised by the converter for
        fields the loadmodel didn't mention) are skipped -- they have
        no real source so a live partition would just 404 in PBI.
        """
        assert self.credentials is not None
        for t in self.tables:
            if t.get("source") == "extras":
                continue
            entry = self.credentials.match(
                table_name=t["name"],
                default_class=self.default_connection_class,
            )
            if entry is None:
                continue
            conn = entry.apply_to({"class": entry.cls})
            if not is_live_connection(conn):
                continue
            t["connection"] = conn
            # The credentials manifest is the secure hand-off doc the
            # user (or their deploy script) consumes after the PBIP is
            # built. We track entries here so converter.py can write
            # them out without re-doing the lookup.
            self.manifest_entries.append(
                manifest_entry_for(t["name"], conn, entry)
            )
            _log.info(
                f"  live connection -> table {t['name']!r}: "
                f"{conn.get('class')} @ {conn.get('server')}"
            )

    # ------------------------------------------------------------------
    def _attach_db_source_connections(self) -> None:
        """Repoint tables loaded from a SQL data connection at their live
        source (a DB Import partition) instead of the engine-loaded snapshot.

        Uses the script-detected ``db_sources`` (connection name + catalog /
        schema / source table + raw SQL columns) and the user-supplied
        per-connection details (``self.db_connections``). Only tables whose
        connection has supplied details are repointed; everything else keeps
        its existing partition (so a mixed file/DB app converts correctly).

        Column reconciliation: the source delivers RAW columns, but the model's
        columns carry the post-LOAD names. For each model column we find its raw
        source column (via the script rename map) and:
          * keep it (renaming raw->model in the M) when the source delivers it;
          * DROP it when it's a Qlik-computed column the source can't deliver
            (e.g. an APPLYMAP geo point) -- the declared columns must be a subset
            of what the partition returns or PBI errors at refresh.
        """
        db_sources: Dict[str, Any] = self.ir.get("db_sources") or {}
        if not db_sources:
            return
        field_renames: Dict[str, Dict[str, str]] = self.ir.get("field_renames") or {}
        # Case-insensitive lookup of the script detection by table name.
        src_by_lower = {(k or "").strip().lower(): v for k, v in db_sources.items()}

        attached = 0
        for t in self.tables:
            if t.get("source") == "extras":
                continue
            src = src_by_lower.get((t["name"] or "").strip().lower())
            if not src:
                continue
            conn_name = (str(src.get("connection") or "")).strip().lower()
            # Match the user-supplied config by connection name, then fall back
            # to a single supplied config (the common one-connection case).
            cfg = self.db_connections.get(conn_name)
            if cfg is None and len(self.db_connections) == 1:
                cfg = next(iter(self.db_connections.values()))
            if not cfg:
                continue

            # Class: explicit from the user, else inferred from the connection
            # name (a connection literally named "Databricks" is databricks).
            cls = (cfg.get("class") or "").strip().lower()
            if not cls:
                cls = self._infer_db_class(conn_name)
            server = (cfg.get("server") or cfg.get("host") or "").strip()

            conn: Dict[str, Any] = {
                "class":        cls,
                "server":       server,
                "http_path":    (cfg.get("http_path") or cfg.get("httppath") or "").strip(),
                "catalog":      (cfg.get("catalog") or src.get("catalog") or "").strip(),
                "schema":       (cfg.get("schema") or src.get("schema") or "").strip(),
                "source_table": (src.get("source_table") or t["name"]).strip(),
                "mode":         (cfg.get("mode") or "import").strip().lower(),
            }
            if cfg.get("warehouse"):
                conn["warehouse"] = str(cfg["warehouse"]).strip()
            if cfg.get("connector_function"):
                conn["connector_function"] = str(cfg["connector_function"]).strip()

            if not is_live_connection(conn):
                _log.warning(
                    f"  DB source: table {t['name']!r} -> connection "
                    f"{conn_name!r} needs class + server; skipping (kept loaded "
                    f"data). Provide the connection's host to repoint it."
                )
                continue

            # Reconcile columns against what the source can deliver.
            sql_cols_lower = {
                (c or "").strip().lower() for c in (src.get("sql_columns") or [])
            }
            renames = field_renames.get(t["name"]) or {}
            kept: List[Dict[str, Any]] = []
            col_renames: List[Tuple[str, str]] = []
            dropped: List[str] = []
            for col in t["columns"]:
                if col.get("expression"):
                    # A converter-synthesised calc column (not from the source).
                    kept.append(col)
                    continue
                post_load = col.get("sourceColumn") or col["name"]
                # Candidate RAW source-column names for this model column. The
                # source delivers raw Databricks names; the model column may be
                # the post-LOAD engine name (plain key like HCP_ID, or a script-
                # qualified name like fact_crm.Rep_ID) or a loadmodel-qualified
                # one (fact_crm.HCP_ID). Try, in order: the script rename's
                # original; the post-LOAD name; the field part after a table
                # qualifier; that part's rename original.
                leaf = post_load.split(".")[-1]
                candidates = [
                    renames.get(post_load),
                    post_load,
                    leaf,
                    renames.get(leaf),
                ]
                raw = next(
                    (c for c in candidates
                     if c and c.strip().lower() in sql_cols_lower),
                    None,
                )
                if raw:
                    # Source delivers it (under the raw name); rename raw -> the
                    # model name in M and bind the column to that.
                    if raw != col["name"]:
                        col_renames.append((raw, col["name"]))
                    col["sourceColumn"] = col["name"]
                    kept.append(col)
                else:
                    dropped.append(col["name"])
            if not kept:
                _log.warning(
                    f"  DB source: table {t['name']!r} has no source-backed "
                    f"columns; keeping loaded data."
                )
                continue
            if col_renames:
                conn["column_renames"] = col_renames
            t["columns"] = kept
            t["connection"] = conn
            # The data-dir CSV/Parquet (if any) must NOT also bind -- the live
            # partition wins, but drop the stale binding so the writer doesn't
            # copy an now-unused data file for this table.
            t.pop("csv", None)
            self.table_csv.pop(t["name"], None)
            attached += 1
            self.manifest_entries.append({
                "table":    t["name"],
                "class":    cls,
                "server":   server,
                "http_path": conn.get("http_path", ""),
                "catalog":  conn.get("catalog", ""),
                "schema":   conn.get("schema", ""),
                "source_table": conn.get("source_table", ""),
                "mode":     conn["mode"],
            })
            msg = (
                f"  DB source -> table {t['name']!r}: {cls} import from "
                f"{conn['catalog']}.{conn['schema']}.{conn['source_table']}"
            )
            if dropped:
                msg += f" (dropped {len(dropped)} computed col(s) the source can't deliver: {dropped})"
            _log.info(msg)

        if attached:
            _log.info(
                f"Repointed {attached} table(s) at their live DB source "
                f"(Import); other tables keep their existing partition."
            )

    @staticmethod
    def _infer_db_class(conn_name: str) -> str:
        """Infer a connector class from a Qlik connection name."""
        n = (conn_name or "").lower()
        for key, cls in (
            ("databricks", "databricks"), ("spark", "databricks"),
            ("snowflake", "snowflake"),
            ("redshift", "redshift"),
            ("postgres", "postgres"),
            ("sqlserver", "sqlserver"), ("sql server", "sqlserver"),
            ("mssql", "sqlserver"), ("synapse", "sqlserver"), ("azure sql", "sqlserver"),
        ):
            if key in n:
                return cls
        return ""

    # ------------------------------------------------------------------
    def _build_stub_table(self) -> None:
        fields = self.ir.get("fields", []) or []
        if not fields:
            fields = ["Value"]
        cols, used_csv = self._columns_for_table(
            DEFAULT_TABLE_NAME, [(f, f) for f in fields],
        )
        for raw_col in fields:
            self.field_table[raw_col] = DEFAULT_TABLE_NAME
            sanitized = _sanitize_column_name(raw_col)
            if sanitized != raw_col:
                self.field_table.setdefault(sanitized, DEFAULT_TABLE_NAME)
        self.tables.append({
            "name":    DEFAULT_TABLE_NAME,
            "columns": cols,
            "source":  "stub",
            "csv":     used_csv,
        })

    def _build_from_engine_schema(self, engine_schema: Dict[str, Any]) -> None:
        """Build TMDL tables from the engine's authoritative current
        schema (the ``engine-schema.json`` sidecar written by
        ``engine_fetch``).

        Shape consumed::

            {"tables": {<table_name>: {
                "fields": [{"name", "key_type", "tags", "is_hidden",
                            "is_system", "is_semantic"}, ...],
                "row_count": <int>,
              }, ...},
             "keys": [{"key_fields": [...], "tables": [...]}, ...]}

        This path REPLACES the loadmodel walk. We still consult
        ``self.ir['load_model']`` later for soft metadata (loose
        tables, associations) but never for the field list itself --
        the engine snapshot is post-script and therefore authoritative.
        """
        for tname_raw, tbl in (engine_schema.get("tables") or {}).items():
            t_name = _sanitize_table_name(tname_raw)
            if not t_name:
                continue
            # Skip system tables ($Field, $Table, $Rows...) and
            # entirely-hidden tables -- these are engine internals,
            # not user data.
            fields = tbl.get("fields") or []
            visible_fields = [
                f for f in fields
                if not f.get("is_system")
            ]
            if not visible_fields:
                continue
            field_pairs: List[Tuple[str, str]] = [
                (f["name"], f["name"])
                for f in visible_fields
                if f.get("name")
            ]
            # Engine-tagged types: pass each field's qTags so the
            # column-builder can prefer them over CSV sniffing.
            # ``$integer`` / ``$numeric`` / ``$date`` / ``$timestamp``
            # / ``$text`` are authoritative because the Qlik engine
            # assigned them during load -- they survive CSV column
            # misalignment, header-order drift, and the all-string
            # fallback path.
            field_tags = {
                f["name"]: list(f.get("tags") or [])
                for f in visible_fields
                if f.get("name")
            }
            cols, used_csv = self._columns_for_table(
                tname_raw, field_pairs, field_tags=field_tags,
            )
            # Mark engine-hidden fields so the TMDL emits ``isHidden``
            # (kept in the model for joins / measure refs, hidden from
            # the field well). Match on the source field name.
            hidden_names = {
                f["name"] for f in visible_fields
                if f.get("is_hidden") and f.get("name")
            }
            if hidden_names:
                for col in cols:
                    if col["name"] in hidden_names or col.get("sourceColumn") in hidden_names:
                        col["isHidden"] = True
            for raw_field, _ in field_pairs:
                self.field_table.setdefault(raw_field, t_name)
                san = _sanitize_column_name(raw_field)
                if san != raw_field:
                    self.field_table.setdefault(san, t_name)
            for col in cols:
                self.field_table.setdefault(col["name"], t_name)
                if col.get("sourceColumn"):
                    self.field_table.setdefault(col["sourceColumn"], t_name)
            if not cols:
                continue
            self.tables.append({
                "name":    t_name,
                "columns": cols,
                "source":  "engine_schema",
                "csv":     used_csv,
            })

        # Relationships: prefer the engine's key records (qk). Each
        # record names a set of fields and the tables they participate
        # in -- this is the engine's own statement of join keys, more
        # reliable than shared-field-name inference.
        self._extract_relationships_from_engine(engine_schema)
        # Fall back to shared-name inference if the engine returned no
        # key records (older versions, app-not-reloaded).
        if not self.relationships:
            self._infer_relationships_from_shared_fields()

    def _extract_relationships_from_engine(
        self, engine_schema: Dict[str, Any],
    ) -> None:
        """Translate engine ``qk`` source-key records into PBI
        relationships.

        Each ``qk`` entry has ``key_fields`` (one or more field names
        that compose the key) and ``tables`` (every table that carries
        this key). PBI relationships are pairwise, so for each table
        pair we emit a many-to-many edge per key field. Cardinality
        defaults to many/many since Qlik's associative model doesn't
        declare uniqueness; the user can promote to one/many in
        Desktop's Manage Relationships pane.
        """
        # {table_name (sanitised): set(column_names)} for the tables
        # we actually emitted -- used to filter dangling refs.
        cols_by_table: Dict[str, Set[str]] = {
            t["name"]: {c["name"] for c in t["columns"]}
            for t in self.tables
        }
        # Pull the script-derived rename map -- needed to translate
        # qk key fields (which name the engine field) to per-table
        # column names. For the From_HCP_ID-HCP_ID join key, that
        # means relating Referral Edge[From_HCP_ID] -> HCP[HCP_ID].
        # First check the schema sidecar; fall back to the IR-level
        # map written by parser.py.
        field_renames: Dict[str, Dict[str, str]] = (
            engine_schema.get("field_renames")
            or self.ir.get("field_renames")
            or {}
        )
        renames_by_table_lower: Dict[str, Dict[str, str]] = {
            (k or "").lower(): v or {}
            for k, v in field_renames.items()
        }

        def per_table_col_name(
            table_orig: str, table_san: str, engine_field: str,
        ) -> Optional[str]:
            """Resolve the actual TMDL column name on ``table_san``
            for an engine field. Tries (in order):
              * the script rename map's per-table entry,
              * the engine name verbatim,
              * the sanitised engine name,
              * a CSV-sniffed sourceColumn match.
            Returns None when no column on this table matches.
            """
            cols_on_table = cols_by_table.get(table_san, set())
            # 1. Rename map says ``engine_field`` was named ``orig`` on
            # this table. Sanitise and check.
            rename_map = (
                renames_by_table_lower.get((table_orig or "").lower())
                or renames_by_table_lower.get((table_san  or "").lower())
                or {}
            )
            orig = rename_map.get(engine_field)
            if orig:
                cand = _sanitize_column_name(orig)
                if cand in cols_on_table:
                    return cand
            # 2. Engine name (possibly sanitised).
            for cand in (engine_field, _sanitize_column_name(engine_field)):
                if cand in cols_on_table:
                    return cand
            # 3. Last resort: a column whose sourceColumn matches.
            for t in self.tables:
                if t["name"] != table_san:
                    continue
                for col in t["columns"]:
                    if col.get("sourceColumn") in (engine_field, orig):
                        return col["name"]
                break
            return None

        emitted: Set[Tuple[str, str, str, str]] = set()
        for k in engine_schema.get("keys") or []:
            raw_tables = list(k.get("tables") or [])
            san_tables = [_sanitize_table_name(t) for t in raw_tables]
            # Drop any table the engine names but we didn't actually
            # build (system tables, hidden-only tables).
            table_pairs = [
                (raw, san)
                for raw, san in zip(raw_tables, san_tables)
                if san in cols_by_table
            ]
            key_fields = list(k.get("key_fields") or [])
            if len(table_pairs) < 2 or not key_fields:
                continue
            for fname in key_fields:
                # Pairwise edges between all tables that carry this
                # key -- typical case is exactly 2 tables; composite
                # / shared keys may span 3+.
                for i in range(len(table_pairs)):
                    for j in range(i + 1, len(table_pairs)):
                        a_raw, a = table_pairs[i]
                        b_raw, b = table_pairs[j]
                        a_col = per_table_col_name(a_raw, a, fname)
                        b_col = per_table_col_name(b_raw, b, fname)
                        if not a_col or not b_col:
                            continue
                        key = (a, b, a_col, b_col)
                        rev = (b, a, b_col, a_col)
                        if key in emitted or rev in emitted:
                            continue
                        emitted.add(key)
                        # Convention: the smaller table is the "one"
                        # side. (Column count is a proxy for row count;
                        # engine_schema carries row_count too, plumb
                        # through if the heuristic proves too crude.)
                        a_cols = len(cols_by_table.get(a, set()))
                        b_cols = len(cols_by_table.get(b, set()))
                        if a_cols <= b_cols:
                            one_table, one_col = a, a_col
                            many_table, many_col = b, b_col
                        else:
                            one_table, one_col = b, b_col
                            many_table, many_col = a, a_col
                        self.relationships.append({
                            "name":       lineage_tag(
                                many_table, one_table, many_col, one_col,
                            ),
                            "fromTable":  many_table,
                            "fromColumn": many_col,
                            "toTable":    one_table,
                            "toColumn":   one_col,
                        })
        if self.relationships:
            _log.info(
                f"Built {len(self.relationships)} relationship(s) from "
                "engine key records (qk)."
            )

    def _build_from_load_model(self, load_model: Dict[str, Any]) -> None:
        """Build TMDL tables from the Qlik `loadmodel---loadmodel.json`.

        Schema (real Qlik shape, not the q-prefixed Engine API shape):

            tables[]:
              id: "dsd.<TableAlias>"          - logical id, used as
                                                 the join target for queries.tableRef.
              tableAlias / tableName: str     - display name.
              fields[]:
                id: "dsd.<TableAlias>.<name>" - logical field id.
                name: raw column name.
                alias: display column name.

            queries[]:
              id: <uuid>                      - the id `associations`
                                                 actually references.
              tableRef: "dsd.<TableAlias>"    - back to tables[].id.
              fields[]:
                id: <uuid>                    - `associations.fieldId`
                                                 references this.
                name: display column name.

            associations:  {"<assoc-name>": [{tableId, fieldId}, ...]}
              tableId  -> queries[].id (uuid)
              fieldId  -> queries[].fields[].id (uuid)

        We resolve associations through `queries` because that's the
        layer the uuid graph lives in.
        """
        seen_table: Set[str] = set()
        # `dsd.<table>` -> display table name (so we can hop queries.tableRef).
        logical_id_to_name: Dict[str, str] = {}
        # uuid (queries.id) -> display table name.
        table_uuid_to_name: Dict[str, str] = {}
        # uuid (queries.fields[].id) -> display field name.
        field_uuid_to_name: Dict[str, str] = {}

        for tbl in load_model.get("tables", []) or []:
            t_id = (tbl.get("id") or "").strip()
            raw_name = (tbl.get("tableAlias")
                        or tbl.get("tableName")
                        or "Table").strip()
            t_name = _sanitize_table_name(raw_name)
            if not t_name or t_name in seen_table:
                continue
            seen_table.add(t_name)

            # Gather (raw, source) field pairs from the loadmodel block.
            field_pairs: List[Tuple[str, str]] = []
            for f in tbl.get("fields", []) or []:
                raw_field = (f.get("alias") or f.get("name") or "").strip()
                if not raw_field:
                    continue
                field_pairs.append((raw_field, raw_field))

            cols, used_csv = self._columns_for_table(raw_name, field_pairs)
            # Populate field_table from BOTH the loadmodel field list AND
            # whatever the CSV ended up with (a CSV may add columns the
            # loadmodel didn't know about, or use a different display name).
            for raw_field, _ in field_pairs:
                self.field_table.setdefault(raw_field, t_name)
                san = _sanitize_column_name(raw_field)
                if san != raw_field:
                    self.field_table.setdefault(san, t_name)
            for col in cols:
                self.field_table.setdefault(col["name"], t_name)
                if col.get("sourceColumn"):
                    self.field_table.setdefault(col["sourceColumn"], t_name)

            if not cols:
                continue
            if t_id:
                logical_id_to_name[t_id] = t_name
            self.tables.append({
                "name":    t_name,
                "columns": cols,
                "source":  "loadmodel",
                "csv":     used_csv,
            })

        # Walk `queries` to resolve the uuid layer used by associations.
        for q in load_model.get("queries", []) or []:
            q_id = (q.get("id") or "").strip()
            t_ref = (q.get("tableRef") or "").strip()
            t_name = logical_id_to_name.get(t_ref)
            if not (q_id and t_name):
                continue
            table_uuid_to_name[q_id] = t_name
            for f in q.get("fields", []) or []:
                f_id = (f.get("id") or "").strip()
                f_name = (f.get("name") or "").strip()
                if f_id and f_name:
                    field_uuid_to_name[f_id] = f_name

        # NOTE: an earlier version synthesised an ``Extras`` table here
        # to catch fields referenced from dimensions / measures / sheet
        # cells that the loadmodel didn't list (typical for stale
        # loadmodel snapshots). That table is intentionally NOT created
        # now: it added phantom columns that did not exist in the real
        # Qlik data source, which is confusing and (combined with the
        # engine-current schema refresh in ``engine_fetch``) no longer
        # needed -- the engine snapshot already includes every real
        # field, including script-added renames and GeoMakePoint
        # synthetic columns. Visuals that reference a non-existent
        # field will fall through to the inline-measure stub path or
        # be skipped, matching the user's explicit "only base data
        # source tables" requirement.

        self._extract_relationships(
            load_model, table_uuid_to_name, field_uuid_to_name,
        )
        # Fallback: if the loadmodel had no usable associations / queries
        # (common when the unbuild snapshot is stale or comes from a
        # direct-parsed QVF where queries[] is empty), infer relationships
        # heuristically from shared field names. Qlik's associative
        # model implicitly links tables on shared field names anyway.
        if not self.relationships:
            self._infer_relationships_from_shared_fields()

    # ------------------------------------------------------------------
    def _columns_for_table(
        self,
        raw_table_name: str,
        loadmodel_fields: List[Tuple[str, str]],
        field_tags: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Path]]:
        """Decide the column list for ``raw_table_name``.

        Type-resolution priority (strongest signal first):

          1. **Engine-derived qTags**   (``field_tags`` arg): the Qlik
             engine's own type judgement, captured via
             ``GetTablesAndKeys``. Independent of any CSV being
             present -- so the TMDL has correct types even when the
             data file is not available.
          2. **CSV header + content sniffing**: when a data file
             matches this table, sample its first ~200 rows to detect
             int/double/date/text. Only used for fields not covered
             by engine tags.
          3. **All-string fallback**: when neither engine nor CSV
             provides a signal, every field is typed as ``string`` so
             the empty-stub partition still loads.

        The CSV path is also recorded in ``self.table_csv`` so the
        writer can copy it into ``SemanticModel/data/``.

        Returns ``(columns, csv_path_or_None)``.
        """
        from .csv_schema import type_from_qlik_tags as _ttags

        csv_path: Optional[Path] = None
        if self._csv_files:
            csv_path = match_csv_for_table(raw_table_name, self._csv_files)

        # Build a per-field type override map from engine tags. ``None``
        # entries mean "engine didn't tell us"; the CSV sniff or
        # string-fallback will fill those in.
        tag_types: Dict[str, Dict[str, Any]] = {}
        for fname, tags in (field_tags or {}).items():
            t = _ttags(tags or [])
            if t is not None:
                tag_types[fname] = t

        if csv_path is not None:
            # Pick the schema reader by format. Parquet carries a typed
            # schema (read it directly, no content scan); CSV is sniffed.
            if Path(csv_path).suffix.lower() == ".parquet":
                from .parquet_io import sniff_parquet_schema
                sniffed = sniff_parquet_schema(csv_path)
            else:
                sniffed = sniff_csv_schema(csv_path)
            if sniffed:
                cols: List[Dict[str, Any]] = []
                # PBI's column-name uniqueness check inside a table is
                # CASE-INSENSITIVE. Qlik / source CSVs can legitimately
                # carry both "Medicare enrollees" and "Medicare Enrollees"
                # as distinct columns; we keep them both but tack a "_N"
                # suffix onto each later collision so the TMDL parses
                # and the M binding still resolves to the original CSV
                # header via `sourceColumn`.
                seen_ci: Set[str] = set()
                for c in sniffed:
                    cname = _sanitize_column_name(c["name"])
                    final = cname
                    if final.lower() in seen_ci:
                        i = 2
                        while f"{cname}_{i}".lower() in seen_ci:
                            i += 1
                        final = f"{cname}_{i}"
                    seen_ci.add(final.lower())
                    # Engine tags trump the sniffer -- the sniffer
                    # works off the data file's content, which can be
                    # misaligned (header order != data order) and
                    # produce wildly wrong types. Engine tags are
                    # immune to CSV layout issues.
                    src_col = c["sourceColumn"]
                    override = (
                        tag_types.get(src_col)
                        or tag_types.get(_sanitize_column_name(src_col))
                        or tag_types.get(final)
                    )
                    if override is not None:
                        data_type = override["dataType"]
                        m_type    = override["mType"]
                        fmt       = override.get("formatString") or ""
                    else:
                        data_type = c["dataType"]
                        m_type    = c["mType"]
                        fmt       = c.get("formatString") or ""
                    col_entry: Dict[str, Any] = {
                        "name":         final,
                        "sourceColumn": src_col,
                        "dataType":     data_type,
                        "mType":        m_type,
                    }
                    # Only carry the format string when it was derived
                    # from a real type signal (engine tag or sniffer-
                    # detected date pattern). We do NOT stamp a per-
                    # type default for numeric columns: PBI Desktop
                    # renders int64 / double columns correctly with no
                    # formatString attached, and stamping ``'#,##0'``
                    # blindly causes the format pattern itself to appear
                    # in the data view whenever the M cast doesn't
                    # deliver the matching numeric storage type.
                    if fmt:
                        col_entry["formatString"] = fmt
                    cols.append(col_entry)
                self.table_csv[_sanitize_table_name(raw_table_name)] = csv_path
                _log.info(
                    f"  {raw_table_name!r} -> {csv_path.name} ({len(cols)} cols, "
                    f"{len(tag_types)} engine-tagged)"
                )
                return cols, csv_path
            else:
                _log.warning(
                    f"  {csv_path.name} matched {raw_table_name!r} but "
                    f"could not be parsed; falling back to stub columns."
                )

        # Stub / loadmodel fallback. Engine tags (when available) drive
        # types per field so the model is still correctly typed even
        # when no CSV is present -- a partition without data still has
        # the right shape for visuals and DAX measures to bind to.
        cols = []
        seen_ci: Set[str] = set()
        for raw_field, source_col in loadmodel_fields:
            cname = _sanitize_column_name(raw_field)
            final = cname
            if final.lower() in seen_ci:
                i = 2
                while f"{cname}_{i}".lower() in seen_ci:
                    i += 1
                final = f"{cname}_{i}"
            seen_ci.add(final.lower())
            override = tag_types.get(raw_field) or tag_types.get(source_col)
            if override is not None:
                col: Dict[str, Any] = {
                    "name":         final,
                    "sourceColumn": source_col,
                    "dataType":     override["dataType"],
                    "mType":        override["mType"],
                }
                if override.get("formatString"):
                    col["formatString"] = override["formatString"]
            else:
                col = {
                    "name":         final,
                    "sourceColumn": source_col,
                    "dataType":     _guess_type(final),
                }
            cols.append(col)
        if not cols:
            cols.append({
                "name": "Value", "sourceColumn": "Value", "dataType": "string",
            })
        return cols, None

    def _extract_relationships(
        self,
        load_model: Dict[str, Any],
        table_id_to_name: Dict[str, str],
        field_id_to_name: Dict[str, str],
    ) -> None:
        """Resolve loadmodel `associations` into PBI relationships.

        Each association entry can be one of two shapes:

        * Modern: ``{ <name>: [{tableId, fieldId}, {tableId, fieldId}] }``
          - two members joining table-1.field to table-2.field.
        * Legacy / sparse: ``{ <name>: [...]}`` with members that
          reference id strings we haven't seen (stale loadmodel). In
          that case we skip rather than emitting a broken relationship
          that breaks PBI's model validation.
        """
        assocs = load_model.get("associations")
        if not isinstance(assocs, dict):
            return

        for assoc_name, members in assocs.items():
            if not isinstance(members, list) or len(members) < 2:
                continue
            m1, m2 = members[0], members[1]
            if not (isinstance(m1, dict) and isinstance(m2, dict)):
                continue
            t1 = table_id_to_name.get(m1.get("tableId", ""))
            t2 = table_id_to_name.get(m2.get("tableId", ""))
            f1 = field_id_to_name.get(m1.get("fieldId", ""))
            f2 = field_id_to_name.get(m2.get("fieldId", ""))
            if not (t1 and t2 and f1 and f2):
                continue
            if t1 == t2:
                continue
            # Resolve the field NAME (raw) to the sanitised column name
            # so the relationship target matches what's actually emitted
            # on the table.
            f1_col = _sanitize_column_name(f1)
            f2_col = _sanitize_column_name(f2)
            self.relationships.append({
                "name":       lineage_tag(t1, t2, f1_col, f2_col),
                "fromTable":  t1,
                "fromColumn": f1_col,
                "toTable":    t2,
                "toColumn":   f2_col,
            })

    # ------------------------------------------------------------------
    def _prune_dangling_relationships(self) -> None:
        """Drop relationships pointing at non-existent table.column refs.

        A relationship is kept only when both ``fromTable.fromColumn``
        and ``toTable.toColumn`` resolve to a real column on the named
        table. Anything else would cause PBI Desktop's TMDL loader to
        reject the entire project at open time with::

            Property FromColumn of object "relationship ..." refers to
            an object which cannot be found.

        Mismatches typically come from loadmodel/engine divergence -- a
        field the loadmodel declares as a join key is silently renamed
        by the load script to a Qlik-internal synthetic key, so the
        actual table built from the engine extract does not carry it.
        """
        # Build {table_name: set(column_names)} for fast lookup.
        cols_by_table: Dict[str, Set[str]] = {}
        for t in self.tables:
            cols_by_table[t["name"]] = {col["name"] for col in t["columns"]}

        kept: List[Dict[str, Any]] = []
        dropped = 0
        for rel in self.relationships:
            ft, fc = rel["fromTable"], rel["fromColumn"]
            tt, tc = rel["toTable"],   rel["toColumn"]
            if (
                fc in cols_by_table.get(ft, set())
                and tc in cols_by_table.get(tt, set())
            ):
                kept.append(rel)
            else:
                missing = []
                if fc not in cols_by_table.get(ft, set()):
                    missing.append(f"{ft}.{fc}")
                if tc not in cols_by_table.get(tt, set()):
                    missing.append(f"{tt}.{tc}")
                _log.warning(
                    f"  dropping relationship {ft}.{fc} -> {tt}.{tc}: "
                    f"missing column(s) {missing}"
                )
                dropped += 1
        self.relationships = kept
        if dropped:
            _log.info(
                f"Pruned {dropped} relationship(s) with dangling column refs"
            )

    # ------------------------------------------------------------------
    def _infer_relationships_from_shared_fields(self) -> None:
        """Propose many-to-one relationships from shared column names.

        Qlik's associative model implicitly joins tables on every shared
        field name. When the loadmodel doesn't expose explicit
        ``associations`` (the typical case for direct-parsed QVFs and
        stale unbuilt snapshots), we fall back to the same heuristic:

        * For each column name that appears in **exactly two** tables,
          emit one many-to-one relationship between them.
        * Skip names shared by 3+ tables (key fields used everywhere)
          to avoid fan-out / circular routing in PBI's model validator.
        * Pick the table with **fewer columns** as the "one" side
          (dimension), the other as "many" (fact). Cheap heuristic
          that gets the cardinality right for ~80% of star-shaped
          models in our test set.
        """
        # column_name -> [table_name, ...]
        col_to_tables: Dict[str, List[str]] = {}
        for t in self.tables:
            tname = t["name"]
            for col in t["columns"]:
                cname = col["name"]
                col_to_tables.setdefault(cname, []).append(tname)

        # table_name -> column count (for "one" vs "many" heuristic)
        tbl_col_count: Dict[str, int] = {
            t["name"]: len(t["columns"]) for t in self.tables
        }

        emitted: Set[Tuple[str, str, str]] = set()
        for col_name, tables in col_to_tables.items():
            if len(tables) != 2:
                continue
            a, b = tables[0], tables[1]
            if a == b:
                continue
            # Prefer the smaller table as the dimension (one) side.
            if tbl_col_count.get(a, 0) <= tbl_col_count.get(b, 0):
                one_side, many_side = a, b
            else:
                one_side, many_side = b, a
            key = (many_side, one_side, col_name)
            if key in emitted:
                continue
            emitted.add(key)
            self.relationships.append({
                "name":       lineage_tag(many_side, one_side, col_name),
                "fromTable":  many_side,
                "fromColumn": col_name,
                "toTable":    one_side,
                "toColumn":   col_name,
            })
        if self.relationships:
            _log.info(
                f"Inferred {len(self.relationships)} relationship(s) from "
                "shared column names (no loadmodel associations)."
            )

    # ------------------------------------------------------------------
    def _build_what_if_parameters(self) -> None:
        """For each user-facing Qlik variable with a numeric default,
        emit a PBI "What If" parameter as:

          * a synthetic table ``<VarName> Parameter`` with a single
            ``GENERATESERIES`` partition (min, max, step),
          * a measure ``<VarName> Parameter Value`` returning
            ``SELECTEDVALUE``,
          * configured so a slicer dropped onto it lets the user
            scrub the value.

        We skip variables that look system-internal (those whose name
        starts with ``_`` or that have no numeric default).
        """
        var_count = 0
        for v in self.ir.get("variables", []) or []:
            vname = (v.get("qName") or "").strip()
            vdef  = (v.get("qDefinition") or "").strip()
            if not vname or vname.startswith("_"):
                continue
            # Only synthesise for variables whose default is a literal
            # number. Expression-driven variables can't become a
            # static What-If range without losing semantics.
            try:
                default_val = float(vdef.lstrip("="))
            except (TypeError, ValueError):
                continue
            # Skip variables already absorbed as parameters (re-runs).
            existing = {t["name"] for t in self.tables}
            tbl_name = f"{vname} Parameter"
            if tbl_name in existing:
                continue
            # Range: span +/- 2x the default with 21 steps. Authors
            # can tune in PBI Desktop > Modeling > New Parameter.
            v_min = -abs(default_val) * 2 if default_val else -10.0
            v_max =  abs(default_val) * 2 if default_val else  10.0
            if v_min == v_max:
                v_min, v_max = default_val - 10.0, default_val + 10.0
            step = max((v_max - v_min) / 20.0, 0.1)
            col_name = vname
            self.tables.append({
                "name":    tbl_name,
                "columns": [{
                    "name":         col_name,
                    "sourceColumn": col_name,
                    "dataType":     "double",
                    "mType":        "type number",
                }],
                "source":       "what_if_parameter",
                "csv":          None,
                "what_if": {
                    "default": default_val,
                    "min":     v_min,
                    "max":     v_max,
                    "step":    step,
                    "varname": vname,
                },
            })
            # Add a corresponding measure that resolves the slicer's
            # currently selected value.
            self.measures.append({
                "name":         f"{vname} Value",
                "table":        tbl_name,
                "expression":   f"SELECTEDVALUE('{tbl_name}'[{col_name}], {default_val})",
                "formatString": "0.00",
                "source":       f"What-If parameter for Qlik variable '{vname}'",
            })
            # Register field_table so downstream visuals can bind.
            self.field_table.setdefault(vname, tbl_name)
            var_count += 1
        if var_count:
            _log.info(
                f"Built {var_count} What-If parameter(s) from Qlik variables."
            )

    # ------------------------------------------------------------------
    def _make_field_resolver(self) -> Callable[[str], Optional[str]]:
        """Return a resolver mapping a Qlik field name to the
        fully-qualified DAX reference of the column on its OWNING table
        (``'Table'[Column]``), or ``None`` when the name isn't a known
        column.

        Passed to ``translate_qlik_to_dax`` so every field in a measure
        expression binds to the table that actually holds it -- not the
        measure's single home table. Without it, an expression such as
        ``Sum([Sales]) / Sum([Budget])`` whose operands live on different
        tables would emit ``'Fact'[Budget]`` for a column that isn't on
        ``Fact`` and the measure would break.

        The column name follows the same convention as the rest of the
        model: the on-disk name is ``_sanitize_column_name`` of the raw
        Qlik field. ``field_table`` is seeded with both raw and sanitised
        keys, so we try the raw name first, then the sanitised one.

        The closure only captures ``self.field_table`` (a live dict
        reference, not a snapshot), so the same resolver stays valid as
        the field table grows. Cache it: the report builder asks for one
        on every inline-measure / calc-column translation, and allocating
        a fresh closure each time is needless churn."""
        cached = getattr(self, "_field_resolver_cache", None)
        if cached is not None:
            return cached
        field_table = self.field_table

        def resolver(name: str) -> Optional[str]:
            n = (name or "").strip().strip("[]")
            if not n:
                return None
            san = _sanitize_column_name(n)
            owner = field_table.get(n) or field_table.get(san)
            if owner:
                return f"'{owner}'[{san}]"
            # Not a column. Qlik also permits a BARE ``varName`` (not only
            # ``$(varName)``) inside an expression -- resolve a known
            # variable to its materialised measure ref / inline scalar
            # rather than letting it fall through to a bogus
            # ``'home'[varName]`` column, which fails at refresh with
            # "Column 'varName' ... cannot be found".
            return self._resolve_bare_variable(n)

        self._field_resolver_cache = resolver
        return resolver

    def _resolve_bare_variable(self, name: str) -> Optional[str]:
        """Resolve a bare ``name`` that is NOT a column but IS a known
        Qlik variable. Returns the variable's materialised measure ref
        (``[var]``) or an inline scalar literal; ``None`` when ``name``
        isn't a variable or its body is an ``=expression`` we can't
        safely inline (caller then keeps its prior behaviour).

        Why only scalars: a config variable like ``var_lat_offset = 0``
        is referenced bare inside another expression and must inline as
        ``0``. An ``=expression`` body would need translation (and could
        recurse), so it's left for the normal ``$(var)`` materialisation
        path instead of inlining raw Qlik here."""
        mname = self.materialized_vars.get(name)
        if mname:
            return f"[{mname}]"
        cache = getattr(self, "_var_defs_raw", None)
        if cache is None:
            cache = {}
            for v in self.ir.get("variables", []) or []:
                vn = (v.get("qName") or "").strip()
                if vn:
                    cache[vn] = (v.get("qDefinition") or "")
            self._var_defs_raw = cache
        body = cache.get(name)
        if body is None:
            return None
        b = body.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", b):
            return b
        m = re.fullmatch(r"'([^']*)'", b) or re.fullmatch(r'"([^"]*)"', b)
        if m:
            return '"' + m.group(1).replace('"', '""') + '"'
        return None

    def _materialize_variables_as_measures(self) -> None:
        """Convert Qlik variables defined as expressions into real DAX
        measures. Each successfully materialised variable is registered
        in ``self.materialized_vars`` (Qlik name -> measure name) so the
        report's ``_var_lookup`` (and ``_build_measures`` below) can
        replace ``$(varX)`` references with the bare measure ref
        ``[varX]`` rather than re-inlining the body in every consumer.

        Skipped:
          - empty / plain config values (no leading ``=``)
          - parameterized macros (``$1``/``$2`` placeholders)
          - bodies whose translation stubs out or yields invalid DAX

        Materialisation runs in dependency order (topological sort) so
        a variable that references another variable sees the latter
        already exposed as a measure ref."""
        if not self.tables:
            return

        # Local imports to avoid cycles.
        from .dax_translator import translate_qlik_to_dax
        from .report import _extract_first_field

        # Build raw lookup of variable bodies.
        var_defs: Dict[str, str] = {}
        for v in self.ir.get("variables", []) or []:
            n = (v.get("qName") or "").strip()
            d = (v.get("qDefinition") or "")
            if n and d:
                var_defs[n] = d

        # Topo-sort by ``$(other)`` dependencies. Cycles are broken
        # arbitrarily; cap depth via ``visited`` so we never loop.
        dep_re = re.compile(r"\$\(\s*=?\s*([A-Za-z_]\w*)\s*\)")
        ordered: List[str] = []
        visited: Set[str] = set()
        in_progress: Set[str] = set()

        def visit(n: str) -> None:
            if n in visited or n in in_progress:
                return
            in_progress.add(n)
            for d in dep_re.findall(var_defs.get(n, "")):
                if d in var_defs:
                    visit(d)
            in_progress.discard(n)
            visited.add(n)
            ordered.append(n)

        for n in var_defs:
            visit(n)

        # Reserved-name set for measure-name dedupe (case-insensitive,
        # against every existing column).
        reserved_ci: Set[str] = set()
        for t in self.tables:
            for col in t["columns"]:
                reserved_ci.add((col["name"] or "").lower())

        def lookup_var(name: str) -> Optional[str]:
            # Materialised var -> bracketed measure-ref snippet.
            # Non-materialised -> original Qlik body for inline.
            mname = self.materialized_vars.get(name)
            if mname:
                return f"[{mname}]"
            return var_defs.get(name)

        # O(1) name index for ``lookup_meas`` instead of re-scanning
        # ``self.measures`` on every translated reference. At this stage
        # the only measures present are the ones THIS loop materialises
        # (it runs before ``_build_measures``), so a set of their exact
        # names -- maintained in place as we append -- is equivalent to
        # the old ``next(m for m in self.measures if m["name"] == name)``
        # scan but without the O(V x M) cost on a variable-heavy app.
        _materialised_names: Set[str] = {m["name"] for m in self.measures}

        def lookup_meas(name: str) -> Optional[str]:
            # Recognise any measure we've already materialised (matched
            # by exact name, as before). Symmetric with _build_measures'
            # lookup below.
            return name if name in _materialised_names else None

        field_resolver = self._make_field_resolver()
        count = 0
        for var_name in ordered:
            body = var_defs[var_name]
            if not _is_materialisation_candidate(body):
                continue
            # Pick home table from the first referenced real field.
            inner = body[1:].lstrip() if body.startswith("=") else body
            operand = _extract_first_field(
                inner,
                is_known=lambda n: n in self.field_table
                or _sanitize_column_name(n) in self.field_table,
            ) or ""
            home = (
                self.field_table.get(operand)
                or self.field_table.get(_sanitize_column_name(operand) if operand else "")
                or self.tables[0]["name"]
            )
            dax = translate_qlik_to_dax(
                body, home,
                variable_lookup=lookup_var,
                measure_lookup=lookup_meas,
                field_resolver=field_resolver,
            )
            if dax.startswith("BLANK() /* qlik:"):
                continue

            m_name = _sanitize_measure_name(var_name) or var_name
            if m_name.lower() in reserved_ci:
                b, i = m_name, 2
                m_name = f"{b} ({i})"
                while m_name.lower() in reserved_ci:
                    i += 1
                    m_name = f"{b} ({i})"
            self.measures.append({
                "name":         m_name,
                "table":        home,
                "expression":   dax,
                "formatString": "",
                "source":       body,
            })
            reserved_ci.add(m_name.lower())
            _materialised_names.add(m_name)
            self.materialized_vars[var_name] = m_name
            count += 1

        if count:
            _log.info(
                f"Materialised {count} Qlik variable(s) as DAX measures: "
                + ", ".join(sorted(self.materialized_vars.keys())[:8])
                + (f" (+{count - 8} more)" if count > 8 else "")
            )

    # ------------------------------------------------------------------
    def _build_measures(self) -> None:
        # Default home table for measures whose expression doesn't
        # reference any known field. Tables[0] only as a last resort.
        default_home = (
            self.tables[0]["name"] if self.tables else DEFAULT_TABLE_NAME
        )

        # Build a (name -> definition) lookup for variable expansion.
        var_defs: Dict[str, str] = {}
        for v in self.ir.get("variables", []) or []:
            vname = (v.get("qName") or "").strip()
            vdef = v.get("qDefinition") or ""
            if vname:
                var_defs[vname] = vdef

        def lookup_var(name: str) -> Optional[str]:
            # Materialised variable -> ``[varX]`` measure reference;
            # otherwise the raw Qlik body for inline expansion.
            mname = self.materialized_vars.get(name)
            if mname:
                return f"[{mname}]"
            return var_defs.get(name)

        # PBI rejects load if a measure shares a name with ANY column in
        # the model -- and the uniqueness check is CASE-INSENSITIVE.
        # Seed the reserved-name set with every column we declared, then
        # add measure names as we go, comparing lower-cased. Measures
        # already materialised from variables are also reserved.
        reserved_ci: Set[str] = set()
        for t in self.tables:
            for col in t["columns"]:
                reserved_ci.add((col["name"] or "").lower())
        for m in self.measures:
            reserved_ci.add((m["name"] or "").lower())

        # Measure-aware resolver so the translator's bare/bracketed
        # references hit ``[varX]`` (no table prefix) when the name is
        # one of our materialised variable measures.
        _materialised_names_ci = {n.lower() for n in self.materialized_vars.values()}

        def lookup_meas(name: str) -> Optional[str]:
            if name and name.lower() in _materialised_names_ci:
                return name
            return None

        # Local import to avoid the parser/report/model import cycle.
        from .report import _extract_first_field

        # Resolve each in-expression field to the column on its OWNING
        # table, so multi-table measure expressions emit correct refs.
        field_resolver = self._make_field_resolver()

        for m in self.ir.get("measures", []) or []:
            q_id = (m.get("qInfo", {}) or {}).get("qId", "")
            qmes = m.get("qMeasure", {}) or {}
            meta = m.get("qMetaDef", {}) or {}
            name = (
                meta.get("title")
                or qmes.get("qLabel")
                or clean_label(qmes.get("qLabelExpression", ""))
                or "Measure"
            )
            # Strip DAX-forbidden characters from the measure name. The
            # raw Qlik label is often the literal expression
            # ``Count(distinct [Field])`` which PBI rejects -- see
            # ``_sanitize_measure_name``.
            name = _sanitize_measure_name(name)
            expr = qmes.get("qDef", "")

            # Resolve the home table from the FIRST referenced field in
            # the expression rather than always using tables[0]. Without
            # this, a library measure like `Sum([Sales Margin Amount])`
            # gets homed on (e.g.) ProductGroup and the translator
            # rewrites the column ref to `'ProductGroup'[Sales Margin
            # Amount]` -- which doesn't exist there, producing a broken
            # DAX measure. Use the same logic as the inline-measure
            # path in report.py for consistency.
            operand_raw = _extract_first_field(
                expr,
                is_known=lambda n: n in self.field_table
                or _sanitize_column_name(n) in self.field_table,
            )
            home = default_home
            translate_src = expr
            if operand_raw:
                operand_san = _sanitize_column_name(operand_raw)
                resolved = (
                    self.field_table.get(operand_raw)
                    or self.field_table.get(operand_san)
                )
                if resolved:
                    home = resolved
                    # Substitute the sanitised column name into the
                    # source expression before translation, so the
                    # translator's emitted column ref matches the
                    # on-disk TMDL column name.
                    if operand_san and operand_san != operand_raw:
                        translate_src = expr.replace(
                            operand_raw, operand_san,
                        )

            # Disambiguate against columns AND earlier measures.
            # Preferred path: if the measure's expression is a simple
            # aggregation (Sum/Count/Avg/Min/Max/...) of a field, use
            # that aggregation in the name -- "Sum of Amount" reads as
            # a measure in PBI's field well, where "Amount (Measure)"
            # just looks like a duplicate column. Only fall back to the
            # generic suffix when no aggregation can be inferred.
            base_name = name
            if name.lower() in reserved_ci:
                preferred = _aggregated_measure_name(name, expr)
                if preferred and preferred.lower() not in reserved_ci:
                    name = preferred
                else:
                    candidate = preferred or f"{base_name} (Measure)"
                    i = 2
                    while candidate.lower() in reserved_ci:
                        candidate = f"{base_name} (Measure {i})"
                        i += 1
                    name = candidate

            dax = translate_qlik_to_dax(
                translate_src, home,
                variable_lookup=lookup_var,
                measure_lookup=lookup_meas,
                field_resolver=field_resolver,
            )
            fmt = _qlik_format_to_pbi(qmes.get("qNumFormat", {}) or {})

            reserved_ci.add(name.lower())
            self.measures.append({
                "name":       name,
                "table":      home,
                "expression": dax,
                "formatString": fmt,
                "source":     expr,
            })
            if q_id:
                self.measure_by_id[q_id] = name

    # ------------------------------------------------------------------
    # TMDL emit
    # ------------------------------------------------------------------
    def _reconcile_column_types_with_dax(self) -> None:
        """Fix column data types using how the measures actually use them.

        The loadmodel / empty-stub fallback (`_guess_type`) types every
        column ``string``. A measure that does ``SUM('T'[X])`` or filters
        ``'T'[F] = 1`` then fails at QUERY time -- "SUM cannot work with
        values of type String" / "DAX comparison operations do not
        support comparing values of type Text with values of type
        Integer". The measure expression is itself the strongest signal
        that a column is numeric, so:

          1. Promote a ``string`` column to ``int64`` / ``double`` when a
             measure SUMs/averages it, uses it in ``*`` / ``/``
             arithmetic, or compares it to a numeric literal. Only
             columns on tables NOT backed by a sniffed CSV are promoted
             -- the CSV's content is the authoritative type signal there,
             and the empty-stub partition (which derives its M type from
             ``dataType`` via ``_m_type_for``) has no data to fail the
             cast. In Qlik set analysis an UNQUOTED value (``{<F={1}>}``)
             already means F holds the number 1, so the promotion matches
             Qlik semantics.
          2. For a column that must stay text but is compared to a
             numeric literal, quote the literal so the DAX is a valid
             ``text = "1"`` instead of the failing ``text = 1``.

        Called from ``write_tmdl`` -- after the report builder has added
        its inline-chart measures -- so every measure is in scope.
        """
        if not self.measures:
            return

        # Tables whose partition is the EMPTY STUB -- the ONLY place a
        # dataType promotion is safe. The stub partition derives its M
        # type from ``dataType`` (``_m_type_for``) and has no rows to fail
        # a cast, so promoting string->numeric keeps TMDL<->M consistent.
        # CSV / live-DB / script-derived / what-if partitions get their
        # column types from an external source (sniffed ``mType``, the DB
        # schema, ``Table.PromoteHeaders`` with no cast, a GenerateSeries);
        # promoting ``dataType`` there would declare a numeric type the
        # partition actually delivers as text, and the column then fails
        # to load. Mirror ``_render_table_tmdl``'s partition-selection
        # order so the set matches exactly what gets emitted.
        stub_tables: Set[str] = set()
        for t in self.tables:
            if (t.get("what_if") is not None
                    or t.get("connection")
                    or t.get("csv")
                    or t.get("name") in self.table_csv):
                continue
            # A script-derived partition that resolves to a REAL source
            # (Csv.Document / Excel.Workbook / Odbc / ...) delivers typed data,
            # so promoting its dataType would mismatch what the partition
            # actually loads -- keep it excluded. BUT a QVD / unreadable source
            # emits an EMPTY ``#table({}, {})`` stub (PBI has no QVD reader),
            # which delivers NO rows -- it is a stub in disguise, so a numeric
            # measure-operand column on it MUST still be promoted. Without this,
            # the extremely common "fact table loaded from a .qvd" case leaves
            # e.g. ``Facts[Value]`` -- summed by ``SUMX('Facts', 'Facts'[Value]
            # * 'Accounts'[Weight])`` -- typed ``string``, so it shows as a
            # non-numeric field and the measure mis-evaluates / fails once real
            # data is bound. The empty stub has no rows to fail the cast, so
            # promotion is safe (same reasoning as the no-source stub).
            script_m = self._script_partition_m(t)
            if script_m and "#table({}, {})" not in script_m:
                continue
            stub_tables.add(t["name"])

        col_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for t in self.tables:
            for c in t["columns"]:
                if not c.get("expression"):   # skip calculated columns
                    col_index[(t["name"], c["name"])] = c

        agg_re = re.compile(r"\b(?:SUM|AVERAGE)\s*\(\s*'([^']+)'\[([^\]]+)\]\s*\)")
        mul_a  = re.compile(r"'([^']+)'\[([^\]]+)\]\s*[*/]")
        mul_b  = re.compile(r"[*/]\s*'([^']+)'\[([^\]]+)\]")
        cmp_a  = re.compile(r"'([^']+)'\[([^\]]+)\]\s*(=|<>|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\b")
        cmp_b  = re.compile(r"(?<![\w\].])(-?\d+(?:\.\d+)?)\s*(=|<>|<=|>=|<|>)\s*'([^']+)'\[([^\]]+)\]")
        # Set analysis emits ``'T'[Col] IN {v1, v2, ...}`` for multi-value
        # filters. When the value list is all-numeric, the column is
        # numeric (a string column there fails "comparing Text with
        # Integer"), so reconcile must see it too -- cmp_a/cmp_b only
        # catch a single scalar comparison.
        in_re   = re.compile(r"'([^']+)'\[([^\]]+)\]\s+IN\s+\{([^{}]*)\}")
        _num_lit = re.compile(r"-?\d+(?:\.\d+)?")

        def _numeric_in_list(vals: str) -> Optional[bool]:
            """True/False (has decimal?) when every value is numeric;
            None when the list is empty or has a non-numeric (string)
            value -- a text IN-list we leave alone."""
            parts = [v.strip() for v in vals.split(",") if v.strip()]
            if not parts or not all(_num_lit.fullmatch(p) for p in parts):
                return None
            return any("." in p for p in parts)

        wants_double: Set[Tuple[str, str]] = set()
        wants_int:    Set[Tuple[str, str]] = set()
        for m in self.measures:
            dax = m.get("expression") or ""
            for tbl, col in agg_re.findall(dax):
                wants_double.add((tbl, col))
            for tbl, col in mul_a.findall(dax):
                wants_double.add((tbl, col))
            for tbl, col in mul_b.findall(dax):
                wants_double.add((tbl, col))
            for tbl, col, _op, num in cmp_a.findall(dax):
                (wants_double if "." in num else wants_int).add((tbl, col))
            for num, _op, tbl, col in cmp_b.findall(dax):
                (wants_double if "." in num else wants_int).add((tbl, col))
            for tbl, col, vals in in_re.findall(dax):
                has_dec = _numeric_in_list(vals)
                if has_dec is not None:
                    (wants_double if has_dec else wants_int).add((tbl, col))

        # Pass 1: promote string columns proven numeric -- but ONLY on
        # empty-stub tables (see ``stub_tables`` above). Columns on CSV /
        # live / script / what-if partitions keep the type their source
        # delivers.
        promoted = 0
        for key in (wants_double | wants_int):
            if key[0] not in stub_tables:
                continue
            c = col_index.get(key)
            if c is not None and (c.get("dataType") or "").lower() == "string":
                c["dataType"] = "double" if key in wants_double else "int64"
                promoted += 1

        # Pass 2: a column compared to a numeric literal that is STILL
        # text (e.g. a CSV-backed text column we won't recast) -- quote
        # the literal so the comparison stays type-valid.
        text_cmp_cols = {
            key for key in (wants_double | wants_int)
            if ((col_index.get(key) or {}).get("dataType") or "").lower() == "string"
        }
        rewrites = 0
        if text_cmp_cols:
            def _q_a(m: "re.Match") -> str:
                nonlocal rewrites
                tbl, col, op, num = m.groups()
                if (tbl, col) in text_cmp_cols:
                    rewrites += 1
                    return f"'{tbl}'[{col}] {op} \"{num}\""
                return m.group(0)

            def _q_b(m: "re.Match") -> str:
                nonlocal rewrites
                num, op, tbl, col = m.groups()
                if (tbl, col) in text_cmp_cols:
                    rewrites += 1
                    return f"\"{num}\" {op} '{tbl}'[{col}]"
                return m.group(0)

            def _q_in(m: "re.Match") -> str:
                nonlocal rewrites
                tbl, col, vals = m.groups()
                if (tbl, col) not in text_cmp_cols or _numeric_in_list(vals) is None:
                    return m.group(0)
                rewrites += 1
                quoted = ", ".join(
                    f'"{v.strip()}"' for v in vals.split(",") if v.strip()
                )
                return f"'{tbl}'[{col}] IN {{{quoted}}}"

            for m in self.measures:
                expr = m.get("expression") or ""
                if "[" in expr:
                    m["expression"] = in_re.sub(
                        _q_in, cmp_b.sub(_q_b, cmp_a.sub(_q_a, expr))
                    )

        if promoted or rewrites:
            _log.info(
                f"Type reconciliation: promoted {promoted} column(s) to numeric "
                f"from measure usage; quoted {rewrites} text-comparison literal(s)."
            )

    def write_tmdl(self, sem_model_dir: Path) -> None:
        defdir = sem_model_dir / "definition"
        write_text(defdir / "database.tmdl",
                   "database\n\tcompatibilityLevel: 1600\n\n")
        write_text(defdir / "cultures" / "en-US.tmdl", "cultureInfo en-US\n")

        # Reconcile column data types with how the measures use them
        # (a SUM / numeric comparison proves a column is numeric) so a
        # string-typed stub column does not fail its measures at query
        # time. Here, not in build(), so the report builder's inline
        # chart measures are already part of self.measures.
        self._reconcile_column_types_with_dax()

        # Reset partition flag; `_render_table_tmdl` flips it to True
        # whenever a table emits a CSV partition that references
        # `RepoPath`.
        self._uses_repo_path = False

        for t in self.tables:
            write_text(
                defdir / "tables" / f"{safe_filename(t['name'])}.tmdl",
                self._render_table_tmdl(t),
            )

        write_text(defdir / "relationships.tmdl", self._render_relationships())

        ref_lines = "\n".join(
            f"ref table {tmdl_quote(t['name'])}" for t in self.tables
        )
        # `RepoPath` parameter expression - only emitted when at least
        # one CSV-backed partition references it. The default value is
        # the build-machine SemanticModel folder; PBI Desktop honours
        # it for the first load, and users repoint via
        # Model > Manage Parameters when the project moves machines.
        repo_block = ""
        if self._uses_repo_path:
            default_path = str(sem_model_dir.resolve())
            if default_path.startswith("\\\\?\\"):
                default_path = default_path[4:]
            default_path = default_path.replace('"', '""')
            repo_block = (
                "\n"
                f'expression RepoPath = "{default_path}" '
                'meta [IsParameterQuery=true, Type="Text", '
                'IsParameterQueryRequired=true]\n'
            )
        # `ref cultureInfo en-US` is what PBI Desktop emits at the end
        # of model.tmdl to bind the culture document. Omitting it leaves
        # the culture file unreferenced and Desktop rejects the load
        # with a generic "Failed to load file" error.
        write_text(defdir / "model.tmdl",
                   "model Model\n"
                   "\tculture: en-US\n"
                   "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                   "\tsourceQueryCulture: en-US\n"
                   "\tdataAccessOptions\n"
                   "\t\tlegacyRedirects\n"
                   "\t\treturnErrorValuesAsNull\n"
                   "\n"
                   "annotation PBI_TimeIntelligenceEnabled = 1\n"
                   "\n"
                   "annotation PBI_ProTooling = [\"DevMode\"]\n"
                   "\n"
                   f"{ref_lines}\n"
                   f"{repo_block}"
                   "\n"
                   "ref cultureInfo en-US\n")

    # ------------------------------------------------------------------
    def _render_table_tmdl(self, table: Dict[str, Any]) -> str:
        """Render a single table TMDL document.

        Property ordering inside each ``column`` block matches what
        Power BI Desktop emits when it saves a TMDL project — column
        order matters because the TMDL parser's indentation+ordering
        rules are stricter than the public schema suggests:

            column <Name>
                dataType: <dt>
                lineageTag: <uuid>
                summarizeBy: <none|sum>
                sourceColumn: <on-disk name>

                annotation SummarizationSetBy = Automatic

        The ``annotation`` block at the bottom of every column is what
        PBI Desktop's UI looks at to decide whether to expose a
        "Summarize By" picker. Without it, the column still loads but
        the picker is hidden.
        """
        name = table["name"]
        is_what_if = table.get("what_if") is not None
        lines: List[str] = [f"table {tmdl_quote(name)}"]
        lines.append(f"\tlineageTag: {lineage_tag('table', name)}")
        lines.append("")

        for col in table["columns"]:
            cname = col["name"]
            source_col = col.get("sourceColumn", cname)
            dt = col["dataType"]
            # Calculated column: TMDL form is ``column 'Name' = <DAX>``
            # with NO sourceColumn (the value comes from the expression,
            # not a partition column). Used for Qlik expression
            # dimensions (=MonthName(Date) etc.).
            calc_expr = col.get("expression")
            if calc_expr:
                # Flatten to one line (like measures): a calc-column DAX
                # expression with embedded newlines would otherwise put
                # its continuation lines at column 0 -- shallower than the
                # 1-tab ``column`` declaration -- which TMDL rejects with
                # "Invalid indentation was detected!". DAX is whitespace-
                # insensitive, so collapsing is loss-free.
                lines.append(f"\tcolumn {tmdl_quote(cname)} = {_flatten_expr(calc_expr)}")
                lines.append(f"\t\tdataType: {dt}")
                # Hidden helper columns (e.g. the chronological sort key
                # behind a "MMM yyyy" label) stay in the model so
                # ``sortByColumn`` resolves, but are kept out of the field
                # list.
                if col.get("isHidden"):
                    lines.append("\t\tisHidden")
                lines.append(f"\t\tlineageTag: {lineage_tag('col', name, cname)}")
                lines.append(f"\t\tsummarizeBy: {col.get('summarizeBy', 'none')}")
                # ``sortByColumn`` makes PBI order this column's values by
                # another column (a sortable numeric/date key) instead of
                # alphabetically -- e.g. a "MMM yyyy" month label sorted
                # chronologically by a YEAR*100+MONTH key.
                sort_by = col.get("sortByColumn")
                if sort_by:
                    lines.append(f"\t\tsortByColumn: {tmdl_quote(sort_by)}")
                lines.append("")
                lines.append("\t\tannotation SummarizationSetBy = Automatic")
                lines.append("")
                continue
            lines.append(f"\tcolumn {tmdl_quote(cname)}")
            lines.append(f"\t\tdataType: {dt}")
            # Engine-hidden fields ($hidden): keep the column in the model
            # (relationships / measures that reference it still resolve)
            # but hide it from the field list -- faithfully mirroring
            # Qlik's intent instead of cluttering the field well with
            # engine-internal helper fields.
            if col.get("isHidden"):
                lines.append("\t\tisHidden")
            # Format strings on columns:
            #   * What-If parameter columns keep their explicit
            #     ``#,##0.00`` format so the slicer renders nicely.
            #   * Date / dateTime columns keep their layout pattern
            #     (``yyyy-MM-dd``) -- without it, PBI prints the long
            #     ISO timestamp.
            #   * Every other data column gets NO formatString. The
            #     previous behaviour stamped ``#,##0`` / ``#,##0.00``
            #     defaults; PBI Desktop renders those literally in the
            #     data view when the column's load result does not
            #     match the declared numeric type. Leaving the format
            #     unset lets PBI's "Detect Data Type" decide the
            #     display when the user invokes it.
            fmt = col.get("formatString")
            if not fmt:
                if is_what_if or (dt or "").lower() in ("datetime", "date"):
                    fmt = _default_format_for_type(dt)
            if fmt:
                lines.append(f"\t\tformatString: {tmdl_quote(fmt)}")
            lines.append(f"\t\tlineageTag: {lineage_tag('col', name, cname)}")
            # SummarizeBy heuristics:
            #   * What-If parameter column -- ``none`` so PBI doesn't
            #     re-aggregate the slicer's selected value.
            #   * Identifier columns (typically int64 ending in ``ID``
            #     / ``_ID`` / ``Id``) -- ``none``; summing a PK is
            #     meaningless and the auto-aggregation makes the
            #     "Sum of HCO_ID" appear in the field well.
            #   * Strings -- ``none``.
            #   * Everything else numeric -- ``sum``.
            summarise = "sum"
            sum_set_by = "Automatic"
            dt_lower = (dt or "").lower()
            looks_like_id = (
                dt_lower in ("int64", "decimal")
                and (cname.lower().endswith("id")
                     or cname.lower().endswith("_id"))
            )
            if is_what_if:
                summarise = "none"
                sum_set_by = "User"
            elif dt_lower in ("string", "datetime", "date", "boolean", "time", "binary"):
                # Strings, dates and bools never make sense to ``sum``;
                # PBI Desktop hides the Sum option for them, so emit
                # ``none`` explicitly and let the user pick a relevant
                # aggregation on the field well (Count / Earliest /
                # Latest).
                summarise = "none"
            elif looks_like_id:
                summarise = "none"
            lines.append(f"\t\tsummarizeBy: {summarise}")
            lines.append(f"\t\tsourceColumn: {source_col}")
            lines.append("")
            lines.append(f"\t\tannotation SummarizationSetBy = {sum_set_by}")
            lines.append("")

        # Measures whose home is this table.
        for m in self.measures:
            if m["table"] != name:
                continue
            mname = m["name"]
            lines.append(f"\tmeasure {tmdl_quote(mname)} = {_flatten_expr(m['expression'])}")
            if m.get("formatString"):
                lines.append(f"\t\tformatString: {tmdl_quote(m['formatString'])}")
            lines.append(f"\t\tlineageTag: {lineage_tag('meas', name, mname)}")
            lines.append("")

        # M partition. Three shapes:
        #
        #   1. Live DB connection (when credentials.json matched the
        #      table -- handled by ``render_partition_m``):
        #        Source = Sql.Database / PostgreSQL.Database / ...
        #
        #   2. CSV-backed:
        #        Source         = Csv.Document(File.Contents(RepoPath & "/data/<file>"))
        #        PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars=true])
        #        ChangedTypes   = Table.TransformColumnTypes(PromotedHeaders, {...})
        #
        #   3. Empty stub (no data-dir match, no live connection):
        #        Source = Table.FromRows({}, type table [...])
        #
        # ``RepoPath`` is a model-level parameter that points at the
        # ``SemanticModel`` folder itself; we declare it in model.tmdl
        # iff at least one table partition references it.
        live = render_partition_m(table, type_table_kw={}) if table.get("connection") else None
        csv_path = table.get("csv")
        what_if = table.get("what_if")
        # ``final_step`` is the name of the M expression's last step --
        # PBI Desktop stamps this onto the partition as
        # ``PBI_NavigationStepName``. Without it, Desktop reports
        # "pending changes that haven't been applied" on every open,
        # because it can't verify which step of the M was the canonical
        # output the cached data was built from.
        final_step = "Source"
        if what_if is not None:
            # What-If parameter partition: GenerateSeries between
            # min and max with the configured step. PBI Desktop's
            # default "New Parameter" wizard emits exactly this.
            wmin = what_if["min"]
            wmax = what_if["max"]
            step = what_if["step"]
            col  = table["columns"][0]["name"]
            lines.append(f"\tpartition {tmdl_quote(name)} = m")
            lines.append("\t\tmode: import")
            lines.append("\t\tsource =")
            lines.append("\t\t\tlet")
            lines.append(
                f"\t\t\t\tSource = List.Numbers({wmin}, "
                f"Number.From(({wmax} - {wmin}) / {step}) + 1, {step}),"
            )
            lines.append(
                f"\t\t\t\tTable = Table.FromList(Source, "
                "Splitter.SplitByNothing(), null, null, ExtraValues.Error),"
            )
            lines.append(
                f"\t\t\t\tRenamed = Table.RenameColumns(Table, "
                f'{{{{"Column1", {_m_string(col)}}}}}),'
            )
            lines.append(
                f"\t\t\t\tTyped = Table.TransformColumnTypes(Renamed, "
                f'{{{{{_m_string(col)}, type number}}}})'
            )
            lines.append("\t\t\tin")
            lines.append("\t\t\t\tTyped")
            final_step = "Typed"
        elif live is not None:
            m_expr, mode = live
            lines.append(f"\tpartition {tmdl_quote(name)} = m")
            lines.append(f"\t\tmode: {mode}")
            lines.append("\t\tsource =")
            # m_expr already carries leading "\t\t\t" indentation per line.
            lines.append(m_expr.rstrip())
            # Derive the final step from the M itself (the token after the
            # closing ``in``) -- it's "Navigation", "NativeQuery", or "Renamed"
            # depending on the connector shape and whether columns were renamed.
            _ms = m_expr.rstrip().splitlines()
            final_step = _ms[-1].strip() if _ms else "Navigation"
        elif csv_path:
            rel = f"/data/{Path(csv_path).name}"
            self._uses_repo_path = True
            if Path(csv_path).suffix.lower() == ".parquet":
                # Parquet carries its column types in the file schema, so
                # the partition is a SINGLE step: no PromoteHeaders, no
                # ``Table.TransformColumnTypes`` cast (the CSV cast is what
                # can fail at refresh -- "We couldn't convert to Number").
                # ``File.Contents`` on a LOCAL path gives random access, so
                # there's no "streamed binary values" error and no size
                # limit beyond Import's normal model cap. ``Parquet.Document``
                # options are left unset (TypeMapping = null) to preserve
                # maximum type fidelity. See docs/large-data-strategy.md.
                lines.append(f"\tpartition {tmdl_quote(name)} = m")
                lines.append("\t\tmode: import")
                lines.append("\t\tsource =")
                lines.append("\t\t\tlet")
                lines.append(
                    f"\t\t\t\tSource = Parquet.Document(File.Contents("
                    f"RepoPath & {_m_string(rel)}))"
                )
                lines.append("\t\t\tin")
                lines.append("\t\t\t\tSource")
                final_step = "Source"
            else:
                type_fields = ", ".join(
                    f"{{{_m_string(col['sourceColumn'])}, {col.get('mType') or 'type text'}}}"
                    for col in table["columns"]
                    if col.get("sourceColumn") and not col.get("expression")
                )
                lines.append(f"\tpartition {tmdl_quote(name)} = m")
                lines.append("\t\tmode: import")
                lines.append("\t\tsource =")
                lines.append("\t\t\tlet")
                lines.append(
                    f"\t\t\t\tSource = Csv.Document(File.Contents(RepoPath & "
                    f"{_m_string(rel)}), [Delimiter=\",\", Encoding=65001, "
                    f"QuoteStyle=QuoteStyle.Csv]),"
                )
                lines.append(
                    "\t\t\t\tPromotedHeaders = Table.PromoteHeaders(Source, "
                    "[PromoteAllScalars=true]),"
                )
                # Vanilla ``Table.TransformColumnTypes`` -- the exact form
                # PBI Desktop itself emits when a user opens a CSV. We
                # tried a ``List.Select`` pre-filter against column names
                # to tolerate missing fields, but PBI's M parser loses
                # the inner-list type during the projection and ``_{0}``
                # returns an opaque value -- the predicate then evaluates
                # false for every entry, the cast list ends up empty and
                # every column loads as plain text. That breaks Sum /
                # Count / Avg aggregations on numeric columns (they
                # render as quoted strings like ``'1'`` in the data
                # view). Going back to the canonical PBI Desktop shape
                # restores proper int64 / double / dateTime typing. Since
                # the type list is sniffed from the same CSV we're
                # loading, the names always match.
                lines.append(
                    f"\t\t\t\tChangedTypes = Table.TransformColumnTypes(PromotedHeaders, {{{type_fields}}})"
                )
                lines.append("\t\t\tin")
                lines.append("\t\t\t\tChangedTypes")
                final_step = "ChangedTypes"
        else:
            # Script-derived partition: when parser.py extracted a
            # LOAD block whose target table matches this one, use the
            # block's M expression (Csv.Document / Excel.Workbook /
            # Qvd.Tables / etc.) instead of the empty stub. The user
            # can still repoint the source in PBI Desktop's "Transform
            # Data" if the path needs adjusting -- the M expression
            # carries the original Qlik source as a guide.
            script_m = self._script_partition_m(table)
            if script_m:
                lines.append(f"\tpartition {tmdl_quote(name)} = m")
                lines.append("\t\tmode: import")
                lines.append("\t\tsource =")
                # ``script_to_m`` returns canonical M -- ``let``/``in`` at
                # column 0, steps at 2 spaces. TMDL requires the
                # expression block to be indented *deeper* than its
                # ``source =`` property line (2 tabs), so re-indent to the
                # same depth every other branch here uses: structural
                # ``let``/``in`` at 3 tabs, step lines at 4 tabs. Emitting
                # the raw column-0 M is what produced Desktop's "Invalid
                # indentation was detected!" load error.
                for raw in script_m.rstrip().splitlines():
                    stripped = raw.strip()
                    if not stripped:
                        lines.append("")
                    elif stripped == "let" or stripped == "in" \
                            or stripped.startswith("in "):
                        lines.append("\t\t\t" + stripped)
                    else:
                        lines.append("\t\t\t\t" + stripped)
                # Pull final step name out of the M expression. M's
                # ``let ... in <Step>`` may put the step on the ``in``
                # line itself (``in Step``) or on the line below.
                # Falling back to Source is safe.
                m_tail = script_m.rstrip().split("\n")[-1].strip()
                if m_tail.startswith("in "):
                    final_step = m_tail[3:].strip() or "Source"
                elif m_tail == "in":
                    final_step = "Source"
                else:
                    final_step = m_tail or "Source"
            else:
                type_fields = ", ".join(
                    f"{_m_quote_ident(col['name'])} = {_m_type_for(col['dataType'])}"
                    for col in table["columns"]
                    if not col.get("expression")
                ) or "Value = type any"
                lines.append(f"\tpartition {tmdl_quote(name)} = m")
                lines.append("\t\tmode: import")
                lines.append("\t\tsource =")
                lines.append("\t\t\tlet")
                lines.append(
                    f"\t\t\t\tSource = Table.FromRows({{}}, type table [{type_fields}])"
                )
                lines.append("\t\t\tin")
                lines.append("\t\t\t\tSource")
                final_step = "Source"

        # Table-level annotations. PBI Desktop itself emits these at
        # *table* scope (one tab, sibling of ``column`` / ``partition``
        # blocks), AFTER the partition body, with a blank line between
        # each annotation. Putting them inside the ``partition`` block
        # is syntactically accepted but breaks PBI's PQ Editor round-
        # trip (it cannot map back to a "current step" and the table's
        # M cannot be visually edited). See microsoft/terraform-
        # provider-fabric#372 for the canonical PBI Desktop emit.
        lines.append("")
        lines.append(f"\tannotation PBI_NavigationStepName = {final_step}")
        lines.append("")
        lines.append(f"\tannotation PBI_ResultType = Table")
        lines.append("")
        if is_what_if:
            # What-If parameter marker so PBI Desktop's Modeling pane
            # treats the table as a numeric parameter source. Without
            # this, the synthesised table loads as plain data and the
            # parameter never appears in the "Parameters" UI.
            lines.append("\tannotation PBI_Parameter = True")
            lines.append("")
        return "\n".join(lines)

    def _script_partition_m(self, table: Dict[str, Any]) -> Optional[str]:
        """Return the script-derived M expression for ``table`` or None.

        Walks ``ir['script_blocks']`` for an entry whose table name
        matches ours (case-insensitive). When found, returns the
        ready-to-use M expression that ``script_to_m`` already built --
        Csv.Document(File.Contents(...)) for CSVs,
        Excel.Workbook(File.Contents(...)) for XLSX, etc.

        We skip resident / inline / unknown blocks: there's no useful
        partition to emit (resident requires a previous table; inline
        we could materialise but the source data probably isn't here).
        Returns None for any of those so the caller falls back to the
        empty stub.
        """
        blocks = self.ir.get("script_blocks") or []
        if not blocks:
            return None
        tname_lower = (table.get("name") or "").lower()
        for b in blocks:
            if (b.get("table") or "").lower() != tname_lower:
                continue
            src_type = (b.get("source_type") or "").lower()
            if src_type in ("resident", "inline", "unknown", ""):
                continue
            m_expr = b.get("m_expression") or ""
            if not m_expr.strip():
                continue
            return m_expr
        return None

    def _assign_relationship_cardinality(self) -> None:
        """Set every relationship to **many-to-many, single-direction**.

        This is unconditional and deliberate -- the converter NEVER emits
        a many-to-one relationship automatically. Reasoning:

        * Qlik's associative model is many-to-many by nature: a shared
          field name associates two tables where EITHER side may repeat.
          Composite / synthetic keys (``AggKey = '103-10015824-roadway'``
          appearing on multiple rows), bridge/link tables, and blank keys
          are all routine and legitimate in Qlik.
        * PBI's many-to-one imposes a one-side uniqueness-and-non-blank
          constraint Qlik never had, and a wrong many-to-one is
          LOAD-FATAL: PBI aborts the entire refresh with *"Column 'X'
          contains a duplicate value '...' ... not allowed on the one side
          of a many-to-one relationship"*, which cascades as opaque
          ``OLE DB or ODBC error: 0x80040E4E`` / "Load was cancelled by an
          error in loading a previous table" across every other table.
        * The converter CANNOT reliably prove one-side uniqueness at
          convert time. We tried reading the bound CSV -- but cloud fetch
          is row-capped (~500 rows; see data-fetch-modes), so a sample
          that looks unique does NOT mean the full column is, and the
          upgrade then fails at refresh against the complete data. Other
          paths (engine-sampled extracts, unfetched stubs, parsing/locale
          differences vs PBI's ``Csv.Document``) are equally unreliable.

        The consequences are asymmetric: a false "unique" breaks the
        whole report; staying many-to-many merely makes a relationship
        "limited" (slightly slower, a few advanced DAX patterns differ).
        So we ALWAYS emit many-to-many and let the user tighten specific
        relationships to many-to-one in Desktop, where PBI validates
        against the actually-loaded full data and reports if it's safe.

        ``_render_relationships`` adds ``crossFilteringBehavior:
        oneDirection`` so the filter still propagates dimension -> fact
        exactly like a many-to-one would for ordinary fact-by-dimension
        visuals (no over-counting, no bidirectional-path ambiguity that
        PBI would reject at load)."""
        for rel in self.relationships:
            rel["toCardinality"] = "many"
        if self.relationships:
            _log.info(
                f"Relationship cardinality: all {len(self.relationships)} "
                "emitted many-to-many, single-direction (Qlik-associative; "
                "avoids the load-fatal duplicate-on-one-side error -- tighten "
                "to many-to-one in Desktop where data proves uniqueness)."
            )

    def _render_relationships(self) -> str:
        """Render relationships.tmdl.

        ``fromTable``/``fromColumn`` is the "many" side (fact); ``toTable``
        /``toColumn`` is the dimension side, by both
        ``_extract_relationships_from_engine`` and
        ``_infer_relationships_from_shared_fields``.

        ``_assign_relationship_cardinality`` sets every ``toCardinality``
        to **many** (see there for the full rationale: PBI's many-to-one
        needs a one-side uniqueness Qlik never enforced and the converter
        cannot reliably verify, and a wrong one is load-fatal). For each
        many-to-many we emit ``crossFilteringBehavior: oneDirection`` so
        the filter still propagates dimension -> fact exactly like a
        many-to-one would for ordinary visuals, and PBI's ``automatic``
        resolution can't pick bidirectional (which can make the load
        ambiguous). The ``to_card == "one"`` branch is kept defensively
        (it emits the plain single-direction form PBI Desktop uses) in
        case a caller ever sets a proven-unique relationship by hand, but
        the automatic pipeline never does.
        """
        chunks: List[str] = []
        for r in self.relationships:
            to_card = r.get("toCardinality", "many")
            line = (
                f"relationship {r['name']}\n"
                f"\tfromCardinality: many\n"
                f"\ttoCardinality: {to_card}\n"
            )
            if to_card == "many":
                line += "\tcrossFilteringBehavior: oneDirection\n"
            line += (
                f"\tfromColumn: {tmdl_quote(r['fromTable'])}.{tmdl_quote(r['fromColumn'])}\n"
                f"\ttoColumn: {tmdl_quote(r['toTable'])}.{tmdl_quote(r['toColumn'])}\n"
            )
            chunks.append(line)
        return "\n".join(chunks) + ("\n" if chunks else "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Column-name conventions -> PBI type, for the loadmodel / empty-stub
# fallback (no engine qTags, no CSV to sniff). Case-sensitive: the
# date/number word must be a real CamelCase / UPPER suffix, not a
# coincidental lowercase substring -- so "Update" / "Discount" do NOT
# read as date / count.
_NAME_DATE_RE = __import__("re").compile(
    r"(?:^|[A-Za-z0-9_ ])(?:Date|DateTime|Datetime|Timestamp)$|(?:DT|DTTM)$"
)
_NAME_NUM_RE = __import__("re").compile(
    r"(?:^|[A-Za-z0-9_ ])"
    r"(?:Amount|Count|Quantity|Number|Rate|Ratio|Percent|Balance|Charge|Charges"
    r"|Cost|Price|Score|Weight|Total)$"
    r"|(?:AMT|QTY|NBR|CNT|PCT)$"
)


def _guess_type(field_name: str) -> str:
    """Heuristic TMDL type from the column name, for the loadmodel /
    empty-stub fallback (no engine ``qTags``, no CSV to sniff).

    Qlik's loadmodel carries no per-field data type, but Qlik field
    names follow strong conventions we can map: a ``...DT`` / ``...Date``
    suffix is a date, and ``...AMT`` / ``...Count`` / ``...Rate`` /
    ``...QTY`` etc. are numeric. Applying these ONLY here is safe -- this
    path backs an EMPTY ``Table.FromRows({})`` partition (no rows to fail
    a cast against); the type just gives PBI a sensible storage / format
    default and lets date columns drive date hierarchies. Columns
    actually aggregated or compared in a measure are further corrected by
    ``_reconcile_column_types_with_dax``; CSV-backed and engine-tagged
    (qTags) columns never reach this fallback. Unknown names stay
    ``string`` -- the type Power Query always accepts.
    """
    n = (field_name or "").strip()
    if not n:
        return "string"
    if _NAME_DATE_RE.search(n):
        return "dateTime"
    if _NAME_NUM_RE.search(n):
        return "double"
    return "string"


def _default_format_for_type(data_type: str) -> str:
    """Return a sensible default format string for a TMDL data type.

    PBI's default rendering for dates is the long ISO string; for
    decimals it's the precision-free machine literal. Stamping a
    per-type default keeps charts and tables looking like the Qlik
    original without forcing every column to be explicit.
    """
    # Lower-cased keys: callers normalise with ``.lower()`` so a
    # camelCase ``"dateTime"`` from the parser still hits the table.
    return {
        "datetime": "yyyy-MM-dd HH:mm:ss",
        "date":     "yyyy-MM-dd",
        "double":   "#,##0.00",
        "int64":    "#,##0",
        "decimal":  "#,##0.00",
    }.get((data_type or "").strip().lower(), "")


def _qlik_format_to_pbi(num_format: Dict[str, Any]) -> str:
    """Translate a Qlik ``qNumFormat`` block into a DAX/PBI format string.

    DAX understands a large subset of Excel's format-string grammar
    (``#,##0.00``, ``"$"#,##0;("$"#,##0)``, ``YYYY-MM-DD``...). Qlik's
    ``qNumFormat`` carries:
      * ``qType``: ``"U"`` (default), ``"M"`` (money), ``"D"`` (date),
                   ``"T"`` (time), ``"TS"`` (timestamp), ``"F"`` (fraction),
                   ``"IV"`` (interval), ``"R"`` (real)
      * ``qFmt``: the literal format pattern when set
      * ``qnDec`` / ``qDec`` / ``qThou``: numeric precision + separators
      * ``qUseThou``: 0/1 toggle for thousand separator
      * ``qDecimals``: alt decimal precision

    For known patterns we emit the exact DAX-friendly string. For
    Qlik expression formats (``=Date(...)``, ``=if(...)``) we drop
    back to a sensible default per ``qType`` -- DAX rejects an
    expression-as-format and would fail load otherwise.
    """
    if not isinstance(num_format, dict):
        return ""

    raw_fmt = (num_format.get("qFmt") or "").strip()
    qtype   = (num_format.get("qType") or "U").strip().upper()
    n_dec   = num_format.get("qnDec")
    if n_dec is None:
        n_dec = num_format.get("qDecimals")
    use_thou = num_format.get("qUseThou")
    if use_thou is None:
        use_thou = 1 if "," in raw_fmt else 0

    # Expression formats (=Date(Max(Date),'MM/DD/YYYY')) are not valid
    # PBI format strings. Drop to a default for the type.
    if raw_fmt.startswith("="):
        raw_fmt = ""

    # Type-specific defaults when no explicit pattern.
    #
    # Design choice: for unknown / real (U / R) qtypes with only a
    # ``qnDec`` precision hint, we DO NOT synthesise a format string
    # for the measure. Two reasons:
    #
    #   1. The underlying column already carries a type-derived
    #      formatString (``#,##0`` for int64, ``#,##0.00`` for double,
    #      ``yyyy-MM-dd`` for dateTime). Measures aggregating that
    #      column inherit it in PBI Desktop's data view without us
    #      having to re-translate Qlik's qnDec hint.
    #   2. Qlik's ``qnDec`` is often a wild value (e.g. 10) that does
    #      not represent the author's intent; respecting it as a format
    #      yields garbage like ``#,##0.0000000000``.
    #
    # We still emit explicit formats for qtype = M (money) / D / T /
    # TS / IV because those ARE explicit type signals from the author.
    if not raw_fmt:
        if qtype == "M":   # money
            try:
                dec = int(n_dec) if n_dec is not None else 2
            except (TypeError, ValueError):
                dec = 2
            decimals = ("." + "0" * dec) if dec > 0 else ""
            thou = "#," if use_thou else "#"
            return f'"$"{thou}##0{decimals};"$"-{thou}##0{decimals}'
        if qtype == "D":
            return "yyyy-MM-dd"
        if qtype == "T":
            return "HH:mm:ss"
        if qtype == "TS":
            return "yyyy-MM-dd HH:mm:ss"
        if qtype == "IV":
            return "[h]:mm:ss"
        # F (fraction), U (unknown), R (real) and everything else: let
        # the column's data-type default drive the measure's display.
        return ""

    # Explicit Qlik pattern: most are DAX-compatible verbatim. A few
    # quirks to normalise:
    #   * Qlik uses '%' as a literal percent suffix; DAX needs the
    #     '%' to be inside the pattern body to scale automatically.
    #   * Qlik's date tokens are case-insensitive (``YYYY`` = ``yyyy``);
    #     DAX prefers the lower-case month / upper-case year split:
    #         yyyy / MM / dd / HH / mm / ss
    pat = raw_fmt
    if qtype in ("D", "T", "TS"):
        # Normalise common date tokens. DAX is permissive but
        # PBI Desktop's parser sometimes mis-renders mixed-case patterns.
        pat = (pat
               .replace("YYYY", "yyyy").replace("YYY", "yyy").replace("YY", "yy")
               .replace("DD", "dd")
               .replace("HH24", "HH").replace("HH12", "hh")
               .replace("hh:mm", "HH:mm"))  # default to 24h unless author marked am/pm
    else:
        # Numeric patterns: Qlik often emits ``###,#`` / ``###,###`` /
        # ``# ##0`` etc. -- DAX requires a literal ``0`` in the last
        # integer position and ``,`` as the thousand separator. Without
        # this rewrite PBI's data view renders the format string
        # literally.
        pat = _normalise_numeric_format(pat)
    return pat


_NUMERIC_BODY_RE = __import__("re").compile(r"^[#0,.\s]+$")
_DATE_TOKEN_RE   = __import__("re").compile(r"[YyMdHhSs]{2,}|am/pm|AM/PM", __import__("re").IGNORECASE)
_CURRENCY_PREFIX_RE = __import__("re").compile(r'^("[^"]*"|[$€£¥])\s*')


def _normalise_numeric_format(pat: str) -> str:
    """Convert Qlik-flavoured numeric format strings into DAX-valid form.

    Qlik commonly emits patterns like ``###,#`` (no terminating ``0``),
    ``###,###``, ``# ##0`` (space thousand sep) or ``###,###.##``.
    DAX requires:
      * A ``0`` in the last integer position (``#,##0``); otherwise
        the data view renders the format string literally.
      * ``,`` as thousand separator (not space / dot).

    Patterns we recognise and rewrite:
      * ``###,#``         -> ``#,##0``
      * ``###,###``       -> ``#,##0``
      * ``###,###.##``    -> ``#,##0.##``
      * ``# ##0``         -> ``#,##0``
      * ``0%`` / ``0.0%`` -> kept verbatim (already valid)
      * ``$#,##0.00``     -> kept verbatim
      * Date / time / sign-template patterns are passed through.
    """
    if not pat:
        return pat
    # Pass date / time patterns through untouched.
    if _DATE_TOKEN_RE.search(pat):
        return pat
    # Sign template: normalise each segment independently.
    if ";" in pat:
        return ";".join(_normalise_numeric_format(p) for p in pat.split(";"))
    # Peel off currency prefix tokens so they survive the rewrite.
    prefix = ""
    body = pat
    while True:
        m = _CURRENCY_PREFIX_RE.match(body)
        if not m:
            break
        prefix += m.group(0)
        body = body[m.end():]
    # Peel off trailing % / ‰ suffix.
    suffix = ""
    if body.endswith("%"):
        suffix, body = "%", body[:-1]
    elif body.endswith("‰"):
        suffix, body = "‰", body[:-1]
    body = body.strip()
    # Only normalise pure digit-shape bodies. Anything else is passed
    # through (custom string formats, escape sequences, etc.).
    if not body or not _NUMERIC_BODY_RE.match(body):
        return pat
    if "." in body:
        ipart, dpart = body.rsplit(".", 1)
    else:
        ipart, dpart = body, ""
    # Only add thousands grouping when the Qlik mask actually carried a
    # separator. The old "4+ digits -> group" rule mangled zero-padded
    # codes: ``0000`` (a 4-digit year / store code) became ``#,##0`` and
    # rendered ``2024`` as ``2,024``. A wide all-# mask without a
    # separator just becomes ``0`` (no grouping) -- showing ``1000000``
    # instead of ``1,000,000`` is cosmetic; corrupting a code is not.
    has_thou = ("," in ipart) or (" " in ipart)
    if has_thou:
        ipart_norm = "#,##0"
    else:
        ipart_norm = "0"
    dpart_clean = dpart.replace(",", "").replace(" ", "")
    if dpart_clean:
        norm = f"{ipart_norm}.{dpart_clean}"
    else:
        norm = ipart_norm
    return f"{prefix}{norm}{suffix}"


_AGG_PRETTY = {
    "sum":   "Sum of",
    "count": "Count of",
    "avg":   "Average of",
    "min":   "Min of",
    "max":   "Max of",
    "only":  "Only",
    "first": "First of",
    "last":  "Last of",
}

_AGG_RE = __import__("re").compile(
    r"\s*=?\s*(Sum|Count|Avg|Min|Max|Only|First|Last)\s*\(\s*"
    r"(?:distinct\s+)?"
    r"\[?([A-Za-z0-9_ .\-]+?)\]?\s*\)\s*$",
    __import__("re").IGNORECASE,
)


def _is_search_string_body(body: str) -> bool:
    """True when a Qlik variable body is a SEARCH/RANGE-STRING macro, e.g.
    ``vD_YTD = ='>=$(=MakeDate(...))<=$(=MonthEnd(...))'``.

    Such a body is Qlik text meant to be substituted verbatim inside a set
    modifier (``Date={"$(vD_YTD)"}``) -- it is NOT a scalar value. Materialising
    it as a DAX measure makes ``$(var)`` resolve to a measure ref and the set
    filter come out as ``'Facts'[Date] = "([var])"`` (dateTime vs text -> a
    query-time error). So these are excluded from materialisation and left for
    inline range-expansion by the translator instead."""
    if not body:
        return False
    s = _strip_comments(body).strip()
    if s.startswith("="):
        s = s[1:].strip()
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return s[:1] in ("<", ">")


def _is_materialisation_candidate(body: str) -> bool:
    """Return True for Qlik variable bodies safe to materialise as DAX
    measures. Skips plain config values (no leading ``=``), parameterised
    macros (``$1`` placeholders), search/range-string macros (handled inline
    as set-analysis range filters), and anything that's clearly a snippet
    rather than a full expression."""
    if not body:
        return False
    # Strip comments first -- a variable body may lead with a ``//`` line
    # comment whose newline precedes the ``=`` that marks it as an
    # expression. Without this the candidate would be rejected (and the
    # variable never materialised) just because a comment came first.
    s = _strip_comments(body).strip()
    if not s.startswith("="):
        return False
    inner = s[1:].strip()
    if not inner:
        return False
    if re.search(r"\$[1-9]", inner):
        return False
    # A search/range-string macro (``='>=...<=...'``) is Qlik text for a set
    # modifier, not a value measure -- leave it for inline range-expansion.
    if _is_search_string_body(body):
        return False
    # Pure set-analysis fragment ``<F={V}>`` with no surrounding
    # aggregator -- can't be a DAX measure on its own.
    if (inner.startswith("<") or inner.startswith("{<")) and not re.search(
        r"\b(Sum|Count|Avg|Min|Max|Only|Median|Stdev|Fractile)\s*\(",
        inner, re.IGNORECASE,
    ):
        return False
    return True


def _aggregated_measure_name(original_name: str, qlik_expr: str) -> Optional[str]:
    """Produce a more semantic name for a measure that collides with a
    column.

    For a Qlik measure named ``Amount`` with ``qDef = "=Sum(Amount)"``,
    the user-visible name in PBI is better as ``Sum of Amount`` than
    the literal ``Amount (Measure)`` collision suffix. We detect the
    pattern ``<Agg>(<field>)`` (optionally wrapped in ``=...``) and
    return ``"<Sum of/Count of/etc.> <field>"``.

    Returns None if the expression isn't a recognised single-agg
    pattern -- the caller then falls back to the generic suffix.
    """
    if not qlik_expr:
        return None
    m = _AGG_RE.match(qlik_expr.strip())
    if not m:
        return None
    agg = (m.group(1) or "").lower()
    operand = (m.group(2) or "").strip()
    if not operand:
        return None
    pretty = _AGG_PRETTY.get(agg)
    if not pretty:
        return None
    # Sanitise: the operand comes from a Qlik expression and may carry a
    # ``.`` / other char that's forbidden in a DAX measure name (the
    # caller uses this name verbatim). e.g. ``Sum(HCO.Region)`` ->
    # ``Sum of HCO_Region``.
    return _sanitize_measure_name(f"{pretty} {operand}")


_SANITIZE_RE = __import__("re").compile(r"[^A-Za-z0-9 _\-]")

# DAX-forbidden characters in table / column / measure NAMES (per
# Microsoft Learn's DAX Syntax Reference). These cannot appear in
# identifiers even when single-quoted in TMDL -- ``[`` / ``]`` collide
# with the DAX column-reference grammar; ``(`` / ``)`` and ``{`` /
# ``}`` collide with function-call / list grammar; and so on. A measure
# whose NAME contains any of these silently fails to load and every
# visual referencing it shows up empty -- a real-world failure mode we
# hit with the Qlik default measure name ``"Count(distinct [FieldX])"``.
#
# We strip these wherever a user-facing name lands in the model. The
# replacement is a single space (collapsed below) so word boundaries
# survive: ``Count(distinct [X])`` -> ``Count distinct X``.
_DAX_FORBIDDEN_RE = __import__("re").compile(r"[.,;:/\\*|?&%$!+=()\[\]{}<>'\"@#`~^]")
_COLLAPSE_WS_RE = __import__("re").compile(r"\s+")


def _sanitize_column_name(name: str) -> str:
    """Replace TMDL-hostile characters with underscores.

    PBI's TMDL parser uses ``.`` as the table.column separator inside
    quoted refs. A column called ``HCP.City`` thus collides with the
    grammar even when wrapped in quotes -- a relationship pointing at
    ``HCP.'HCP.City'`` is parsed as a three-segment reference. We
    rewrite dots (and other separators) to underscores.
    """
    cleaned = _SANITIZE_RE.sub("_", name or "").strip(" _")
    return cleaned or "Column"


# Table names that Power BI / Analysis Services RESERVE: the AS model-schema
# validator rejects the WHOLE file at load if a table uses one. "measures"
# collides with MDX's special ``[Measures]`` dimension -- the Feb-2025 PBI
# Desktop validation that raises
# ``Unsupported Table name "Measures" has been found in data model schema``
# (ModelSchemaValidationFailed). A Qlik app can legitimately have a table
# called "Measures", so we remap it to a safe, deterministic, readable name.
# Compared case-insensitively (the leading-space "[ Measures]" variant also
# normalises here because the sanitiser strips leading spaces first).
_RESERVED_TABLE_NAMES = {"measures"}
# Suffix appended to a reserved name; chosen so re-sanitising is stable
# ("Measures Table".casefold() is not reserved) and it survives the
# forbidden-char strip (space + word, no special chars).
_RESERVED_TABLE_SUFFIX = " Table"


def _sanitize_table_name(name: str) -> str:
    """Same hygiene as ``_sanitize_column_name`` but keep spaces — PBI
    happily handles spaces in table names when they're quoted with
    single quotes. A name colliding with a Power BI RESERVED table name
    (see ``_RESERVED_TABLE_NAMES``) is remapped so the file actually opens
    in Desktop instead of failing model-schema validation."""
    cleaned = _SANITIZE_RE.sub("_", name or "").strip(" _")
    cleaned = cleaned or "Table"
    if cleaned.casefold() in _RESERVED_TABLE_NAMES:
        cleaned = cleaned + _RESERVED_TABLE_SUFFIX
    return cleaned


def _sanitize_measure_name(name: str) -> str:
    """Strip DAX-forbidden characters from a measure name.

    Qlik routinely auto-labels measures with the literal expression --
    ``"Count(distinct [FieldX])"`` is the default for a count-distinct.
    Those parentheses / brackets / etc. are illegal in DAX identifiers
    (see ``_DAX_FORBIDDEN_RE``) so PBI Desktop silently refuses to load
    the measure and every visual that references it renders empty.

    Forbidden runs are replaced with a space; multiple spaces collapse;
    leading/trailing whitespace is trimmed. The result preserves the
    original tokens so the cleaned name still reads naturally:

        ``Count(distinct [From_HCP_ID-HCP_ID])`` -> ``Count distinct From_HCP_ID-HCP_ID``

    Hyphens are NOT stripped: per the DAX spec they are valid in
    identifiers, and removing them breaks compound IDs like
    ``From_HCP_ID-HCP_ID`` that we already accept as column names.
    """
    if not name:
        return "Measure"
    # Map Qlik's symbol prefixes to words BEFORE the forbidden-char strip
    # deletes them. Without this, "# of Admissions" collapses to the
    # badly-reading "of Admissions" and "% Unemployed" to just
    # "Unemployed". '#' is Qlik's "number of" idiom; '%' is percent.
    s = re.sub(r"#\s*of\b", "Number of", name, flags=re.IGNORECASE)
    s = re.sub(r"#\s*(?=[A-Za-z])", "Number of ", s)
    # '%' as a percent indicator ("% Unemployed", "Margin %") -> word.
    # NOT a digit-adjacent '%' (a format/value like "0%", "100%") --
    # leave those for the forbidden-char strip so a measure label that
    # embeds a format string doesn't gain a stray "Percent".
    s = re.sub(r"(?<![\d.])%", " Percent ", s)
    cleaned = _DAX_FORBIDDEN_RE.sub(" ", s)
    cleaned = _COLLAPSE_WS_RE.sub(" ", cleaned).strip(" _-")
    return cleaned or "Measure"


def _m_string(s: str) -> str:
    """Quote a Python string as a Power Query M string literal.

    M strings use ``"`` delimiters; embedded ``"`` is doubled. No
    backslash escaping (unlike C/Python).
    """
    return '"' + (s or "").replace('"', '""') + '"'


def _m_quote_ident(name: str) -> str:
    """Quote a column name for use inside a Power Query M record/type
    expression. M's identifier syntax is restrictive; the safe form is
    ``#"col with spaces"`` which accepts any character except ``"``.
    Embedded quotes are doubled.
    """
    escaped = (name or "").replace('"', '""')
    return f'#"{escaped}"'


def _m_type_for(dt: str) -> str:
    """Map our internal dataType to the M-language type literal used
    inside ``type table [...]`` declarations."""
    return {
        "string":   "text",
        "double":   "number",
        "int64":    "Int64.Type",
        "boolean":  "logical",
        "dateTime": "datetime",
    }.get(dt, "text")


def _flatten_expr(expr: str) -> str:
    if not expr:
        return "BLANK()"
    flat = expr.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    while "  " in flat:
        flat = flat.replace("  ", " ")
    return flat.strip()
