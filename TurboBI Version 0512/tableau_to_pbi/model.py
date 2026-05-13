"""Semantic model builder.

Turns parsed datasource dicts into a TMDL semantic model. The shape is:

    one PBI table per Tableau logical-table object (or one per
    datasource if there's no <object-graph>).

We never invent measures, never guess relationships from name overlap,
and never create date or parameter tables. If the user wants those,
they'll add them after opening the project."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import lineage_tag, safe_filename, tmdl_quote

# Lazy import: hyper.py is only needed when a HyperData object is passed in.
# We type-hint with a string to avoid a hard dependency at import time.
_HyperData = Any   # tableau_to_pbi.hyper.HyperData

# Lazy import: dax_translator is only needed when calculated fields exist.
_DAXTranslator = Any  # tableau_to_pbi.dax_translator.translate_tableau_to_dax


def _flatten_dax_expr(expr: str) -> str:
    """Collapse a measure expression to a single line.

    TMDL writes a measure as `\\tmeasure Name = <expr>` on one line. Any
    newline in <expr> bleeds into subsequent TMDL lines, which the parser
    then tries to interpret as property declarations and rejects with
    InvalidLineType. We normalise CR/LF/TAB to spaces and drop runs of
    whitespace so the emitted line is well-formed regardless of what the
    translator (or a fallback comment block) produced.
    """
    if not expr:
        return expr
    flat = expr.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    # Collapse runs of spaces — long Tableau formulas often have many
    # consecutive spaces around operators that don't matter in DAX.
    while "  " in flat:
        flat = flat.replace("  ", " ")
    return flat.strip()


def _geo_data_category(
    col_name: str, tmdl_type: str, semantic_role: str = "",
) -> Optional[str]:
    """Return 'Latitude' / 'Longitude' / None for a numeric column whose
    name OR Tableau semantic-role marks it as a geographic coordinate.

    Power BI's map visual silently ignores latitude/longitude bindings
    unless the underlying column carries the matching dataCategory. We
    only tag numeric columns (double / decimal / int / number) — a
    string column called 'latitude' is not a coordinate.

    Two signals, in priority order:
      1. Tableau's `<column semantic-role='[Geographical].[Latitude]'>`
         attribute — explicit user/system intent, survives renames.
      2. Name match (case-insensitive, non-letters stripped) — covers
         'Latitude', 'Latitude (generated)', 'lat', 'lng', etc. We
         deliberately do NOT match the 3-char 'lat' / 'lon' alone —
         too many unrelated columns ('Total', 'Latency') would collide.
    """
    if tmdl_type not in ("double", "decimal", "int64", "number"):
        return None

    # Authoritative — Tableau's own semantic-role attribute.
    sr = (semantic_role or "").lower()
    if "[geographical].[latitude]" in sr or sr.endswith(".[latitude]"):
        return "Latitude"
    if "[geographical].[longitude]" in sr or sr.endswith(".[longitude]"):
        return "Longitude"

    import re
    norm = re.sub(r"[^a-z]", "", (col_name or "").lower())
    # Match the full token ("latitude") OR a prefix followed by another
    # word ("latitudegenerated" from "Latitude (generated)"). Pure prefix
    # match would also catch "latency"; checking startswith("latitude")
    # specifically is safe because "latency" only shares 3 letters.
    if norm == "lat" or norm.startswith("latitude"):
        return "Latitude"
    if norm in ("lon", "long", "lng") or norm.startswith("longitude"):
        return "Longitude"
    return None


_DAX_FAILURE_PATTERNS = (
    ("LOD expression",      r"\{\s*(fixed|include|exclude)\b"),
    ("table calculation",   r"\b(window_(sum|avg|max|min)|running_(sum|avg)|"
                            r"previous_value|lookup|first|last|index|rank)\b"),
    ("ATTR aggregation",    r"\battr\s*\("),
    ("string function",     r"\b(left|right|mid|find|replace|regexp_\w+)\s*\("),
    ("date function",       r"\b(datediff|dateadd|datetrunc|datepart|datename)\s*\("),
    ("conditional pattern", r"\b(elseif|then|when)\b"),
    ("parameter ref",       r"\[parameters?\]\.\["),
)


def _classify_dax_failure(formula: str) -> str:
    """Best-guess label for why translate_tableau_to_dax returned None."""
    if not formula:
        return "empty formula"
    f = formula.lower()
    import re
    for label, pattern in _DAX_FAILURE_PATTERNS:
        if re.search(pattern, f, re.IGNORECASE):
            return label
    return "unsupported syntax"


class SemanticModel:
    """Semantic model can run in two modes:

        full mode  - one PBI table per Tableau logical-table object, with
                     every TWB column reflected. Used for TWBX inputs.
        stub mode  - a single empty placeholder table only. Used for TWB
                     inputs, where we deliberately ignore data-model
                     details and focus on emitting visuals with the right
                     layout (per user direction).
    """

    def __init__(
        self,
        datasources: List[Dict[str, Any]],
        parameters:  List[Dict[str, Any]] = None,
        stub_only:   bool = False,
        hyper_data_by_ds: Optional[Dict[str, _HyperData]] = None,
        # Legacy single-HyperData parameter, retained so old callers don't
        # break.  Treated as the hyper data for the FIRST datasource only.
        hyper_data:  Optional[_HyperData] = None,
        # Worksheets, optional. When provided, the model uses them to
        # detect Tableau data blending and synthesize cross-datasource
        # relationships. Empty/None preserves the legacy single-ds-per-
        # worksheet behaviour.
        worksheets:       Optional[List[Dict[str, Any]]] = None,
        credential_store: Optional[Any] = None,
     ):
        self.datasources = datasources
        self.parameters  = parameters or []
        self.stub_only   = stub_only
        self.worksheets  = worksheets or []
        # Credential store (tableau_to_pbi.credentials.CredentialStore or None).
        # Used in write_tmdl to apply connection overrides before partition-M
        # emission.  Typed as Any to avoid a hard import at module load time.
        self._credential_store: Optional[Any] = credential_store
        # Phase C diagnostics: blend warnings the user should see.
        self.blend_warnings: List[str] = []
        # Datasource-scoped hyper data: {ds_name -> HyperData}. Scoping by
        # datasource is what keeps a hyper extract from datasource A from
        # contaminating TMDL tables built from datasource B — Tableau's
        # own <extract> mapping is the authoritative ds->hyper binding,
        # captured by the parser.
        self.hyper_data_by_ds: Dict[str, _HyperData] = dict(hyper_data_by_ds or {})
        if hyper_data is not None and not self.hyper_data_by_ds and datasources:
            self.hyper_data_by_ds[datasources[0]["name"]] = hyper_data

        # (datasource_name, tableau_column_name) -> [(pbi_table, pbi_col), ...].
        # A list is used because a single datasource can contain multiple
        # logical tables with identically-named columns (e.g. HCP_ID in
        # both Dim_HCP and Dim_HCO).
        self.col_locator:    Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
        # (ds_name, tableau_key) -> (pbi_table, pbi_col), populated only
        # for entries that came from Tableau's own <cols><map> block.
        # That block is Tableau's explicit, authoritative column-to-table
        # binding (e.g. '[Patients_Diagnosed]' -> '[Dim_HCP].[...]').
        # When `resolve_field` finds an entry here for a no-suffix field,
        # it returns it directly — beating heuristics like the worksheet's
        # primary-table tiebreaker, which can vote for the wrong table
        # when most siblings carry an unrelated (Object!Suffix) hint.
        self._cols_map_lookup: Dict[Tuple[str, str], Tuple[str, str]] = {}
        # pbi_table -> {tableau_col -> pbi_col}, for diagnostics.
        self.table_columns:  Dict[str, Dict[str, str]] = {}
        # Per-datasource alias map.
        self._aliases: Dict[str, Dict[str, str]] = {}
        self.tables:         List[Dict[str, Any]] = []
        self.relationships:  List[Dict[str, str]] = []
        # Relationships present in the TWB <object-graph> but skipped
        # during build — usually because one of the join columns didn't
        # survive deduplication or the endpoints resolved to the same
        # table. Tracked so the mapping report can surface what would
        # otherwise be a silent drop.
        self.skipped_relationships: List[Dict[str, str]] = []
        self._group_aliases: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Populated after _match_hyper_tables():
        #   tmdl_table_name -> absolute Path of its CSV file
        self._csv_paths:       Dict[str, Path]       = {}
        #   tmdl_table_name -> [{name, type, tmdlType}] from hyper catalog
        self._hyper_col_meta:  Dict[str, List[Dict]] = {}
        #   (ds_name, hyper_key) -> tmdl_table_name (inverse index for write_tmdl)
        self._hyper_key_to_tmdl: Dict[Tuple[str, str], str] = {}


    def _register_group_aliases(self) -> None:
        """Register Tableau group metadata for filter expansion + fallback resolution.

        The parser materialises every group / categorical-bin column as a
        DAX calculated column on the same TMDL table as the source column,
        so the group field is already a first-class entry in `col_locator`.
        This pass:

          1. Stashes the parsed group metadata into `_group_aliases` so
             `group_info()` can answer "is this a group?" and return the
             {baseField, membersByGroup} dict that the report layer uses
             to expand group-label filters back to base-column members.
          2. As a backstop, registers an alias entry in `col_locator`
             ONLY when the group field has no calc-column entry yet —
             this covers workbooks where the bin XML was malformed and
             `_compile_categorical_bin` returned `BLANK()`.

        The parser writes the registry as `ds["groupAliases"]`. The legacy
        `ds["groups"]` key was never populated, which is why this pass had
        been a no-op for the entire branch.
        """
        for ds in self.datasources:
            ds_name = ds.get("name", "")
            groups = (
                ds.get("groupAliases")
                or ds.get("groups")
                or {}
            )

            if not ds_name or not groups:
                continue

            for group_field, info in groups.items():
                base_field = (
                    info.get("baseField")
                    or info.get("source")
                    or ""
                )
                if not base_field:
                    print(
                        f"[GROUP] '{group_field}' in ds='{ds_name}' has no "
                        f"baseField; group metadata not registered."
                    )
                    continue

                group_keys = {
                    group_field,
                    info.get("caption", ""),
                    self._strip_obj_suffix(group_field) or group_field,
                }

                # Always stash metadata so `group_info()` can answer
                # filter-expansion queries regardless of the calc-column
                # emission outcome.
                for key in group_keys:
                    if not key:
                        continue
                    self._group_aliases[(ds_name, key)] = {
                        **info,
                    }

                # Backstop: only inject a base-column alias for group
                # fields that have no real column entry yet. The parser's
                # DAX-calc-column path is the primary route — overwriting
                # `col_locator[(ds, group_field)]` here would mask the
                # synthesised calc column and resolve back to the raw
                # source values, defeating the whole point of the group.
                base_loc = self.resolve_field(ds_name, base_field)
                if not base_loc:
                    continue

                for key in group_keys:
                    if not key:
                        continue
                    existing = self.col_locator.get((ds_name, key)) or []
                    if existing:
                        continue
                    self.col_locator[(ds_name, key)] = [base_loc]
                    print(
                        f"[GROUP] Alias fallback (no calc column emitted): "
                        f"ds='{ds_name}' group='{group_field}' -> "
                        f"{base_loc[0]}.{base_loc[1]}"
                    )

    # ------------------------------------------------------------------
    # Trivial-alias calc-field resolution
    # ------------------------------------------------------------------

    # Compiled lazily on first use so module load stays cheap.
    _CALC_SIMPLE_REF_RE = None
    _CALC_TABLE_HINT_RE = None
    _CALC_NON_TABLE_SUFFIXES = {
        "group", "groups", "set", "sets", "bin", "bins",
        "parameter", "parameters", "calculation",
    }

    def _register_calc_alias_resolutions(self) -> None:
        """Register every trivial-alias calc field in ``col_locator``.

        Tableau auto-generates calc-field names like
        ``Calculation_4313392949811654658`` whose formula is just a
        comment plus a single column reference::

            //Region
            [REGION]

        These calcs are pure renames — there's no arithmetic, predicate,
        or function to translate. The DAX measure path emits an opaque
        measure under the calc's own name, but visuals reference the
        calc by its ``Calculation_<id>`` identifier, which had no entry
        in ``col_locator`` — the resolver then logged
        ``[RESOLVE] 'Calculation_xxx' not found`` and the visual lost
        the field.

        Previously the LLM-assisted agent
        (``tableau_to_pbi_agent/resolvers/formula_resolver.py``) covered
        these via a Pass-2 hint sidecar. Folding the same regex / table-
        hint / candidate-table logic into the deterministic core means
        the standalone converter resolves them on the first pass — no
        agent re-run needed.

        Recognised formula shapes:

            ``[X]``                                -> column X anywhere
            ``// caption\\n[X]``                   -> column X anywhere
            ``[X (TableHint)]``                    -> column X in TableHint
            ``// caption\\n[X (TableHint)]``       -> same

        Suffixes that are Tableau-internal markers (``group``, ``bin``,
        ``parameter``, ``calculation``) are NOT treated as table hints —
        ``[Region (group)]`` means "the Region group field", not "the
        Region column in table 'group'".
        """
        import re
        if SemanticModel._CALC_SIMPLE_REF_RE is None:
            SemanticModel._CALC_SIMPLE_REF_RE = re.compile(
                r"^\s*(?://[^\r\n]*[\r\n]+\s*)*"  # leading // comment lines
                r"\[(?P<inner>[^\[\]]+)\]"        # the [token]
                r"\s*$"
            )
            SemanticModel._CALC_TABLE_HINT_RE = re.compile(
                r"^(?P<col>.+?)\s*\((?P<tbl>[^()]+)\)\s*$"
            )

        n_resolved = 0
        for ds in self.datasources:
            ds_name = ds.get("name", "")
            if not ds_name or ds_name.lower() == "parameters":
                continue
            for col in ds.get("columns") or []:
                formula = col.get("formula") or ""
                if not formula:
                    continue
                calc_name = col.get("name") or ""
                if not calc_name:
                    continue

                m = SemanticModel._CALC_SIMPLE_REF_RE.match(formula)
                if not m:
                    continue

                inner = m.group("inner").strip()
                table_hint: Optional[str] = None
                col_token: str = inner
                h = SemanticModel._CALC_TABLE_HINT_RE.match(inner)
                if h:
                    col_token = h.group("col").strip()
                    suffix = h.group("tbl").strip()
                    if suffix.lower() not in SemanticModel._CALC_NON_TABLE_SUFFIXES:
                        table_hint = suffix

                # Resolve the inner ref through the same logic visuals use.
                # `prefer_table` is the table-name hint when the formula
                # carries `(SomeTable)` — substring match against TMDL
                # table names because Tableau's casing rarely matches.
                target_table: Optional[str] = None
                if table_hint:
                    hint_l = table_hint.lower()
                    for t in self.tables:
                        if t.get("datasource") != ds_name:
                            continue
                        tname = t["name"].lower()
                        if hint_l in tname or tname in hint_l:
                            target_table = t["name"]
                            break

                loc = self.resolve_field(
                    ds_name, col_token,
                    prefer_table=target_table,
                )
                if not loc:
                    continue

                caption = (col.get("caption") or "").strip()
                # Register the calc-field name AND its caption as aliases.
                # Use insert-at-front semantics matching the cols/map
                # promotion path, so the calc's existing binding (if any)
                # is preserved as a fallback.
                for key in {calc_name, caption}:
                    if not key:
                        continue
                    bucket = self.col_locator.setdefault((ds_name, key), [])
                    if loc in bucket:
                        bucket.remove(loc)
                    bucket.insert(0, loc)

                n_resolved += 1
                print(
                    f"[CALC-ALIAS] '{calc_name}' (ds='{ds_name}') -> "
                    f"{loc[0]}.{loc[1]}"
                )

        if n_resolved:
            print(f"[CALC-ALIAS] resolved {n_resolved} trivial-alias calc "
                  f"field(s) to their underlying columns.")

    def group_info(
        self,
        ds_name: str,
        field: str,
    ) -> Optional[Dict[str, Any]]:
        """Return group metadata for a Tableau group field, if available."""
        from .utils import clean_bracket

        clean = clean_bracket(field)
        stripped = self._strip_obj_suffix(clean) or clean

        return (
            self._group_aliases.get((ds_name, clean))
            or self._group_aliases.get((ds_name, stripped))
            or self._group_aliases.get((ds_name, field))
        )

# ------------------------------------------------------------------
    # Build pass
    # ------------------------------------------------------------------

    def _build_relationships_stub_mode(self) -> None:
        """Build relationships in stub mode using raw datasource relationship data.
        
        In stub mode, we don't have a full table structure but we still want to emit
        relationships to the pbip file. We use the table/column names directly from
        the parser's relationship data without trying to resolve them through col_locator.
        """
        seen_rels: set = set()
        
        for ds in self.datasources:
            for rel in ds.get("relationships", []):
                # Skip duplicates
                raw_key = (
                    ds["name"].lower(),
                    rel["fromColumn"].lower(),
                    rel["toColumn"].lower()
                )
                if raw_key in seen_rels:
                    continue
                seen_rels.add(raw_key)
                
                # Get table names from the relationship - use fromTable/toTable from parser data
                ft = rel.get("fromTable", "")
                fc = rel.get("fromColumn", "")
                tt = rel.get("toTable", "")
                tc = rel.get("toColumn", "")
                
                # Skip if missing required info
                if not ft or not fc or not tt or not tc:
                    continue
                
                # Skip self-joins
                if ft.lower() == tt.lower():
                    continue
                
                self.relationships.append({
                    "name":       lineage_tag("rel", ft, fc, tt, tc),
                    "fromTable":  ft,
                    "fromColumn": fc,
                    "toTable":    tt,
                    "toColumn":   tc,
                    "isActive":   True,
                })

    # ------------------------------------------------------------------
    # Build pass
    # ------------------------------------------------------------------

    def build(self) -> None:
        if self.stub_only:
            # Stub mode: one empty placeholder table for data, BUT still
            # emit measures from calculated fields and a Parameters table
            # because those are defined in the TWB XML itself.
            stub = self._stub_table("Data")
            self.tables.append(stub)
            self.table_columns[stub["name"]] = {}
            
            # In stub mode, still process relationships from the raw datasource data.
            # Even though we don't have a full table structure, the relationships
            # should be emitted to the pbip file so they appear in Power BI.
            self._build_relationships_stub_mode()
        else:
            self._build_full()

        # Parameters live in the TWB XML, so they are always built
        # regardless of stub/full mode. _build_parameters_table appends
        # one table per list-type parameter to self.tables directly, and
        # returns a single shared table for any non-list parameters.
        # Only append the shared table when it actually has columns —
        # workbooks that only use list parameters end up with an empty
        # 'other_params' bucket and shouldn't produce a stub table.
        # NOTE: parameter tables must exist BEFORE measures are translated
        # so that a calc field referencing a parameter resolves to the
        # right table and gets wrapped in SELECTEDVALUE.
        if self.parameters:
            self._build_parameters_table()

        # Measures are translated last — every table (data + parameter)
        # is now in self.tables and col_locator, so cross-table refs and
        # parameter SELECTEDVALUE wrapping all resolve correctly.
        if self.stub_only:
            self._build_measures_for_stub()
        else:
            self._build_all_measures()

        # After normal measure generation, ensure every visual/filter/value
        # field exists. This catches visual-referenced measures that were
        # skipped or could not be translated.
        self._ensure_visual_fields_in_model()

        if not self.tables:
            self.tables.append(self._stub_table("Stub"))
            self.table_columns["Stub"] = {}

        # Phase C — Tableau data-blending → Power BI relationships.
        # Runs LAST so col_locator and self.tables are fully populated.
        # No-ops in stub mode (no real ds tables to relate) and when
        # the worksheet list wasn't passed in.
        if self.worksheets and not self.stub_only:
            self._synthesize_blend_relationships()

    def _build_full(self) -> None:
        used_names: set = set()
        for ds in self.datasources:
            self._aliases[ds["name"]] = ds.get("columnAliases", {}) or {}
            self.tables.extend(self._tables_for_datasource(ds, used_names))

        # After all tables are built, register every entry from each
        # datasource's authoritative cols/map into col_locator. This
        # gives the resolver a deterministic top-priority lookup that
        # mirrors Tableau's own column->table binding, so a column
        # referenced as plain '[Region]' in a worksheet binds to the
        # exact logical table Tableau expected — never to a same-named
        # column in another logical table or another datasource.
        self._register_cols_map_entries()
        self._register_group_aliases()
        self._register_calc_alias_resolutions()
        # Ensure fields referenced only by visuals/filters/sorts/tooltips exist
        # before report.py tries to bind them.
        # self._ensure_visual_fields_in_model()

        # Measure translation is deferred to build() so that parameter
        # tables (added after _build_full) are visible — calc fields
        # that reference parameters need those entries in col_locator
        # before the DAX translator runs.

        # Relationships only when both endpoints survive column dedup —
        # the "honest model" rule: we never emit a relationship pointing
        # at a missing column.
        # Deduplicate relationships to avoid "ambiguous paths" error in Power BI
        # More robust deduplication: check both raw AND resolved relationships
        seen_rels: set = set()
        resolved_rels: set = set()  # Track resolved (table, col) pairs

        for ds in self.datasources:
            for rel in ds["relationships"]:
                # First, check raw relationship in source ds - skip if already seen
                raw_key = (
                    ds["name"].lower(),
                    rel["fromColumn"].lower(),
                    rel["toColumn"].lower()
                )
                if raw_key in seen_rels:
                    print(f"[REL] Skipping duplicate raw relationship: {ds['name']}.{rel['fromColumn']} -> {rel['toColumn']}")
                    continue
                seen_rels.add(raw_key)

                # Resolve column endpoints through col_locator, but
                # PREFER candidates whose TMDL table matches the table
                # the parser already extracted from the TWB rel operand
                # (e.g. 'HCO_Id' on the Fact side stays on the Fact side
                # instead of getting hijacked to whichever table was
                # registered first in col_locator).
                from_cands = self.col_locator.get((ds["name"], rel["fromColumn"])) or []
                to_cands   = self.col_locator.get((ds["name"], rel["toColumn"]))   or []

                def _pick(cands, want_table):
                    if not cands:
                        return None
                    if want_table:
                        for tbl, c in cands:
                            if tbl == want_table:
                                return (tbl, c)
                        # Loose match (case-insensitive) — Tableau
                        # captions sometimes drift from TMDL table names.
                        wl = want_table.lower()
                        for tbl, c in cands:
                            if tbl.lower() == wl:
                                return (tbl, c)
                    return cands[0]

                from_pick = _pick(from_cands, rel.get("fromTable", ""))
                to_pick   = _pick(to_cands,   rel.get("toTable", ""))
                if not from_pick or not to_pick:
                    self.skipped_relationships.append({
                        "datasource": ds["name"],
                        "fromTable":  rel.get("fromTable", ""),
                        "fromColumn": rel["fromColumn"],
                        "toTable":    rel.get("toTable", ""),
                        "toColumn":   rel["toColumn"],
                        "reason":     "endpoint column did not survive model build",
                    })
                    continue
                ft, fc = from_pick
                tt, tc = to_pick
                if ft == tt:
                    self.skipped_relationships.append({
                        "datasource": ds["name"],
                        "fromTable":  ft, "fromColumn": fc,
                        "toTable":    tt, "toColumn":   tc,
                        "reason":     "self-join (same table on both sides)",
                    })
                    continue

                # Check resolved relationship - skip exact duplicates
                res_key = (ft.lower(), fc.lower(), tt.lower(), tc.lower())
                if res_key in resolved_rels:
                    print(f"[REL] Skipping duplicate resolved relationship: {ft}.{fc} -> {tt}.{tc}")
                    continue
                resolved_rels.add(res_key)

                self.relationships.append({
                    "name":       lineage_tag("rel", ft, fc, tt, tc),
                    "fromTable":  ft, "fromColumn": fc,
                    "toTable":    tt, "toColumn":   tc,
                    "isActive":   True,
                })

        # Power BI allows only ONE active relationship between any pair of
        # tables. When a fact has multiple FKs to the same dim (e.g.
        # Fact_Referral_Edge.Referring_HCP_ID and .Receiving_HCP_ID both
        # pointing at Dim_HCP[HCP_ID]), the first wins; the rest must be
        # marked inactive so the model loads. Users can invoke them in DAX
        # via USERELATIONSHIP().
        active_pairs: set = set()
        for r in self.relationships:
            pair = (r["fromTable"].lower(), r["toTable"].lower())
            if pair in active_pairs:
                r["isActive"] = False
                print(f"[REL] Marking inactive (ambiguous path): "
                      f"{r['fromTable']}.{r['fromColumn']} -> "
                      f"{r['toTable']}.{r['toColumn']}")
            else:
                active_pairs.add(pair)

        if not self.tables:
            self.tables.append(self._stub_table("Stub"))
            self.table_columns["Stub"] = {}

        # Enrich column types from hyper catalog data when available.
        # Matching is scoped per-datasource so the column registry is never
        # cross-contaminated.
        if self.hyper_data_by_ds:
            self._match_hyper_tables()

    # ------------------------------------------------------------------
    # Authoritative cols/map registration
    # ------------------------------------------------------------------

    def _register_cols_map_entries(self) -> None:
        """Promote each datasource's <cols><map> entries into col_locator.

        The cols/map block carries Tableau's own column-to-table binding
        (e.g. '[Region]' -> '[Dim_HCP].[Region]'). Registering each entry
        as a TOP-PRIORITY candidate ensures resolve_field returns the
        exact (table, column) Tableau expected, even when:

            * The same Tableau column name appears in multiple logical
              tables — the un-suffixed form binds to its real owner
              instead of whichever metadata-record was seen first.
            * A column is referenced from a worksheet but has no
              <column> tag in the .twb XML — the cols/map still names
              its table.
            * Hyper enrichment renamed sourceColumn to a slightly
              different physical name — the map's column name takes
              precedence so visual queryRefs match the TMDL output.

        Each entry is INSERTED AT THE FRONT of the candidate list for
        its (ds_name, key) pair, so existing TMDL-derived entries are
        kept as fallbacks but the cols/map wins ties.
        """
        # Index TMDL tables by (ds_name, caption) so we can resolve a
        # logical-table caption from the cols/map back to its TMDL name
        # — important when name collisions across ds yield 'Dim_HCP' vs
        # 'Dim_HCP (2)'.
        tmdl_by_caption: Dict[Tuple[str, str], str] = {}
        for t in self.tables:
            ds = t.get("datasource", "")
            cap = t.get("caption", "")
            if ds and cap:
                tmdl_by_caption.setdefault((ds, cap), t["name"])

        # Also index columns by (tmdl_table, source_col_name) so we can
        # confirm the mapped physical column actually exists in the table.
        # Without this check we'd register phantom (table, column) pairs
        # for columns that were dropped during dedup.
        cols_in_table: Dict[Tuple[str, str], str] = {}
        for t in self.tables:
            for col in t.get("columns", []):
                cols_in_table[(t["name"], col["sourceCol"].lower())] = col["name"]

        for ds in self.datasources:
            ds_name  = ds["name"]
            cols_map = ds.get("colsMap", {}) or {}
            for tableau_key, (logical_tbl, physical_col) in cols_map.items():
                tmdl_name = tmdl_by_caption.get((ds_name, logical_tbl))
                if not tmdl_name:
                    continue
                pbi_col = cols_in_table.get((tmdl_name, physical_col.lower()))
                if not pbi_col:
                    # Mapped column not present in the TMDL table; could
                    # mean the column was dropped or renamed during build.
                    # Skip so we don't introduce a phantom resolution.
                    continue
                pair = (tmdl_name, pbi_col)
                # Insert at the front so the cols/map entry wins over any
                # earlier-registered TMDL candidate.
                bucket = self.col_locator.setdefault((ds_name, tableau_key), [])
                if pair in bucket:
                    bucket.remove(pair)
                bucket.insert(0, pair)
                # Record the cols/map answer as authoritative — used by
                # resolve_field to win over prefer_table heuristics for
                # no-suffix references.
                self._cols_map_lookup[(ds_name, tableau_key)] = pair

    # ------------------------------------------------------------------
    # Phase C — blend relationship synthesis
    # ------------------------------------------------------------------

    def _synthesize_blend_relationships(self) -> None:
        """Detect Tableau data blending and emit synthetic relationships.

        Tableau "data blending" links a primary datasource to one or more
        secondaries on shared field names (case-insensitive). When a
        worksheet binds a dimension from one ds and a measure from
        another, those two ds need a relationship in PBI's data model so
        the visual's query can join them. Tableau enforces "primary ->
        secondary" as a one-direction filter, which we mirror via
        `crossFilteringBehavior: oneDirection`.

        Inference order for each cross-ds worksheet:
          1. Walk its `datasourceDeps` list (parser captured).
          2. Treat the FIRST entry as the primary; rest are secondaries.
          3. For each secondary, find shared column NAMES with the
             primary (case-insensitive). Each shared name becomes a
             candidate blend key.
          4. Resolve each (ds, key) pair through col_locator. Only emit
             a relationship when both endpoints land on real TMDL
             columns and on different tables.
          5. Skip pairs we've already emitted (deterministic ordering).

        Because Tableau's TWB XML doesn't expose explicit blend keys
        in this corpus (only the boolean `<aliases enabled='yes'/>`
        flag), we fall back to "shared column names" exclusively. That
        choice is documented in CHANGES_ABCD.md.
        """
        if not self.worksheets or not self.tables:
            return

        # Build {ds_name: {col_lower: [pbi_table, pbi_col]}} from
        # col_locator so blend-key inference is fast. Filter out entries
        # whose target is a measure rather than a real column — a blend
        # relationship's join column must be a column, not a measure
        # (PBI rejects ``Total Distinct Patients`` as a relationship
        # endpoint). Build a {table: set(column_names)} index once and
        # use it to skip measure-typed candidates.
        cols_per_table: Dict[str, set] = {}
        for tbl in self.tables:
            cols_per_table[tbl["name"]] = {
                c["name"] for c in tbl.get("columns") or []
            }

        ds_cols: Dict[str, Dict[str, Tuple[str, str]]] = {}
        for (ds_name, tableau_col), cands in self.col_locator.items():
            if not cands or ds_name == "Parameters":
                continue
            tgt_tbl, tgt_col = cands[0]
            if tgt_col not in cols_per_table.get(tgt_tbl, set()):
                # Measure (or stale binding) — not eligible as a blend key.
                continue
            ds_cols.setdefault(ds_name, {})[tableau_col.lower()] = cands[0]

        emitted_keys: set = set()
        seen_pairs:   set = set()  # dedup (ds_a, ds_b, key) across worksheets
        n_relationships = 0
        n_warnings_before = len(self.blend_warnings)

        for ws in self.worksheets:
            deps = ws.get("datasourceDeps") or []
            # Need 2+ data ds (primary + secondary) for blending. Skip
            # single-ds worksheets — they use the legacy single-ds path.
            real_deps = [d for d in deps
                         if (d.get("datasource") or "").lower() != "parameters"]
            if len(real_deps) < 2:
                continue

            primary = real_deps[0]
            for secondary in real_deps[1:]:
                p_name = primary.get("datasource", "")
                s_name = secondary.get("datasource", "")
                if not p_name or not s_name or p_name == s_name:
                    continue
                p_cols = ds_cols.get(p_name) or {}
                s_cols = ds_cols.get(s_name) or {}
                if not p_cols or not s_cols:
                    continue
                # Shared bindings — Tableau auto-blend default. Cross-
                # reference column names declared in *both* ds (intersection
                # of `dep['columns']` lists, case-insensitive). The
                # `dep['columns']` list is the per-worksheet declared
                # columns, so we only consider keys actually used by
                # this worksheet — irrelevant shared names elsewhere
                # don't generate noise.
                p_decl = {c.lower() for c in primary.get("columns") or []}
                s_decl = {c.lower() for c in secondary.get("columns") or []}
                shared_names = p_decl & s_decl
                if not shared_names:
                    # Fall back to col_locator overlap so a worksheet that
                    # only declares one side's column can still blend on a
                    # shared key the other ds carries — Tableau's default
                    # auto-blend rule.
                    shared_names = set(p_cols.keys()) & set(s_cols.keys())
                    # Restrict to keys the worksheet actually mentioned on
                    # at least one side; arbitrary shared names across two
                    # large datasources would emit dozens of garbage rels.
                    if p_decl or s_decl:
                        shared_names &= (p_decl | s_decl)

                for key_lc in sorted(shared_names):
                    p_resolved = p_cols.get(key_lc)
                    s_resolved = s_cols.get(key_lc)
                    if not p_resolved or not s_resolved:
                        continue
                    p_tbl, p_col = p_resolved
                    s_tbl, s_col = s_resolved
                    if p_tbl == s_tbl:
                        continue
                    # Stable hash dedup across worksheets.
                    pair_key = (p_tbl.lower(), s_tbl.lower(), key_lc)
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)

                    # Cardinality + duplication check. PBI rejects
                    # many-to-one when the "to" side has duplicate keys;
                    # rejects one-to-one when both sides have duplicates.
                    # We don't have actual data here (model layer is
                    # schema-only) so we can't *prove* uniqueness — but
                    # we can detect when BOTH sides have the same column
                    # name appearing on multiple TMDL tables, which is a
                    # strong signal that a strict relationship would
                    # fail at load. In that case, log and skip.
                    p_dup = self._has_duplicate_column(p_name, key_lc)
                    s_dup = self._has_duplicate_column(s_name, key_lc)
                    if p_dup and s_dup:
                        msg = (
                            f"[BLEND-WARN] using TREATAS fallback for "
                            f"{p_name} <-> {s_name} on '{key_lc}' "
                            f"(both sides have duplicate keys; relationship skipped)"
                        )
                        if msg not in self.blend_warnings:
                            print(msg)
                            self.blend_warnings.append(msg)
                        continue

                    # Default many-to-many. Tableau blends do not enforce
                    # uniqueness or non-null on either side — the join is
                    # purely a left lookup on shared field values, with
                    # nulls / duplicates simply producing fewer matches.
                    # PBI's many-to-one rejects (a) duplicate keys on the
                    # "one" side AND (b) blank values on the "one" side.
                    # We can't statically prove either condition is safe
                    # (data isn't loaded at this stage), so we emit
                    # many-to-many to mirror Tableau's loose semantics
                    # and let the model load. Users can tighten cardinality
                    # in the Model view after verifying their data.
                    rel_name = lineage_tag(
                        "blend", p_tbl, s_tbl, key_lc,
                    )[:8]
                    rel_name = f"blend_{rel_name}"
                    if rel_name in emitted_keys:
                        continue
                    emitted_keys.add(rel_name)

                    from_card = "many"
                    to_card   = "many"

                    # Many-to-many in PBI requires bothDirections cross-
                    # filtering for the relationship to actually propagate
                    # filters; oneDirection on m-to-m makes the relationship
                    # effectively passive (USERELATIONSHIP-only). Tableau
                    # blend default is primary-filters-secondary, but PBI
                    # m-to-m offers no such asymmetry — bothDirections is
                    # the closest-fit choice. Cycles in the active graph
                    # are still prevented by _deactivate_ambiguous_paths.
                    self.relationships.append({
                        "name":        rel_name,
                        "fromTable":   s_tbl,
                        "fromColumn":  s_col,
                        "toTable":     p_tbl,
                        "toColumn":    p_col,
                        "isActive":    True,
                        "isBlend":     True,
                        "crossFiltering": "bothDirections",
                        "fromCardinality": from_card,
                        "toCardinality":   to_card,
                    })
                    n_relationships += 1
                    print(
                        f"[BLEND] {ws.get('name','?')}: synthesizing "
                        f"{s_tbl}.{s_col} -> {p_tbl}.{p_col} "
                        f"(key='{key_lc}', cardinality={from_card}-to-{to_card})"
                    )
        if n_relationships == 0:
            print(f"[BLEND] no cross-datasource relationships synthesized "
                  f"({len(self.worksheets)} worksheets scanned).")
        else:
            new_warns = len(self.blend_warnings) - n_warnings_before
            print(f"[BLEND] synthesized {n_relationships} blend "
                  f"relationship(s); {new_warns} warning(s).")

        self._deactivate_ambiguous_paths()

    def _deactivate_ambiguous_paths(self) -> None:
        """PBI requires exactly one ACTIVE relationship path between any two
        tables. The blend synthesis above can produce two kinds of conflict:

          (a) two parallel relationships on different keys between the same
              pair of tables (e.g. Extract (3) <-> Extract on both
              ``Date of Visit`` and ``Encounter ID``), and
          (b) a triangle of three tables where the third edge closes the
              cycle (e.g. ``Extract (3) -> Extract -> Extract (10)`` plus a
              direct ``Extract (3) -> Extract (10)``).

        Both raise ``PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH`` on load. Fix:
        keep the first relationship as active, mark the rest as
        ``isActive=false`` so the user can still pick them via
        ``USERELATIONSHIP`` in DAX. We process relationships in the order
        they were added — earlier ones (lower-numbered worksheets) win.

        Conflict (a) is detected by an unordered (table_a, table_b) seen-set.
        Conflict (b) is detected by a union-find pass: if the two endpoints
        are already in the same component via active edges, this edge would
        close a cycle and is deactivated.
        """
        if not self.relationships:
            return

        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent.get(x, x), parent.get(x, x))
                x = parent[x]
            parent.setdefault(x, x)
            return x

        def union(a: str, b: str) -> bool:
            ra, rb = find(a), find(b)
            if ra == rb:
                return False
            parent[ra] = rb
            return True

        seen_pair: set = set()
        n_deactivated = 0
        for r in self.relationships:
            if not r.get("isActive", True):
                continue
            a, b = r["fromTable"], r["toTable"]
            pair = tuple(sorted((a, b)))
            if pair in seen_pair:
                r["isActive"] = False
                n_deactivated += 1
                if r.get("isBlend"):
                    print(f"[BLEND] deactivated parallel relationship "
                          f"{r['name']} on {pair} ({r['fromColumn']} <-> "
                          f"{r['toColumn']}); only one active rel allowed "
                          f"per table-pair")
                continue
            if not union(a, b):
                r["isActive"] = False
                n_deactivated += 1
                if r.get("isBlend"):
                    print(f"[BLEND] deactivated cycle-closing relationship "
                          f"{r['name']} ({a} <-> {b}); a path already "
                          f"connects them via other active relationships")
                continue
            seen_pair.add(pair)

        if n_deactivated:
            print(f"[BLEND] deactivated {n_deactivated} relationship(s) to "
                  f"avoid ambiguous paths; {sum(1 for r in self.relationships if r.get('isActive', True))} "
                  f"active relationship(s) remain. Inactive ones are kept in "
                  f"the model and can be invoked via USERELATIONSHIP in DAX.")

    def _has_duplicate_column(self, ds_name: str, key_lc: str) -> bool:
        """Return True when the same column name (case-insensitive) appears
        in multiple TMDL tables from `ds_name`. Heuristic for blend-key
        uniqueness: when a column shows up on two ds-owned tables,
        Tableau's blend "key uniqueness" assumption is unsafe so the
        synthesized relationship would likely fail at model load.
        """
        cands = self.col_locator.get((ds_name, key_lc)) or []
        if not cands:
            # Try case-insensitive lookup across keys the locator stores.
            for (ds, k), c in self.col_locator.items():
                if ds == ds_name and k.lower() == key_lc:
                    cands = c
                    break
        tables = {tbl for tbl, _ in cands}
        return len(tables) > 1

    # ------------------------------------------------------------------
    # Hyper enrichment
    # ------------------------------------------------------------------

    def _match_hyper_tables(self) -> None:
        """Match each hyper table to its best-fitting TMDL table and
        override column types with the more accurate Hyper catalog types.

        Matching is restricted to TMDL tables that belong to the SAME
        datasource as the hyper file — established by the parser via the
        <extract><connection class='hyper' dbname='...'> block in the .twb
        XML. Without that scope, a hyper extract from datasource A whose
        column names overlap with datasource B's tables can be misrouted,
        and the columns it adds end up registered against the wrong
        datasource in `col_locator`. That is the root cause of visuals
        binding to columns from the wrong datasource.
        """
        from .hyper import match_hyper_to_tmdl

        for ds_name, hyper_data in self.hyper_data_by_ds.items():
            if not hyper_data or not hyper_data.available:
                continue
            ds_tables = [t for t in self.tables if t.get("datasource") == ds_name]
            if not ds_tables:
                print(f"[HYPER] Datasource '{ds_name}' has no TMDL tables to "
                      f"enrich — hyper data ignored.")
                continue
            for key, meta_cols in hyper_data.metadata.items():
                hyper_col_names = [c["name"] for c in meta_cols]
                tmdl_name = match_hyper_to_tmdl(key, hyper_col_names, ds_tables)
                if not tmdl_name:
                    print(f"[HYPER] No TMDL match for hyper table '{key}' "
                          f"in datasource '{ds_name}' — skipped.")
                    continue
                self._hyper_key_to_tmdl[(ds_name, key)] = tmdl_name
                self._hyper_col_meta[tmdl_name]        = meta_cols
                self._apply_hyper_types(ds_name, tmdl_name, meta_cols)
                print(f"[HYPER] ds='{ds_name}'  '{key}'  ->  TMDL table '{tmdl_name}'")

    def _apply_hyper_types(
        self, ds_name: str, tmdl_name: str, hyper_cols: List[Dict],
    ) -> None:
        """Override TMDL column dataTypes and sourceColumn names using
        the Hyper catalog so that CSV-backed partitions bind correctly.
        Also ADD any hyper columns that are missing from the TMDL table.

        `ds_name` is the OWNING datasource of the hyper extract — used so
        any newly added column lands in `col_locator` under the right
        datasource only.  Even if a column name happens to also exist in
        another datasource, registering only under the owner keeps
        `resolve_field` from accidentally pointing visuals at the wrong
        TMDL table.
        """
        # Case-insensitive lookup: physical column name -> hyper metadata
        type_map = {c["name"].lower(): c["tmdlType"] for c in hyper_cols}
        name_map = {c["name"].lower(): c["name"] for c in hyper_cols}

        def _hyper_lookup(src: str, pbi_name: str) -> Tuple[Optional[str], Optional[str]]:
            """Try matching the column to hyper metadata by:
              1. raw sourceCol  (e.g. 'Region')
              2. stripped TWB form  (e.g. 'Region (Dim!HCO)' -> 'Region')
              3. the PBI name itself (already stripped)
            The CSV header that Pandas writes from the hyper extract is
            the *clean* hyper name. When the TWB-side column is suffixed
            (e.g. 'Region (Dim!HCO)'), step 1 misses but step 2/3 hit and
            the binding is preserved.
            """
            for k in (src, SemanticModel._strip_obj_suffix(src), pbi_name):
                if not k:
                    continue
                low = k.lower()
                if low in type_map:
                    return type_map[low], name_map[low]
            return None, None

        for tbl in self.tables:
            if tbl["name"] != tmdl_name:
                continue
            # Defensive guard: if for any reason the matched TMDL table is
            # not from this datasource, refuse to enrich.  The matcher in
            # _match_hyper_tables already filters by ds, so this should
            # never trigger in practice — leaving it here makes the
            # invariant explicit.
            if tbl.get("datasource") and tbl.get("datasource") != ds_name:
                print(f"[HYPER]   Refusing to enrich '{tmdl_name}' "
                      f"(belongs to ds '{tbl.get('datasource')}', "
                      f"not '{ds_name}').")
                return
            matched = updated = renamed = added = 0
            # Track which hyper columns we've seen in the TMDL table
            seen_hyper_cols: set = set()
            for col in tbl["columns"]:
                hyper_type, hyper_name = _hyper_lookup(col["sourceCol"], col["name"])
                if hyper_type:
                    matched += 1
                    seen_hyper_cols.add(hyper_name.lower())
                if hyper_type and hyper_type != col["tmdlType"]:
                    col["tmdlType"] = hyper_type
                    updated += 1
                # Align sourceColumn to the exact CSV header name so
                # Table.PromoteHeaders + ChangedTypes bind correctly.
                # This is the load-time fix for TWB-suffixed cols like
                # 'Region (Dim!HCO)' that map to a clean 'Region' header
                # in the hyper extract.
                if hyper_name and hyper_name != col["sourceCol"]:
                    col["sourceCol"] = hyper_name
                    renamed += 1
            # Add any hyper columns that are missing from the TMDL table
            for hc in hyper_cols:
                low = hc["name"].lower()
                if low in seen_hyper_cols:
                    continue
                # New column from hyper that wasn't in the TWB XML.
                # Add it to the TMDL so refresh hydrates the data, BUT only
                # register it in col_locator when no other table already
                # claims this column name FOR THIS datasource. The TWB XML
                # is authoritative for which logical table "owns" a column
                # — joining the data in the hyper extract doesn't grant
                # ownership. Registering against another datasource would
                # let a visual from that datasource accidentally bind to
                # this column.
                tbl["columns"].append({
                    "name":       hc["name"],
                    "tmdlType":   hc["tmdlType"],
                    "lineageTag": lineage_tag("col", ds_name, hc["name"]),
                    "sourceCol":  hc["name"],
                    "hidden":     False,
                    "format":     "",
                    "tableauRef": "(hyper-only)",
                })
                if ds_name and not self.col_locator.get((ds_name, hc["name"])):
                    # Column is not mapped from TWB XML — hyper is the only
                    # source, so register it so visuals can resolve it.
                    self.col_locator.setdefault((ds_name, hc["name"]), []).append(
                        (tmdl_name, hc["name"])
                    )
                added += 1
            print(f"[HYPER]   {matched} columns matched, {updated} type(s) updated, {renamed} renamed, {added} added")
            break

    # ------------------------------------------------------------------
    # Per-datasource table layout
    # ------------------------------------------------------------------

    def _tables_for_datasource(
        self, ds: Dict[str, Any], used_names: set,
    ) -> List[Dict[str, Any]]:
        # Group columns by their parent-table caption (resolved during parse).
        groups: Dict[str, List[Dict[str, Any]]] = {}
        unassigned: List[Dict[str, Any]] = []
        calc_fields: List[Dict[str, Any]] = []
        for col in ds["columns"]:
            if col["isCalc"]:
                calc_fields.append(col)
                continue
            pt = col.get("parentTable") or ""
            if pt:
                groups.setdefault(pt, []).append(col)
            else:
                unassigned.append(col)

        if not groups:
            base = ds["caption"] or ds["name"] or "Data"
            groups.setdefault(base, []).extend(unassigned)
        elif unassigned:
            # Park the unassigned columns under the first known group
            # rather than inventing a new "_Other" table.
            first = next(iter(groups.keys()))
            groups[first].extend(unassigned)

        out: List[Dict[str, Any]] = []
        for parent_caption, cols in groups.items():
            tname = self._claim_name(parent_caption, used_names)
            pbi_cols, fmap = self._build_columns(cols)
            self.table_columns[tname] = fmap
            for tableau_name, pbi_name in fmap.items():
                self.col_locator.setdefault((ds["name"], tableau_name), []).append(
                    (tname, pbi_name)
                )
            # Defer measure translation: stash the calc fields on the
            # table and let _build_all_measures process them once the
            # full col_locator (across all datasources and tables) is
            # ready. Translating now would resolve cross-table field
            # refs to whatever subset of col_locator happened to be
            # populated.
            table_calc_fields = [
                c for c in calc_fields
                if c.get("parentTable") == parent_caption
                or (not c.get("parentTable") and parent_caption == next(iter(groups.keys())))
            ]
            out.append({
                "name":       tname,
                "lineageTag": lineage_tag("table", ds["name"], parent_caption),
                "columns":    pbi_cols,
                "measures":   [],
                "datasource": ds["name"],
                "caption":    parent_caption,
                # Per-datasource connection metadata — drives partition-M
                # emission (Csv.Document for Hyper extracts, Sql.Database
                # for live SQL Server, etc.). Stored on every table so the
                # writer doesn't have to re-look up the datasource by name.
                "connection": ds.get("connection") or {},
                "customSql":  ds.get("customSql") or [],
                "_pending_calc_fields": table_calc_fields,
                "_pending_ds_ref":      ds,
            })
        return out

    def _build_measures_for_stub(self) -> None:
        """In stub mode all calculated fields from every datasource are
        attached to the single stub table ('Data') so they still appear
        in the model and can be referenced by visuals."""
        list_param_tables = {
            t["name"] for t in self.tables if t.get("paramRows") is not None
        }
        self._parameter_refs = set()
        for (loc_ds, loc_field), cands in self.col_locator.items():
            if loc_ds != "Parameters" or not cands:
                continue
            t_out, _ = cands[0]
            if t_out in list_param_tables:
                self._parameter_refs.add(loc_field)
        stub_table = self.tables[0]["name"] if self.tables else "Data"
        for ds in self.datasources:
            calc_fields = [c for c in ds.get("columns", []) if c.get("isCalc")]
            if not calc_fields:
                continue
            measures = self._build_measures(calc_fields, stub_table, ds)
            # Attach measures to the stub table dict
            if self.tables:
                self.tables[0].setdefault("measures", []).extend(measures)

    def _build_all_measures(self) -> None:
        """Second-pass measure build, called after col_locator is fully
        populated. For each table we pop its `_pending_calc_fields` /
        `_pending_ds_ref` markers, translate them, and write the result
        into the table's `measures` list."""
        # Compute parameter refs now — parameter tables have just been
        # added to self.tables, so list_param_tables is complete.
        list_param_tables = {
            t["name"] for t in self.tables if t.get("paramRows") is not None
        }
        self._parameter_refs = set()
        for (loc_ds, loc_field), cands in self.col_locator.items():
            if loc_ds != "Parameters" or not cands:
                continue
            t_out, _ = cands[0]
            if t_out in list_param_tables:
                self._parameter_refs.add(loc_field)

        # Pre-register every calc field's name forms in col_locator
        # BEFORE translating any DAX. Calc-A may reference Calc-B by
        # Tableau internal id (e.g. [Calculation_1234567...]) and unless
        # B's mapping is in col_locator first, A's translation leaks
        # the raw token through. We register at the FRONT so the calc
        # shadows any same-named column (matching Tableau's semantics).
        # Note: the final measure name is determined inside
        # _build_measures (collision rename), but the most common case
        # is no collision — we predict pbi_name = caption|name and rely
        # on _build_measures to overwrite the bucket head if it renames.
        for tbl in self.tables:
            calc_fields = tbl.get("_pending_calc_fields") or []
            ds_ref      = tbl.get("_pending_ds_ref")
            if not calc_fields or not ds_ref:
                continue
            for col in calc_fields:
                pbi_name = (col.get("caption") or col["name"]).strip()
                pair = (tbl["name"], pbi_name)
                for alt in {col["name"], col.get("caption", ""), pbi_name}:
                    if not alt:
                        continue
                    bucket = self.col_locator.setdefault(
                        (ds_ref["name"], alt), []
                    )
                    if pair in bucket:
                        bucket.remove(pair)
                    bucket.insert(0, pair)

        for tbl in self.tables:
            calc_fields = tbl.pop("_pending_calc_fields", None)
            ds_ref      = tbl.pop("_pending_ds_ref",      None)
            if not calc_fields or not ds_ref:
                continue
            tbl["measures"] = self._build_measures(calc_fields, tbl["name"], ds_ref)

        self._enforce_global_measure_uniqueness()

    def _enforce_global_measure_uniqueness(self) -> None:
        """Power BI requires globally-unique measure names across the entire
        model — a measure on table A and a measure on table B cannot share
        a name (case-insensitive). Tableau allows it because each datasource
        is its own namespace, so it's common for multi-datasource workbooks
        to reuse names like 'Total Visits' across several twb datasources;
        without this pass the resulting PBIP fails to load with
        ``PFE_TM_OBJECT_NAME_ALREADY_EXISTS``.

        For every name that appears on 2+ tables we keep it on one canonical
        table (lexicographic by table name for determinism) and rename the
        rest to ``<name> (<table>)``. DAX expressions referencing the
        renamed measures via ``'Table'[Name]`` are rewritten in place, and
        col_locator entries are updated so the report builder picks the
        post-rename names.
        """
        by_name_ci: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for tbl in self.tables:
            for m in tbl.get("measures") or []:
                by_name_ci[m["name"].lower()].append((tbl["name"], m))

        rename_map: dict[tuple[str, str], str] = {}
        for occurrences in by_name_ci.values():
            if len(occurrences) <= 1:
                continue
            keep_table = sorted(t for t, _ in occurrences)[0]
            for tname, m in occurrences:
                if tname == keep_table:
                    continue
                old = m["name"]
                new = f"{old} ({tname})"
                m["name"] = new
                rename_map[(tname, old)] = new
                print(f"[MEAS-DEDUP] Renamed measure '{old}' on table "
                      f"'{tname}' -> '{new}' (kept on '{keep_table}')")

        if not rename_map:
            return

        for tbl in self.tables:
            for m in tbl.get("measures") or []:
                expr = m.get("expression", "")
                if not expr:
                    continue
                for (rtbl, rold), rnew in rename_map.items():
                    expr = expr.replace(f"'{rtbl}'[{rold}]", f"'{rtbl}'[{rnew}]")
                m["expression"] = expr

        for key, bucket in self.col_locator.items():
            self.col_locator[key] = [
                (tbl, rename_map.get((tbl, name), name)) for tbl, name in bucket
            ]

    def _field_to_pbi_for_ds(
        self,
        ds_name: str,
        exclude_self: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Tuple[str, str]]:
        """Build a {tableau_field_ref: (pbi_table, pbi_col)} map for the
        given datasource, sourced from col_locator. The first candidate
        in each bucket is the canonical PBI binding (the same one the
        resolver returns). Used by the DAX translator so measures emit
        PBI column names — not raw Tableau refs that PBI would reject
        because of (Object!Suffix) decorations or stripped spaces.

        Tableau parameters live in a special 'Parameters' meta-datasource
        and can be referenced from calc fields in ANY datasource, so we
        always merge the Parameters bucket on top — same-name conflicts
        (rare; the actual ds wins) are resolved by setdefault.

        ``exclude_self`` lets the measure-translation caller drop its own
        ``(table, pbi_name)`` from every bucket so the DAX it emits can't
        self-reference. ``_build_all_measures`` pre-registers each calc
        field's caption at the front of its bucket (so calcs shadow
        same-named columns — Tableau semantics). Without exclusion, a
        measure named ``Response Date`` on ``HCP Info (2)`` whose
        formula references ``[Response Date]`` would resolve through
        ``field_to_pbi`` back to itself and emit
        ``'HCP Info (2)'[Response Date]``, producing a
        ``PFE_XL_CALCCOLUMN_CIRCULAR_DEPENDENCIES`` error in PBI at load.
        """
        def _pick(cands: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
            if not cands:
                return None
            if exclude_self is None:
                return cands[0]
            for c in cands:
                if c != exclude_self:
                    return c
            return None

        out: Dict[str, Tuple[str, str]] = {}
        for (loc_ds, loc_field), cands in self.col_locator.items():
            if loc_ds != ds_name:
                continue
            pick = _pick(cands)
            if pick is not None:
                out[loc_field] = pick
        for (loc_ds, loc_field), cands in self.col_locator.items():
            if loc_ds != "Parameters":
                continue
            pick = _pick(cands)
            if pick is not None:
                out.setdefault(loc_field, pick)
        return out

    def _build_measures(
        self,
        calc_fields: List[Dict[str, Any]],
        table_name: str,
        ds: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Translate Tableau calculated fields to DAX measures.

        Returns a list of measure dicts ready for TMDL serialization.
        If translation fails, the measure is emitted as a comment-only
        placeholder so the user knows it was skipped.

        Power BI enforces a single namespace per table — a measure and a
        column cannot share a name (case-insensitive). Tableau's data
        model is more permissive: a calc field can shadow a same-named
        column. When that happens, this method picks a non-colliding
        measure name (deterministic suffixed form) and updates the
        col_locator entries so worksheet field refs still resolve to
        the renamed measure.
        """
        from .dax_translator import translate_tableau_to_dax

        measures: List[Dict[str, Any]] = []
        aliases = ds.get("columnAliases", {})
        cols = ds.get("columns", [])
        # Per-measure: field_to_pbi is rebuilt below with an `exclude_self`
        # of the measure's own (table, pbi_name) so the DAX can't bind a
        # bare ``[Caption]`` ref back to the measure being defined. The
        # default version (no exclusion) is used for the boolean-dim
        # calculated-column path below, where self-reference isn't a risk.
        field_to_pbi = self._field_to_pbi_for_ds(ds["name"])
        # Calc-field rows whose translation produces a DAX measure go on
        # this set as (table, pbi_name). The translator uses it to detect
        # `SUM([SomeOtherCalc])` patterns where the inner ref is itself
        # a measure — wrapping a measure in SUM/AVG/etc. produces invalid
        # DAX ('SUM only accepts a column reference'), so the translator
        # drops the wrapper and emits the bare measure ref instead.
        measure_refs: set = set()
        for col in calc_fields:
            cap = (col.get("caption") or col.get("name") or "").strip()
            if cap:
                measure_refs.add((table_name, cap))

        # Lowercased name registry for the target table — covers both
        # existing columns and any measures already accepted in this
        # pass. Each new measure must claim a name not already in here.
        target_tbl = next((t for t in self.tables if t["name"] == table_name), None)
        used_lc: set = set()
        if target_tbl:
            for c in target_tbl.get("columns", []):
                used_lc.add(c["name"].lower())
            for m in target_tbl.get("measures", []):
                used_lc.add(m["name"].lower())

        for col in calc_fields:
            formula = col.get("formula", "")
            if not formula:
                continue

            # Use the Tableau caption (user-visible name) as the PBI measure
            # name. The internal col["name"] is often an auto-generated ID
            # like "Calculation_1234567890" while caption is "My Measure".
            pbi_name = (col.get("caption") or col["name"]).strip()
            original_pbi_name = pbi_name

            # Boolean dimension calcs (e.g. ``Date Range = [Date] >=
            # [Start] AND [Date] <= [End]``) must be calculated COLUMNS,
            # not measures. As measures the scalar-context wrapper would
            # MIN() the row's date column, which collapses to the
            # global min when the visual doesn't include the date on
            # an axis — the filter then either always passes or always
            # fails and breaks the chart. As a calculated column the
            # comparison runs row-by-row against each fact-table row,
            # which is what Tableau does. Cross-table refs (params)
            # stay MIN-wrapped because each parameter table has 1 row,
            # so MIN() correctly returns the parameter's current value.
            is_bool_dim = (
                col.get("role") == "dimension"
                and col.get("tmdlType") == "boolean"
            )
            if is_bool_dim:
                dax_expr = translate_tableau_to_dax(
                    formula, table_name, aliases, cols,
                    field_to_pbi=field_to_pbi,
                    parameter_refs=getattr(self, "_parameter_refs", set()),
                    measure_refs=measure_refs,
                )
                if dax_expr is not None:
                    # Strip MIN(...) wrapper around same-table column refs
                    # so the calc column gets row-context evaluation. The
                    # pattern MIN('SameTable'[Col]) -> 'SameTable'[Col].
                    same_tbl_pat = re.compile(
                        r"MIN\(\s*'" + re.escape(table_name) + r"'\[([^\]]+)\]\s*\)"
                    )
                    dax_col_expr = same_tbl_pat.sub(
                        f"'{table_name}'[\\1]", dax_expr,
                    )
                    if pbi_name.lower() in used_lc:
                        base = f"{pbi_name} (Column)"
                        candidate, idx = base, 1
                        while candidate.lower() in used_lc:
                            idx += 1
                            candidate = f"{base} {idx}"
                        pbi_name = candidate
                    used_lc.add(pbi_name.lower())
                    # Append to the table's columns list (not measures).
                    if target_tbl is not None:
                        target_tbl.setdefault("columns", []).append({
                            "name":          pbi_name,
                            "tmdlType":      "boolean",
                            "lineageTag":    lineage_tag("calccol", ds["name"], col["name"]),
                            "sourceCol":     pbi_name,
                            "daxColumnExpr": dax_col_expr,
                            "hidden":        col.get("hidden", False),
                            "format":        col.get("format", ""),
                            "role":          "dimension",
                        })
                        col_pair = (table_name, pbi_name)
                        for alt in {original_pbi_name, pbi_name, col["name"]}:
                            if not alt:
                                continue
                            bucket = self.col_locator.setdefault(
                                (ds["name"], alt), []
                            )
                            if col_pair in bucket:
                                bucket.remove(col_pair)
                            bucket.insert(0, col_pair)
                    print(f"[CALC-COL] Boolean dim '{pbi_name}' on table "
                          f"'{table_name}' emitted as calculated column "
                          f"(row-level evaluation for filter use)")
                    continue
                # Translation failed — fall through to the placeholder
                # path below so the user sees a [DAX-DROP] log line and
                # can fix it manually.

            # Resolve measure-vs-column collisions. If a column on the
            # target table already owns this name, append a deterministic
            # ' (Measure)' suffix; if that's also taken, suffix with an
            # incrementing index. This is generic — no per-workbook
            # hardcoding — and the column keeps its original name so
            # existing visual queryRefs against the column still bind.
            if pbi_name.lower() in used_lc:
                base = f"{pbi_name} (Measure)"
                candidate, idx = base, 1
                while candidate.lower() in used_lc:
                    idx += 1
                    candidate = f"{base} {idx}"
                print(f"[MEAS] Renaming measure '{pbi_name}' -> '{candidate}' "
                      f"on table '{table_name}' (column with same name exists)")
                pbi_name = candidate
            used_lc.add(pbi_name.lower())

            # Rebuild field_to_pbi excluding THIS measure's own (table,
            # pbi_name). Without the exclusion, a calc whose caption
            # matches a column on another table (e.g. caption='Response
            # Date' with formula '... [Response Date] ...') would resolve
            # the bare ref back to the measure itself, emitting
            # 'Table'[ResponseDate] = ...measure name... and tripping
            # PBI's circular-dependency check on load.
            measure_field_to_pbi = self._field_to_pbi_for_ds(
                ds["name"],
                exclude_self=(table_name, pbi_name),
            )

            dax_expr = translate_tableau_to_dax(
                formula, table_name, aliases, cols,
                field_to_pbi=measure_field_to_pbi,
                parameter_refs=getattr(self, "_parameter_refs", set()),
                measure_refs=measure_refs,
            )

            # Auto-generated Tableau calc fields with no user-set caption
            # carry internal IDs like ``Calculation_4313392949811654658``.
            # These are noise in PBI's field pane — they're meant to be
            # internal references between measures, not user-facing
            # picks. Hide them so the field pane shows only named
            # measures, while the DAX measure still exists for other
            # measures to reference.
            is_auto_generated = bool(re.match(
                r"^Calculation[_ ]\d+$", pbi_name, re.IGNORECASE,
            ))
            
            is_visual_referenced = False
            if self.worksheets:
                refs = {
                    (ds_ref, fld)
                    for ds_ref, fld in self._field_refs_from_worksheets()
                }
                is_visual_referenced = (
                    (ds["name"], col.get("name", "")) in refs
                    or (ds["name"], col.get("caption", "")) in refs
                    or (ds["name"], pbi_name) in refs
                )

            hidden = (col.get("hidden", False) or is_auto_generated) and not is_visual_referenced


            if dax_expr is not None:
                measures.append({
                    "name":       pbi_name,
                    "expression": _flatten_dax_expr(dax_expr),
                    "lineageTag": lineage_tag("measure", ds["name"], col["name"]),
                    "format":     col.get("format", ""),
                    "hidden":     hidden,
                })
            else:
                # Placeholder measure — visible in the field list so the
                # user can manually replace the BLANK() with proper DAX.
                # The original Tableau formula is preserved inline as a
                # /* */ comment for reference. Both the DAX and the
                # comment must collapse to a single line because TMDL
                # treats every line in the measure block as a property
                # declaration — a stray newline in the comment turns the
                # next bit of formula text (e.g. 'WHEN ... THEN ...')
                # into an InvalidLineType error at load time.
                safe_formula = formula.replace("*/", "* /")
                # Log the untranslatable formula with a hint about WHY
                # so users can prioritise which calcs to fix first. The
                # heuristic isn't perfect (we just look for known-hard
                # tokens) but pointing at LOD / table-calc / window-fn
                # is right >90% of the time.
                reason = _classify_dax_failure(formula)
                preview = (formula or "").replace("\n", " ").strip()[:80]
                print(f"[DAX-DROP] '{pbi_name}' on table '{table_name}' "
                      f"({reason}): {preview!r}")
                measures.append({
                    "name":       pbi_name,
                    "expression": _flatten_dax_expr(
                        f"BLANK()  /* TODO: translate Tableau formula -- {safe_formula} */"
                    ),
                    "lineageTag": lineage_tag("measure", ds["name"], col["name"]),
                    "format":     col.get("format", ""),
                    "hidden":     False,
                })

            # Register the calc's Tableau name forms → (table, pbi_name)
            # so worksheets that reference the field by either internal
            # ID or display caption resolve to the (possibly renamed)
            # measure. We INSERT at the front of each bucket because in
            # Tableau a calc shadows a same-named column — the measure
            # must win the resolver's tie-break, even when the column
            # was registered first.
            measure_pair = (table_name, pbi_name)
            for alt in {original_pbi_name, pbi_name, col["name"]}:
                if not alt:
                    continue
                bucket = self.col_locator.setdefault((ds["name"], alt), [])
                if measure_pair in bucket:
                    bucket.remove(measure_pair)
                bucket.insert(0, measure_pair)
        return measures
    def _datasource_by_name(self, ds_name: str) -> Optional[Dict[str, Any]]:
        """Return datasource dict by Tableau datasource name."""
        for ds in self.datasources:
            if ds.get("name") == ds_name:
                return ds
        return None

    def _tables_for_ds_name(self, ds_name: str) -> List[Dict[str, Any]]:
        """Return semantic model tables that belong to one Tableau datasource."""
        return [
            t for t in self.tables
            if t.get("datasource") == ds_name
            and t.get("paramRows") is None
        ]

    def _field_refs_from_worksheets(self) -> List[Tuple[str, str]]:
        """Collect every Tableau field referenced by visuals, filters, labels,
        tooltips, details, sort definitions, and Top-N filters.

        Returns:
            [(datasource_name, field_name), ...]
        """
        refs: List[Tuple[str, str]] = []
        seen: set = set()

        FIELD_KEYS = {"field", "measure", "dimension"}

        def _clean_ds(ws: Dict[str, Any], obj: Dict[str, Any]) -> str:
            ds = (obj.get("datasource") or "").strip()
            if ds and ds.lower() != "parameters":
                return ds
            return (ws.get("datasourceRef") or "").strip()

        def _walk(ws: Dict[str, Any], obj: Any) -> None:
            if isinstance(obj, dict):
                ref_ds_name = _clean_ds(ws, obj)

                for key in FIELD_KEYS:
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        field = val.strip()

                        # Tableau generated geo pseudo-fields are not physical columns.
                        if "(generated)" in field.lower():
                            continue

                        item = (ref_ds_name, field)
                        if ref_ds_name and item not in seen:
                            seen.add(item)
                            refs.append(item)

                for v in obj.values():
                    _walk(ws, v)

            elif isinstance(obj, list):
                for x in obj:
                    _walk(ws, x)

        for ws in self.worksheets or []:
            _walk(ws, ws)

        return refs

    def _find_existing_column_meta(
        self,
        ds: Dict[str, Any],
        field: str,
        physical_col: str,
    ) -> Optional[Dict[str, Any]]:
        """Find parser column metadata for an inferred visual field."""
        from .utils import clean_bracket

        clean = clean_bracket(field)
        stripped = self._strip_obj_suffix(clean) or clean

        for col in ds.get("columns") or []:
            names = {
                str(col.get("name", "")),
                str(col.get("caption", "")),
                str(col.get("sourceName", "")),
                str(col.get("rawName", "")),
            }

            normalized_names = set()
            for n in names:
                if not n:
                    continue
                normalized = self._strip_obj_suffix(n) or n
                normalized_names.add(normalized)

            if (
                clean in normalized_names
                or stripped in normalized_names
                or physical_col in normalized_names
            ):
                return col

        return None

    def _pick_table_for_inferred_field(
        self,
        ds_name: str,
        parent_table: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Pick the table where an inferred visual/filter column should live."""
        candidates = self._tables_for_ds_name(ds_name)
        if not candidates:
            return None

        if parent_table:
            parent_lc = parent_table.lower()
            for t in candidates:
                t_caption = (t.get("caption") or "").lower()
                t_name = (t.get("name") or "").lower()

                if (
                    t_caption == parent_lc
                    or t_name == parent_lc
                    or parent_lc in t_name
                    or t_name in parent_lc
                ):
                    return t

        return candidates[0]

    def _add_inferred_visual_column(
        self,
        ds_name: str,
        field: str,
    ) -> Optional[Tuple[str, str]]:
        """Add a physical column required by a visual/filter if missing.

        This is intentionally conservative:
          - Runs only after normal parser/model resolution fails.
          - Prefers Tableau colsMap for table + physical source column.
          - Defaults type to string when metadata is unavailable.
        """
        from .utils import clean_bracket

        ds = self._datasource_by_name(ds_name)
        if not ds:
            return None

        clean = clean_bracket(field)
        if not clean or "(generated)" in clean.lower():
            return None
        
        
        if self.group_info(ds_name, clean):
            return self.resolve_field(ds_name, clean)


        stripped = self._strip_obj_suffix(clean) or clean

        cols_map = ds.get("colsMap", {}) or {}
        mapped = (
            cols_map.get(clean)
            or cols_map.get(stripped)
            or cols_map.get(field)
        )

        if mapped:
            parent_table, physical_col = mapped
        else:
            parent_table, physical_col = "", stripped

        target_table = self._pick_table_for_inferred_field(
            ds_name,
            parent_table=parent_table,
        )
        if not target_table:
            return None

        # Already present on the picked table?
        for c in target_table.get("columns") or []:
            if (
                c.get("name") == stripped
                or c.get("sourceCol") == physical_col
                or c.get("tableauRef") == clean
            ):
                loc = (target_table["name"], c["name"])

                for key in {clean, stripped, physical_col, field}:
                    if key:
                        self.col_locator.setdefault((ds_name, key), []).insert(0, loc)

                if "(" not in clean and "!" not in clean:
                    self._cols_map_lookup[(ds_name, clean)] = loc

                return loc

        meta = self._find_existing_column_meta(ds, clean, physical_col)

        base_name = (
            (meta or {}).get("caption")
            or stripped
            or physical_col
        )
        base_name = self._strip_obj_suffix(base_name) or base_name

        used_ci = {
            c.get("name", "").lower()
            for c in target_table.get("columns") or []
            if c.get("name")
        }

        pbi_name = base_name
        idx = 1
        while pbi_name.lower() in used_ci:
            idx += 1
            pbi_name = f"{base_name}_{idx}"

        col_obj = {
            "name":       pbi_name,
            "tmdlType":   (meta or {}).get("tmdlType", "string"),
            "lineageTag": lineage_tag("col", ds_name, clean),
            "sourceCol":  physical_col,
            "daxColumnExpr": "",
            "hidden":     False,
            "format":     (meta or {}).get("format", ""),
            "role":       (meta or {}).get("role", "dimension"),
            "semanticRole": (meta or {}).get("semanticRole", ""),
            "tableauRef": clean,
            "inferredFromVisual": True,
        }

        target_table.setdefault("columns", []).append(col_obj)

        self.table_columns.setdefault(target_table["name"], {})[stripped] = pbi_name
        self.table_columns.setdefault(target_table["name"], {})[clean] = pbi_name
        self.table_columns.setdefault(target_table["name"], {})[physical_col] = pbi_name

        loc = (target_table["name"], pbi_name)

        for key in {clean, stripped, physical_col, field}:
            if key:
                self.col_locator.setdefault((ds_name, key), []).insert(0, loc)

        # Bare-name authoritative lookup for later visual/filter resolution.
        if "(" not in clean and "!" not in clean:
            self._cols_map_lookup[(ds_name, clean)] = loc

        print(
            f"[MODEL] Added inferred visual field: "
            f"ds='{ds_name}' field='{field}' -> "
            f"{target_table['name']}.{pbi_name} "
            f"(sourceColumn='{physical_col}')"
        )

        return loc

    def _ensure_visual_fields_in_model(self) -> None:
        """Ensure every worksheet visual/filter/value field exists in model.

        Measures are handled before physical columns so Tableau calculated
        fields used in Values / Filters / Fields are not incorrectly added as
        source columns.
        """
        if not self.worksheets:
            return

        added_measures = 0
        added_columns = 0

        for ds_name, field in self._field_refs_from_worksheets():
            if not ds_name or not field:
                continue

            if self.resolve_field(ds_name, field):
                continue

            measure_loc = self._add_placeholder_visual_measure(ds_name, field)
            if measure_loc:
                added_measures += 1
                continue

            col_loc = self._add_inferred_visual_column(ds_name, field)
            if col_loc:
                added_columns += 1

        if added_measures or added_columns:
            print(
                f"[MODEL] Added inferred visual/filter fields: "
                f"{added_measures} measure(s), {added_columns} column(s)."
            )


    def _tables_for_ds_name(self, ds_name: str) -> List[Dict[str, Any]]:
        return [
            t for t in self.tables
            if t.get("datasource") == ds_name
            and t.get("paramRows") is None
        ]

    def _field_refs_from_worksheets(self) -> List[Tuple[str, str]]:
        """Collect every Tableau field referenced by visuals, filters, labels,
        tooltips, details, sort definitions, and top-N filters.

        Returns:
            [(datasource_name, field_name), ...]
        """
        refs: List[Tuple[str, str]] = []
        seen: set = set()

        FIELD_KEYS = {"field", "measure", "dimension"}

        def _clean_ds(ws: Dict[str, Any], obj: Dict[str, Any]) -> str:
            ds = (obj.get("datasource") or "").strip()
            if ds and ds.lower() != "parameters":
                return ds
            return (ws.get("datasourceRef") or "").strip()

        def _walk(ws: Dict[str, Any], obj: Any) -> None:
            if isinstance(obj, dict):
                ds_name = _clean_ds(ws, obj)

                for key in FIELD_KEYS:
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        field = val.strip()

                        # Tableau generated geo pseudo-fields are not real data columns.
                        if "(generated)" in field.lower():
                            continue

                        item = (ds_name, field)
                        if ds_name and item not in seen:
                            seen.add(item)
                            refs.append(item)

                for v in obj.values():
                    _walk(ws, v)

            elif isinstance(obj, list):
                for x in obj:
                    _walk(ws, x)

        for ws in self.worksheets or []:
            _walk(ws, ws)

        return refs

    def _find_existing_column_meta(
        self,
        ds: Dict[str, Any],
        field: str,
        physical_col: str,
    ) -> Optional[Dict[str, Any]]:
        """Find parser column metadata for an inferred visual field."""
        from .utils import clean_bracket

        clean = clean_bracket(field)
        stripped = self._strip_obj_suffix(clean) or clean

        for col in ds.get("columns") or []:
            names = {
                str(col.get("name", "")),
                str(col.get("caption", "")),
                str(col.get("sourceName", "")),
                str(col.get("rawName", "")),
            }
            names = {self._strip_obj_suffix(n) or n for n in names if n}

            if (
                clean in names
                or stripped in names
                or physical_col in names
            ):
                return col

        return None

    def _pick_table_for_inferred_field(
        self,
        ds_name: str,
        parent_table: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Pick the table where an inferred visual/filter column should live."""
        candidates = self._tables_for_ds_name(ds_name)
        if not candidates:
            return None

        if parent_table:
            parent_lc = parent_table.lower()
            for t in candidates:
                if (
                    t.get("caption", "").lower() == parent_lc
                    or t.get("name", "").lower() == parent_lc
                    or parent_lc in t.get("name", "").lower()
                ):
                    return t

        return candidates[0]

    def _add_inferred_visual_column(
            self,
            ds_name: str,
            field: str,
        ) -> Optional[Tuple[str, str]]:
            """Add a physical column required by a visual/filter if missing.

            This is intentionally conservative:
            - It only runs after normal parser/model resolution fails.
            - It prefers Tableau colsMap for table + physical source column.
            - It defaults type to string when metadata is unavailable.
            """
            from .utils import clean_bracket

            ds = self._datasource_by_name(ds_name)
            if not ds:
                return None

            clean = clean_bracket(field)
            if not clean or "(generated)" in clean.lower():
                return None

            stripped = self._strip_obj_suffix(clean) or clean

            cols_map = ds.get("colsMap", {}) or {}
            mapped = (
                cols_map.get(clean)
                or cols_map.get(stripped)
                or cols_map.get(field)
            )

            if mapped:
                parent_table, physical_col = mapped
            else:
                parent_table, physical_col = "", stripped

            target_table = self._pick_table_for_inferred_field(
                ds_name,
                parent_table=parent_table,
            )
            if not target_table:
                return None



    def _build_columns(
        self, cols: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        result: List[Dict[str, Any]] = []
        fmap:   Dict[str, str] = {}
        # Power BI's column-name uniqueness inside a table is case-
        # insensitive, so 'HCO ID' and 'Hco Id' collide on load even
        # though they're distinct strings. We track the lowercase form
        # to catch that, but keep the user-visible name in its original
        # casing.
        seen_ci: set = set()
        for col in cols:
            base = (col.get("caption") or col["name"]).strip()
            base = self._strip_obj_suffix(base)
            name, idx = base, 1
            while name.lower() in seen_ci:
                idx += 1
                name = f"{base}_{idx}"
            seen_ci.add(name.lower())
            fmap[col["name"]] = name
            result.append({
                "name":       name,
                "tmdlType":   col["tmdlType"],
                "lineageTag": lineage_tag("col", col["datasource"], col["name"]),
                "sourceCol":  col.get("sourceName") or col["name"],
                # When set, the column is a DAX calculated column (e.g. a
                # Tableau categorical-bin) — the writer emits ``column 'X'
                # = <expr>`` and skips the ``sourceColumn`` property.
                # ``sourceCol`` is kept above so any code that looks it up
                # for diagnostics still works, but it's not written to TMDL.
                "daxColumnExpr": col.get("daxColumnExpr") or "",
                "hidden":     col["hidden"],
                "format":     col["format"],
                # 'measure' / 'dimension' from Tableau's <column role='...'>.
                # Carried through so the TMDL writer can emit
                # `summarizeBy: sum` for numeric measures (which gives the
                # column the Σ aggregator icon in PBI's field list and
                # makes plain Column references on a visual auto-Sum,
                # matching Tableau's measure-by-default behavior).
                "role":       col.get("role", "dimension"),
                # Tableau's authoritative geo signal — '[Geographical].[Latitude]'
                # / '[Longitude]'. Used by _geo_data_category alongside
                # the name heuristic so renamed coords still pick up the
                # right dataCategory.
                "semanticRole": col.get("semanticRole", ""),
                # tableauRef preserves the original TWB column reference
                # (e.g. 'Region (Dim!HCO)') so the mapping report can show
                # how the user-facing PBI column name was derived.
                "tableauRef": col["name"],
            })
        return result, fmap

    @staticmethod
    def _strip_obj_suffix(name: str) -> str:
        return re.sub(r"\s*\([^()]+!.+?\)\s*$", "", name).strip()

# Lazy-import regex for _pick_from_candidates so the module loads
    # quickly even when the model is not used.
    _HINT_RE = None

    @staticmethod
    def _claim_name(base: str, used: set) -> str:
        # Power BI table-name uniqueness within the model is case-
        # insensitive, so we compare lowercase but keep the original
        # casing in the returned name. `used` is expected to be a set
        # of lowercased names.
        n = (base or "Table").strip() or "Table"
        candidate, idx = n, 1
        while candidate.lower() in used:
            idx += 1
            candidate = f"{n} ({idx})"
        used.add(candidate.lower())
        return candidate

    def _build_parameters_table(self) -> None:
        """Build PBI tables for Tableau parameters.

        - **List parameters**: each becomes its own table with a single
          ``Value`` column populated with one row per ``<member>`` in
          the .twb XML. Standard PBI pattern for slicer-driven
          selection parameters; all values appear in the dropdown.
        - **Any / range parameters**: each becomes its own single-row,
          single-column table named after the parameter caption. This
          mirrors PBI's "What If" parameter pattern — a measure
          downstream reads the current value via ``SELECTEDVALUE``.
          Lumping disparate parameters (e.g. ``Top N`` int + ``Start
          Date``/``End Date`` datetime) into one shared table is wrong
          because (a) date and int columns end up sharing a row that
          must accept both types, (b) the date-hierarchy synthesis
          fires on the param table's date columns even though there's
          only one row, and (c) PBI's slicer / what-if UX expects one
          parameter per table.

        All parameter tables are appended to ``self.tables`` directly.
        Returns None (the legacy "shared parameters" return value is
        gone — every parameter is now its own table).
        """
        list_params  = [p for p in self.parameters if p.get("domainType") == "list" and p.get("listValues")]
        other_params = [p for p in self.parameters if p not in list_params]

        used_names: set = {t["name"].lower() for t in self.tables}

        for p in list_params:
            # The user-visible parameter label (caption). Falls back to
            # the internal name when no caption was set.
            display_name = (p.get("caption") or p["name"]).strip() or p["name"]
            tname = self._claim_name(display_name, used_names)

            param_rows: List[Dict[str, Any]] = []
            for lv in p["listValues"]:
                # listValues is now a list of {value, label} dicts (parser
                # change). Tolerate the older flat-string form too.
                if isinstance(lv, dict):
                    value = lv.get("value", "")
                    label = lv.get("label", value)
                else:
                    value = label = lv
                param_rows.append({"value": value, "label": label})

            cols = [
                {
                    "name":       "Value",
                    "tmdlType":   p["tmdlType"],
                    "lineageTag": lineage_tag("param-val", p["name"]),
                    "sourceCol":  "Value",
                    "hidden":     False,
                    "format":     "",
                },
                {
                    "name":       "Label",
                    "tmdlType":   "string",
                    "lineageTag": lineage_tag("param-lbl", p["name"]),
                    "sourceCol":  "Label",
                    "hidden":     False,
                    "format":     "",
                },
            ]

            self.tables.append({
                "name":       tname,
                "lineageTag": lineage_tag("table", "param", p["name"]),
                "columns":    cols,
                "datasource": "Parameters",
                "caption":    display_name,
                "paramRows":  param_rows,
                "paramListColumn": "Value",
            })

            # Make the parameter resolvable from worksheet field refs by
            # both internal name and display caption. Worksheets typically
            # reference parameters by internal name; visuals built from
            # display labels reference the caption.
            self.col_locator.setdefault(("Parameters", p["name"]), []).append(
                (tname, "Value")
            )
            if display_name != p["name"]:
                self.col_locator.setdefault(("Parameters", display_name), []).append(
                    (tname, "Value")
                )

        # One table per any/range parameter — single column, single row.
        for p in other_params:
            display_name = (p.get("caption") or p["name"]).strip() or p["name"]
            tname = self._claim_name(display_name, used_names)

            cv = p.get("currentValue")
            dv = p.get("defaultValue", "")
            param_value = cv if cv is not None else dv

            cols = [{
                "name":       "Value",
                "tmdlType":   p["tmdlType"],
                "lineageTag": lineage_tag("param-val", p["name"]),
                "sourceCol":  "Value",
                "hidden":     False,
                "format":     "",
            }]

            self.tables.append({
                "name":       tname,
                "lineageTag": lineage_tag("table", "param", p["name"]),
                "columns":    cols,
                "datasource": "Parameters",
                "caption":    display_name,
                # paramData carries one row per parameter value. We pack
                # it as a single-element list to reuse the same TMDL
                # writer path that handles the legacy lumped table.
                "paramData":  [{
                    "tmdlType":     p["tmdlType"],
                    "defaultValue": param_value,
                }],
                # Single-column scalar param — column name is the
                # canonical "Value" so downstream measures can reference
                # it consistently as ``SELECTEDVALUE('<paramName>'[Value])``.
                "paramSingleCol": "Value",
            })

            # Resolve worksheet field refs to the new table. Both the
            # internal Tableau name and the display caption point at the
            # same (table, "Value") binding so a calc that references
            # the parameter by either form lands correctly.
            self.col_locator.setdefault(("Parameters", p["name"]), []).append(
                (tname, "Value")
            )
            if display_name != p["name"]:
                self.col_locator.setdefault(("Parameters", display_name), []).append(
                    (tname, "Value")
                )

    @staticmethod
    def _stub_table(name: str) -> Dict[str, Any]:
        return {
            "name":       name,
            "lineageTag": lineage_tag("stub", name),
            "columns":    [{
                "name": "Placeholder", "tmdlType": "string",
                "lineageTag": lineage_tag("stub-col", name),
                "sourceCol":  "Placeholder",
                "hidden":     False, "format": "",
            }],
            "datasource": "",
            "caption":    name,
        }

    # ------------------------------------------------------------------
    # Field-reference resolver (used by the report builder)
    # ------------------------------------------------------------------

    @classmethod
    def _pick_from_candidates(
        cls,
        candidates:   List[Tuple[str, str]],
        hint:         str,
        prefer_table: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """When a column name exists in multiple tables, choose the right
        candidate. Resolution order:

            1. (Object!Suffix) disambiguator embedded in the field string
               (e.g. 'HCP_ID (Dim!HCP)') — strongest hint.
            2. prefer_table — the worksheet's primary table, derived from
               its unambiguous and suffix-hinted fields.
            3. Substring match: a candidate's table name appears in the
               hint string.
            4. Insertion order (first candidate). The TWB-XML-defined
               column is registered first, so this generally favors the
               canonical Tableau-defined home over hyper-derived fallbacks.
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # 1. Explicit (Object!Suffix) hint in the field reference.
        if cls._HINT_RE is None:
            cls._HINT_RE = re.compile(r"\(\s*([^) !]+)!\s*([^)]+)\s*\)")
        m = cls._HINT_RE.search(hint or "")
        if m:
            table_guess = m.group(2).strip().lower()
            for tbl, pbi_col in candidates:
                if table_guess in tbl.lower():
                    return (tbl, pbi_col)
        # 2. Worksheet's preferred table (computed from sibling fields).
        if prefer_table:
            for tbl, pbi_col in candidates:
                if tbl == prefer_table:
                    return (tbl, pbi_col)
        # 3. Substring match against the hint.
        hint_lower = (hint or "").lower()
        for tbl, pbi_col in candidates:
            if tbl.lower() in hint_lower:
                return (tbl, pbi_col)
        # 4. Insertion order — first candidate is the TWB-XML canonical home.
        return candidates[0]

    def _find_existing_measure_meta(
        self,
        ds: Dict[str, Any],
        field: str,
    ) -> Optional[Dict[str, Any]]:
        """Find a Tableau calculated field / measure metadata row by visual ref.

        This is used when a visual references a Tableau calc that did not
        survive normal measure generation. We only return calculated fields,
        not ordinary physical columns.
        """
        from .utils import clean_bracket

        clean = clean_bracket(field)
        stripped = self._strip_obj_suffix(clean) or clean

        for col in ds.get("columns") or []:
            is_calc_like = bool(col.get("isCalc") or col.get("formula"))
            if not is_calc_like:
                continue

            names = {
                str(col.get("name", "")),
                str(col.get("caption", "")),
                str(col.get("rawName", "")),
            }

            normalized_names = set()
            for n in names:
                if not n:
                    continue
                n_clean = clean_bracket(n)
                normalized_names.add(n_clean)
                normalized_names.add(self._strip_obj_suffix(n_clean) or n_clean)

            if clean in normalized_names or stripped in normalized_names:
                return col

        return None

    def _unique_measure_name(
        self,
        table_name: str,
        base_name: str,
    ) -> str:
        """Return a measure name that does not collide with columns/measures."""
        target_tbl = next(
            (t for t in self.tables if t.get("name") == table_name),
            None,
        )

        used_lc: set = set()
        if target_tbl:
            for c in target_tbl.get("columns", []) or []:
                if c.get("name"):
                    used_lc.add(c["name"].lower())
            for m in target_tbl.get("measures", []) or []:
                if m.get("name"):
                    used_lc.add(m["name"].lower())

        name = base_name.strip() or "Missing Measure"

        if name.lower() not in used_lc:
            return name

        candidate = f"{name} (Measure)"
        idx = 1
        while candidate.lower() in used_lc:
            idx += 1
            candidate = f"{name} (Measure) {idx}"

        return candidate

    def _register_measure_locator(
        self,
        ds_name: str,
        field_names: set,
        table_name: str,
        measure_name: str,
    ) -> None:
        """Register Tableau field-name variants to a PBI measure."""
        pair = (table_name, measure_name)

        for alt in field_names:
            if not alt:
                continue

            bucket = self.col_locator.setdefault((ds_name, alt), [])
            if pair in bucket:
                bucket.remove(pair)
            bucket.insert(0, pair)

    def _add_placeholder_visual_measure(
        self,
        ds_name: str,
        field: str,
    ) -> Optional[Tuple[str, str]]:
        """Add a placeholder measure for a visual-referenced Tableau calc.

        This prevents visuals from breaking when a Tableau calc exists in the
        workbook but was skipped or failed conversion.
        """
        from .utils import clean_bracket

        ds = self._datasource_by_name(ds_name)
        if not ds:
            return None

        clean = clean_bracket(field)
        if not clean:
            return None

        meta = self._find_existing_measure_meta(ds, clean)
        if not meta:
            return None

        parent_table = meta.get("parentTable") or ""
        target_table = self._pick_table_for_inferred_field(
            ds_name,
            parent_table=parent_table,
        )

        if not target_table:
            return None

        base_name = (
            meta.get("caption")
            or self._strip_obj_suffix(clean)
            or clean
            or meta.get("name")
            or "Missing Measure"
        )

        measure_name = self._unique_measure_name(
            target_table["name"],
            base_name,
        )

        formula = meta.get("formula") or ""
        safe_formula = formula.replace("*/", "* /").replace("\n", " ")

        # If the formula is missing, still create a visible placeholder.
        if safe_formula:
            expr = f"BLANK()  /* TODO: translate Tableau formula -- {safe_formula} */"
        else:
            expr = (
                "BLANK()  /* TODO: Tableau calculated field was referenced "
                "by a visual/filter but no formula was available in parsed XML. */"
            )

        measure_obj = {
            "name":       measure_name,
            "expression": _flatten_dax_expr(expr),
            "lineageTag": lineage_tag(
                "measure",
                ds_name,
                meta.get("name") or clean,
            ),
            "format":     meta.get("format", ""),
            "hidden":     False,
            "placeholderFromVisual": True,
        }

        target_table.setdefault("measures", []).append(measure_obj)

        field_names = {
            field,
            clean,
            self._strip_obj_suffix(clean) or clean,
            meta.get("name", ""),
            meta.get("caption", ""),
            measure_name,
        }

        self._register_measure_locator(
            ds_name,
            field_names,
            target_table["name"],
            measure_name,
        )

        loc = (target_table["name"], measure_name)

        print(
            f"[MODEL] Added placeholder measure for visual field: "
            f"ds='{ds_name}' field='{field}' -> "
            f"{target_table['name']}.{measure_name}"
        )

        return loc

    def resolve_field(
        self,
        ds_name:      str,
        field:        str,
        prefer_table: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """Look up a Tableau field reference and return the
        (pbi_table, pbi_col) it should map to. Three resolution attempts
        in order: direct, alias-translated (parser-built map), then
        suffix-stripped fallback.

        When `prefer_table` is provided (typically the worksheet's
        primary table), it is used as a tiebreaker for ambiguous
        columns that don't carry an explicit suffix hint.

        Authoritative cols/map answers (Tableau's explicit `[col]` ->
        `[table].[physical]` bindings) win over `prefer_table` when the
        field reference itself has no `(Object!Suffix)` disambiguator.
        That's the case where the user is relying on Tableau's default
        binding for the bare name — which is captured exactly by the
        cols/map and would otherwise be overridden by the worksheet's
        voted-on primary table.
        """
        from .utils import clean_bracket

        if not field:
            return None
        col = clean_bracket(field)

        # No (Object!Suffix) hint embedded in the field? Then a cols/map
        # entry for the bare name is authoritative — return it directly
        # before any heuristic kicks in.
        if "(" not in col and "!" not in col:
            authoritative = self._cols_map_lookup.get((ds_name, col))
            if authoritative:
                return authoritative

        cands = self.col_locator.get((ds_name, col))
        if cands:
            return self._pick_from_candidates(cands, field, prefer_table)

        # Parser built an aliases map ('HCO_ID (Dim!HCO)' -> 'HCO_ID').
        aliased = self._aliases.get(ds_name, {}).get(col)
        if aliased:
            cands = self.col_locator.get((ds_name, aliased))
            if cands:
                return self._pick_from_candidates(cands, field, prefer_table)

        # Last-ditch: strip the (Object!Suffix) tail manually.
        stripped = self._strip_obj_suffix(col)
        if stripped and stripped != col:
            cands = self.col_locator.get((ds_name, stripped))
            if cands:
                return self._pick_from_candidates(cands, field, prefer_table)
        return None

    def first_table(self) -> str:
        return self.tables[0]["name"] if self.tables else "Stub"

    def is_measure_ref(self, table_name: str, col_name: str) -> bool:
        """Return True when (table_name, col_name) is a DAX measure rather
        than a regular column. Used by the report builder to emit a Measure
        reference (which doesn't take an outer aggregation) instead of a
        Column reference wrapped in Aggregation.
        """
        for t in self.tables:
            if t.get("name") != table_name:
                continue
            for m in t.get("measures", []) or []:
                if m.get("name") == col_name:
                    return True
            return False
        return False

    def is_datetime_col(self, table_name: str, col_name: str) -> bool:
        """Return True when (table_name, col_name) is a dateTime column.
        Used by the report builder to redirect date-part aggregations
        (Tableau's yr:/qr:/mn:/dy:) to the synthesized Year/Quarter/
        Month/Day hierarchy columns the TMDL writer emits."""
        for t in self.tables:
            if t.get("name") != table_name:
                continue
            for c in t.get("columns") or []:
                if c.get("name") == col_name:
                    return c.get("tmdlType") == "dateTime"
            return False
        return False

    def has_column(self, table_name: str, col_name: str) -> bool:
        """Return True when `col_name` exists in `table_name` (column or
        measure). Used as a guard before redirecting a date-part agg to
        a synthesized hierarchy column — if the writer didn't produce one
        (e.g. the column isn't actually dateTime), fall back to the
        original binding."""
        for t in self.tables:
            if t.get("name") != table_name:
                continue
            for c in t.get("columns") or []:
                if c.get("name") == col_name:
                    return True
            for m in t.get("measures") or []:
                if m.get("name") == col_name:
                    return True
            return False
        return False

    # ------------------------------------------------------------------
    # TMDL serialization
    # ------------------------------------------------------------------

    def write_tmdl(self, sem_model_dir: Path) -> None:
        defdir = sem_model_dir / "definition"
        defdir.mkdir(parents=True, exist_ok=True)
        (defdir / "tables").mkdir(exist_ok=True)
        (defdir / "cultures").mkdir(exist_ok=True)

        # Write CSV data files and build tmdl_table_name -> csv_path index.
        # One CSV per (datasource, hyper_table) so files from two different
        # datasources cannot overwrite each other if they happen to share
        # a hyper table name.
        if self.hyper_data_by_ds:
            from .hyper import write_csv
            data_root = sem_model_dir / "data"
            for ds_name, hyper_data in self.hyper_data_by_ds.items():
                if not hyper_data or not hyper_data.available:
                    continue
                # Each datasource gets its own subdirectory under /data
                # so a hyper table named 'Extract' from ds A doesn't
                # clobber a same-named one from ds B.
                ds_data_dir = data_root / safe_filename(ds_name)
                csv_by_key  = write_csv(hyper_data, ds_data_dir)
                for (cur_ds, hyper_key), tmdl_name in self._hyper_key_to_tmdl.items():
                    if cur_ds != ds_name:
                        continue
                    if hyper_key in csv_by_key:
                        self._csv_paths[tmdl_name] = csv_by_key[hyper_key]

        # database.tmdl — compatibilityLevel 1600 matches Power BI Desktop
        # April 2024+ (the level the reference report we modeled used).
        (defdir / "database.tmdl").write_text(
            "database\n\tcompatibilityLevel: 1600\n\n",
            encoding="utf-8",
        )

        ref_lines = "\n".join(
            f"ref table {tmdl_quote(t['name'])}" for t in self.tables
        )
        (defdir / "model.tmdl").write_text(
            "model Model\n"
            "\tculture: en-US\n"
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
            "\tsourceQueryCulture: en-US\n"
            "\tdataAccessOptions\n"
            "\t\tlegacyRedirects\n"
            "\t\treturnErrorValuesAsNull\n"
            "\n"
            # PBI_TimeIntelligenceEnabled = 1 turns on PBI Desktop's
            # auto date hierarchy. Every dateTime column gets an
            # implicit Year > Quarter > Month > Day hierarchy that
            # visuals can drill into. This mirrors Tableau's default
            # behavior where a date field on a shelf shows Year /
            # Quarter / Month levels via the column-instance agg
            # prefixes (yr:/qr:/mn:/dy:).
            "annotation PBI_TimeIntelligenceEnabled = 1\n"
            "\n"
            "annotation PBI_ProTooling = [\"DevMode\"]\n"
            "\n"
            f"{ref_lines}\n\n"
            "ref cultureInfo en-US\n",
            encoding="utf-8",
        )

        # Emit relationships as many-to-many. Tableau's noodle-style
        # relationships do not enforce uniqueness on either side, so a
        # Tableau dimension can legitimately contain duplicate join-key
        # values. Power BI's default cardinality is many-to-one, which
        # FAILS to load whenever the "to" side has duplicates (error:
        # 'Column X contains a duplicate value Y and this is not allowed
        # for columns on the one side of a many-to-one relationship').
        # Setting both sides to many preserves the Tableau semantic and
        # lets the model load. Users can tighten cardinality manually
        # after verifying their dim tables are unique.
        rel_chunks: List[str] = []
        for r in self.relationships:
            chunk = f"relationship {r['name']}\n"
            if not r.get("isActive", True):
                chunk += "\tisActive: false\n"
            # Phase C blend relationships carry explicit cardinality and
            # crossFilteringBehavior overrides. Standard relationships
            # keep the legacy many-to-many shape (works around Tableau's
            # noodle joins not declaring uniqueness).
            if r.get("isBlend"):
                cf = r.get("crossFiltering", "oneDirection")
                fc = r.get("fromCardinality", "many")
                tc = r.get("toCardinality", "one")
                chunk += (
                    f"\tfromColumn: {tmdl_quote(r['fromTable'])}.{tmdl_quote(r['fromColumn'])}\n"
                    f"\ttoColumn: {tmdl_quote(r['toTable'])}.{tmdl_quote(r['toColumn'])}\n"
                    f"\tcrossFilteringBehavior: {cf}\n"
                    f"\tfromCardinality: {fc}\n"
                    f"\ttoCardinality: {tc}\n"
                )
            else:
                chunk += (
                    "\tfromCardinality: many\n"
                    "\ttoCardinality: many\n"
                    f"\tfromColumn: {tmdl_quote(r['fromTable'])}.{tmdl_quote(r['fromColumn'])}\n"
                    f"\ttoColumn: {tmdl_quote(r['toTable'])}.{tmdl_quote(r['toColumn'])}\n"
                )
            rel_chunks.append(chunk)
        (defdir / "relationships.tmdl").write_text(
            "\n".join(rel_chunks) + ("\n" if rel_chunks else ""),
            encoding="utf-8",
        )

        (defdir / "cultures" / "en-US.tmdl").write_text(
            "cultureInfo en-US\n", encoding="utf-8",
        )

        for t in self.tables:
            csv_path   = self._csv_paths.get(t["name"])
            hyper_cols = self._hyper_col_meta.get(t["name"])

            # Apply credential overrides before deciding whether this table
            # should use Hyper/CSV extract or live Databricks.
            t_effective = t
            effective_conn = t.get("connection") or {}

            if self._credential_store is not None and t.get("connection"):
                effective_conn = self._credential_store.apply_overrides(
                    t["connection"],
                    datasource=t.get("datasource", ""),
                    caption=t.get("caption", ""),
                )
                if effective_conn is not t["connection"]:
                    t_effective = dict(t)
                    t_effective["connection"] = effective_conn
                # Credentials may carry a custom SQL override (`query`
                # field). Surface it on the table dict so
                # `_render_partition_m` picks it up — overriding any
                # Tableau-embedded customSql.
                creds_sql = effective_conn.get("customSql")
                if creds_sql:
                    if t_effective is t:
                        t_effective = dict(t)
                    t_effective["customSql"] = creds_sql

            # Key fix:
            # If a Databricks live connection is configured, ignore Tableau
            # Hyper/CSV extract files even when the original Tableau workbook
            # was extract-based.
            if (
                self._is_databricks_live_connection(effective_conn)
                and self._prefer_live_over_extract(effective_conn)
            ):
                csv_path = None
                hyper_cols = None

            # Custom SQL override always wins. Two routes feed this:
            #   1. The credentials file's `query` field — `apply_to()`
            #      stamps `customSql` onto the connection dict AND sets
            #      `force_live_for_custom_sql=True`.
            #   2. A Tableau-embedded `<relation type='text'>` on a
            #      live-class inner connection (sqlserver / postgres /
            #      snowflake / databricks) where the workbook was
            #      extract-mode but the inner class is live.
            # In either case the partition emits Value.NativeQuery(...),
            # so the CSV/Hyper path must be cleared even when one was
            # bound from upstream. Only fires when the resolved
            # connection class is one we know how to emit native query
            # for; everything else falls through to the CSV path.
            ts_customsql = (t_effective.get("customSql")
                            or t.get("customSql") or [])
            live_classes = {
                "sqlserver", "postgres", "snowflake", "databricks",
                "azure-databricks", "azuredatabricks",
                "databricks-sql", "spark-sql", "spark",
            }
            cls_lc = (effective_conn.get("class") or "").strip().lower()
            if ts_customsql and cls_lc in live_classes and (
                effective_conn.get("force_live_for_custom_sql")
                or self._prefer_live_over_extract(effective_conn)
            ):
                if csv_path is not None or hyper_cols is not None:
                    print(
                        f"[CONN] '{t['name']}' has customSql + live class "
                        f"'{cls_lc}' — dropping extract path and emitting "
                        f"Value.NativeQuery instead of CSV bind."
                    )
                csv_path = None
                hyper_cols = None

            (defdir / "tables" / f"{safe_filename(t['name'])}.tmdl").write_text(
                self._render_table_tmdl(t_effective, csv_path, hyper_cols),
                encoding="utf-8",
            )

    @staticmethod
    def _render_table_tmdl(
        t:          Dict[str, Any],
        csv_path:   Optional[Path]       = None,
        hyper_cols: Optional[List[Dict]] = None,
    ) -> str:
        lines: List[str] = []
        lines.append(f"table {tmdl_quote(t['name'])}")
        lines.append(f"\tlineageTag: {t['lineageTag']}")
        lines.append("")
        for col in t["columns"]:
            # DAX calculated columns (e.g. Tableau categorical-bin) emit as
            # ``column 'X' = <expr>`` and skip the ``sourceColumn`` line —
            # the data is computed at refresh time from other columns in
            # the same table, not bound to a CSV/source header. Detection
            # via the ``daxColumnExpr`` field stashed in `_build_columns`.
            calc_expr = col.get("daxColumnExpr") or ""
            if calc_expr:
                lines.append(
                    f"\tcolumn {tmdl_quote(col['name'])} = "
                    f"{_flatten_dax_expr(calc_expr)}"
                )
            else:
                lines.append(f"\tcolumn {tmdl_quote(col['name'])}")
            lines.append(f"\t\tdataType: {col['tmdlType']}")
            lines.append(f"\t\tlineageTag: {col['lineageTag']}")
            # `summarizeBy: sum` for numeric measure columns gives them the
            # Σ aggregator marker in PBI's field list AND makes a plain
            # Column reference auto-aggregate when dropped on a visual
            # (matching Tableau's default-aggregation-for-measures rule).
            # Strings, dates, and dimension-roled columns stay 'none'.
            # Geographic coordinates (Latitude/Longitude) ALSO stay 'none' —
            # Tableau marks them as numeric measures, but PBI's map visual
            # rejects coordinate columns whose summarizeBy is sum (and a
            # summed lat/lon makes no sense numerically anyway).
            is_numeric = col.get("tmdlType") in ("int64", "decimal", "double")
            is_measure = col.get("role") == "measure"
            is_geo = _geo_data_category(
                col["name"], col["tmdlType"],
                col.get("semanticRole", ""),
            ) is not None
            if is_numeric and is_measure and not is_geo:
                lines.append("\t\tsummarizeBy: sum")
            else:
                lines.append("\t\tsummarizeBy: none")
            # ``sourceColumn`` is only valid on physical (data-bound) columns.
            # DAX calculated columns derive their values from the expression
            # on the ``column = <expr>`` line; emitting a ``sourceColumn``
            # alongside would point at a CSV header that doesn't exist.
            if not calc_expr:
                lines.append(f"\t\tsourceColumn: {col['sourceCol']}")
            if col["hidden"]:
                lines.append("\t\tisHidden")
            if col["tmdlType"] == "dateTime":
                lines.append("\t\tformatString: General Date")
            # Tag latitude/longitude columns so PBI Desktop's map visual
            # accepts them as geographic coordinates instead of treating
            # them as plain numeric measures (a numeric column without a
            # dataCategory won't bind to the Latitude/Longitude wells of
            # the map visual). Detection is conservative: numeric type
            # AND the column name matches 'latitude'/'longitude' after
            # stripping non-letters (so 'Latitude (generated)', 'lat',
            # 'LATITUDE', 'Latitude_' all match).
            geo_cat = _geo_data_category(
                col["name"], col["tmdlType"], col.get("semanticRole", ""),
            )
            if geo_cat:
                lines.append(f"\t\tdataCategory: {geo_cat}")
            lines.append("")
            lines.append("\t\tannotation SummarizationSetBy = Automatic")
            lines.append("")

        # Measures (translated from Tableau calculated fields)
        for meas in t.get("measures", []):
            lines.append(
                f"\tmeasure {tmdl_quote(meas['name'])} = "
                f"{_flatten_dax_expr(meas['expression'])}"
            )
            lines.append(f"\t\tlineageTag: {meas['lineageTag']}")
            if meas.get("hidden"):
                lines.append("\t\tisHidden")
            if meas.get("format"):
                lines.append(f"\t\tformatString: {meas['format']}")
            lines.append("")
            lines.append("\t\tannotation SummarizationSetBy = Automatic")
            lines.append("")

        # Date hierarchies. For each dateTime column, emit Year / Quarter /
        # Month / Day calculated columns plus a hierarchy block grouping
        # them. This is the user-visible counterpart to PBI Desktop's
        # Auto date/time feature — we declare the hierarchy explicitly in
        # TMDL so it appears in the data pane on first open, and so card /
        # chart visuals binding a date-part agg (yr/qr/mn/dy from Tableau)
        # can target the precomputed level column.
        #
        # Skip parameter tables: parameters carry a single scalar value
        # (or a small list-of-values) — synthesizing 8+ hierarchy columns
        # per dateTime parameter bloats the model and clutters the PBI
        # field pane with year/quarter/month splits of a single value
        # that have no analytical meaning. The partition emit further
        # down still runs so the parameter data is loaded normally.
        is_param_table = (
            t.get("paramRows") is not None or t.get("paramData") is not None
        )
        for col in t["columns"]:
            if is_param_table:
                break
            if col.get("tmdlType") != "dateTime":
                continue
            base_name = col["name"]
            base_tag  = col["lineageTag"]
            # DAX column references use bracket notation, not the TMDL
            # single-quote form. `'Date Added'` in DAX is parsed as a
            # *table* name; the column has to be `[Date Added]`. Embedded
            # `]` is doubled per the DAX spec — extremely rare in practice
            # but cheap to handle correctly.
            base_dax = "[" + base_name.replace("]", "]]") + "]"
            # Hidden Month-Number column powers the sortByColumn on
            # 'Month of <date>' so the Month names sort Jan..Dec instead
            # of alphabetically (Apr, Aug, Dec, ...).
            month_num_name = f"Month Number of {base_name}"
            # Truncated-date columns (dateTime type, value = first instant
            # of the period). These give Tableau's TRUNCATE-DATE
            # aggregations (`ty:`/`tqr:`/`tmn:`/`tmd:`) a binding target —
            # without them, a slicer/filter on `tmn:Date of Visit` (which
            # Tableau materialises as the 1st-of-month date) gets dropped
            # because PBI has no native truncate-date aggregation. The
            # resolver in report.py rewrites the binding to these columns.
            year_trunc_name    = f"Year-Trunc of {base_name}"
            quarter_trunc_name = f"Year-Quarter of {base_name}"
            month_trunc_name   = f"Year-Month of {base_name}"
            for trunc_name, expr, kind in (
                (year_trunc_name,    f"= DATE(YEAR({base_dax}), 1, 1)",                    "year"),
                (quarter_trunc_name, f"= DATE(YEAR({base_dax}), (QUARTER({base_dax})-1)*3+1, 1)", "qtr"),
                (month_trunc_name,   f"= DATE(YEAR({base_dax}), MONTH({base_dax}), 1)",    "mon"),
            ):
                lines.append(f"\tcolumn {tmdl_quote(trunc_name)} {expr}")
                lines.append("\t\tdataType: dateTime")
                lines.append(
                    f"\t\tlineageTag: "
                    f"{lineage_tag('datetrunc', base_tag, kind)}"
                )
                lines.append("\t\tsummarizeBy: none")
                lines.append("\t\tformatString: General Date")
                lines.append("")
                lines.append("\t\tannotation SummarizationSetBy = Automatic")
                lines.append("")

            level_specs = [
                ("Year",    f"Year of {base_name}",
                 "int64",   f"= YEAR({base_dax})",
                 None),
                ("Quarter", f"Quarter of {base_name}",
                 "string",
                 f'= "Qtr " & ROUNDUP(MONTH({base_dax}) / 3, 0)',
                 None),
                ("Month",   f"Month of {base_name}",
                 "string",
                 f'= FORMAT({base_dax}, "MMMM")',
                 month_num_name),
                ("Day",     f"Day of {base_name}",
                 "int64",   f"= DAY({base_dax})",
                 None),
            ]
            # Emit Month Number first so the Month column's sortByColumn
            # forward-reference resolves cleanly.
            lines.append(f"\tcolumn {tmdl_quote(month_num_name)} = MONTH({base_dax})")
            lines.append(f"\t\tdataType: int64")
            lines.append(f"\t\tlineageTag: {lineage_tag('datepart', base_tag, 'monthnum')}")
            lines.append("\t\tsummarizeBy: none")
            lines.append("\t\tisHidden")
            lines.append("")
            lines.append("\t\tannotation SummarizationSetBy = Automatic")
            lines.append("")

            for level_name, col_name, dtype, expr, sort_by in level_specs:
                lines.append(f"\tcolumn {tmdl_quote(col_name)} {expr}")
                lines.append(f"\t\tdataType: {dtype}")
                lines.append(
                    f"\t\tlineageTag: "
                    f"{lineage_tag('datepart', base_tag, level_name.lower())}"
                )
                lines.append("\t\tsummarizeBy: none")
                if sort_by:
                    lines.append(f"\t\tsortByColumn: {tmdl_quote(sort_by)}")
                lines.append("")
                lines.append("\t\tannotation SummarizationSetBy = Automatic")
                lines.append("")

            hier_name = f"{base_name} Hierarchy"
            hier_tag  = lineage_tag("hierarchy", base_tag)
            lines.append(f"\thierarchy {tmdl_quote(hier_name)}")
            lines.append(f"\t\tlineageTag: {hier_tag}")
            for level_name, col_name, _dtype, _expr, _sort_by in level_specs:
                lines.append("")
                lines.append(f"\t\tlevel {level_name}")
                lines.append(
                    f"\t\t\tlineageTag: "
                    f"{lineage_tag('hier-level', base_tag, level_name.lower())}"
                )
                lines.append(f"\t\t\tcolumn: {tmdl_quote(col_name)}")
            lines.append("")

        # M type keyword / identifier for each TMDL type
        # Used in Table.TransformColumnTypes pairs: {"col", Int64.Type}
        _m_type_kw = {
            "string":   "type text",
            "int64":    "Int64.Type",
            "double":   "type number",
            "boolean":  "type logical",
            "dateTime": "type datetime",
        }
        # Bare primitive M type keywords for use inside type table [col = ...]
        # Int64.Type is NOT valid there — the M parser only accepts primitive
        # type keywords (text, number, logical, etc.) in table-type definitions.
        _type_table_kw = {
            "string":   "text",
            "int64":    "number",
            "double":   "number",
            "boolean":  "logical",
            "dateTime": "datetime",
        }

        def _m_literal(value: Any, dtype: str) -> str:
            """Format a Python value as an M Power Query literal."""
            if dtype == "string":
                if value is None or value == "":
                    return '""'
                return '"' + str(value).replace('"', '""') + '"'
            if dtype == "int64":
                if value is None or value == "":
                    return "0"
                try:
                    return str(int(float(str(value))))
                except (ValueError, TypeError):
                    return "0"
            if dtype == "double":
                if value is None or value == "":
                    return "0.0"
                try:
                    return str(float(str(value)))
                except (ValueError, TypeError):
                    return "0.0"
            if dtype == "boolean":
                if value is None:
                    return "false"
                if isinstance(value, bool):
                    return "true" if value else "false"
                return "true" if str(value).lower() in ("true", "1", "yes") else "false"
            if dtype == "dateTime":
                # M-language datetime literal: `#datetime(yyyy, m, d, h, mi, s)`.
                # Emitting a quoted string here makes M see a `text` value
                # for a column typed `datetime`, which fails table load
                # with "Expression.Error: The type of the value does not
                # match the type of the column" (the user-visible error).
                if value is None or value == "":
                    return "null"
                s = str(value).strip()
                # Tableau wraps date literals in '#' markers ('#2024-01-01#').
                if s.startswith("#") and s.endswith("#"):
                    s = s[1:-1].strip()
                # Strip surrounding quotes if any.
                if (s.startswith('"') and s.endswith('"')) or (
                    s.startswith("'") and s.endswith("'")
                ):
                    s = s[1:-1].strip()
                # Match either a date (YYYY-MM-DD) or full datetime
                # (YYYY-MM-DD[ T]HH:MM[:SS]). Anything else falls back
                # to null so the row loads instead of erroring.
                m = re.match(
                    r"^(\d{4})-(\d{1,2})-(\d{1,2})"
                    r"(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$",
                    s,
                )
                if not m:
                    return "null"
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                h  = int(m.group(4) or 0)
                mi = int(m.group(5) or 0)
                se = int(m.group(6) or 0)
                return f"#datetime({y}, {mo}, {d}, {h}, {mi}, {se})"
            # fallback: text
            if value is None or value == "":
                return '""'
            return '"' + str(value).replace('"', '""') + '"'

        if csv_path is not None:
            # ── CSV-backed partition: loads real data from the hyper extract ──
            # Use the hyper catalog column list when available so that names
            # and types exactly match the CSV headers written by pandas.
            # Fall back to TMDL column list when hyper_cols is absent.
            if hyper_cols:
                type_pairs_src = [
                    (c["name"], _m_type_kw.get(c["tmdlType"], "type text"))
                    for c in hyper_cols
                ]
            else:
                seen_ci: set = set()
                type_pairs_src = []
                for col in t["columns"]:
                    # DAX calculated columns are model-only; they do not
                    # exist in the physical source/query result shape.
                    # Including them in the partition schema creates a
                    # duplicate-name conflict when the model also defines
                    # the same calculated column.
                    if col.get("daxColumnExpr"):
                        continue
                    src = col["sourceCol"]
                    if not src or src.lower() in seen_ci:
                        continue
                    seen_ci.add(src.lower())
                    type_pairs_src.append(
                        (src, _m_type_kw.get(col["tmdlType"], "type text"))
                    )

            # Escape column names for M string literals ("" for a literal ")
            def _m_str(s: str) -> str:
                return '"' + s.replace('"', '""') + '"'

            pair_strs = [f"{{{_m_str(n)}, {tp}}}" for n, tp in type_pairs_src]
            type_list = ", ".join(pair_strs)

            # M string literals: backslash is NOT an escape character,
            # so just write the path as-is; only " needs to be doubled.
            # Resolve to an absolute path so PBI Desktop can locate the
            # CSV regardless of where the workbook was opened from. A
            # relative path only works when PBI's working directory is
            # the PBIP's parent — moving the folder breaks the load.
            csv_str = str(Path(csv_path).resolve()).replace('"', '""')

            # QuoteStyle.Csv (not .None) is mandatory: many real-world
            # workbooks have description fields with embedded newlines
            # inside quoted text. With QuoteStyle.None, PBI ignores
            # quotes and treats those newlines as row separators —
            # cascading into "couldn't convert to Number" errors when
            # description fragments leak into typed columns. .Csv
            # honours quote-escaped newlines, which matches how pandas
            # writes the CSV.
            m_expr = (
                "\t\t\tlet\n"
                f'\t\t\t    Source = Csv.Document(File.Contents("{csv_str}"), '
                '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
                "\t\t\t    PromotedHeaders = Table.PromoteHeaders(Source, "
                "[PromoteAllScalars=true]),\n"
                f"\t\t\t    ChangedTypes = Table.TransformColumnTypes("
                f"PromotedHeaders, {{{type_list}}})\n"
                "\t\t\tin\n"
                "\t\t\t    ChangedTypes\n"
            )
        elif t.get("paramRows") is not None:
            # ── List-parameter table: one row per <member> from .twb XML ───
            # Two columns: Value (typed) and Label (string). Each row is
            # one Tableau list-parameter value.
            value_dtype = next(
                (c["tmdlType"] for c in t["columns"] if c["name"] == "Value"),
                "string",
            )
            value_mt = _type_table_kw.get(value_dtype, "text")
            schema   = f'#"Value" = {value_mt}, #"Label" = text'
            row_strs: List[str] = []
            for row in t["paramRows"]:
                v_lit = _m_literal(row.get("value"), value_dtype)
                l_lit = _m_literal(row.get("label", ""), "string")
                row_strs.append("{" + v_lit + ", " + l_lit + "}")
            if row_strs:
                all_rows = "{" + ", ".join(row_strs) + "}"
                m_expr = (
                    "\t\t\tlet\n"
                    "\t\t\t    Source = Table.FromRows(" + all_rows + ", "
                    "type table [" + schema + "])\n"
                    "\t\t\tin\n"
                    "\t\t\t    Source\n"
                )
            else:
                m_expr = (
                    "\t\t\tlet\n"
                    f"\t\t\t    Source = Table.FromRows({{}}, "
                    f"type table [{schema}])\n"
                    "\t\t\tin\n"
                    "\t\t\t    Source\n"
                )
        elif t.get("paramData"):
            # ── Parameters table: one row with current/default values ────────
            # Iterate columns and paramData together (same order, built in
            # _build_parameters_table) so schema and row stay in sync.
            m_pairs: List[str] = []
            row_values: List[str] = []
            seen_ci_2: set = set()
            for col, pd in zip(t["columns"], t["paramData"]):
                src = col["sourceCol"]
                if not src or src.lower() in seen_ci_2:
                    continue
                seen_ci_2.add(src.lower())
                mt = _type_table_kw.get(col["tmdlType"], "text")
                esc = src.replace('"', '""')
                m_pairs.append(f'#"{esc}" = {mt}')
                dv = pd.get("defaultValue")
                row_values.append(_m_literal(dv if dv is not None else "", col["tmdlType"]))

            if m_pairs and row_values:
                schema   = ", ".join(m_pairs)
                inner    = "{" + ", ".join(row_values) + "}"
                all_rows = "{" + inner + "}"
                m_expr = (
                    "\t\t\tlet\n"
                    "\t\t\t    Source = Table.FromRows(" + all_rows + ", "
                    "type table [" + schema + "])\n"
                    "\t\t\tin\n"
                    "\t\t\t    Source\n"
                )
            else:
                m_expr = (
                    "\t\t\tlet\n"
                    "\t\t\t    Source = Table.FromRows({})\n"
                    "\t\t\tin\n"
                    "\t\t\t    Source\n"
                )
        else:
            # ── Empty placeholder partition (original behaviour) ──────────────
            # Branches on the datasource's connection class:
            #   sqlserver  -> Sql.Database(server, db) navigated by Schema/Item
            #   postgres   -> PostgreSQL.Database("server:port", db)
            #   snowflake  -> Snowflake.Databases(server, warehouse) navigation
            #   <other live class> -> Table.FromRows placeholder + TODO comment
            #   federated/empty/extract source -> Table.FromRows placeholder
            partition_mode = "import"
            m_expr = SemanticModel._render_partition_m(
                t, _type_table_kw,
            )
            if m_expr is None:
                # Fallback: original empty-placeholder shape.
                m_pairs_2: List[str] = []
                seen_ci_3: set = set()
                for col in t["columns"]:
                    if col.get("daxColumnExpr"):
                        continue
                    src = col["sourceCol"]
                    if not src or src.lower() in seen_ci_3:
                        continue
                    seen_ci_3.add(src.lower())
                    mt  = _type_table_kw.get(col["tmdlType"], "text")
                    esc = src.replace('"', '""')
                    m_pairs_2.append(f'#"{esc}" = {mt}')
                if m_pairs_2:
                    schema = ", ".join(m_pairs_2)
                    m_expr = (
                        "\t\t\tlet\n"
                        f"\t\t\t    Source = Table.FromRows({{}}, "
                        f"type table [{schema}])\n"
                        "\t\t\tin\n"
                        "\t\t\t    Source\n"
                    )
                else:
                    m_expr = (
                        "\t\t\tlet\n"
                        "\t\t\t    Source = Table.FromRows({})\n"
                        "\t\t\tin\n"
                        "\t\t\t    Source\n"
                    )
            else:
                # _render_partition_m returns a 2-tuple when emit succeeded.
                m_expr, partition_mode = m_expr

        lines.append(f"\tpartition {tmdl_quote(t['name'])} = m")
        # `mode: directQuery` swaps PBI's load semantics from snapshot to
        # live query. Emitted only when the connection class advertises a
        # supported live connector (sqlserver/postgres/snowflake) and we
        # produced a real Sql.Database / PostgreSQL.Database / Snowflake
        # source for it. Unrecognised live connectors fall back to the
        # placeholder above with mode: import so PBI loads without erroring;
        # users can fix the binding manually.
        if (csv_path is None
                and not t.get("paramRows")
                and not t.get("paramData")
                and partition_mode == "directQuery"):
            lines.append("\t\tmode: directQuery")
        else:
            lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.append(m_expr.rstrip("\n"))
        lines.append("")
        lines.append("\tannotation PBI_ResultType = Table")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Live-connection partition M emit
    # ------------------------------------------------------------------

    @staticmethod
    def _render_partition_m(
        t:                 Dict[str, Any],
        type_table_kw:     Dict[str, str],
    ) -> Optional[Tuple[str, str]]:
        """Emit a partition-M expression for live connections.

        Returns a (m_expr, partition_mode) tuple when the table's
        connection metadata is known and we can build a source. Returns
        None when there's no live-connection class — caller falls back to
        the original Table.FromRows placeholder.

        partition_mode is one of "import" / "directQuery". DirectQuery
        is used for sqlserver / postgres / snowflake live connections;
        every other case falls through to import.
        """
        from .utils import escape_m_string

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
        FILE_LIVE = {"excel-direct", "textscan", "csv", "json"}
        if cls in FILE_LIVE:
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
            return SemanticModel._render_unsupported_partition(
                t, cls, type_table_kw,
            )

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
        if cls in (
            "databricks",
            "azure-databricks",
            "azuredatabricks",
            "databricks-sql",
            "spark-sql",
            "spark",
        ):
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

            return m_expr, "import"

    @staticmethod
    def _is_databricks_live_connection(conn: Dict[str, Any]) -> bool:
        cls = (conn.get("class") or "").strip().lower()
        return (
            cls in {
                "databricks",
                "azure-databricks",
                "azuredatabricks",
                "databricks-sql",
                "spark-sql",
                "spark",
            }
            and bool(conn.get("server"))
            and bool(
                conn.get("http_path")
                or conn.get("httppath")
                or conn.get("http path")
                or conn.get("warehouse")
            )
        )

    @staticmethod
    def _prefer_live_over_extract(conn: Dict[str, Any]) -> bool:
        value = conn.get("prefer_live_over_extract")
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    @staticmethod
    def _render_unsupported_partition(
        t:             Dict[str, Any],
        cls:           str,
        type_table_kw: Dict[str, str],
    ) -> Tuple[str, str]:
        """Placeholder for live connectors we don't synthesize today.

        Emits the same Table.FromRows({}) shape used for empty extracts,
        prefixed with a TODO comment so the user can find the spot to
        rewire the connection. mode stays "import" so PBI loads cleanly.
        """
        print(f"[CONN] live connection class '{cls}' - emitting placeholder Table.FromRows.")
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
            f"\t\t\t    // TODO: live connection class '{cls}' - rewire the connector in PBI Desktop\n"
            + body
            + "\t\t\tin\n"
            "\t\t\t    Source\n"
        )
        return m_expr, "import"
