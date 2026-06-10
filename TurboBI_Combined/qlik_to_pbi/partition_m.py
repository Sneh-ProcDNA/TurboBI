"""Partition-M expression rendering for live database connections.

When a Qlik table has been matched to a credentials entry (see
``credentials.py``), this module emits the Power Query M expression
that points PBI at the live source instead of the empty stub /
CSV-import path the converter defaults to.

Supported live connectors:

  * ``sqlserver``  -> ``Sql.Database(server, db) -> [Schema/Item]``
  * ``postgres``   -> ``PostgreSQL.Database(server:port, db) -> ...``
  * ``redshift``   -> ``AmazonRedshift.Database(server:port, db) -> ...``
  * ``snowflake``  -> ``Snowflake.Databases(server, warehouse) -> [Name/Data]``
  * ``databricks`` -> ``DatabricksMultiCloud.Catalogs(server, http_path) -> ...``

When a credentials entry carries a ``query`` (custom SQL), the
partition uses ``Value.NativeQuery`` instead of the navigation
shape -- useful when the operator wants to repoint a Qlik table at
a different SQL view without editing the load script.

Public API:
    * ``render_partition_m(table_dict, type_table_kw)`` -- returns
      ``(m_expr, partition_mode)`` for live connections; ``None``
      when the caller should fall back to the CSV / empty-stub path.
    * ``is_live_connection(conn)`` -- predicate the caller consults
      to decide whether to attempt the live path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger

_log_conn = get_logger("CONN")


_SUPPORTED_CLASSES = {
    "sqlserver", "mssql",
    "postgres", "postgresql",
    "redshift",
    "snowflake",
    "databricks",
    "azure-databricks",
    "azuredatabricks",
    "databricks-sql",
    "spark-sql",
    "spark",
}

_DATABRICKS_CLASSES = {
    "databricks",
    "azure-databricks",
    "azuredatabricks",
    "databricks-sql",
    "spark-sql",
    "spark",
}


def is_live_connection(conn: Optional[Dict[str, Any]]) -> bool:
    """Return True when ``conn`` describes a live source we can render."""
    if not isinstance(conn, dict):
        return False
    cls = (conn.get("class") or "").strip().lower()
    return cls in _SUPPORTED_CLASSES and bool(conn.get("server"))


def _escape_m_string(s: str) -> str:
    """M strings double embedded ``"``. No backslash escaping."""
    return (s or "").replace('"', '""')


def _normalize_host(server: str) -> str:
    """Normalise a server hostname for the connector functions.

    The connectors (Databricks / Snowflake / Sql.Database / ...) want a BARE
    hostname -- no scheme, no path, no trailing slash. A very common paste
    mistake is the full ``https://adb-xxx.azuredatabricks.net`` (or a trailing
    ``/``), which makes the connector build a malformed URL and fail at session
    open with a 404 (THttpTransport). Strip the scheme + any trailing path/slash
    so the bare host is what reaches the connector."""
    s = (server or "").strip()
    for scheme in ("https://", "http://", "wss://", "ws://"):
        if s.lower().startswith(scheme):
            s = s[len(scheme):]
            break
    # Drop anything after the first '/' (a pasted path) and trailing slashes.
    s = s.split("/", 1)[0].strip().rstrip("/")
    return s


def _normalize_http_path(http_path: str) -> str:
    """Normalise a Databricks SQL-warehouse HTTP path: trimmed, leading ``/``,
    no trailing slash. The Databricks "Connection details" tab shows it as
    ``/sql/1.0/warehouses/<id>``; a value missing the leading slash (or with a
    pasted ``https://host`` prefix) yields a 404 at session open."""
    p = (http_path or "").strip()
    if not p:
        return p
    # If a full URL was pasted, keep only the path part.
    for scheme in ("https://", "http://"):
        if p.lower().startswith(scheme):
            rest = p[len(scheme):]
            slash = rest.find("/")
            p = rest[slash:] if slash != -1 else ""
            break
    p = p.rstrip("/")
    if p and not p.startswith("/"):
        p = "/" + p
    return p


def render_partition_m(
    table:           Dict[str, Any],
    type_table_kw:   Dict[str, str],
) -> Optional[Tuple[str, str]]:
    """Emit a partition-M expression for a live connection.

    Returns ``(m_expr, partition_mode)`` when the table's
    ``connection`` dict describes a supported live source. Returns
    ``None`` otherwise, signalling the caller should fall back to
    the existing CSV / empty-stub path.

    ``partition_mode`` is one of ``"import"`` / ``"directQuery"``.
    DirectQuery is used for the SQL connectors so credential changes
    take effect without a refresh; switch to import in PBI Desktop
    if folding hurts performance.
    """
    conn = table.get("connection") or {}
    cls  = (conn.get("class") or "").strip().lower()
    if cls not in _SUPPORTED_CLASSES:
        return None

    server   = _normalize_host(conn.get("server", ""))
    dbname   = conn.get("dbname", "") or conn.get("database", "")
    schema   = conn.get("schema", "")
    if not server:
        return None

    # Partition mode: ``import`` (load the source into the model -- the faithful
    # analog of a Qlik in-memory app) or ``directQuery`` (query live). Defaults
    # to directQuery for back-compat with the credentials-driven path; the
    # script-detected DB-source path sets ``import``.
    mode = (conn.get("mode") or "directQuery").strip().lower()
    if mode not in ("import", "directquery"):
        mode = "directQuery"
    elif mode == "directquery":
        mode = "directQuery"

    # Source table name: prefer an explicit override from the
    # credentials entry (so the operator can rename Qlik tables
    # without breaking the binding), then fall back to the PBI
    # table name itself.
    source_table = (
        conn.get("source_table")
        or conn.get("table_name")
        or table.get("source_table")
        or table.get("name")
        or ""
    ).strip()

    # Custom SQL override (rare for Qlik but supported for parity).
    custom_sql_list = conn.get("customSql") or []
    custom_sql = (
        custom_sql_list[0]["sql"]
        if custom_sql_list and isinstance(custom_sql_list[0], dict)
        else ""
    )

    # ------------------------------------------------------------------
    # SQL Server
    # ------------------------------------------------------------------
    if cls in ("sqlserver", "mssql"):
        if custom_sql:
            escaped = _escape_m_string(custom_sql)
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = Sql.Database("{server}", "{dbname}"),\n'
                f'\t\t\t\tNativeQuery = Value.NativeQuery(Source, "{escaped}", null, '
                "[EnableFolding=true])\n"
                "\t\t\tin\n"
                "\t\t\t\tNativeQuery\n"
            )
        else:
            used_schema = schema or "dbo"
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = Sql.Database("{server}", "{dbname}"),\n'
                f'\t\t\t\tNavigation = Source{{[Schema="{used_schema}",'
                f'Item="{source_table}"]}}[Data]\n'
                "\t\t\tin\n"
                "\t\t\t\tNavigation\n"
            )
        return m_expr, mode

    # ------------------------------------------------------------------
    # PostgreSQL / Redshift (similar shape)
    # ------------------------------------------------------------------
    if cls in ("postgres", "postgresql", "redshift"):
        normalized_cls = "postgres" if cls in ("postgres", "postgresql") else "redshift"
        port = conn.get("port") or ("5432" if normalized_cls == "postgres" else "5439")
        host = f"{server}:{port}" if server else server
        if normalized_cls == "postgres":
            if custom_sql:
                escaped = _escape_m_string(custom_sql)
                m_expr = (
                    "\t\t\tlet\n"
                    f'\t\t\t\tSource = PostgreSQL.Database("{host}", "{dbname}"),\n'
                    f'\t\t\t\tNativeQuery = Value.NativeQuery(Source, "{escaped}", null, '
                    "[EnableFolding=true])\n"
                    "\t\t\tin\n"
                    "\t\t\t\tNativeQuery\n"
                )
            else:
                used_schema = schema or "public"
                m_expr = (
                    "\t\t\tlet\n"
                    f'\t\t\t\tSource = PostgreSQL.Database("{host}", "{dbname}"),\n'
                    f'\t\t\t\tNavigation = Source{{[Schema="{used_schema}",'
                    f'Item="{source_table}"]}}[Data]\n'
                    "\t\t\tin\n"
                    "\t\t\t\tNavigation\n"
                )
            return m_expr, mode
        # Redshift navigation (AmazonRedshift.Database)
        used_schema = schema or "public"
        m_expr = (
            "\t\t\tlet\n"
            f'\t\t\t\tSource = AmazonRedshift.Database("{host}", "{dbname}"),\n'
            f'\t\t\t\tNavigation = Source{{[Schema="{used_schema}",'
            f'Item="{source_table}"]}}[Data]\n'
            "\t\t\tin\n"
            "\t\t\t\tNavigation\n"
        )
        return m_expr, mode

    # ------------------------------------------------------------------
    # Snowflake
    # ------------------------------------------------------------------
    if cls == "snowflake":
        warehouse = conn.get("warehouse", "")
        db        = conn.get("db") or dbname
        sch       = schema or "PUBLIC"
        if custom_sql:
            # `Snowflake.Databases(...)` returns a Table of databases;
            # `Value.NativeQuery` rejects a multi-DB Table value, so
            # navigate to a single Database first.
            escaped = _escape_m_string(custom_sql)
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = Snowflake.Databases("{server}", '
                f'"{warehouse}", [Implementation="2.0"]),\n'
                f'\t\t\t\tDatabase = Source{{[Name="{db}"]}}[Data],\n'
                f'\t\t\t\tNativeQuery = Value.NativeQuery(Database, "{escaped}", null, '
                "[EnableFolding=true])\n"
                "\t\t\tin\n"
                "\t\t\t\tNativeQuery\n"
            )
        else:
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = Snowflake.Databases("{server}", '
                f'"{warehouse}", [Implementation="2.0"]),\n'
                f'\t\t\t\tDatabase = Source{{[Name="{db}"]}}[Data],\n'
                f'\t\t\t\tSchema = Database{{[Name="{sch}"]}}[Data],\n'
                f'\t\t\t\tNavigation = Schema{{[Name="{source_table}"]}}[Data]\n'
                "\t\t\tin\n"
                "\t\t\t\tNavigation\n"
            )
        return m_expr, mode

    # ------------------------------------------------------------------
    # Databricks
    # ------------------------------------------------------------------
    if cls in _DATABRICKS_CLASSES:
        http_path = _normalize_http_path(
            conn.get("http_path")
            or conn.get("httppath")
            or conn.get("http path")
            or conn.get("warehouse")
            or ""
        )
        catalog = (
            conn.get("catalog")
            or conn.get("database")
            or conn.get("db")
            or conn.get("dbname")
            or ""
        )
        sch = schema or ""
        connector_fn = (
            conn.get("connector_function")
            or "DatabricksMultiCloud.Catalogs"
        )

        options_parts = [
            'Catalog=""',
            'Database=""',
            "QueryTags=null",
            "EnableAutomaticProxyDiscovery=null",
            'Implementation="2.0"',
        ]
        options_record = "[" + ", ".join(options_parts) + "]"

        used_catalog = catalog or "hive_metastore"
        used_schema  = sch or "default"

        # Optional raw->model column rename (Table.RenameColumns). The Qlik
        # LOAD layer renames some source columns ([Rep_ID] AS [fact_crm.Rep_ID]);
        # the model's columns carry the post-LOAD names, but the Databricks
        # source delivers the RAW names, so rename them to line up. ``column_renames``
        # is a list of (raw, model) pairs the model derives from the script.
        rename_step = _rename_columns_step(conn.get("column_renames") or [])
        if custom_sql:
            escaped = _escape_m_string(custom_sql)
            base = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = {connector_fn}("{server}", "{http_path}", {options_record}),\n'
                f'\t\t\t\tCatalog = Source{{[Name="{used_catalog}", Kind="Database"]}}[Data],\n'
                f'\t\t\t\tNativeQuery = Value.NativeQuery(Catalog, "{escaped}", null, '
                "[EnableFolding=true])"
            )
            m_expr = _close_let(base, "NativeQuery", rename_step)
        else:
            base = (
                "\t\t\tlet\n"
                f'\t\t\t\tSource = {connector_fn}("{server}", "{http_path}", {options_record}),\n'
                f'\t\t\t\tCatalog = Source{{[Name="{used_catalog}", Kind="Database"]}}[Data],\n'
                f'\t\t\t\tSchema = Catalog{{[Name="{used_schema}", Kind="Schema"]}}[Data],\n'
                f'\t\t\t\tNavigation = Schema{{[Name="{source_table}", Kind="Table"]}}[Data]'
            )
            m_expr = _close_let(base, "Navigation", rename_step)
        return m_expr, mode

    return None


def _rename_columns_step(renames: List[Any]) -> str:
    """Return the M ``{{"raw","model"}, ...}`` rename list, or ``""`` when
    there's nothing to rename. Each entry is a ``(raw, model)`` pair (only
    pairs where the names actually differ are emitted)."""
    pairs = [
        (str(r), str(m)) for r, m in renames
        if r and m and str(r) != str(m)
    ]
    if not pairs:
        return ""
    inner = ", ".join(
        f'{{"{_escape_m_string(r)}", "{_escape_m_string(m)}"}}' for r, m in pairs
    )
    return "{" + inner + "}"


def _close_let(base_steps: str, last_step: str, rename_list: str) -> str:
    """Close a ``let`` block. When ``rename_list`` is non-empty, append a
    ``Renamed = Table.RenameColumns(<last_step>, <list>)`` step and return on
    ``Renamed``; otherwise return on ``last_step``. ``base_steps`` is the
    ``let`` + steps WITHOUT a trailing comma on its final line."""
    if rename_list:
        return (
            base_steps + ",\n"
            f"\t\t\t\tRenamed = Table.RenameColumns({last_step}, {rename_list})\n"
            "\t\t\tin\n"
            "\t\t\t\tRenamed\n"
        )
    return base_steps + "\n\t\t\tin\n" + f"\t\t\t\t{last_step}\n"


def manifest_entry_for(
    table_name: str,
    conn:       Dict[str, Any],
    cred_entry: Any,
) -> Dict[str, Any]:
    """Build one row for ``credentials_manifest.json``.

    Captures everything the user needs to wire up the data source in
    PBI Desktop -- WITHOUT including the secret itself.
    """
    cls = (conn.get("class") or "").strip().lower()
    entry: Dict[str, Any] = {
        "table":    table_name,
        "class":    cls,
        "server":   conn.get("server", ""),
        "database": conn.get("dbname", "") or conn.get("database", ""),
        "schema":   conn.get("schema", ""),
    }
    if conn.get("warehouse"):
        entry["warehouse"] = conn["warehouse"]
    if conn.get("http_path"):
        entry["http_path"] = conn["http_path"]
    if conn.get("catalog"):
        entry["catalog"] = conn["catalog"]

    # Only NAMES of secrets are reported, never the values.
    if cred_entry is not None:
        if getattr(cred_entry, "username", ""):
            entry["username"] = cred_entry.username
        entry["password_supplied"] = bool(getattr(cred_entry, "password", ""))
        entry["token_supplied"]    = bool(getattr(cred_entry, "token", ""))
    return entry
