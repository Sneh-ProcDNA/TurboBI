"""Partition-M expression rendering.

A "partition M" is the Power Query expression that defines where a
PBI table's data comes from. The Tableau converter emits one of:

  * ``Csv.Document(File.Contents(...))`` — when a Hyper extract was
    converted to a CSV and the converter chose import mode.
  * ``Sql.Database(...) / PostgreSQL.Database(...) / Snowflake.Databases(...) /
    DatabricksMultiCloud.Catalogs(...)`` — for live connections, with
    optional ``Value.NativeQuery`` wrapping when the workbook embeds
    custom SQL.
  * ``Table.FromRows({}, type table [...])`` — placeholder for
    connectors we don't synthesise (Redshift, others).

Each branch is a pure function of the parsed connection metadata. The
SemanticModel orchestrator decides which branch applies; this module
owns the actual M-string construction.

Public API:
    * ``render_partition_m(table_dict, type_table_kw)`` — returns
      (m_expr, partition_mode) for live connections; None when caller
      should fall back to the import / placeholder path.
    * ``render_unsupported_partition(table_dict, cls, type_table_kw)`` —
      placeholder Table.FromRows for unsynthesised live connectors.
    * ``is_databricks_live_connection(conn)`` — predicate consulted by
      the orchestrator to decide whether to override an extract path
      with the live Databricks branch.
    * ``prefer_live_over_extract(conn)`` — boolean flag (defaults to
      True) carried from the credentials file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger
from .utils import escape_m_string

_log_conn = get_logger("CONN")


_FILE_LIVE_CLASSES = {"excel-direct", "textscan", "csv", "json"}

_DATABRICKS_CLASSES = {
    "databricks",
    "azure-databricks",
    "azuredatabricks",
    "databricks-sql",
    "spark-sql",
    "spark",
}


def is_databricks_live_connection(conn: Dict[str, Any]) -> bool:
    """True when ``conn`` describes a Databricks live source with the
    minimum metadata (server + http_path / warehouse) we need to emit
    a working ``DatabricksMultiCloud.Catalogs`` partition.
    """
    cls = (conn.get("class") or "").strip().lower()
    return (
        cls in _DATABRICKS_CLASSES
        and bool(conn.get("server"))
        and bool(
            conn.get("http_path")
            or conn.get("httppath")
            or conn.get("http path")
            or conn.get("warehouse")
        )
    )


def prefer_live_over_extract(conn: Dict[str, Any]) -> bool:
    """Return the ``prefer_live_over_extract`` flag from a credentials-
    enriched connection dict. Defaults to True — when both an extract
    and a live source are available, the converter biases toward live
    so credential overrides take effect without editing the .twb.
    """
    value = conn.get("prefer_live_over_extract")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def render_unsupported_partition(
    t:             Dict[str, Any],
    cls:           str,
    type_table_kw: Dict[str, str],
) -> Tuple[str, str]:
    """Placeholder for live connectors we don't synthesize today.

    Emits the same ``Table.FromRows({})`` shape used for empty extracts,
    prefixed with a TODO comment so the user can find the spot to
    rewire the connection. ``mode`` stays ``"import"`` so PBI loads
    cleanly.
    """
    _log_conn.warning(
        f"live connection class '{cls}' - "
        f"emitting placeholder Table.FromRows."
    )
    m_pairs: List[str] = []
    seen: set = set()
    for col in t["columns"]:
        if col.get("daxColumnExpr"):
            continue
        src = col["sourceCol"]
        if not src or src.lower() in seen:
            continue
        seen.add(src.lower())
        mt  = type_table_kw.get(col["tmdlType"], "text")
        esc = src.replace('"', '""')
        m_pairs.append(f'#"{esc}" = {mt}')
    schema = ", ".join(m_pairs) if m_pairs else ""
    if schema:
        body = (
            f"\t\t\t    Source = Table.FromRows({{}}, "
            f"type table [{schema}])\n"
        )
    else:
        body = "\t\t\t    Source = Table.FromRows({})\n"
    m_expr = (
        "\t\t\tlet\n"
        f"\t\t\t    // TODO: live connection class '{cls}' - "
        "rewire the connector in PBI Desktop\n"
        + body
        + "\t\t\tin\n"
        "\t\t\t    Source\n"
    )
    return m_expr, "import"


def render_partition_m(
    t:             Dict[str, Any],
    type_table_kw: Dict[str, str],
) -> Optional[Tuple[str, str]]:
    """Emit a partition-M expression for live connections.

    Returns a (m_expr, partition_mode) tuple when the table's
    connection metadata is known and we can build a source. Returns
    None when there's no live-connection class — caller falls back to
    the original Table.FromRows placeholder.

    ``partition_mode`` is one of ``"import"`` / ``"directQuery"``.
    DirectQuery is used for sqlserver / postgres / snowflake live
    connections; every other case falls through to import.
    """
    conn = t.get("connection") or {}
    cls  = (conn.get("class") or "").strip().lower()
    if not cls or cls in ("federated", "hyper", "extract"):
        # Federated wrapper / hyper extract / no connection -> use
        # the original placeholder behaviour.
        return None
    # File-based connectors (excel/textscan/csv) carry hyper-extract
    # data through the parent <federated> wrapper. When CSV path
    # binding succeeded those tables already used the Csv.Document
    # branch — only the tables WITHOUT a CSV land here. They were
    # working with empty Table.FromRows before this change; preserve
    # that to keep the smoke-test corpus stable. Re-routing them
    # through Excel.Workbook would need a full file-path rewrite that
    # isn't part of A/B/C/D scope.
    if cls in _FILE_LIVE_CLASSES:
        return None

    # Custom SQL takes precedence: if the datasource carries a
    # <relation type='text'>/'query'>, prefer Value.NativeQuery for
    # live (DirectQuery) and Sql.Database with [Query=...] for
    # Import-mode-with-no-Hyper. When a Hyper extract exists, the
    # CSV path was already chosen upstream — this branch only fires
    # in the fallback (else) branch where csv_path is None.
    custom_sql_list = t.get("customSql") or []
    custom_sql = custom_sql_list[0]["sql"] if custom_sql_list else ""

    server   = conn.get("server", "")
    dbname   = conn.get("dbname", "")
    schema   = conn.get("schema", "")

    # Look for a representative source table name. For SQL
    # connectors, this is the parent caption captured on the
    # table — most workbooks pin it to the actual physical name.
    # `caption` is the parsed parent-table caption; falls back
    # to the table's name (which is the unique safe-claim'd one).
    source_table = (t.get("caption") or t.get("name") or "").strip()

    if cls == "sqlserver":
        if custom_sql:
            # DirectQuery custom SQL -> Value.NativeQuery (M-native
            # query folding) on top of an Sql.Database connection.
            # NOTE: when a Hyper extract exists upstream, the CSV
            # path is preferred because the data is already baked.
            escaped = escape_m_string(custom_sql)
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = Sql.Database("{server}", "{dbname}"),\n'
                f'\t\t\t    NativeQuery = Value.NativeQuery(Source, "{escaped}", null, '
                "[EnableFolding=true])\n"
                "\t\t\tin\n"
                "\t\t\t    NativeQuery\n"
            )
        else:
            used_schema = schema or "dbo"
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = Sql.Database("{server}", "{dbname}"),\n'
                f'\t\t\t    Navigation = Source{{[Schema="{used_schema}",'
                f'Item="{source_table}"]}}[Data]\n'
                "\t\t\tin\n"
                "\t\t\t    Navigation\n"
            )
        return m_expr, "directQuery"

    if cls in ("postgres", "redshift"):
        # Postgres uses `Server:port` as the M-syntax host argument.
        # Fall back to 5432 only when we actually need to interpolate;
        # leaving port out of the M call entirely doesn't work for the
        # PostgreSQL.Database connector — it requires the port suffix.
        port = conn.get("port") or ("5432" if cls == "postgres" else "5439")
        host = f"{server}:{port}" if server else server
        if cls == "postgres":
            if custom_sql:
                escaped = escape_m_string(custom_sql)
                m_expr = (
                    "\t\t\tlet\n"
                    f'\t\t\t    Source = PostgreSQL.Database("{host}", "{dbname}"),\n'
                    f'\t\t\t    NativeQuery = Value.NativeQuery(Source, "{escaped}", null, '
                    "[EnableFolding=true])\n"
                    "\t\t\tin\n"
                    "\t\t\t    NativeQuery\n"
                )
            else:
                used_schema = schema or "public"
                m_expr = (
                    "\t\t\tlet\n"
                    f'\t\t\t    Source = PostgreSQL.Database("{host}", "{dbname}"),\n'
                    f'\t\t\t    Navigation = Source{{[Schema="{used_schema}",'
                    f'Item="{source_table}"]}}[Data]\n'
                    "\t\t\tin\n"
                    "\t\t\t    Navigation\n"
                )
            return m_expr, "directQuery"
        # Redshift falls into the unsupported branch — it can use
        # AmazonRedshift.Database but we don't synthesize that here.
        return render_unsupported_partition(t, cls, type_table_kw)

    if cls == "snowflake":
        warehouse = conn.get("warehouse", "")
        db        = conn.get("db") or dbname
        sch       = schema or "PUBLIC"
        if custom_sql:
            # Value.NativeQuery requires a Database-typed value as its
            # first argument. `Snowflake.Databases(server, warehouse)`
            # returns a Table of databases (the catalog listing), so
            # passing it directly raises::
            #     Expression.Error: Native queries aren't supported by
            #     this value.  Details: [Table]
            # Navigate to `[Name=<db>][Data]` first to obtain a real
            # Database value before invoking NativeQuery. The schema
            # level is NOT needed — Snowflake's NativeQuery resolves
            # fully-qualified names inside the SQL itself.
            escaped = escape_m_string(custom_sql)
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = Snowflake.Databases("{server}", '
                f'"{warehouse}", [Implementation="2.0"]),\n'
                f'\t\t\t    Database = Source{{[Name="{db}"]}}[Data],\n'
                f'\t\t\t    NativeQuery = Value.NativeQuery(Database, "{escaped}", null, '
                "[EnableFolding=true])\n"
                "\t\t\tin\n"
                "\t\t\t    NativeQuery\n"
            )
        else:
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = Snowflake.Databases("{server}", '
                f'"{warehouse}", [Implementation="2.0"]),\n'
                f'\t\t\t    Database = Source{{[Name="{db}"]}}[Data],\n'
                f'\t\t\t    Schema = Database{{[Name="{sch}"]}}[Data],\n'
                f'\t\t\t    Navigation = Schema{{[Name="{source_table}"]}}[Data]\n'
                "\t\t\tin\n"
                "\t\t\t    Navigation\n"
            )
        return m_expr, "directQuery"

    # Other live connectors — emit a placeholder with a TODO so the
    # user can rewire the connection in PBI Desktop. We still produce
    # a valid Table.FromRows shape so the model loads.
    if cls in _DATABRICKS_CLASSES:
        http_path = (
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

        # Defaults used by BOTH the custom-SQL branch (which needs
        # to navigate to a Database-typed value before NativeQuery)
        # and the no-SQL branch (which navigates all the way to a
        # physical table). Computed once so the two branches stay
        # in sync on which catalog/schema is picked.
        used_catalog = catalog or "hive_metastore"
        used_schema  = sch or "default"

        if custom_sql:
            # Value.NativeQuery rejects the multi-catalog Table that
            # `DatabricksMultiCloud.Catalogs(...)` returns directly
            # (Expression.Error: Native queries aren't supported by
            # this value. Details: [Table]). Navigate one level into
            # the named catalog so we pass a Database value. The
            # schema layer is unnecessary — Databricks NativeQuery
            # accepts fully-qualified `catalog.schema.table` refs
            # inside the SQL.
            escaped = escape_m_string(custom_sql)
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = {connector_fn}("{server}", "{http_path}", {options_record}),\n'
                f'\t\t\t    Catalog = Source{{[Name="{used_catalog}", Kind="Database"]}}[Data],\n'
                f'\t\t\t    NativeQuery = Value.NativeQuery(Catalog, "{escaped}", null, '
                "[EnableFolding=true])\n"
                "\t\t\tin\n"
                "\t\t\t    NativeQuery\n"
            )
        else:
            # No custom SQL: navigate all the way to the named
            # table. `used_catalog`/`used_schema` were computed
            # above and carry the credentials-or-default fallback.
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = {connector_fn}("{server}", "{http_path}", {options_record}),\n'
                f'\t\t\t    Catalog = Source{{[Name="{used_catalog}", Kind="Database"]}}[Data],\n'
                f'\t\t\t    Schema = Catalog{{[Name="{used_schema}", Kind="Schema"]}}[Data],\n'
                f'\t\t\t    Navigation = Schema{{[Name="{source_table}", Kind="Table"]}}[Data]\n'
                "\t\t\tin\n"
                "\t\t\t    Navigation\n"
            )

        return m_expr, "directQuery"

    # Fall-through for connector classes that weren't matched anywhere
    # above (matches the pre-refactor behaviour of returning None
    # implicitly when no branch fired).
    return None
