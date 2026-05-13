"""Tableau .twb XML reader.

Produces simple dicts, not pydantic models, because everything downstream
treats them as opaque containers. Three top-level lists come out:

    datasources — name, caption, columns, relationships, connection
    worksheets  — name, mark class, shelves, encodings, filters, datasource ref
    dashboards  — name, canvas size, list of zones (already deduped/scaled)

The cardinal rule for relationships: only emit what's declared in
<object-graph>. Don't infer joins from name overlap. The downstream model
builder reflects this and will silently drop relationship endpoints that
don't survive column dedup."""

import re
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .config import (
    DATATYPE_TMDL,
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
)
from .utils import clean_bracket, safe_int


# Layout-only zone types — they wrap their children but never represent a
# visible visual. Worksheet zones leave type-v2 empty (or absent).
LAYOUT_ZONE_TYPES = {"layout-basic", "layout-flow", "layout-grid"}


def _strip_obj_suffix_name(name: str) -> str:
    """Strip a trailing '(Object!Suffix)' added by Tableau to disambiguate
    a column that exists in multiple federated tables. The suffix is
    a UI-only hint; the underlying physical column is the same."""
    return re.sub(r"\s*\([^()]+!.+?\)\s*$", "", name or "").strip()


class TWBParser:
    def __init__(self, twb_path: str):
        self.twb_path     = twb_path
        self.datasources: List[Dict[str, Any]] = []
        self.parameters:  List[Dict[str, Any]] = []
        self.worksheets:  List[Dict[str, Any]] = []
        self.dashboards:  List[Dict[str, Any]] = []

    def parse(self) -> None:
        root = ET.parse(self.twb_path).getroot()
        self._parse_datasources(root)
        self._parse_worksheets(root)
        self._parse_dashboards(root)
        # Worksheet-scoped `<column name='[Calculation_xxx]'>` entries
        # never made it into the datasource column list (only top-level
        # <datasources>/<datasource>/<column> are processed above). Pick
        # them up now so the model's trivial-alias resolver can register
        # the calc IDs against their underlying columns. Without this
        # pass, visuals referencing `Calculation_<big_id>` produce
        # `[RESOLVE] not found` warnings and the field gets dropped.
        self._merge_worksheet_calc_fields(root)

    # ------------------------------------------------------------------
    # Datasources
    # ------------------------------------------------------------------

    @classmethod
    def _parse_datasource_color_maps(
        cls, ds: ET.Element,
    ) -> Dict[str, Dict[str, str]]:
        """Build {field_name: {bucket_value: hex_color}} for every
        datasource-level color encoding.

        Tableau emits color-palette overrides at the datasource style
        layer, NOT inside individual worksheets. The block looks like:

            <style>
              <style-rule element='mark'>
                <encoding attr='color' field='[none:type:nk]' type='palette'>
                  <map to='#da323f'>
                    <bucket>&quot;Movie&quot;</bucket>
                  </map>
                  <map to='#fb9984'>
                    <bucket>&quot;TV Show&quot;</bucket>
                  </map>
                </encoding>
              </style-rule>
            </style>

        Field is canonicalised (role prefix `none:`/`yr:` stripped, type
        suffix removed) so lookups match the same form the worksheet's
        color encoding registers downstream. Bucket values are unquoted
        (Tableau quotes string buckets but leaves numbers bare).
        """
        out: Dict[str, Dict[str, str]] = {}
        for enc in ds.findall(".//style/style-rule[@element='mark']/encoding"):
            if enc.get("attr") != "color":
                continue
            field_ref = enc.get("field", "").strip()
            if not field_ref:
                continue
            # Canonicalise — _parse_field_ref handles the [role:name:k]
            # forms and strips type-suffixes.
            _agg, fname = cls._parse_field_ref(f"[ds].{field_ref}"
                                               if field_ref.startswith("[")
                                               else f"[ds].[{field_ref}]")
            if not fname:
                fname = clean_bracket(field_ref)
            mapping: Dict[str, str] = {}
            for m in enc.findall("map"):
                hex_color = (m.get("to") or "").strip()
                if not hex_color:
                    continue
                for b in m.findall("bucket"):
                    raw = (b.text or "").strip()
                    if not raw:
                        continue
                    # Tableau quotes string buckets ('"Movie"'); strip.
                    if raw.startswith('"') and raw.endswith('"'):
                        raw = raw[1:-1]
                    elif raw.startswith("'") and raw.endswith("'"):
                        raw = raw[1:-1]
                    mapping[raw] = hex_color
            if mapping:
                out[fname] = mapping
        return out

    def _parse_datasources(self, root: ET.Element) -> None:
        for ds in root.findall("./datasources/datasource"):
            name    = ds.get("name", "")
            caption = ds.get("caption") or name
            # Case-insensitive check for the special Parameters datasource
            if name.lower() == "parameters":
                self.parameters = self._parse_parameters(ds)
                continue

            objects, rel_pairs   = self._parse_object_graph(ds)
            col_parent           = self._parse_metadata_records(ds)
            # Authoritative column->table mapping straight from Tableau.
            # The federated-level <cols><map key='[Region]' value='[Dim_HCP].[Region]'>
            # block disambiguates columns that share names across logical
            # tables — far more reliable than guessing from metadata-record
            # parent-names (which collapse same-named columns onto whichever
            # record was seen first in DFS order).
            cols_map             = self._parse_cols_map(ds)
            group_aliases = self._parse_groups(ds, cols_map)

            columns, col_aliases = self._parse_columns(
                ds,
                name,
                caption,
                col_parent,
                objects,
                cols_map,
                group_aliases,
            )
            # Also pick up any top-level <calculation> elements that are
            # NOT nested inside a <column> (some workbook versions do this).
            columns = self._parse_standalone_calculations(ds, name, caption, columns)

            connection = self._parse_connection_metadata(ds)

            # Custom SQL detection — <relation type='text'> / 'query'.
            # Captured as a list of {name, sql} entries per datasource so
            # the model layer can decide whether to emit Value.NativeQuery
            # vs Sql.Database([Query=...]) vs the existing Hyper CSV path.
            custom_sql = self._parse_custom_sql(ds)

            # Hyper extract references — the authoritative datasource->hyper
            # binding declared by Tableau itself. Each <extract> wraps a
            # <connection class='hyper' dbname='Data/.../TEMP_xxx.hyper'>
            # that points at the .hyper file produced for THIS datasource.
            # Downstream code uses this mapping (instead of column-overlap
            # heuristics) to match hyper tables to TMDL tables, so a hyper
            # file from datasource A is never resolved against a TMDL
            # table belonging to datasource B.
            extracts = self._parse_hyper_extracts(ds)

            relationships = self._resolve_relationships(rel_pairs, columns, objects)

            # Datasource-level color encodings — Tableau's "color palette"
            # picker writes these into <style-rule element='mark'>:
            #   <encoding attr='color' field='[none:type:nk]' type='palette'>
            #     <map to='#da323f'><bucket>"Movie"</bucket></map>
            #     <map to='#fb9984'><bucket>"TV Show"</bucket></map>
            #   </encoding>
            # The map is shared across every worksheet that puts that
            # field on the color shelf, so the report builder can emit
            # per-category color overrides on bar / pie / area / etc.
            color_maps = self._parse_datasource_color_maps(ds)

            self.datasources.append({
                "name":          name,
                "caption":       caption,
                "objects":       objects,
                "columns":       columns,
                "columnAliases": col_aliases,
                "relationships": relationships,
                "connection":    connection,
                "extracts":      extracts,
                # Authoritative tableau-name -> (logical_table, physical_col)
                # mapping used by the model's resolver as a first-priority
                # fallback when col_locator doesn't already have a hit.
                "colsMap":       cols_map,
                # "groups":        groups,
                "groupAliases":  group_aliases,
                # {field_name: {bucket_value: hex_color}} mapped from the
                # datasource's color-encoding style block. field_name is
                # canonical (suffix stripped, role prefix stripped).
                "colorMaps":     color_maps,
                # List of {name, sql} entries for any <relation type='text'> /
                # 'query' nested inside this datasource. Empty list when the
                # datasource only carries plain <relation type='table'> refs.
                "customSql":     custom_sql,
            })

    def _merge_worksheet_calc_fields(self, root: ET.Element) -> None:
        """Pick up worksheet-local `Calculation_<id>` calc fields and
        attach them to their owning datasource's column list.

        Tableau emits per-worksheet calc fields under
        ``<view>/<datasource-dependencies datasource='X'>/<column>``.
        ``_parse_datasources`` only walks the top-level
        ``<datasources>/<datasource>/<column>`` set, so these never reach
        ``ds["columns"]`` and visuals that reference them by their
        ``Calculation_<big_id>`` name produce ``[RESOLVE]`` warnings.

        We mirror the agent-side ``build_calc_index`` workflow that
        previously covered these via a hint sidecar: walk every
        worksheet's datasource-dependencies blocks, find ``<column>``
        entries whose name contains ``Calculation_`` AND carry a
        ``<calculation formula='...'>`` child, and append a synthetic
        column dict to the matching datasource. The model's
        ``_register_calc_alias_resolutions`` pass then resolves trivial
        ``[X]`` / ``// caption\\n[X]`` formulas against the underlying
        column. First definition wins (Tableau repeats the calc element
        verbatim in every worksheet that uses it).
        """
        ds_by_name: Dict[str, Dict[str, Any]] = {
            ds["name"]: ds for ds in self.datasources
        }
        seen_per_ds: Dict[str, set] = {n: set() for n in ds_by_name}
        for ds in self.datasources:
            for col in ds.get("columns") or []:
                cname = (col.get("name") or "").strip()
                if cname:
                    seen_per_ds[ds["name"]].add(cname)

        n_added = 0
        for ws_el in root.findall("./worksheets/worksheet"):
            for view in ws_el.findall(".//view"):
                for dd in view.findall("datasource-dependencies"):
                    ds_attr = (dd.get("datasource") or "").strip()
                    if not ds_attr or ds_attr.lower() == "parameters":
                        continue
                    ds_dict = ds_by_name.get(ds_attr)
                    if ds_dict is None:
                        continue
                    for col_el in dd.findall("column"):
                        raw_name = (col_el.get("name") or "").strip()
                        if "Calculation_" not in raw_name:
                            continue
                        cname = clean_bracket(raw_name)
                        if not cname:
                            continue
                        if cname in seen_per_ds[ds_attr]:
                            continue
                        calc_el = col_el.find("calculation")
                        if calc_el is None:
                            continue
                        formula = (calc_el.get("formula") or "").strip()
                        if not formula:
                            continue
                        datatype = col_el.get("datatype", "string")
                        caption  = (col_el.get("caption") or cname).strip()
                        ds_dict.setdefault("columns", []).append({
                            "name":         cname,
                            "rawName":      raw_name,
                            "caption":      _strip_obj_suffix_name(caption),
                            "datatype":     datatype,
                            "tmdlType":     DATATYPE_TMDL.get(datatype, "string"),
                            "role":         col_el.get("role", "dimension"),
                            "isCalc":       True,
                            "formula":      formula,
                            "daxColumnExpr": "",
                            "hidden":       col_el.get("hidden", "false").lower() == "true",
                            "format":       col_el.get("default-format", ""),
                            "datasource":   ds_attr,
                            "dsCaption":    ds_dict.get("caption", ""),
                            "parentTable":  "",
                            "sourceName":   cname,
                            "semanticRole": col_el.get("semantic-role", ""),
                        })
                        seen_per_ds[ds_attr].add(cname)
                        n_added += 1
        if n_added:
            print(f"[CALC-INDEX] merged {n_added} worksheet-local calc "
                  f"field(s) into datasource column list(s).")

    @staticmethod
    def _parse_parameters(ds: ET.Element) -> List[Dict[str, Any]]:
        """Extract Tableau parameters from the special 'Parameters' datasource.

        Each parameter becomes a dict with:
            name, caption, datatype, default_value, current_value,
            param_domain_type ('any', 'list', 'range'), role,
            list_values (for list params), range_min/max (for range params)
        """
        params: List[Dict[str, Any]] = []
        for col in ds.findall("column"):
            # Skip internal columns
            raw_name = col.get("name", "")
            name = clean_bracket(raw_name)
            if not name or name.startswith("__tableau_internal_"):
                continue

            calc = col.find("calculation")
            formula = calc.get("formula", "") if calc is not None else ""

            # Parse the default value from the formula
            default_value = TWBParser._parse_param_default(formula, col.get("datatype", "string"))

            # Domain type: 'any', 'list', or 'range'
            domain_type = col.get("param-domain-type", "any")

            list_values: List[Dict[str, str]] = []
            if domain_type == "list":
                members = col.find("members")
                if members is not None:
                    for m in members.findall("member"):
                        val = m.get("value", "")
                        # Tableau wraps string values in literal double quotes
                        # in the .twb XML — strip the wrapping quotes and
                        # un-double any escaped inner quotes.
                        val = val.strip()
                        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                            val = val[1:-1].replace('""', '"')
                        if val == "":
                            continue
                        # Display label: prefer alias (user-typed display
                        # name), fall back to the raw value.
                        alias = m.get("alias", "").strip() or val
                        list_values.append({"value": val, "label": alias})

            range_min = range_max = None
            if domain_type == "range":
                rng = col.find("range")
                if rng is not None:
                    range_min = rng.get("min")
                    range_max = rng.get("max")

            params.append({
                "name":         name,
                "caption":      col.get("caption", name),
                "datatype":     col.get("datatype", "string"),
                "tmdlType":     DATATYPE_TMDL.get(col.get("datatype", "string"), "string"),
                "defaultValue": default_value,
                "currentValue": default_value,
                "domainType":   domain_type,
                "role":         col.get("role", "dimension"),
                "listValues":   list_values,
                "rangeMin":     range_min,
                "rangeMax":     range_max,
                "formula":      formula,
            })
        return params

    @staticmethod
    def _parse_param_default(formula: str, datatype: str) -> Any:
        """Extract the scalar default value from a parameter's formula."""
        if not formula:
            return ""
        s = formula.strip()
        # String literal wrapped in quotes
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1]
        # Date literal #YYYY-MM-DD#
        if s.startswith("#") and s.endswith("#"):
            return s[1:-1]
        # Numeric
        try:
            if datatype in ("integer", "int"):
                return int(float(s))
            if datatype in ("real", "float", "double"):
                return float(s)
            if datatype == "boolean":
                return s.lower() in ("true", "1", "yes")
            # Fallback: try numeric anyway
            return float(s)
        except (ValueError, TypeError):
            pass
        return s

    @staticmethod
    def _parse_groups(
        ds: ET.Element,
        cols_map: Optional[Dict[str, Tuple[str, str]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Parse Tableau group fields as metadata, not calculated columns.

        Output shape:
            {
              "Group Field Name": {
                "source": "Underlying Physical Field",
                "baseField": "Underlying Physical Field",
                "caption": "Group Caption",
                "bins": {
                    "Group Label": ["Member A", "Member B"]
                },
                "membersByGroup": {
                    "Group Label": ["Member A", "Member B"]
                }
              }
            }

        The model layer uses this registry to map group fields back to the
        original physical column. The report layer expands group-label filters
        into filters on the underlying base/source column.
        """
        cols_map = cols_map or {}
        out: Dict[str, Dict[str, Any]] = {}

        def _clean_ref(value: str) -> str:
            return clean_bracket(value or "").strip()

        def _unquote(value: Any) -> str:
            s = str(value or "").strip()
            if s.startswith('"') and s.endswith('"') and len(s) >= 2:
                s = s[1:-1].replace('""', '"')
            return s.strip()

        def _is_group_calc(calc_el: ET.Element) -> bool:
            cls = (calc_el.get("class") or "").strip().lower()
            return cls in {
                "group",
                "categorical-bin",
                "bin",
            }

        def _extract_base_field(
            col_el: ET.Element,
            calc_el: ET.Element,
        ) -> str:
            # Tableau versions vary. Try common attributes first.
            for attr in ("column", "field", "source-column", "base-column"):
                val = calc_el.get(attr) or col_el.get(attr)
                if val:
                    return _clean_ref(val)

            # Fallback: inspect descendants.
            for node in calc_el.iter():
                for attr in ("column", "field", "source-column", "base-column"):
                    val = node.get(attr)
                    if val:
                        return _clean_ref(val)

            return ""

        def _extract_members(calc_el: ET.Element) -> Dict[str, List[str]]:
            """Extract group label -> underlying source members.

            Handles common Tableau categorical-bin XML:

                <calculation class="categorical-bin" column="[Field]">
                  <bin value='"Group A"'>
                    <value>"Member 1"</value>
                    <value>"Member 2"</value>
                  </bin>
                </calculation>

            Also handles defensive variants where containers are named
            group/bucket/member.
            """
            members_by_group: Dict[str, List[str]] = {}

            # Primary Tableau categorical-bin shape.
            for bin_el in calc_el.findall("bin"):
                label = _unquote(bin_el.get("value") or "")
                if not label:
                    continue

                vals: List[str] = []
                for value_el in bin_el.findall("value"):
                    val = _unquote(value_el.text or "")
                    if val and val not in vals:
                        vals.append(val)

                if vals:
                    members_by_group[label] = vals

            # Defensive fallback for alternate XML shapes.
            for node in calc_el.iter():
                tag = node.tag.lower()

                if not any(token in tag for token in ("group", "bucket")):
                    continue

                label = _unquote(
                    node.get("name")
                    or node.get("caption")
                    or node.get("label")
                    or node.get("value")
                    or ""
                )

                if not label or label in members_by_group:
                    continue

                vals: List[str] = []

                for child in node.iter():
                    if child is node:
                        continue

                    child_tag = child.tag.lower()
                    if not any(token in child_tag for token in ("member", "value", "bucket")):
                        continue

                    val = _unquote(
                        child.get("value")
                        or child.get("member")
                        or child.get("name")
                        or child.text
                        or ""
                    )

                    if val and val not in vals:
                        vals.append(val)

                if vals:
                    members_by_group[label] = vals

            return members_by_group

        for col_el in ds.findall("column"):
            calc_el = None

            for child in col_el.iter():
                if child.tag == "calculation":
                    calc_el = child
                    break

            if calc_el is None or not _is_group_calc(calc_el):
                continue

            group_name = _clean_ref(col_el.get("name", ""))
            if not group_name:
                continue

            group_caption = (
                _clean_ref(col_el.get("caption", ""))
                or group_name
            )

            base_field = _extract_base_field(col_el, calc_el)

            # If colsMap knows this group/base field, use the physical column.
            mapped = (
                cols_map.get(group_name)
                or cols_map.get(group_caption)
                or cols_map.get(base_field)
            )

            if mapped:
                base_field = mapped[1]

            members_by_group = _extract_members(calc_el)

            group_def = {
                # Canonical names used by the model/report fixes.
                "source": base_field,
                "bins": members_by_group,

                # Compatibility names matching your pasted version.
                "baseField": base_field,
                "caption": group_caption,
                "membersByGroup": members_by_group,
            }

            out[group_name] = group_def

            # Also expose caption as an alias if different.
            if group_caption and group_caption != group_name:
                out[group_caption] = group_def

        return out

    @staticmethod
    def _compile_categorical_bin(
        calc_el: ET.Element,
        source_col_override: Optional[str] = None,
    ) -> str:
        """Compile a Tableau ``<calculation class='categorical-bin'>`` block
        into a DAX expression for a calculated column.

        Tableau categorical-bin XML shape::

            <calculation class='categorical-bin' column='[Payer type]' new-bin='true'>
              <bin default-name='false' value='"Others"'>
                <value>"All sources of payment are blank"</value>
                <value>"No charge/Charity"</value>
                ...
              </bin>
              <bin default-name='false' value='"Insured"'>
                <value>"Private insurance"</value>
                ...
              </bin>
            </calculation>

        DAX shape (chained SWITCH on a constant 1, since SWITCH(TRUE(), ...)
        works for arbitrary predicates and is the canonical pattern for
        multi-bin classifications)::

            SWITCH(TRUE(),
                [Payer type] IN { "All sources of payment are blank", ... }, "Others",
                [Payer type] IN { "Private insurance", ... }, "Insured",
                [Payer type]
            )

        ``source_col_override`` lets callers pass the PBI display name of
        the source column when it differs from the raw Tableau name
        (typically because the source ``<column>`` has a ``caption``
        attribute, and ``_build_columns`` renders columns under their
        caption — e.g. raw name ``HCO_Type`` becomes PBI column
        ``HCO Type``). Without the override the DAX emits ``[HCO_Type]``,
        which fails to resolve in PBI because the column there is named
        ``HCO Type`` (DAX is case-insensitive but NOT space/underscore
        tolerant).

        Falls back to ``BLANK()`` if the calc element is malformed.
        """
        if source_col_override is not None and str(source_col_override).strip():
            source_col = str(source_col_override).strip()
        else:
            source_col_raw = calc_el.get("column", "").strip()
            # Tableau wraps column refs in brackets: ``[Payer type]``. Strip
            # them — the DAX form is ``[Payer type]`` (unqualified column ref
            # inside a calc column, same table).
            if source_col_raw.startswith("[") and source_col_raw.endswith("]"):
                source_col = source_col_raw[1:-1]
            else:
                source_col = source_col_raw
        if not source_col:
            return "BLANK()"
        # DAX column ref. Embedded ']' in column names is doubled per the
        # DAX spec. Same table → no prefix needed.
        col_ref = "[" + source_col.replace("]", "]]") + "]"

        clauses: List[str] = []
        for bin_el in calc_el.findall("bin"):
            # Bin's output value (the new bucket label). Tableau stores
            # string values wrapped in literal double-quote markers, e.g.
            # value='"Others"'. We have to strip the wrapping quotes.
            new_val_raw = bin_el.get("value", "").strip()
            if new_val_raw.startswith('"') and new_val_raw.endswith('"') and len(new_val_raw) >= 2:
                new_val = new_val_raw[1:-1].replace('""', '"')
            else:
                new_val = new_val_raw
            if not new_val:
                continue
            new_val_dax = '"' + new_val.replace('"', '""') + '"'

            # Source values that fall into this bin. Each <value> child has
            # a body like '"All sources of payment are blank"' (string with
            # surrounding quotes) or a numeric literal.
            source_vals: List[str] = []
            for v_el in bin_el.findall("value"):
                v_text = (v_el.text or "").strip()
                if not v_text:
                    continue
                if v_text.startswith('"') and v_text.endswith('"') and len(v_text) >= 2:
                    v_text = v_text[1:-1].replace('""', '"')
                # Re-quote for DAX. Tableau already did the outer quoting,
                # so what we extracted is the literal content. DAX needs
                # double quotes with embedded " escaped via "".
                source_vals.append('"' + v_text.replace('"', '""') + '"')
            if not source_vals:
                continue
            in_list = "{ " + ", ".join(source_vals) + " }"
            clauses.append(f"{col_ref} IN {in_list}, {new_val_dax}")

        if not clauses:
            return "BLANK()"
        # Final default branch: pass through the source column unchanged.
        return "SWITCH(TRUE(),\n        " + ",\n        ".join(clauses) + ",\n        " + col_ref + "\n    )"


    def _parse_object_graph(
        self, ds: ET.Element,
    ) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
        """Logical-table objects + raw relationship operand pairs."""
        objects:   Dict[str, str]            = {}
        rel_pairs: List[Tuple[str, str]]     = []

        og = None
        for el in ds.iter():
            if el.tag.endswith("object-graph"):
                og = el
                break
        if og is None:
            return objects, rel_pairs

        for ob in og.iter("object"):
            oid = ob.get("id", "")
            cap = ob.get("caption", "")
            if oid and cap:
                objects[oid] = cap

        for rel in og.iter("relationship"):
            for outer in rel.findall("expression"):
                if outer.get("op", "") != "=":
                    continue
                operands = outer.findall("expression")
                if len(operands) >= 2:
                    rel_pairs.append((operands[0].get("op", ""),
                                      operands[1].get("op", "")))
        return objects, rel_pairs

    @staticmethod
    def _parse_cols_map(ds: ET.Element) -> Dict[str, Tuple[str, str]]:
        """Parse Tableau's federated-level <cols><map key value> block.

        Tableau emits an authoritative mapping from each tableau column
        reference to the (logical_table, physical_column) it resolves
        to. The federated-level form (a direct child of <connection
        class='federated'>) uses logical-table captions:

            <map key='[City]'         value='[Dim_HCP].[City]' />
            <map key='[City (Dim!HCO)]' value='[Dim_HCO].[City]' />

        This is what Tableau itself uses to bind worksheet column
        references to data, so it is THE authoritative resolver — no
        heuristics needed. The deeper hyper-extract-level cols block
        uses GUID-suffixed object IDs (e.g. 'Dim!HCO_xxx') and is
        skipped here because the federated form already carries the
        clean table names.

        Returns: { canonical_tableau_name -> (logical_table, physical_col) }.
        Both the suffixed form ('City (Dim!HCO)') and the un-suffixed form
        ('City') are stored as separate keys when both appear in the XML,
        so the resolver can disambiguate either way.
        """
        out: Dict[str, Tuple[str, str]] = {}
        # Walk down to the federated <connection> only — never recurse
        # into <extract> blocks, which carry hyper-physical paths instead.
        federated = None
        for child in ds:
            if child.tag.endswith("connection") and child.get("class") == "federated":
                federated = child
                break
        if federated is None:
            return out

        for cols in federated.iter():
            if cols.tag != "cols":
                continue
            # Skip nested <cols> blocks that live inside <extract>: those
            # are physical/hyper mappings, not the logical map we want.
            parent = cols
            while True:
                # Climb back up to verify we're at the federated level.
                # ElementTree doesn't expose .getparent(), so we trust
                # the iter() ordering: extract blocks live deeper than
                # the top-level cols. We filter by checking the value
                # form: federated map values are "[TableCaption].[Col]"
                # while extract values are "[Object_GUID].[Col]" — the
                # latter contain '!' or a 32-char hex tail, which the
                # parsing below rejects naturally.
                break
            for m in cols.findall("map"):
                key = clean_bracket(m.get("key", ""))
                val = (m.get("value") or "").strip()
                if not key or not val:
                    continue
                # Match '[<table>].[<col>]'
                mt = re.match(r"^\s*\[([^\]]+)\]\s*\.\s*\[([^\]]+)\]\s*$", val)
                if not mt:
                    continue
                table = mt.group(1).strip()
                col   = mt.group(2).strip()
                # Skip GUID-suffixed object IDs (extract-level cols block):
                # those carry '!' AND a 32-char hex tail. The federated
                # form uses plain table captions like 'Dim_HCP'.
                if "!" in table and re.search(r"_[0-9A-Fa-f]{32}$", table):
                    continue
                if key not in out:
                    out[key] = (table, col)
        return out

    @staticmethod
    def _parse_connection_metadata(ds: ET.Element) -> Dict[str, Any]:
        """Pull every connection attribute we can use downstream.

        Tableau wraps real database connections inside a <named-connection>
        whose `<connection class='...'>` carries the dialect and server
        information. The outermost <connection> is usually `class='federated'`
        which is just a Tableau wrapper — not a Power Query connector. We
        prefer the first non-federated/non-hyper inner connection so the
        emitted partition M targets the actual source dialect.

        Returned dict has only the attributes that were ACTUALLY present on
        the XML element. Missing values are stored as empty strings (not
        invented defaults) so model.py can decide on its own fallbacks
        ("PUBLIC" schema, port 5432, etc.).
        """
        # Hyper extract connections live at this level too; we want the
        # *primary* live/source connection so look past hyper. Federated is
        # Tableau's own wrapper class, so look past it as well — but keep it
        # as a fallback when no inner connection exists (rare).
        primary: Optional[ET.Element] = None
        federated_fallback: Optional[ET.Element] = None
        # Walk every descendant <connection> in document order.
        for conn in ds.iter():
            if not conn.tag.endswith("connection"):
                continue
            cls = (conn.get("class") or "").strip().lower()
            if not cls:
                continue
            if cls == "hyper":
                continue
            if cls == "federated":
                if federated_fallback is None:
                    federated_fallback = conn
                continue
            if primary is None:
                primary = conn
                # Don't break: we want the FIRST inner connection so
                # subsequent named-connections of the same datasource
                # don't override this one.

        chosen = primary if primary is not None else federated_fallback
        if chosen is None:
            return {}

        out: Dict[str, Any] = {
            "class":  (chosen.get("class") or "").strip(),
            "dbname": (chosen.get("dbname") or chosen.get("filename") or "").strip(),
            "server": (chosen.get("server") or "").strip(),
        }
        # Optional attributes — only included when present so downstream
        # code can branch on truthiness without inventing defaults.
        for key, attr in (
            ("port",           "port"),
            ("schema",         "schema"),
            ("authentication", "authentication"),
            ("service",        "service"),
            ("warehouse",      "warehouse"),
            ("db",             "db"),
            ("role",           "role"),
            ("sslmode",        "sslmode"),
        ):
            val = chosen.get(attr)
            if val is not None and val != "":
                out[key] = val.strip() if isinstance(val, str) else val
        # Live connector log (helps users see connector branching at
        # conversion time). Keeps auth attribute visible but never
        # emitted into M.
        cls = out.get("class") or ""
        if cls and cls.lower() not in ("federated", "hyper"):
            auth = out.get("authentication") or ""
            print(
                f"[CONN] class='{cls}' server='{out.get('server','')}' "
                f"dbname='{out.get('dbname','')}' "
                f"schema='{out.get('schema','')}' "
                f"auth='{auth}'"
            )
        return out

    @staticmethod
    def _parse_custom_sql(ds: ET.Element) -> List[Dict[str, str]]:
        """Detect Tableau custom-SQL relations.

        Two shapes are emitted by Tableau:
            <relation type='text'  name='Custom SQL'>SELECT ...</relation>
            <relation type='query' name='X'>SELECT ...</relation>

        Both carry the SQL fragment as the element text. Other shapes
        (`type='table'`, `type='collection'`, `type='join'`) reference
        physical tables, not custom SQL, so they're skipped.

        Returns a list of {name, sql} dicts. Empty list when the datasource
        contains no custom SQL — which is the common case (the converter
        falls through to whichever path the rest of the pipeline picks).
        """
        out: List[Dict[str, str]] = []
        for rel in ds.iter():
            if not rel.tag.endswith("relation"):
                continue
            rtype = (rel.get("type") or "").strip().lower()
            if rtype not in ("text", "query"):
                continue
            sql_text = (rel.text or "").strip()
            if not sql_text:
                continue
            out.append({
                "name": rel.get("name") or "Custom SQL",
                "sql":  sql_text,
            })
        return out

    @staticmethod
    def _parse_hyper_extracts(ds: ET.Element) -> List[str]:
        """Return relative .hyper dbnames declared by this datasource.

        Tableau emits one or more <connection class='hyper' dbname='...'>
        elements per datasource that point at the hyper file produced for
        that datasource. The most common form is wrapped in <extract>:

            <extract>
              <connection class='hyper'
                          dbname='Data/TableauTemp/TEMP_xxx.hyper' .../>
            </extract>

        Direct-to-hyper datasources omit the <extract> wrapper, so we
        scan for any descendant <connection class='hyper'> with a dbname
        attribute. Multiple hyper connections per datasource are rare
        but legal; we return all of them so the converter can pair each
        one with the hyper file it actually owns. Paths are returned
        exactly as written (relative to the .twbx root) so the caller
        can match them against the file names extracted from the archive.
        """
        out: List[str] = []
        seen: set = set()
        for conn in ds.iter():
            if not conn.tag.endswith("connection"):
                continue
            if conn.get("class", "") != "hyper":
                continue
            dbname = (conn.get("dbname") or "").strip()
            if dbname and dbname not in seen:
                seen.add(dbname)
                out.append(dbname)
        return out

    @staticmethod
    def _parse_metadata_records(ds: ET.Element) -> Dict[str, str]:
        """remote-name -> parent-name (caption form preferred)."""
        out: Dict[str, str] = {}
        for mr in ds.iter("metadata-record"):
            if mr.get("class") != "column":
                continue
            remote = (mr.findtext("remote-name") or "").strip()
            parent = clean_bracket((mr.findtext("parent-name") or "").strip())
            local  = clean_bracket((mr.findtext("local-name")  or "").strip())
            if parent:
                if remote and remote not in out:
                    out[remote] = parent
                if local and local not in out:
                    out[local] = parent
        return out

    
    def _parse_columns(
        self,
        ds:         ET.Element,
        ds_name:    str,
        ds_caption: str,
        col_parent: Dict[str, str],
        objects:    Dict[str, str],
        cols_map:   Dict[str, Tuple[str, str]] = None,
        group_aliases: Dict[str, Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:

        """Extract a single column entry per logical column.

        Tableau federated workbooks emit *two* representations of the same
        physical column:

            <column name='[HCO_ID (Dim!HCO)]' caption='HCO ID (Dim!HCO)' />
            <metadata-record class='column'>
              <remote-name>HCO_ID</remote-name>
              <parent-name>[Dim_HCO]</parent-name>
            </metadata-record>

        The suffix `(Dim!HCO)` is purely a Tableau-UI disambiguation hint
        ('this column came from the Dim_HCO object'). Both entries point
        at the same physical column. We collapse them under the
        unsuffixed canonical name and remember the suffixed form as an
        alias so worksheets that reference the suffixed form still
        resolve.
        """
        group_aliases = group_aliases or {}
        cols: List[Dict[str, Any]] = []
        cols_map = cols_map or {}
        # Canonical names already added — keyed by (parent_table, name)
        # so the same canonical name can legitimately appear in two
        # different tables (e.g. HCO_ID in both Dim_HCO and Dim_HCP).
        seen_keys:   set = set()
        # alias map: every observed name -> the canonical column name
        # used in this datasource.
        aliases: Dict[str, str] = {}

        def parent_for(name_clean: str, formula: str) -> str:
            if formula:
                return ""
            # Authoritative source first: Tableau's own federated cols/map.
            # The map key matches the column reference exactly (with or
            # without the (Object!Suffix) hint), so two same-named columns
            # in different logical tables stay distinct here even though
            # metadata-record would collapse them.
            mapped = cols_map.get(name_clean)
            if mapped:
                return mapped[0]
            raw = col_parent.get(name_clean, "")
            if raw and raw in objects:
                return objects[raw]
            return raw

        # Pre-build {canonical_source_name -> PBI display name} so group /
        # categorical-bin DAX can reference its source column by the name
        # PBI will actually use. `_build_columns` (model.py) renders every
        # column under ``(caption or name)`` post-suffix-strip, so the DAX
        # ref must match that, not the raw Tableau identifier. Example:
        # source ``<column caption='HCO Type' name='[HCO_Type]'/>`` lands
        # in PBI as ``column 'HCO Type'`` — a group calc that emits
        # ``[HCO_Type]`` then fails to bind (DAX is case-insensitive but
        # NOT space/underscore tolerant).
        source_display_names: Dict[str, str] = {}
        for src_col_el in ds.findall("column"):
            src_calc = None
            for child in src_col_el.iter():
                if child.tag == "calculation":
                    src_calc = child
                    break
            src_calc_cls = (
                (src_calc.get("class") or "").strip().lower()
                if src_calc is not None else ""
            )
            # Skip group / categorical-bin entries — those are calc columns,
            # not source columns; they can't be the lhs of another group's DAX.
            if src_calc_cls in {"group", "categorical-bin", "bin"}:
                continue
            src_raw_name = src_col_el.get("name", "")
            src_clean = clean_bracket(src_raw_name)
            if not src_clean:
                continue
            src_canonical = _strip_obj_suffix_name(src_clean)
            display = src_col_el.get("caption", "").strip() or src_canonical
            display = _strip_obj_suffix_name(display)
            # Store under the canonical, the raw-with-suffix, and any
            # bracketed form so the lookup is forgiving regardless of
            # how the bin XML quotes its source ref.
            for key in (src_canonical, src_clean, src_raw_name):
                if key and key not in source_display_names:
                    source_display_names[key] = display

        # First pass: explicit <column> elements
        for col in ds.findall("column"):
            raw_name = col.get("name", "")
            name     = clean_bracket(raw_name)
            if not name:
                continue
            if name.startswith("__tableau_internal_") or "].[" in name:
                continue

            canonical_group_name = _strip_obj_suffix_name(name)

            # <calculation> may be nested at any depth inside <column>.
            # Some TWB versions wrap it in <calculation-class> or similar.
            calc_el = None
            for child in col.iter():
                if child.tag == "calculation":
                    calc_el = child
                    break
            formula = calc_el.get("formula", "") if calc_el is not None else ""

            calc_class = (
                (calc_el.get("class") or "").strip().lower()
                if calc_el is not None else ""
            )

            # Tableau groups & categorical-bin calcs: synthesise a DAX
            # calculated column on the same TMDL table as the underlying
            # source column. The `<calculation class='categorical-bin' column='[Src]'>`
            # block lists each new bucket label and its source-value members,
            # which `_compile_categorical_bin` turns into
            #   SWITCH(TRUE(), [Src] IN { "m1", "m2" }, "Bucket", [Src])
            # PBI then renders the group field with the user's bucket labels
            # exactly as Tableau did. Without this branch the group XML was
            # dropped and the field reverted to the raw source values.
            dax_column_expr = ""
            if calc_el is not None and calc_class in {"group", "categorical-bin", "bin"}:
                # Resolve the source column's PBI display name so the DAX
                # ref matches what `_build_columns` will emit (caption-or-
                # name, suffix-stripped). Without this, the DAX uses the
                # raw Tableau identifier (e.g. `[HCO_Type]`) which won't
                # bind against a PBI column named `'HCO Type'`.
                src_raw_attr = (calc_el.get("column") or "").strip()
                src_clean = clean_bracket(src_raw_attr)
                src_canonical = _strip_obj_suffix_name(src_clean)
                src_pbi_name = (
                    source_display_names.get(src_canonical)
                    or source_display_names.get(src_clean)
                    or source_display_names.get(src_raw_attr)
                    or src_canonical
                    or src_clean
                )
                dax_column_expr = TWBParser._compile_categorical_bin(
                    calc_el,
                    source_col_override=src_pbi_name,
                )
                if not dax_column_expr or dax_column_expr == "BLANK()":
                    # Malformed bin XML — fall back to the historic
                    # alias-to-source behaviour so visuals at least see
                    # the underlying values rather than nothing.
                    source_field = ""
                    group_def_local = (
                        group_aliases.get(name)
                        or group_aliases.get(canonical_group_name)
                    )
                    if group_def_local:
                        source_field = (
                            group_def_local.get("source")
                            or group_def_local.get("baseField")
                            or ""
                        )
                    if source_field:
                        aliases[name] = source_field
                        aliases[canonical_group_name] = source_field
                    print(
                        f"[GROUP] '{name}' in ds='{ds_name}': bin XML did "
                        f"not yield a DAX expression — falling back to alias."
                    )
                    continue

            canonical = _strip_obj_suffix_name(name)

            mapped = cols_map.get(name) or cols_map.get(canonical)
            parent = mapped[0] if mapped else parent_for(name, formula)
            source_name = mapped[1] if mapped else canonical

            # Group / categorical-bin calc columns: the column itself is
            # virtual (no metadata-record, no cols_map entry), but the DAX
            # references the source column by bare-bracket name — so the
            # calc column MUST live on the same TMDL table as the source.
            # Pull the source column out of `calc_el@column` and re-parent.
            if dax_column_expr and calc_el is not None:
                src_raw = (calc_el.get("column") or "").strip()
                src_clean = clean_bracket(src_raw)
                if src_clean:
                    src_mapped = (
                        cols_map.get(src_clean)
                        or cols_map.get(src_raw)
                    )
                    if src_mapped:
                        parent = src_mapped[0]
                    else:
                        src_parent_raw = col_parent.get(src_clean, "")
                        if src_parent_raw and src_parent_raw in objects:
                            parent = objects[src_parent_raw]
                        elif src_parent_raw:
                            parent = src_parent_raw
                    print(
                        f"[GROUP] '{name}' in ds='{ds_name}' -> "
                        f"DAX calc column on table='{parent}' "
                        f"(source='{src_clean}')"
                    )

            key = (parent, canonical)

            # Always remember the alias so visuals can resolve either form.
            aliases[name] = canonical
            if canonical != name:
                aliases[canonical] = canonical

            if key in seen_keys:
                continue
            seen_keys.add(key)

            datatype = col.get("datatype", "string")
            caption  = _strip_obj_suffix_name(col.get("caption", canonical))
            cols.append({
                "name":        canonical,
                "rawName":     raw_name,
                "caption":     caption,
                "datatype":    datatype,
                "tmdlType":    DATATYPE_TMDL.get(datatype, "string"),
                "role":        col.get("role", "dimension"),
                "isCalc":      bool(formula),
                "formula":     formula,
                # When set, this column emits as a DAX calculated column
                # (`column 'X' = <expr>`) instead of a sourceColumn-bound
                # physical column. Distinct from `isCalc` (which routes to
                # the measure path). Only categorical-bin produces this
                # today; numeric-bin / regex / other class-based calcs
                # fall through to the existing paths.
                "daxColumnExpr": dax_column_expr,
                "hidden":      col.get("hidden", "false").lower() == "true",
                "format":      col.get("default-format", ""),
                "datasource":  ds_name,
                "dsCaption":   ds_caption,
                "parentTable": parent,
                "sourceName": source_name,
                # Tableau's authoritative geo signal — e.g.
                # '[Geographical].[Latitude]' / '[Geographical].[Longitude]'.
                # Used downstream to set PBI dataCategory regardless of the
                # column's display name (so a renamed 'gps_long' still
                # binds to PBI's Longitude well).
                "semanticRole": col.get("semantic-role", ""),
            })

        # Second pass: metadata-record only columns (raw source columns
        # the user never explicitly customized).
        for mr in ds.iter("metadata-record"):
            if mr.get("class") != "column":
                continue
            remote = (mr.findtext("remote-name") or "").strip()
            if not remote:
                continue
            if remote.startswith("__tableau_internal_") or "].[" in remote:
                continue

            canonical = _strip_obj_suffix_name(remote)
            parent    = parent_for(remote, "")
            key       = (parent, canonical)

            aliases[remote] = canonical
            if canonical != remote:
                aliases[canonical] = canonical

            if key in seen_keys:
                continue
            seen_keys.add(key)

            local_type = (mr.findtext("local-type") or "string").lower()
            datatype   = local_type if local_type in DATATYPE_TMDL else "string"
            cols.append({
                "name":        canonical,
                "rawName":     f"[{remote}]",
                "caption":     canonical,
                "datatype":    datatype,
                "tmdlType":    DATATYPE_TMDL.get(datatype, "string"),
                "role":        "dimension",
                "isCalc":      False,
                "formula":     "",
                "hidden":      False,
                "format":      "",
                "datasource":  ds_name,
                "dsCaption":   ds_caption,
                "parentTable": parent,
                "sourceName":  remote,
                "semanticRole": "",
            })

        return cols, aliases

    @staticmethod
    def _parse_standalone_calculations(
        ds: ET.Element,
        ds_name: str,
        ds_caption: str,
        existing_columns: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Pick up <calculation> elements that appear as direct children of
        the datasource (not nested inside a <column>). Some TWB versions
        store calculated fields this way.

        We match them against existing columns by name; if a column with the
        same name already exists and has no formula, we backfill the formula.
        If no matching column exists, we create a new calculated column entry.
        """
        existing_by_name: Dict[str, Dict[str, Any]] = {
            c["name"]: c for c in existing_columns
        }
        out = list(existing_columns)

        for calc in ds.findall("calculation"):
            formula = calc.get("formula", "")
            if not formula:
                continue
            col_name = clean_bracket(calc.get("column", ""))
            if not col_name:
                col_el = calc.find("column")
                if col_el is not None:
                    col_name = clean_bracket(col_el.get("name", ""))
            if not col_name:
                continue

            if col_name in existing_by_name:
                existing = existing_by_name[col_name]
                if not existing.get("formula"):
                    existing["formula"] = formula
                    existing["isCalc"] = True
            else:
                out.append({
                    "name":        col_name,
                    "rawName":     f"[{col_name}]",
                    "caption":     col_name,
                    "datatype":    "string",
                    "tmdlType":    "string",
                    "role":        "dimension",
                    "isCalc":      True,
                    "formula":     formula,
                    "hidden":      False,
                    "format":      "",
                    "datasource":  ds_name,
                    "dsCaption":   ds_caption,
                    "parentTable": "",
                })
                existing_by_name[col_name] = out[-1]
        return out

    @staticmethod
    def _resolve_relationships(
        rel_pairs: List[Tuple[str, str]],
        columns:   List[Dict[str, Any]],
        objects:   Dict[str, str],
    ) -> List[Dict[str, str]]:
        """Map each operand to (table, column).

        Operand forms:
            [Col]                       — column without disambiguator
            [Col (Object!Suffix)]       — column with explicit source object
        """
        col_to_parent = {c["name"]: c["parentTable"] for c in columns
                         if c["parentTable"] and not c["isCalc"]}
        suffix_to_caption: Dict[str, str] = {}
        for oid, cap in objects.items():
            m = re.match(r"^(.+?)_[0-9A-Fa-f]{32}$", oid)
            suffix_to_caption[m.group(1) if m else oid] = cap

        def parse_op(s: str) -> Tuple[str, str]:
            m = re.match(r"^\s*\[(.+?)(?:\s*\(([^)]+!.+?)\))?\]\s*$", s)
            if not m:
                return "", ""
            col    = m.group(1).strip()
            suffix = (m.group(2) or "").strip()
            if suffix:
                return suffix_to_caption.get(suffix, ""), col
            return col_to_parent.get(col, ""), col

        out: List[Dict[str, str]] = []
        for left, right in rel_pairs:
            lt, lc = parse_op(left)
            rt, rc = parse_op(right)
            if lt and lc and rt and rc and lt != rt:
                out.append({"fromTable": lt, "fromColumn": lc,
                            "toTable":   rt, "toColumn":   rc})
        return out

    # ------------------------------------------------------------------
    # Worksheets
    # ------------------------------------------------------------------

    def _parse_worksheets(self, root: ET.Element) -> None:
        for ws in root.findall(".//worksheets/worksheet"):
            name = ws.get("name", "")
            self.worksheets.append(self._parse_one_worksheet(name, ws))

    def _parse_one_worksheet(self, name: str, ws: ET.Element) -> Dict[str, Any]:
        table = ws.find("table")
        view  = ws.find(".//view")

        rows_s, cols_s = "", ""
        for parent in (table, view):
            if parent is None:
                continue
            rs = parent.find("rows"); cs = parent.find("cols")
            if rs is not None and rs.text and rs.text.strip():
                rows_s = rs.text.strip()
            if cs is not None and cs.text and cs.text.strip():
                cols_s = cs.text.strip()
            if rows_s or cols_s:
                break

        mark = "automatic"
        if table is not None:
            pmk = table.find("panes/pane/mark")
            if pmk is not None:
                mark = pmk.get("class", mark)
        if mark == "automatic" and view is not None:
            vmk = view.find("mark") or view.find(".//mark")
            if vmk is not None:
                mark = vmk.get("class", mark)

        # Encoding fields: each carries the field name AND the aggregation
        # token (sum/avg/yr/...). Pie / donut / treemap visuals depend on
        # the agg to figure out whether the size encoding is a measure
        # that should land in the value slot.
        empty_enc = {"field": "", "agg": "", "datasource": ""}
        color_e   = dict(empty_enc)
        size_e    = dict(empty_enc)
        label_e   = dict(empty_enc)
        # Tableau allows MULTIPLE <text> encodings on a single mark
        # (Test KPI in UseCase2 uses two: 'Total Territories' + 'Sum of
        # Patients'). We collect all of them so a multi-value card visual
        # can land each one as its own row in PBI's multiRowCard. The
        # legacy `label_e` keeps the LAST encoding for backward compat
        # with the auto-rule check `encoding: "label"`.
        labels:   List[Dict[str, str]] = []
        details:  List[Dict[str, str]] = []
        tooltips: List[Dict[str, str]] = []

        def _enc(el: ET.Element) -> Dict[str, str]:
            raw_ref = el.get("column", el.get("field", ""))
            agg, fname = self._parse_field_ref(raw_ref)
            ds_hint = self._extract_ds_prefix(raw_ref)
            return {"field": fname, "agg": agg, "datasource": ds_hint}

        for parent_el in (table, view):
            if parent_el is None:
                continue
            if parent_el is table:
                encs = parent_el.findall("panes/pane/encodings")
            else:
                encs = parent_el.findall("encoding")
            for enc in encs:
                for e in enc.findall("color"):
                    new = _enc(e)
                    if new["field"]:
                        color_e = new
                for e in enc.findall("size") + enc.findall("wedge-size") + enc.findall("angle"):
                    new = _enc(e)
                    if new["field"]:
                        size_e = new
                for e in enc.findall("text") + enc.findall("label"):
                    new = _enc(e)
                    if new["field"]:
                        label_e = new
                        # Dedupe by field name so a worksheet that lists
                        # the same column on multiple text encodings
                        # (rare but seen in legacy workbooks) doesn't
                        # double up in the value list.
                        if not any(l.get("field") == new["field"]
                                   for l in labels):
                            labels.append(new)
                for e in enc.findall("detail") + enc.findall("lod"):
                    new = _enc(e)
                    if new["field"]:
                        details.append(new)
                for e in enc.findall("tooltip"):
                    new = _enc(e)
                    if new["field"]:
                        tooltips.append(new)

        # Each worksheet declares which datasource(s) it uses through one or
        # more <datasource-dependencies> blocks. We pin the worksheet to its
        # *real* datasource so column resolution always happens within that
        # datasource's scope — even if column names collide with another
        # datasource.
        #
        # Tableau emits a separate <datasource-dependencies datasource='Parameters'>
        # block whenever the worksheet references a parameter. That block is
        # not the worksheet's data datasource, so we skip it and prefer the
        # first non-Parameters reference. Falling back to Parameters happens
        # only when nothing else is declared.
        ds_ref = self._pick_worksheet_datasource(view) if view is not None else ""

        # Capture the FULL list of datasource dependencies for this worksheet
        # along with the column refs each dependency carries. This is the raw
        # input the converter uses to detect Tableau data blending — when a
        # worksheet binds rows from one datasource and measures from another.
        ds_deps: List[Dict[str, Any]] = []
        if view is not None:
            for dd in view.findall("datasource-dependencies"):
                ds_attr = (dd.get("datasource") or "").strip()
                if ds_attr.lower() == "parameters":
                    continue
                # Only declared columns (real fields) are useful for blend-
                # key inference. column-instance entries are derivations
                # (Year-Trunc / agg / etc.) of an underlying column and
                # don't add new join keys, so we skip them.
                col_refs: List[str] = []
                for col_el in dd.findall("column"):
                    cname = clean_bracket(
                        (col_el.get("name") or "").strip()
                    )
                    if cname:
                        col_refs.append(cname)
                ds_deps.append({
                    "datasource": ds_attr,
                    "columns":    col_refs,
                })

        # Build a per-worksheet column registry from this worksheet's
        # <datasource-dependencies>. Each entry maps a *canonical* column
        # name (without `(Object!Suffix)`) to the raw name as it appears in
        # this worksheet — including any disambiguation suffix Tableau
        # added. The downstream resolver uses the raw name to decide which
        # logical table to bind to when a column name is otherwise
        # ambiguous (e.g. shared join keys across logical tables).
        ws_columns: Dict[str, str] = {}
        if view is not None:
            for dd in view.findall("datasource-dependencies"):
                ds_attr = dd.get("datasource", "") or ""
                if ds_attr.lower() == "parameters":
                    continue
                for col_el in dd.findall("column"):
                    raw = (col_el.get("name") or "").strip()
                    if not raw:
                        continue
                    cleaned = clean_bracket(raw)
                    canonical = _strip_obj_suffix_name(cleaned)
                    if canonical and canonical not in ws_columns:
                        ws_columns[canonical] = cleaned

        filters: List[Dict[str, Any]] = self._parse_worksheet_filters(view)
        sort_specs: List[Dict[str, Any]] = self._parse_worksheet_sort(ws)

        title_text, title_style, title_enabled = self._parse_worksheet_title(
            ws, name)
        label_enabled, label_style = self._parse_worksheet_labels(ws)
        background_color = self._parse_worksheet_background(ws)
        mark_color       = self._parse_worksheet_mark_color(ws)
        # Column-header / row-header styling — feeds the tableEx /
        # pivotTable columnHeaders / rowHeaders bags in report.py.
        # Returns {columnHeaderStyle, rowHeaderStyle} when Tableau
        # supplied a header-element style-rule; empty dict otherwise.
        header_styles    = self._parse_worksheet_header_style(ws)

        # Custom-tooltip field references — even when we can't translate
        # the rich-text template, the underlying fields should land on
        # the visual's Tooltips slot so users can rebuild the tooltip
        # from the available bindings.
        tooltip_template_refs = self._parse_custom_tooltip_refs(ws)
        existing_tooltip_fields = {(t.get("field") or "").strip()
                                   for t in tooltips}
        for ref in tooltip_template_refs:
            if ref["field"] not in existing_tooltip_fields:
                tooltips.append(ref)
                existing_tooltip_fields.add(ref["field"])

        row_fields = self._split_fields(rows_s)
        col_fields = self._split_fields(cols_s)
        # Enrich each shelf field with its column's geo signal — Tableau's
        # `semantic-role='[Geographical].[Latitude]'` / '[Longitude]' is
        # the authoritative coordinate marker. Survives column renames
        # (a field called 'hcp_lat' is still tagged Latitude when its
        # semantic-role says so), which the name-only check below misses.
        # When a worksheet's rows OR cols carry a Geographical lat AND
        # lon, Tableau renders a map regardless of the mark class — that's
        # the dual-axis-circle case we need to surface as `isGeo` so the
        # picker emits a PBI map visual.
        geo_lookup = self._build_geo_role_lookup(ds_ref)
        for f in row_fields + col_fields:
            fname = (f.get("field") or "").strip()
            geo_role = geo_lookup.get(fname) or geo_lookup.get(
                _strip_obj_suffix_name(fname)
            )
            if geo_role:
                f["geoRole"] = geo_role
                f["isGeo"] = True
            else:
                f["geoRole"] = ""
        is_geo = (
            mark.lower() in ("map", "polygon", "filled-map")
            or any(f.get("isGeo") for f in row_fields + col_fields)
        )

        return {
            "name":          name,
            "markClass":     mark,
            "rowFields":     row_fields,
            "colFields":     col_fields,
            # Encoding fields are dicts {field, agg} so downstream code
            # can tell a measure-on-size from a dimension-on-size.
            "colorField":    color_e,
            "sizeField":     size_e,
            # `labelField` is the LAST <text>/<label> encoding (legacy
            # single-value getter); `labelFields` is the full list, in
            # source order, so multi-value cards can land each text
            # encoding as its own row.
            "labelField":    label_e,
            "labelFields":   labels,
            "detailFields":  details,
            "tooltipFields": tooltips,
            "filters":       filters,
            # Tableau's <shelf-sort-v2> / <single-value-per-nest-shelf-sort>
            # directives, lifted into a list of dicts:
            #   {dimension, dimensionAgg, measure, measureAgg, direction, shelf}
            # `measure` is empty when Tableau is sorting the dimension by
            # itself (alphabetic). Report.py turns each entry into a PBI
            # query.sortDefinition.sort entry.
            "sortSpecs":     sort_specs,
            "datasourceRef": ds_ref,
            # All non-Parameters datasource dependencies, in the order
            # Tableau wrote them. First element is the primary; the rest
            # represent secondary blends. Each entry has {datasource,
            # columns: [bare_field_name]}.
            "datasourceDeps": ds_deps,
            # canonical column name -> raw name as it appears in this
            # worksheet's datasource-dependencies (preserves any
            # `(Object!Suffix)` disambiguation hint for the resolver).
            "wsColumns":     ws_columns,
            "isGeo":         is_geo,
            # Title / label / background formatting carried over from twb.
            # All four are optional — report.py defaults gracefully when
            # absent. titleEnabled/labelEnabled use None to mean
            # "Tableau didn't say either way; use PBI default".
            "titleText":     title_text,
            "titleStyle":    title_style,
            "titleEnabled":  title_enabled,
            "labelEnabled":  label_enabled,
            "labelStyle":    label_style,
            "backgroundColor": background_color,
            # Header styling for tableEx / pivotTable column + row headers.
            # Each entry is a dict with fontFamily / fontSize / fontColor /
            # fontWeight / italic / underline / backgroundColor / textAlign,
            # carrying whatever Tableau's `<style-rule element='header'|'column-header'|'row-header'>`
            # blocks declared. Missing keys fall back to PBI defaults in
            # report._make_table_header_props.
            "columnHeaderStyle": header_styles.get("columnHeaderStyle") or {},
            "rowHeaderStyle":    header_styles.get("rowHeaderStyle") or {},
            # Mark-color override: a single hex color that should paint
            # every data point on the visual when no categorical color
            # encoding is bound to a field. Report.py emits it as
            # objects.dataPoint.defaultColor on bar/line/area/scatter
            # visuals and silently ignores it on visual types that use
            # a different color bag.
            "markColor":     mark_color,
        }

    # ------------------------------------------------------------------
    # Worksheet sub-parsers (filters, title, labels, background)
    # ------------------------------------------------------------------

    def _parse_worksheet_filters(
        self, view: Optional[ET.Element],
    ) -> List[Dict[str, Any]]:
        """Extract <view>/<filter> blocks as visual-level filter bindings.

        Filter shapes we honor:

            <filter class='categorical' column='[ds].[role:Field:k]'>
                <groupfilter function='level-members'
                             user:ui-enumeration='all'/>
                <!-- 'all' with no inner members -> column binding only -->
            </filter>

            <filter class='categorical' column='...'>
                <groupfilter function='member' level='...'
                             member='&quot;EMEA&quot;'/>
                <!-- single value selected: 'EMEA' (quotes stripped) -->
            </filter>

            <filter class='categorical' column='...'>
                <groupfilter function='union' level='...'>
                    <groupfilter function='member' member='&quot;X&quot;'/>
                    <groupfilter function='member' member='&quot;Y&quot;'/>
                </groupfilter>
            </filter>

            <filter class='categorical' column='...'>
                <groupfilter function='except'>
                    <groupfilter function='member' member='&quot;Z&quot;'/>
                </groupfilter>
                <!-- exclude: emits NotIn -->
            </filter>

        Tableau encodes string members as &quot;X&quot; (a quoted token)
        and numerics bare. Either way, surrounding quotes are stripped so
        the downstream PBI filter sees the raw value.

        Skipped: Tableau action filters (function='crossjoin' or any
        groupfilter carrying user:ui-action-filter) — those are
        cross-dashboard programmatic filters, not user filter-shelf
        entries.
        """
        out: List[Dict[str, Any]] = []
        if view is None:
            return out
        ui_ns = "{http://www.tableausoftware.com/xml/user}"
        for f in view.findall("filter"):
            raw_ref = f.get("field", f.get("column", ""))
            if not raw_ref:
                continue
            _, fname = self._parse_field_ref(raw_ref)
            if not fname:
                continue

            # Skip action filters — programmatic dashboard linking, not
            # something a user authored on a filter shelf.
            is_action = False
            for gf in f.iter("groupfilter"):
                if gf.get("function") == "crossjoin":
                    is_action = True
                    break
                if gf.get(ui_ns + "ui-action-filter"):
                    is_action = True
                    break
            if is_action:
                continue

            members, exclude = self._collect_filter_members(f)
            top_spec = self._extract_top_n_spec(f)
            entry: Dict[str, Any] = {
                "field":   fname,
                "type":    f.get("class", "categorical"),
                "members": members,
                "exclude": exclude,
            }
            if top_spec:
                # Tableau "Advanced > Top N" filters carry the limit count
                # plus the measure to rank by. Report.py emits this as a
                # PBI TopN visual-level filter when present.
                entry["topN"] = top_spec
            out.append(entry)
        return out

    # Tableau aggregation function names (as they appear in `expression=`
    # attributes inside <groupfilter function='order'>) mapped to the
    # short tokens AGG_TABLE keys on. Lowercase keys; lowercase compare.
    _TOPN_AGG_ALIASES = {
        "sum":     "sum",
        "avg":     "avg",
        "average": "avg",
        "count":   "cnt",
        "cnt":     "cnt",
        "countd":  "ctd",
        "cntd":    "ctd",
        "min":     "min",
        "max":     "max",
        "median":  "med",
        "med":     "med",
        "stdev":   "std",
        "std":     "std",
        "var":     "var",
        "attr":    "attr",
    }

    @classmethod
    def _extract_top_n_spec(
        cls, f: ET.Element,
    ) -> Optional[Dict[str, Any]]:
        """Recognise Tableau Top-N (advanced) filter shape.

        Tableau emits Top N as a nested groupfilter pair: an outer
        function='end' carrying count + direction, and an inner
        function='order' carrying the ranking expression. The actual
        wire shape is:

            <filter class='categorical' column='[ds].[listed_in]'>
              <groupfilter count='10' end='top' function='end'
                           units='records' user:ui-marker='end'
                           user:ui-top-by-field='true'>
                <groupfilter direction='DESC'
                             expression='COUNTD([show_id])'
                             function='order' user:ui-marker='order'>
                  <groupfilter function='level-members'
                               level='[none:listed_in:nk]'/>
                </groupfilter>
              </groupfilter>
            </filter>

        `count` is the N. `end='top'|'bottom'` picks Top vs Bottom. The
        ranking expression is parsed out of the inner <order> element's
        expression attribute, e.g. `COUNTD([show_id])` -> agg=ctd,
        measure='show_id'. Returns None when no end+order pair is found.
        """
        end_gf = None
        order_gf = None
        for gf in f.iter("groupfilter"):
            fn = gf.get("function")
            if fn == "end" and end_gf is None:
                end_gf = gf
            elif fn == "order" and order_gf is None:
                order_gf = gf
            if end_gf is not None and order_gf is not None:
                break

        if end_gf is None:
            return None

        try:
            count = int(end_gf.get("count") or "0")
        except ValueError:
            count = 0
        if count <= 0:
            return None

        end_attr = (end_gf.get("end") or "top").lower()
        direction = "BOTTOM" if end_attr == "bottom" else "TOP"

        measure = ""
        measure_agg = ""
        if order_gf is not None:
            expr = (order_gf.get("expression") or "").strip()
            # `COUNTD([show_id])` form
            m = re.match(r"^\s*([A-Za-z_]+)\s*\(\s*\[([^\]]+)\]\s*\)\s*$", expr)
            if m:
                fn_name = m.group(1).lower()
                measure_agg = cls._TOPN_AGG_ALIASES.get(fn_name, fn_name)
                measure     = m.group(2).strip()
            else:
                # `[field]` bare reference, no agg
                m2 = re.match(r"^\s*\[([^\]]+)\]\s*$", expr)
                if m2:
                    measure = m2.group(1).strip()
            # If Tableau's order block declared a direction, prefer it
            # over the outer 'end' (`end='top'` already implies DESC for
            # Top, but Bottom + DESC is a thing — be explicit).
            order_dir = (order_gf.get("direction") or "").upper()
            if order_dir == "ASC" and direction == "TOP":
                direction = "BOTTOM"
            elif order_dir == "DESC" and direction == "BOTTOM":
                # Bottom-by-DESC is unusual but valid; trust the outer
                # 'end' attr since that's what the user picked in the UI.
                pass

        return {
            "direction":  direction,    # TOP or BOTTOM
            "count":      count,
            "measure":    measure,
            "measureAgg": measure_agg,
        }

    @staticmethod
    def _collect_filter_members(
        f: ET.Element,
    ) -> Tuple[List[str], bool]:
        """Walk a <filter>'s groupfilter tree and collect picked members.

        Returns (members, exclude). exclude=True when the outer wrapping
        is function='except', so the caller emits NotIn. members is a
        deduped list (preserving order) of cleaned values. When the only
        groupfilter is function='level-members' with no members listed,
        members is empty (means 'all selected' — no Where condition).
        """
        members: List[str] = []
        exclude = False
        seen: set = set()

        # Outer wrapper signals exclusion
        for gf in f.findall("groupfilter"):
            if gf.get("function") == "except":
                exclude = True
                break

        for gf in f.iter("groupfilter"):
            if gf.get("function") != "member":
                continue
            raw = gf.get("member", "").strip()
            if not raw:
                continue
            # Strip Tableau's surrounding quotes — strings come as
            # &quot;X&quot; (decoded to "X"); numerics come bare.
            if (raw.startswith('"') and raw.endswith('"')) or \
               (raw.startswith("'") and raw.endswith("'")):
                raw = raw[1:-1]
            if raw and raw not in seen:
                seen.add(raw)
                members.append(raw)

        return members, exclude

    @classmethod
    def _parse_worksheet_sort(
        cls, ws: ET.Element,
    ) -> List[Dict[str, Any]]:
        """Lift Tableau shelf sort directives into structured dicts.

        Tableau encodes sorts as siblings of <view>:

            <shelf-sort-v2 dimension-to-sort='[ds].[col-spec]'
                           direction='ASC|DESC'
                           is-on-innermost-dimension='true'
                           measure-to-sort-by='[ds].[agg:Field:k]'
                           shelf='rows|cols' />

        The single-value variant (one row of dim, one row of measure)
        looks the same with element name `single-value-per-nest-shelf-sort`.
        Both decode the same way.

        `measure-to-sort-by` may be the same column as `dimension-to-sort`
        (alphabetic sort of the dim by itself) — we still parse it; the
        report builder picks whether to emit a Column or Aggregation
        sort field based on whether `measure` resolves to an aggregated
        token.
        """
        out: List[Dict[str, Any]] = []
        for tag in ("shelf-sort-v2", "single-value-per-nest-shelf-sort"):
            for s in ws.iter(tag):
                dim_ref = s.get("dimension-to-sort", "")
                meas_ref = s.get("measure-to-sort-by", "")
                if not dim_ref:
                    continue
                dim_agg, dim_name = cls._parse_field_ref(dim_ref)
                meas_agg, meas_name = cls._parse_field_ref(meas_ref)
                direction = (s.get("direction", "ASC") or "ASC").upper()
                if direction not in ("ASC", "DESC"):
                    direction = "ASC"
                out.append({
                    "dimension":    dim_name,
                    "dimensionAgg": dim_agg,
                    "measure":      meas_name,
                    "measureAgg":   meas_agg,
                    "direction":    direction,
                    "shelf":        s.get("shelf", ""),
                })
        return out

    @staticmethod
    def _parse_worksheet_title(
        ws: ET.Element, name: str,
    ) -> Tuple[str, Dict[str, Any], Optional[bool]]:
        """Return (titleText, titleStyle, titleEnabled).

        Tableau stores the title under <layout-options>/<title>. Each
        <run> inside <formatted-text> has the formatting on its attrs:
            bold='true', fontname='...', fontsize='12', fontcolor='#abc',
            fontalignment='0|1|2'  (left | center | right)

        '<Sheet Name>' is Tableau's placeholder for the worksheet name —
        we substitute it here so PBI shows the actual name.

        titleEnabled is None when Tableau didn't surface a hint; report.py
        treats None as "use default" (show). When a worksheet has no title
        element at all OR has an explicit hide marker, we emit False.
        """
        title_el = ws.find(".//layout-options/title")
        # No title element AT ALL -> treat as hidden. Tableau worksheets
        # default to showing the title, but a user who toggled it off
        # often produces a layout-options block without a title child.
        if title_el is None:
            lo = ws.find(".//layout-options")
            if lo is not None and lo.find("title") is None:
                return "", {}, False
            return "", {}, None

        runs = title_el.findall(".//run")
        if not runs:
            # Title element with no run -> Tableau's "use default" = show
            # the worksheet name with default formatting.
            return "", {}, True

        parts: List[str] = []
        for r in runs:
            t = (r.text or "").strip()
            # Strip Tableau's trailing Æ ligature (U+00C6) and control characters
            # that appear as invisible formatting artifacts in worksheet titles.
            t = TWBParser._clean_text(t)
            if t == "<Sheet Name>":
                parts.append(name)
            elif t:
                parts.append(t)
        title_text = " ".join(parts).strip()

        first = runs[0]
        style: Dict[str, Any] = {}
        if first.get("bold") == "true":
            style["fontWeight"] = "bold"
        if first.get("italic") == "true":
            style["italic"] = True
        if first.get("underline") == "true":
            style["underline"] = True
        if first.get("fontname"):
            style["fontFamily"] = first.get("fontname")
        fs = first.get("fontsize")
        if fs:
            try:
                style["fontSize"] = int(float(fs))
            except (ValueError, TypeError):
                pass
        if first.get("fontcolor"):
            style["fontColor"] = first.get("fontcolor")
        align = first.get("fontalignment")
        # 0 = left (default), 1 = center, 2 = right. We only emit the
        # non-default values so PBI Desktop falls back to its own default
        # alignment instead of a hard 'left'.
        if align == "1":
            style["textAlign"] = "center"
        elif align == "2":
            style["textAlign"] = "right"

        return title_text, style, True

    @classmethod
    def _parse_custom_tooltip_refs(cls, ws: ET.Element) -> List[Dict[str, str]]:
        """Extract field references from a worksheet's custom tooltip
        template.

        Tableau stores rich tooltip templates as:
            <customized-tooltip>
              <formatted-text>
                <run bold='true'>&lt;[ds].[role:field:nk]&gt;</run>
                <run> text </run>
                ...
              </formatted-text>
            </customized-tooltip>

        Each `<[ds].[role:field:k]>` is a placeholder that Tableau
        replaces with the field's aggregated value at render time. PBI
        doesn't support rich tooltip templates the same way, but we can
        still surface the referenced fields on the visual's Tooltips
        slot so the user can drag them into a custom tooltip page.

        Returns a list of {field, agg} dicts, deduped by field name.
        Empty list when no custom tooltip is present.
        """
        ct = ws.find(".//customized-tooltip")
        if ct is None:
            return []
        # Concatenate the decoded text content of every <run>; ET
        # already decoded &lt; / &gt; back into < / >.
        full = ""
        for r in ct.findall(".//run"):
            full += (r.text or "")
        # Match <[ds].[spec]>. The spec is the same form _parse_field_ref
        # already understands ('role:field' or 'role:field:k').
        out: List[Dict[str, str]] = []
        seen: set = set()
        for spec in re.findall(r"<\[[^\]]+\]\.\[([^\]]+)\]>", full):
            agg, fname = cls._parse_field_ref(f"[ds].[{spec}]")
            if not fname or fname in seen:
                continue
            seen.add(fname)
            out.append({"field": fname, "agg": agg})
        return out

    @staticmethod
    def _parse_worksheet_mark_color(ws: ET.Element) -> Optional[str]:
        """Pull the worksheet's manual mark color (the single-color
        override authored on the Color shelf in Tableau).

        Tableau emits this as:
            <style-rule element='mark'>
                <format attr='mark-color' value='#e50914'/>
            </style-rule>

        Returns the hex color, or None when the worksheet uses Tableau's
        default palette. The mark-color override only fires when the
        user picked a specific color (no field on the color shelf, just
        a swatch). When a categorical color encoding IS bound to a
        field, Tableau omits this attr — that case is handled
        separately via the encoding shelf.
        """
        for sr in ws.findall(".//style-rule"):
            if sr.get("element") not in ("mark", "marks"):
                continue
            for f in sr.findall("format"):
                if f.get("attr") == "mark-color" and f.get("value"):
                    return f.get("value")
        return None

    @staticmethod
    def _parse_worksheet_labels(
        ws: ET.Element,
    ) -> Tuple[Optional[bool], Dict[str, Any]]:
        """Return (labelEnabled, labelStyle) for the worksheet's value text.

        Tableau styles the worksheet value across THREE layers:

          1. `<style-rule element='mark'>` with `mark-labels-*` attrs —
             explicit per-visual data-label format (bar / line / etc.).
             Highest precedence.
          2. `<style-rule element='cell'>` with bare `font-*` attrs —
             cell-level format. Applies primarily to text-mark cards
             (where each cell IS the value).
          3. `<style-rule element='worksheet'>` with `font-*` / `color` —
             worksheet-wide default. Lowest precedence.

        We merge the three layers in *reverse precedence order* so the
        most specific rule wins. The result is what feeds the card's
        callout block (and a chart's data-label format).

        labelEnabled is None when no `mark-labels-show` entry is present
        — the report builder treats that as "use PBI default" and won't
        override.
        """
        enabled: Optional[bool] = None
        # Precedence: worksheet (lowest) -> cell -> mark (highest).
        layered: Dict[str, Dict[str, Any]] = {
            "worksheet": {}, "cell": {}, "mark": {},
        }

        def _set_size(bag: Dict[str, Any], val: str) -> None:
            try:
                bag["fontSize"] = int(float(val))
            except (ValueError, TypeError):
                pass

        for sr in ws.findall(".//style-rule"):
            elem = sr.get("element", "")
            if elem in ("mark", "marks"):
                bag = layered["mark"]
                for f in sr.findall("format"):
                    attr = f.get("attr", "")
                    val  = f.get("value", "")
                    if attr == "mark-labels-show":
                        enabled = (val == "true")
                    elif attr == "mark-labels-color":
                        bag["fontColor"] = val
                    elif attr == "mark-labels-font-name":
                        bag["fontFamily"] = val
                    elif attr == "mark-labels-font-size":
                        _set_size(bag, val)
                    elif attr in ("mark-labels-font-bold",
                                  "mark-labels-bold") and val == "true":
                        bag["fontWeight"] = "bold"
                    elif attr in ("mark-labels-font-italic",
                                  "mark-labels-italic") and val == "true":
                        bag["italic"] = True
                    elif attr in ("mark-labels-font-underline",
                                  "mark-labels-underline") and val == "true":
                        bag["underline"] = True
            elif elem in ("cell", "worksheet"):
                # Bare `font-*` / `color` attrs — Tableau emits these
                # at the worksheet/cell layer for text-mark KPIs.
                bag = layered[elem]
                for f in sr.findall("format"):
                    attr = f.get("attr", "")
                    val  = f.get("value", "")
                    if attr == "color":
                        bag["fontColor"] = val
                    elif attr == "font-family":
                        bag["fontFamily"] = val
                    elif attr == "font-size":
                        _set_size(bag, val)
                    elif attr == "font-weight" and val == "bold":
                        bag["fontWeight"] = "bold"
                    elif attr == "font-style" and val == "italic":
                        bag["italic"] = True

        # Customized-label runs — the `<customized-label>/<formatted-text>/
        # <run>` block carries the actual font/color/size the user picks
        # in the marks-card label editor. Highest precedence: the user
        # explicitly authored these for the value text. Tableau emits one
        # block per label "row"; we use the FIRST run's attrs (font is
        # uniform across the customized-label in practice). When absent,
        # fall back to the layered worksheet/cell/mark rules above.
        cust_run = ws.find(".//customized-label/formatted-text/run")
        cust_bag: Dict[str, Any] = {}
        if cust_run is not None:
            if cust_run.get("fontcolor"):
                cust_bag["fontColor"] = cust_run.get("fontcolor")
            if cust_run.get("fontname"):
                cust_bag["fontFamily"] = cust_run.get("fontname")
            fs = cust_run.get("fontsize")
            if fs:
                _set_size(cust_bag, fs)
            if cust_run.get("bold") == "true":
                cust_bag["fontWeight"] = "bold"
            if cust_run.get("italic") == "true":
                cust_bag["italic"] = True
            if cust_run.get("underline") == "true":
                cust_bag["underline"] = True

        # Merge in precedence order: worksheet (lowest), cell, mark,
        # customized-label (highest).
        style: Dict[str, Any] = {}
        for layer in ("worksheet", "cell", "mark"):
            for k, v in layered[layer].items():
                style[k] = v
        for k, v in cust_bag.items():
            style[k] = v
        return enabled, style

    @staticmethod
    def _parse_worksheet_header_style(ws: ET.Element) -> Dict[str, Any]:
        """Extract column/row-header styling from a worksheet.

        Tableau emits header formatting across several style-rule
        elements. The user-facing "Format → Headers" / "Format →
        Columns" / "Format → Field Labels" panels write to different
        elements, and the column-header BACKGROUND specifically lives
        on the ``field-labels-decoration`` element (the band that paints
        behind the field labels in tables / crosstabs) — NOT on
        ``header`` like the font/color attrs.

        Element precedence (most specific wins):

          1. ``column-header`` / ``row-header`` — per-axis explicit
             rules from "Format → Columns" / "Format → Rows".
          2. ``field-labels-decoration``        — the visible "header
             band" background colour. Source for backgroundColor when
             ``header`` / ``column-header`` didn't set one.
          3. ``field-labels``                   — field-label font /
             color (the column-name text itself).
          4. ``header``                         — generic header rule
             ("Format → Headers" default). Picks up font / color /
             alignment / background for whichever axis didn't have a
             more specific rule.

        The format attrs we honour:

          * ``color``            → fontColor
          * ``font-family``      → fontFamily
          * ``font-size``        → fontSize (integer pt)
          * ``font-weight=bold`` → fontWeight=bold
          * ``font-style=italic``→ italic=True
          * ``text-decoration=underline`` (or ``font-underline=true``)
                                 → underline=True
          * ``background-color`` → backgroundColor (skipping transparent)
          * ``text-align``       → textAlign (left/center/right/auto)

        Scoped variants (``data-class='subtotal'`` / ``'total'``,
        ``scope='rows'/'cols'/'totals'``, ``field='...'``) are SKIPPED
        ENTIRELY — they are overrides for specific rows/columns and
        shouldn't drive the general header style. A previous version
        accidentally let a subtotal background through when the base
        rule had no background of its own, producing pale-grey headers
        in a workbook that intended dark blue.

        Returned dict is empty when Tableau supplied nothing — the
        report builder then layers Tableau-like defaults instead.
        """
        # Per-element bags so a more specific rule (column-header)
        # overrides the broader 'header' rule on conflict.
        ELEM_ORDER = (
            "header",
            "field-labels",
            "field-labels-decoration",
            "row-header",
            "column-header",
        )
        layered: Dict[str, Dict[str, Any]] = {e: {} for e in ELEM_ORDER}

        def _set_size(bag: Dict[str, Any], val: str) -> None:
            try:
                bag["fontSize"] = int(float(val))
            except (ValueError, TypeError):
                pass

        for sr in ws.findall(".//style-rule"):
            elem = sr.get("element", "")
            if elem not in layered:
                continue
            bag = layered[elem]
            for f in sr.findall("format"):
                attr = f.get("attr", "")
                val  = (f.get("value") or "").strip()
                if not val:
                    continue
                # Skip scoped overrides entirely. Tableau emits
                # `data-class='subtotal'` / `'total'`, `scope='rows'` /
                # `'cols'` / `'totals'`, or `field='...'` for per-cell
                # overrides. Those describe sub-regions of the table
                # body / totals row, not the general header style PBI
                # columnHeaders / rowHeaders bags expose.
                if (f.get("data-class")
                        or f.get("scope") in ("rows", "cols", "totals")
                        or f.get("field")):
                    continue

                if attr == "color":
                    bag["fontColor"] = val
                elif attr == "font-family":
                    bag["fontFamily"] = val
                elif attr == "font-size":
                    _set_size(bag, val)
                elif attr == "font-weight" and val == "bold":
                    bag["fontWeight"] = "bold"
                elif attr == "font-style" and val == "italic":
                    bag["italic"] = True
                elif attr in ("font-underline", "text-decoration") and val in ("true", "underline"):
                    bag["underline"] = True
                elif attr == "background-color":
                    if val.lower() not in ("#00000000", "none"):
                        bag["backgroundColor"] = val
                elif attr == "text-align" and val not in ("auto",):
                    # PBI alignment values are 'left' / 'center' / 'right'
                    # (lowercase). Tableau emits the same vocabulary.
                    bag["textAlign"] = val

        # Merge: lowest-precedence first, then progressively more
        # specific so the specific rule wins on conflict.
        merged_col: Dict[str, Any] = {}
        for elem in ELEM_ORDER:
            if elem in ("row-header",):
                continue  # row-only rule doesn't feed column
            merged_col.update(layered[elem])

        merged_row: Dict[str, Any] = {}
        for elem in ELEM_ORDER:
            if elem in ("column-header",):
                continue  # column-only rule doesn't feed row
            merged_row.update(layered[elem])

        out: Dict[str, Any] = {}
        if merged_col:
            out["columnHeaderStyle"] = merged_col
        if merged_row:
            out["rowHeaderStyle"] = merged_row
        return out

    @staticmethod
    def _parse_worksheet_background(ws: ET.Element) -> Optional[str]:
        """Pull a background color from a worksheet's style-rules.

        Tableau emits the worksheet canvas background under
        <style-rule element='table'> — confusingly the same element
        name dashboards use, just scoped here under <worksheet>. We
        also accept 'worksheet' and 'pane' for older workbook
        versions. Pure transparency ('#00000000') is treated as 'no
        background' so an opaque value from a more specific rule
        still wins. Element preference order: pane > worksheet >
        table — pane is the actual chart-area paint; the others may
        be the broader card frame.
        """
        candidates: Dict[str, str] = {}
        for sr in ws.findall(".//style-rule"):
            elem = sr.get("element", "")
            if elem not in ("table", "worksheet", "pane"):
                continue
            for f in sr.findall("format"):
                if f.get("attr") != "background-color":
                    continue
                val = (f.get("value") or "").strip()
                if not val or val.lower() in ("#00000000", "none"):
                    continue
                candidates.setdefault(elem, val)
        for elem in ("pane", "worksheet", "table"):
            if elem in candidates:
                return candidates[elem]
        return None

    @staticmethod
    def _pick_worksheet_datasource(view: ET.Element) -> str:
        """Return the data datasource name for a worksheet's <view>.

        A worksheet may declare multiple <datasource-dependencies> blocks
        (one per datasource it touches). The 'Parameters' meta-datasource
        is always present whenever the worksheet references a parameter,
        so we explicitly skip it and prefer the first real datasource. If
        nothing else is declared we fall back to whatever was found, even
        if it's 'Parameters' — that lets diagnostic logs show the full
        truth instead of silently picking a wrong default.
        """
        # Direct children first (the "primary" datasource for the view),
        # then any descendant block.
        for finder in (lambda v: v.findall("datasource-dependencies"),
                       lambda v: v.findall(".//datasource-dependencies")):
            blocks = list(finder(view))
            for dd in blocks:
                ds = (dd.get("datasource") or "").strip()
                if ds and ds.lower() != "parameters":
                    return ds
            # If we got here, every block was Parameters or empty. Fall
            # through to the next finder (deeper search) before giving up.
        # No real datasource — return whatever came first (may be empty).
        first = view.find("datasource-dependencies")
        return (first.get("datasource") if first is not None else "") or ""

    # Tableau column-instance type-suffix tokens. They appear at the end
    # of a column-instance spec to mark how the value participates in
    # the visual: nk=nominal key, qk=quantitative key, ok=ordinal key,
    # ck=continuous key, ik=interval key. May be followed by a duplicate-
    # index integer (e.g. `qk:3`) when the same column is dragged onto
    # two shelves of the same worksheet.
    _TABLEAU_TYPE_SUFFIXES = {"nk", "qk", "ok", "ck", "ik"}

    # Characters Tableau injects as invisible-formatting / separator artifacts
    # in <run> text — these don't appear in the user-edited Tableau title but
    # leak through into the converted PBI textbox / title and render as junk
    # boxes / question-mark glyphs because PBI's text renderer doesn't have
    # them in its font fall-back chain. The set is deliberately conservative:
    # only chars that are *known* to be Tableau artifacts get stripped.
    #   - C0 / DEL controls (0x00-0x1F, 0x7F): zero-width / non-printing
    #   - C1 controls (0x80-0x9F): Tableau RTF leftovers
    #   - U+00C6 (Æ): Tableau's run separator marker
    #   - U+200B-U+200F, U+202A-U+202E, U+2060: zero-width / bidi controls
    #   - U+FEFF: zero-width no-break space (BOM)
    #   - U+FFF9-U+FFFD: interlinear annotation / replacement chars
    _TEXT_ARTIFACT_RE = re.compile(
        r"[\x00-\x1f\x7f-\x9fÆ​-‏‪-‮⁠﻿￹-�]"
    )

    @classmethod
    def _clean_text(cls, s: str) -> str:
        """Strip Tableau-injected text artifacts from user-visible strings
        (worksheet titles, textbox content, captions). Tableau's RTF run
        encoding leaks bidi marks, zero-width separators, and the U+00C6
        ligature into the .twb XML — these don't show up in Tableau itself
        but render as boxes / `?` glyphs in PBI Desktop because the default
        Segoe UI fall-back doesn't cover them. Whitespace runs created by
        the substitution are collapsed to single spaces.
        """
        if not s:
            return s
        cleaned = cls._TEXT_ARTIFACT_RE.sub("", s)
        # Collapse multi-space runs that the strip might have created.
        cleaned = re.sub(r" {2,}", " ", cleaned)
        return cleaned.strip()

    # Aggregation / role tokens that legitimately appear as the FIRST
    # part of a column-instance spec. Without this allow-list, a calc
    # whose internal name happens to look like a Tableau token (e.g.
    # `Calculation_xxx`) would have its first chunk misread as the agg.
    _TABLEAU_AGG_ROLES = {
        # Numeric aggregations
        "sum", "avg", "average", "cnt", "count", "cntd", "ctd",
        "min", "max", "med", "median", "std", "stdev", "var",
        # Date-part extractions (return integer level)
        "yr", "qr", "mn", "wk", "dy", "hr", "mi", "sc", "sd",
        # Truncate-date (return date truncated to period start) — Tableau
        # uses these on continuous date axes and date filter slicers. The
        # converter routes them to synthesized ``Year-Trunc of X`` /
        # ``Year-Quarter of X`` / ``Year-Month of X`` calculated columns
        # in the resolver (see report.py date_trunc_levels).
        "ty", "tqr", "tmn", "tw", "twn", "tmd", "td",
        "thr", "tmin", "ts",
        # Table calculations — currently no DAX rewrite; we strip the
        # prefix so the underlying field still resolves and the bare
        # column gets dropped on the visual rather than the entire ref
        # being lost. Visual calculations (PBI 2024+) cover most of these
        # but the converter doesn't generate them yet.
        "cum", "pcto", "pct", "dif", "pdif", "rnk",
        "wsum", "wavg", "wmin", "wmax", "wstd", "wvar",
        "wmedian", "wcount", "wcountd",
        # Forecast result columns
        "fval", "findic", "fhi", "flo",
        # Calc / parameter passthroughs
        "attr", "usr", "none",
    }

    @classmethod
    def _parse_field_ref(cls, ref: str) -> Tuple[str, str]:
        """Pulls (agg, fieldName) from a Tableau qualified reference.

        Forms in the wild:
            [ds].[role:field:key]    -> (role, field)
            [ds].[role:field:key:N]  -> (role, field)   # duplicate-index
            [ds].[role:field]        -> (role, field)
            [ds].[field:key]         -> ("", field)     # no role, just suffix
            [ds].[field]             -> ("", field)
            AGG([field])             -> (AGG, field)
            [field]                  -> ("", field)

        The trailing type-suffix (`qk`/`nk`/`ok`/`ck`/`ik`, optionally
        followed by a duplicate-index integer) is dropped before
        identifying role vs field. The first part is treated as a role
        only when it's a known Tableau aggregation/role token; otherwise
        it stays part of the field name (so calc IDs like
        `Calculation_xxx` aren't accidentally parsed as a "Calculation_xxx"
        aggregation).
        """
        if not ref:
            return "", ""
        s = ref.strip()
        while s.startswith("(") and s.endswith(")"):
            s = s[1:-1].strip()
        m = re.match(r"^\[[^\]]+\]\s*\.\s*\[(.+?)\]$", s)
        if m:
            spec  = m.group(1)
            parts = spec.split(":")
            # Strip trailing type-suffix and any duplicate-index integer
            # that follows it. We walk from the right looking for the
            # first type-suffix token; everything from there on is meta.
            for i in range(len(parts) - 1, -1, -1):
                if parts[i] in cls._TABLEAU_TYPE_SUFFIXES:
                    parts = parts[:i]
                    break
            if not parts:
                return "", ""
            # Strip leading role tokens iteratively. Tableau allows stacking
            # (e.g. ``fVal:usr:Calculation_xxx`` where ``fVal`` is forecast
            # and ``usr`` is user-calc) — without iterative stripping the
            # second role would be misread as the field name. The
            # OUTERMOST role is the one we report; inner roles are
            # absorbed silently because there's no PBI shape that mirrors
            # a stack of aggregations on a single field.
            role = ""
            while parts and parts[0].lower() in cls._TABLEAU_AGG_ROLES:
                if not role:
                    role = parts[0]
                parts = parts[1:]
            field = ":".join(parts)
            return role.strip().lower(), field.strip()
        m = re.match(r"^(\w+)\s*\(\s*\[(.+?)\]\s*\)\s*$", s)
        if m:
            return m.group(1).lower(), m.group(2).strip()
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            return "", m.group(1).strip()
        return "", s

    def _build_geo_role_lookup(self, ds_ref: str) -> Dict[str, str]:
        """Build {column_name -> 'Latitude'|'Longitude'} for one datasource.

        Looks up `semanticRole` on each parsed column (set from the
        `<column semantic-role='...'>` attribute). Both the canonical
        unsuffixed name and any `(Object!Suffix)` form get registered so
        a shelf reference using either form resolves to the same role.
        Returns {} when the datasource isn't found or has no geo columns —
        callers tolerate an empty mapping.
        """
        out: Dict[str, str] = {}
        if not ds_ref:
            return out
        for ds in self.datasources:
            if ds.get("name") != ds_ref:
                continue
            for col in ds.get("columns", []) or []:
                sr = (col.get("semanticRole") or "").lower()
                role = ""
                if "[geographical].[latitude]" in sr or sr.endswith(".[latitude]"):
                    role = "Latitude"
                elif "[geographical].[longitude]" in sr or sr.endswith(".[longitude]"):
                    role = "Longitude"
                if not role:
                    continue
                cname = col.get("name", "")
                if cname:
                    out[cname] = role
                # Also register the un-suffixed canonical so lookups by
                # the bare name still hit when the column was registered
                # with an Object!Suffix.
                canon = _strip_obj_suffix_name(cname)
                if canon and canon not in out:
                    out[canon] = role
            break
        return out

    def _split_fields(self, raw: str) -> List[Dict[str, Any]]:
        if not raw:
            return []
        # Tableau separators:  '+' = side-by-side measures, '/' = nesting
        stack: List[str] = [raw.strip()]
        flat:  List[str] = []
        while stack:
            cur = stack.pop().strip()
            while cur.startswith("(") and cur.endswith(")"):
                cur = cur[1:-1].strip()
            if not cur:
                continue
            for sep in ("+", "/"):
                parts = self._split_top(cur, sep)
                if len(parts) > 1:
                    stack.extend(parts)
                    break
            else:
                flat.append(cur)

        # The DFS-via-stack-pop produces fields in reverse of Tableau's
        # left-to-right shelf serialization. Restore source order so the
        # downstream report builder lays out PBI columns in the same
        # order the user sees in Tableau (e.g. table column order in
        # 'Logical Tabular View').
        flat.reverse()

        out: List[Dict[str, Any]] = []
        for part in flat:
            agg, fname = self._parse_field_ref(part)
            ds_hint = self._extract_ds_prefix(part)
            low = fname.lower()
            if low in ("measure names", "measure values"):
                continue
            out.append({
                "raw":   part,
                "field": fname,
                "agg":   agg,
                # Datasource hint extracted from the [ds].[col] prefix.
                # Empty when the reference is bare. Used downstream to
                # route a binding to a non-primary datasource for blended
                # worksheets without breaking single-datasource flows.
                "datasource": ds_hint,
                "isGeo": low in ("latitude (generated)",
                                 "longitude (generated)",
                                 "latitude", "longitude"),
            })
        return out

    @staticmethod
    def _extract_ds_prefix(ref: str) -> str:
        """Pull the datasource id out of a `[ds].[col]` reference.

        Returns the bracketed datasource id (without the surrounding
        brackets) when the reference is fully qualified; returns "" when
        the reference is bare (no datasource prefix). The id is the
        Tableau internal name (e.g. `federated.0vxxkgi12h8snh15qbxs40g4th0r
        (copy 4)`), which downstream code matches against
        `datasource['name']` directly.
        """
        if not ref:
            return ""
        s = ref.strip()
        # Strip leading aggregation wrapper: AGG([ds].[col]) -> [ds].[col].
        m = re.match(r"^\w+\s*\(\s*(.+?)\s*\)\s*$", s)
        if m:
            s = m.group(1)
        m = re.match(r"^\[([^\]]+)\]\s*\.\s*\[", s)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _split_top(s: str, sep: str) -> List[str]:
        out, cur, depth = [], [], 0
        for ch in s:
            if   ch == "(":              depth += 1; cur.append(ch)
            elif ch == ")":              depth -= 1; cur.append(ch)
            elif ch == sep and depth == 0:
                out.append("".join(cur)); cur = []
            else:                        cur.append(ch)
        if cur:
            out.append("".join(cur))
        return out

    # ------------------------------------------------------------------
    # Dashboards & zones
    # ------------------------------------------------------------------

    def _parse_dashboards(self, root: ET.Element) -> None:
        for db in root.findall(".//dashboards/dashboard"):
            name = db.get("name", "")
            sz   = db.find("size")
            w    = safe_int(sz.get("maxwidth")  if sz is not None else "",
                            DEFAULT_PAGE_WIDTH)
            h    = safe_int(sz.get("maxheight") if sz is not None else "",
                            DEFAULT_PAGE_HEIGHT)
            self.dashboards.append({
                "name":   name,
                "width":  w or DEFAULT_PAGE_WIDTH,
                "height": h or DEFAULT_PAGE_HEIGHT,
                "zones":  self._collect_zones(db),
                "backgroundColor": self._extract_dashboard_background(db),
            })

    @staticmethod
    def _extract_dashboard_background(db: ET.Element) -> Optional[str]:
        """Pull the dashboard's canvas background color from
        <style-rule element='table'><format attr='background-color'/>.

        Tableau uses 'table' as the CSS-like name for the dashboard's
        outer canvas. Returns None when no background is declared so
        the writer can leave page.json's objects block out entirely.
        """
        for sr in db.findall("./style/style-rule"):
            if sr.get("element") != "table":
                continue
            for f in sr.findall("format"):
                if f.get("attr") == "background-color" and f.get("value"):
                    return f.get("value")
        # Fall back to descendant search — some workbook versions nest
        # the style-rule one level deeper.
        for sr in db.findall(".//style-rule[@element='table']"):
            for f in sr.findall("format"):
                if f.get("attr") == "background-color" and f.get("value"):
                    return f.get("value")
        return None

    def _collect_zones(self, db: ET.Element) -> List[Dict[str, Any]]:
        """Gather every leaf zone in the dashboard.

        Two specific bugs we need to dodge here:

        1. Each filter widget Tableau attaches to a worksheet shares the
           worksheet's *name* (e.g. four 'Region', 'Country', 'Type',
           'Project Type' filters all named "Actual Cost vs Budget").
           Deduping by name throws away the worksheet zone in favor of
           one of the filters. So we dedupe by Tableau's per-zone *id*,
           which is unique within a dashboard.

        2. Tableau emits the dashboard layout twice — desktop and a
           DSD/phone alternate inside a nested layout-basic. The same
           zone ids appear in both. Deduping by id collapses them and
           keeps the desktop ones (they're encountered first in DFS).
        """
        # Outer canvas size (in normalized 0..100000 space) for scaling.
        outer_w = outer_h = 0
        zroot = db.find("zones")
        if zroot is not None:
            first = zroot.find("zone")
            if first is not None:
                outer_w = safe_int(first.get("w", "0"))
                outer_h = safe_int(first.get("h", "0"))

        raw: List[Dict[str, Any]] = []
        self._recurse_zones(db, raw)

        out:      List[Dict[str, Any]] = []
        seen_ids: set = set()
        for z in raw:
            if z["type"] in LAYOUT_ZONE_TYPES:
                continue
            zid = z.get("id", "")
            if zid:
                if zid in seen_ids:
                    continue
                seen_ids.add(zid)

            zx, zy, zw, zh = z["x"], z["y"], z["w"], z["h"]
            if outer_w > 1000:
                zx = int(zx / outer_w * DEFAULT_PAGE_WIDTH)
                zw = int(zw / outer_w * DEFAULT_PAGE_WIDTH)
            if outer_h > 1000:
                zy = int(zy / outer_h * DEFAULT_PAGE_HEIGHT)
                zh = int(zh / outer_h * DEFAULT_PAGE_HEIGHT)
            out.append({
                **z,
                "x": zx, "y": zy,
                "w": max(zw, 40), "h": max(zh, 30),
            })
        return out

    @staticmethod
    def _extract_zone_text(z: ET.Element) -> str:
        """Pull plain text out of Tableau <formatted-text> / <run> markup.

        Tableau stores text in several forms:
            <run>plain text</run>
            <run><value>plain text</value></run>
            <run bold='true'><value>plain text</value></run>
            <run><value>part1</value></run><run><value>part2</value></run>
        We concatenate all run texts with spaces.
        """
        ft = z.find("formatted-text")
        if ft is None:
            # Some older TWB files nest text directly under the zone.
            txt = z.findtext("text")
            if txt:
                return TWBParser._clean_text(txt)
            return ""
        runs: List[str] = []
        for run in ft.iter("run"):
            # Child element text (e.g. <value> or <formatted-text> inside run)
            t = run.findtext("*")
            if t:
                cleaned = TWBParser._clean_text(t)
                if cleaned:
                    runs.append(cleaned)
            else:
                # Direct text on the <run> element itself
                t2 = TWBParser._clean_text(run.text or "")
                if t2:
                    runs.append(t2)
        # Fallback: grab any direct text inside formatted-text
        if not runs:
            raw = TWBParser._clean_text(ft.text or "")
            if raw:
                runs.append(raw)
        return " ".join(r for r in runs if r)

    @staticmethod
    def _run_to_style(run: ET.Element) -> Dict[str, Any]:
        """Translate a Tableau <run> element's font attribs to a style dict.

        Used by both worksheet titles and dashboard text zones — the
        attribute names are identical in both contexts.
        """
        s: Dict[str, Any] = {}
        if run.get("bold") == "true":
            s["fontWeight"] = "bold"
        if run.get("italic") == "true":
            s["italic"] = True
        if run.get("underline") == "true":
            s["underline"] = True
        if run.get("fontname"):
            s["fontFamily"] = run.get("fontname")
        fs = run.get("fontsize")
        if fs:
            try:
                s["fontSize"] = int(float(fs))
            except (ValueError, TypeError):
                pass
        if run.get("fontcolor"):
            s["fontColor"] = run.get("fontcolor")
        align = run.get("fontalignment")
        if align == "1":
            s["textAlign"] = "center"
        elif align == "2":
            s["textAlign"] = "right"
        return s

    @classmethod
    def _extract_zone_text_style(cls, z: ET.Element) -> Dict[str, Any]:
        """Return font formatting from the FIRST <run> inside the zone.

        Mirrors Tableau's rendering: a text zone with multiple runs uses
        each run's individual formatting in-line, but PBI textboxes apply
        a single style to the whole paragraph. Picking the first run's
        style is a reasonable approximation that preserves the dominant
        formatting in the common single-run case.
        """
        ft = z.find("formatted-text")
        if ft is None:
            return {}
        first = ft.find("run")
        if first is None:
            return {}
        return cls._run_to_style(first)

    @staticmethod
    def _extract_zone_style(z: ET.Element) -> Dict[str, Any]:
        """Pull container-level styling from <zone-style>/<format> children.

        Tableau emits per-zone format records like:
            <zone-style>
                <format attr='background-color' value='#FFFFFF'/>
                <format attr='border-color'     value='#000000'/>
                <format attr='border-style'     value='solid'/>
                <format attr='border-width'     value='1'/>
                <format attr='margin'           value='4'/>
            </zone-style>
        Returns the dict of recognised properties; empty when there's no
        zone-style block or no recognised attrs (border-style='none' is
        skipped because it would otherwise paint a transparent border).
        """
        zs = z.find("zone-style")
        if zs is None:
            return {}
        out: Dict[str, Any] = {}
        for f in zs.findall("format"):
            attr = f.get("attr", "")
            val  = f.get("value", "")
            if not val:
                continue
            if attr == "background-color":
                out["backgroundColor"] = val
            elif attr == "border-color":
                out["borderColor"] = val
            elif attr == "border-style" and val != "none":
                out["borderStyle"] = val
            elif attr == "border-width":
                try:
                    bw = int(float(val))
                except (ValueError, TypeError):
                    bw = 0
                if bw > 0:
                    out["borderWidth"] = bw
            elif attr == "margin":
                try:
                    out["padding"] = int(float(val))
                except (ValueError, TypeError):
                    pass
        return out

    def _recurse_zones(self, el: ET.Element, zones: List[Dict[str, Any]]) -> None:
        for z in el.findall("zone"):
            ztype = z.get("type-v2", z.get("type", ""))
            zone_dict: Dict[str, Any] = {
                "id":    z.get("id", ""),
                "name":  z.get("name", ""),
                "type":  ztype,
                "param": z.get("param", ""),
                "mode":  z.get("mode", ""),
                "x":     safe_int(z.get("x", "0")),
                "y":     safe_int(z.get("y", "0")),
                "w":     safe_int(z.get("w", "0")),
                "h":     safe_int(z.get("h", "0")),
            }

            # Container styling (background / border / margin) lives on
            # <zone-style> for every visible zone type. Stash it on
            # `containerStyle` so the report builder can apply it to
            # textbox visuals built for color/legend/bitmap/fallback
            # zones (those don't have their own text styling but a
            # <zone-style background-color> still needs to paint the
            # zone's tile).
            container_style = self._extract_zone_style(z)
            if container_style:
                zone_dict["containerStyle"] = container_style

            if ztype in ("text", "title"):
                zone_dict["text"] = self._extract_zone_text(z)
                # Font formatting from the first <run> + container style.
                # Merge the two so report.py only has to consult one dict.
                text_style = self._extract_zone_text_style(z)
                merged = {**container_style, **text_style}
                if merged:
                    zone_dict["textStyle"] = merged

            if ztype in ("filter", "parameter", "paramctrl"):
                # Filter/parameter widgets render with a title (the field
                # name) and a list of values. Carry container styling so
                # backgrounds and borders match the dashboard. The title
                # font is inherited from worksheet style-rules — looked
                # up later by the dashboard pass.
                if container_style:
                    zone_dict["titleStyle"] = container_style

            if ztype == "dashboard-object":
                caption = self._extract_button_caption(z)
                if caption:
                    zone_dict["caption"] = caption
                style = self._extract_button_style(z)
                # Merge zone-style atop button-visual-state styling so
                # surrounding container properties (margin, background)
                # come through too.
                if container_style:
                    style = {**container_style, **(style or {})}
                if style:
                    zone_dict["buttonStyle"] = style
                action = self._extract_button_action(z)
                if action:
                    zone_dict["buttonAction"] = action
            zones.append(zone_dict)
            self._recurse_zones(z, zones)
        zs = el.find("zones")
        if zs is not None:
            self._recurse_zones(zs, zones)

    @staticmethod
    def _extract_button_caption(z: ET.Element) -> str:
        """Pull the caption out of a dashboard button zone.

        Tableau button zones look like:
            <zone type-v2='dashboard-object'>
              <button ...>
                <button-visual-state>
                  <caption>Overview</caption>
                </button-visual-state>
              </button>
            </zone>
        """
        btn = z.find("button")
        if btn is None:
            return ""
        bvs = btn.find("button-visual-state")
        if bvs is None:
            return ""
        cap = bvs.find("caption")
        if cap is not None and cap.text:
            return cap.text.strip()
        return ""

    @staticmethod
    def _extract_button_style(z: ET.Element) -> Dict[str, Any]:
        """Pull visual styling out of a dashboard button zone.

        Returns a dict with keys: backgroundColor, fontColor, fontSize,
        fontWeight, borderColor, borderStyle, borderWidth (any that are
        found in the XML).  Empty dict if no button styling is present.
        """
        btn = z.find("button")
        if btn is None:
            return {}
        bvs = btn.find("button-visual-state")
        if bvs is None:
            return {}

        style: Dict[str, Any] = {}

        # Caption font style: fontcolor, fontname, fontsize
        cap_font = bvs.find("button-caption-font-style")
        if cap_font is not None:
            fc = cap_font.get("fontcolor")
            if fc:
                style["fontColor"] = fc
            fs = cap_font.get("fontsize")
            if fs:
                try:
                    style["fontSize"] = int(float(fs))
                except (ValueError, TypeError):
                    pass
            fn = cap_font.get("fontname", "")
            if "bold" in fn.lower():
                style["fontWeight"] = "bold"

        # Background / border from <format> elements inside button-visual-state
        for fmt in bvs.findall("format"):
            attr = fmt.get("attr", "")
            val = fmt.get("value", "")
            if attr == "background-color" and val:
                style["backgroundColor"] = val
            elif attr == "border-color" and val:
                style["borderColor"] = val
            elif attr == "border-style" and val:
                style["borderStyle"] = val
            elif attr == "border-width" and val:
                try:
                    style["borderWidth"] = int(float(val))
                except (ValueError, TypeError):
                    pass

        return style

    @staticmethod
    def _extract_button_action(z: ET.Element) -> str:
        """Detect the action type of a dashboard button zone.

        Tableau navigation buttons carry an action attribute like:
            action='tabdoc:goto-sheet window-id=&quot;{GUID}&quot;'
        which ElementTree decodes to the string:
            tabdoc:goto-sheet window-id="{GUID}"

        Returns 'goto-sheet' when the button navigates to another sheet/
        dashboard, or '' for buttons with no recognised navigation action.
        """
        btn = z.find("button")
        if btn is None:
            return ""
        action = btn.get("action", "")
        if "goto-sheet" in action:
            return "goto-sheet"
        return ""
