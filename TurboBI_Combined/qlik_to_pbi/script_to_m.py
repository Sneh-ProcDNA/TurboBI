"""Qlik LOAD script -> Power Query M translator.

Merged in from a sibling project. Parses a Qlik LOAD script into
discrete :class:`LoadBlock`s, each carrying the table name, fields,
source path, and a synthesised M (Power Query) expression that loads
the data.

Handles:

* ``LOAD field1, field2 FROM source (options);``
* ``TableName: LOAD ... FROM ...;``
* ``LOAD * FROM ...;``
* ``field AS alias``
* ``JOIN [LOAD] ... FROM ...;`` (LEFT/RIGHT/INNER)
* ``CONCATENATE [LOAD] ...;``
* ``RESIDENT tableName LOAD ... WHERE ...;``
* ``WHERE`` clause -> ``Table.SelectRows``
* ``STORE table INTO file`` -> flagged, not translated
* ``FOR / NEXT`` loops -> flagged with note
* ``$(varName)`` expansion (substituted when known, else flagged)
* Source types: csv, xlsx, qvd, inline, json, xml, odbc, oledb, lib://

The output M expressions are written verbatim into the TMDL partition
``source`` block by :mod:`qlik_to_pbi.writer`. Callers wire this into
the partition build when ``--use-script-m`` (or similar) is set --
otherwise the loadmodel-driven stub/CSV partition is used.

This module is **opt-in**. Adding LOAD-driven partitions is invasive --
they replace the existing empty-stub / Csv.Document partitions wholesale --
so callers should request it explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class LoadBlock:
    table_name: str
    fields: List[str]
    source: str
    source_type: str          # csv | xlsx | qvd | resident | inline | unknown
    join_type: Optional[str]  # None | 'inner' | 'left' | 'right' | 'outer'
    is_concatenate: bool
    where_clause: Optional[str]
    m_expression: str
    notes: List[str]


class ScriptTranslator:
    """Translate a Qlik load script into :class:`LoadBlock`s.

    Pass any known static variables in the constructor so ``$(varName)``
    references inside LOAD statements are expanded before parsing.
    Dynamic ``=`` variables are deliberately left unresolved.
    """

    TABLE_PREFIX_RE = re.compile(
        r"(?:(?:^|;)\s*)(\w+)\s*:\s*"
        r"(?=(?:LEFT|RIGHT|INNER|OUTER|JOIN|CONCATENATE|LOAD)\b)",
        re.I | re.S,
    )

    LOAD_RE = re.compile(
        r"""
        (?:(?P<join>LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|OUTER\s+JOIN|JOIN)\s+)?
        (?P<concat>CONCATENATE\s*)?
        LOAD\s+(?P<fields>.+?)\s+
        (?:FROM\s+(?P<source>\[[^\]]+\]|`[^`]+`|'[^']+'|\S+?)
           (?:\s*\((?P<options>[^)]*)\))?
        |RESIDENT\s+(?P<resident>\w+)
        |INLINE\s+\[(?P<inline>[^\]]*)\]
        )
        (?:\s+WHERE\s+(?P<where>.+?))?
        \s*;
        """,
        re.I | re.S | re.X,
    )

    def __init__(self, variables: Optional[Dict[str, str]] = None):
        self._vars: Dict[str, str] = variables or {}

    def parse_blocks(self, script: str) -> List[LoadBlock]:
        if not script.strip():
            return []

        cleaned = self._strip_comments(script)
        cleaned = self._expand_variables(cleaned)
        cleaned = self._strip_control_flow(cleaned)

        # Map (LOAD start position) -> table name prefix found just before it
        table_names: Dict[int, str] = {}
        for tm in self.TABLE_PREFIX_RE.finditer(cleaned):
            table_names[tm.end()] = tm.group(1).strip()

        blocks: List[LoadBlock] = []
        for i, m in enumerate(self.LOAD_RE.finditer(cleaned)):
            table = table_names.get(m.start(), f"Table{i + 1}")
            fields_str = m.group("fields")
            fields = self._split_fields(fields_str)
            join_raw = (m.group("join") or "").lower().strip()
            join_type = self._normalise_join(join_raw) if join_raw else None
            is_concat = bool(m.group("concat"))
            where_clause = (m.group("where") or "").strip() or None

            if m.group("resident"):
                source = m.group("resident").strip()
                source_type = "resident"
            elif m.group("inline"):
                source = m.group("inline").strip()
                source_type = "inline"
            else:
                source = (m.group("source") or "").strip().strip("[]`'\"")
                options = (m.group("options") or "").lower()
                source_type = self._detect_source_type(source, options)

            m_expr, notes = self._build_m(
                table, fields, source, source_type,
                (m.group("options") or ""), join_type, is_concat, where_clause,
            )
            blocks.append(LoadBlock(
                table_name=table,
                fields=fields,
                source=source,
                source_type=source_type,
                join_type=join_type,
                is_concatenate=is_concat,
                where_clause=where_clause,
                m_expression=m_expr,
                notes=notes,
            ))
        return blocks

    # ------------------------------------------------------------------
    # Pre-processing
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_comments(script: str) -> str:
        script = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
        out: List[str] = []
        for line in script.splitlines():
            in_bracket = 0
            in_quote: Optional[str] = None
            cut = len(line)
            i = 0
            while i < len(line):
                ch = line[i]
                if in_quote:
                    if ch == in_quote:
                        in_quote = None
                elif ch in ("'", '"'):
                    in_quote = ch
                elif ch == "[":
                    in_bracket += 1
                elif ch == "]" and in_bracket > 0:
                    in_bracket -= 1
                elif in_bracket == 0 and in_quote is None \
                        and ch == "/" and i + 1 < len(line) \
                        and line[i + 1] == "/":
                    cut = i
                    break
                elif in_bracket == 0 and in_quote is None \
                        and ch == "-" and i + 1 < len(line) \
                        and line[i + 1] == "-":
                    cut = i
                    break
                elif in_bracket == 0 and in_quote is None \
                        and ch == "R" and line[i:i + 3].upper() == "REM" \
                        and (i == 0 or not line[i - 1].isalnum()):
                    cut = i
                    break
                i += 1
            out.append(line[:cut])
        return "\n".join(out)

    def _expand_variables(self, script: str) -> str:
        def replacer(m: re.Match) -> str:
            var = m.group(1)
            return self._vars.get(var, m.group(0))
        return re.sub(r"\$\((\w+)\)", replacer, script)

    @staticmethod
    def _strip_control_flow(script: str) -> str:
        # FOR / NEXT
        script = re.sub(
            r"\bFOR\b.+?\bNEXT\b",
            "/* TODO: FOR loop removed -- expand manually */",
            script, flags=re.I | re.S,
        )
        # IF / END IF (script-level, not expression-level)
        script = re.sub(
            r"\bIF\b(?!\s*\().*?\bEND\s+IF\b",
            "/* TODO: IF block removed -- expand manually */",
            script, flags=re.I | re.S,
        )
        # CALL statements
        script = re.sub(
            r"\bCALL\b[^\n;]+[;\n]", "/* TODO: CALL removed */ ",
            script, flags=re.I,
        )
        # STORE
        script = re.sub(
            r"\bSTORE\b[^\n;]+INTO[^\n;]+;",
            "/* TODO: STORE removed -- add explicit export step if needed */",
            script, flags=re.I,
        )
        return script

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _split_fields(fields_str: str) -> List[str]:
        if fields_str.strip() == "*":
            return ["*"]
        parts, depth, cur = [], 0, []
        for ch in fields_str:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur).strip())
        return [p for p in parts if p]

    @staticmethod
    def _parse_alias(field: str) -> Tuple[str, str]:
        m = re.match(r"^(.+?)\s+AS\s+(.+?)$", field, re.I)
        if m:
            src = m.group(1).strip().strip('"[]')
            alias = m.group(2).strip().strip('"[]')
            return src, alias
        name = field.strip().strip('"[]')
        return name, name

    # ------------------------------------------------------------------
    # Source detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_source_type(source: str, options: str) -> str:
        s = source.lower()
        o = options.lower()
        if "qvd" in o or s.endswith(".qvd"):
            return "qvd"
        if "ooxml" in o or s.endswith((".xlsx", ".xlsm", ".xlsb")):
            return "xlsx"
        if "xls" in o or s.endswith(".xls"):
            return "xls"
        if "txt" in o or "csv" in o \
                or s.endswith((".csv", ".txt", ".tsv", ".dat")):
            return "csv"
        if "json" in o or s.endswith(".json"):
            return "json"
        if "xml" in o or s.endswith(".xml"):
            return "xml"
        if "odbc" in o:
            return "odbc"
        if "oledb" in o:
            return "oledb"
        if "parquet" in o or s.endswith(".parquet"):
            return "parquet"
        return "unknown"

    @staticmethod
    def _normalise_join(raw: str) -> str:
        if "left" in raw:
            return "left"
        if "right" in raw:
            return "right"
        if "inner" in raw:
            return "inner"
        return "inner"

    # ------------------------------------------------------------------
    # M expression builder
    # ------------------------------------------------------------------

    def _build_m(
        self,
        table: str,
        fields: List[str],
        source: str,
        source_type: str,
        options: str,
        join_type: Optional[str],
        is_concatenate: bool,
        where_clause: Optional[str],
    ) -> Tuple[str, List[str]]:
        notes: List[str] = []
        steps: List[Tuple[str, str]] = []

        file_arg, todo_comment = self._resolve_source_path(source)

        if file_arg == "FilePath":
            filename = source.rstrip("/").split("/")[-1]
            steps.append(("FilePath", f'"{filename}"'))

        if source_type == "csv":
            delim = self._extract_delim(options) or ","
            enc = self._extract_encoding(options) or "65001"
            steps.append(("Source", (
                f"Csv.Document(File.Contents({file_arg}), "
                f'[Delimiter="{delim}", Encoding={enc}, '
                f"QuoteStyle=QuoteStyle.Csv])"
            )))
            steps.append(("Promoted", (
                "Table.PromoteHeaders(Source, [PromoteAllScalars=true])"
            )))

        elif source_type == "xlsx":
            sheet = self._extract_table_name(options) or "Sheet1"
            steps.append(("Source", (
                f"Excel.Workbook(File.Contents({file_arg}), null, true)"
            )))
            steps.append(("Sheet", f'Source{{[Name="{sheet}"]}}[Data]'))
            steps.append(("Promoted", (
                "Table.PromoteHeaders(Sheet, [PromoteAllScalars=true])"
            )))

        elif source_type == "xls":
            sheet = self._extract_table_name(options) or "Sheet1"
            steps.append(("Source", (
                f"Excel.Workbook(File.Contents({file_arg}), null, true)"
            )))
            steps.append(("Sheet", f'Source{{[Name="{sheet}"]}}[Data]'))
            steps.append(("Promoted", (
                "Table.PromoteHeaders(Sheet, [PromoteAllScalars=true])"
            )))

        elif source_type == "json":
            steps.append(("Source", f"Json.Document(File.Contents({file_arg}))"))
            steps.append(("Converted", (
                "Table.FromList(Source, Splitter.SplitByNothing(), "
                "null, null, ExtraValues.Error)"
            )))
            notes.append(
                f"JSON source '{source}' -- review expand step in Power Query."
            )

        elif source_type == "xml":
            steps.append(("Source", f"Xml.Tables(File.Contents({file_arg}))"))
            notes.append(
                f"XML source '{source}' -- review table navigation."
            )

        elif source_type in ("odbc", "oledb"):
            conn_type = (
                "Odbc.DataSource" if source_type == "odbc"
                else "OleDb.DataSource"
            )
            steps.append(("Source", f"{conn_type}({file_arg}, [])"))
            notes.append(
                f"{source_type.upper()} source -- update connection string."
            )

        elif source_type == "parquet":
            steps.append(("Source", (
                f"Parquet.Document(File.Contents({file_arg}))"
            )))

        elif source_type == "qvd":
            notes.append(
                f"QVD file '{source}' cannot be read by Power BI. "
                "Options: re-export as CSV/Parquet, custom connector, "
                "or QVD-to-Parquet utility."
            )
            steps.append(("Source", "#table({}, {})"))

        elif source_type == "resident":
            notes.append(
                f"RESIDENT load from '{source}' -- this table must be "
                "defined earlier in the Power Query script."
            )
            steps.append(("Source", f"{source}"))

        elif source_type == "inline":
            rows = [r.strip() for r in source.split("\n") if r.strip()]
            if rows:
                header = [c.strip() for c in rows[0].split(",")]
                data_rows = []
                for row in rows[1:]:
                    vals = [f'"{v.strip()}"' for v in row.split(",")]
                    data_rows.append(f'  {{{", ".join(vals)}}}')
                col_names = (
                    "{" + ", ".join(f'"{h}"' for h in header) + "}"
                )
                rows_m = ",\n".join(data_rows) if data_rows else ""
                steps.append(("Source", (
                    f"#table({col_names}, {{\n{rows_m}\n}})"
                )))
            else:
                steps.append(("Source", "#table({}, {})"))

        else:
            notes.append(
                f"Unknown source type for '{source}' -- manual M required."
            )
            steps.append(("Source", "#table({}, {})"))

        last = steps[-1][0]

        # Field selection / rename
        if fields and fields != ["*"]:
            renames = []
            keep = []
            for f in fields:
                src_name, alias = self._parse_alias(f)
                keep.append(f'"{src_name}"')
                if src_name != alias:
                    renames.append(f'{{"{src_name}", "{alias}"}}')
            if renames:
                steps.append(("Renamed", (
                    f'Table.RenameColumns({last}, {{{", ".join(renames)}}})'
                )))
                last = "Renamed"
            steps.append(("Selected", (
                f"Table.SelectColumns({last}, "
                f'{{{", ".join(keep)}}}, MissingField.Ignore)'
            )))
            last = "Selected"

        # WHERE -> Table.SelectRows
        if where_clause:
            m_where = self._translate_where(where_clause)
            steps.append(("Filtered", (
                f"Table.SelectRows({last}, each {m_where})"
            )))
            last = "Filtered"
            if "/* TODO" in m_where:
                notes.append(
                    f"WHERE clause partially translated: {where_clause[:80]}"
                )

        if join_type:
            notes.append(
                f"This is a {join_type.upper()} JOIN onto a previous table. "
                "Implement via Table.Join or Table.NestedJoin after both "
                "tables are defined."
            )

        if is_concatenate:
            notes.append(
                "CONCATENATE: append to previous table via "
                "Table.Combine({Prev, This})."
            )

        step_lines: List[str] = []
        for i, (n, e) in enumerate(steps):
            if n == "FilePath" and todo_comment:
                step_lines.append(f"  {todo_comment}")
            sep = "," if i < len(steps) - 1 else ""
            step_lines.append(f"  {n} = {e}{sep}")
        m_code = "let\n" + "\n".join(step_lines) + f"\nin\n  {last}"
        return m_code, notes

    # ------------------------------------------------------------------
    # WHERE clause translator (simple field comparisons)
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_where(where: str) -> str:
        w = where.strip()
        m = re.match(r"^(\w+)\s*(=|<>|>=|<=|>|<)\s*(.+)$", w, re.I)
        if m:
            field, op, val = m.group(1), m.group(2), m.group(3).strip()
            m_op = {"=": "=", "<>": "<>", ">=": ">=", "<=": "<=",
                    ">": ">", "<": "<"}[op]
            if val.startswith("'") or val.startswith('"'):
                val = f'"{val[1:-1]}"'
            return f"[{field}] {m_op} {val}"
        if re.match(r"^NOT\s+IsNull\s*\(\s*(\w+)\s*\)$", w, re.I):
            field = re.match(
                r"^NOT\s+IsNull\s*\(\s*(\w+)\s*\)", w, re.I,
            ).group(1)
            return f"[{field}] <> null"
        return f"/* TODO: translate WHERE: {w} */ true"

    # ------------------------------------------------------------------
    # Option extractors
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_source_path(source: str) -> Tuple[str, Optional[str]]:
        if source.lower().startswith("lib://"):
            filename = source.rstrip("/").split("/")[-1]
            comment = (
                f"// TODO: Update FilePath - original Qlik source: {source}"
            )
            return ("FilePath", comment)
        return (f'"{source}"', None)

    @staticmethod
    def _extract_delim(options: str) -> Optional[str]:
        m = re.search(r"delimiter\s+is\s+'([^']*)'", options, re.I)
        if m:
            return m.group(1)
        m = re.search(r"delimiter\s*=\s*[\"']([^\"']+)[\"']", options, re.I)
        if m:
            return m.group(1)
        if "tabulator" in options.lower():
            return "\\t"
        return None

    @staticmethod
    def _extract_encoding(options: str) -> Optional[str]:
        m = re.search(r"codepage\s+is\s+(\d+)", options, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _extract_table_name(options: str) -> Optional[str]:
        m = re.search(r"table\s+is\s+['\"]?([^'\",()\s]+)", options, re.I)
        return m.group(1) if m else None
