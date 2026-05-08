"""Tableau formula → DAX measure translator.

Handles the common subset of Tableau calculation syntax:

    - basic arithmetic         (+ - * /)
    - string concatenation     (+ between string operands -> &)
    - comparison and logic     (= == != <> < > <= >=, AND OR NOT)
    - field references         ([X] and [Table].[X] qualified form)
    - aggregations             (SUM, AVG, COUNT, COUNTD, MIN, MAX, MEDIAN ...)
    - date parts               (YEAR, QUARTER, MONTH, DAY, ...)
    - function-call IF/IIF     (IF(cond, then, else))
    - function-call CASE       (CASE(expr, w1, t1, ..., else))
    - block-style CASE         (CASE expr WHEN v1 THEN r1 ... [ELSE re] END)
    - block-style IF           (IF c THEN r1 [ELSEIF c2 THEN r2] [ELSE re] END)
    - string literals          ('foo' or "foo" -> "foo" in DAX)

Anything else (LOD expressions, table calcs, ATTR, unrecognized identifiers)
returns None so the caller emits a placeholder measure rather than broken DAX.
"""

import re
from typing import Any, Dict, List, Optional, Tuple


# Module-level capture for the active call's measure_refs. Avoids
# threading the set through every recursive helper. Reset on each
# top-level translate call.
_ACTIVE_MEASURE_REFS: set = set()


# Regex matching a bare DAX column reference: 'Table Name'[Column Name].
# Only matches when 'Table Name' carries no space-around-and-after — which
# is how _resolve emits them. The Column part allows brackets and spaces
# but no closing bracket inside.
_BARE_COLREF_RE = re.compile(r"'([^']+)'\[([^\]]+)\]")

# Aggregation function tokens that already wrap a column ref into a
# scalar — when one of these immediately precedes a column ref, we
# leave it alone.
_SCALAR_WRAPPERS = {
    "SUM", "AVERAGE", "AVG", "COUNT", "COUNTA", "COUNTROWS",
    "DISTINCTCOUNT", "MIN", "MAX", "MEDIAN", "STDEV.P", "STDEV.S",
    "VAR.P", "VAR.S", "FIRSTNONBLANK", "LASTNONBLANK",
    "FIRSTNONBLANKVALUE", "LASTNONBLANKVALUE",
    "SELECTEDVALUE", "VALUES", "DISTINCT", "RELATED", "RELATEDTABLE",
    "CALCULATE", "CALCULATETABLE", "FILTER", "ALL", "ALLEXCEPT",
    "SUMX", "AVERAGEX", "MAXX", "MINX", "COUNTX", "RANKX",
}


def _wrap_scalar_columns(dax: str) -> str:
    """Wrap bare column references in MIN() for scalar contexts.

    DAX measures must return scalars. A bare `'Table'[Col]` reference is
    a row-context column — invalid in measure context unless wrapped in
    an aggregation. This helper scans the DAX string and wraps each
    column ref that is NOT:
      * already preceded by an aggregation function (SUM, MIN, etc.)
      * a registered DAX measure (in _ACTIVE_MEASURE_REFS)
      * the LHS of an assignment

    MIN works on every column type — strings, numbers, dates — and
    returns a single value. Tableau's `IF [bool] THEN [col] END` written
    as a measure naturally collapses to MIN-of-col when bool is true,
    matching the user's intent in 95% of cases.
    """
    if not dax:
        return dax

    out: List[str] = []
    pos = 0
    for m in _BARE_COLREF_RE.finditer(dax):
        out.append(dax[pos:m.start()])
        tbl, col = m.group(1), m.group(2)
        # Already a known DAX measure → emit bare (measures evaluate to scalar).
        if (tbl, col) in _ACTIVE_MEASURE_REFS:
            out.append(m.group(0))
            pos = m.end()
            continue
        # Look back to see if this column is already inside an
        # aggregation function. We accept any preceding alphabetic-or-
        # underscore-or-dot sequence (function name) followed by '('.
        prefix = dax[:m.start()].rstrip()
        wrap = True
        if prefix.endswith("("):
            # Walk back from the '(' to capture the immediately preceding
            # identifier — that's the function name. fn_end is *exclusive*
            # (one past the last alnum char), so the slice `[fn_start:fn_end]`
            # gives the full name including its last character.
            paren_idx = len(prefix) - 1            # position of '('
            fn_end = paren_idx
            # Skip whitespace between identifier and '(' (rare, but
            # `YEAR (col)` is what Tableau-translated code looks like).
            while fn_end > 0 and prefix[fn_end - 1].isspace():
                fn_end -= 1
            fn_start = fn_end
            while fn_start > 0 and (
                prefix[fn_start - 1].isalnum()
                or prefix[fn_start - 1] in "._"
            ):
                fn_start -= 1
            fn_name = prefix[fn_start:fn_end].upper()
            if fn_name in _SCALAR_WRAPPERS:
                wrap = False
        if wrap:
            out.append(f"MIN('{tbl}'[{col}])")
        else:
            out.append(m.group(0))
        pos = m.end()
    out.append(dax[pos:])
    return "".join(out)


def _translate_lod_fixed(
    formula: str,
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]] = None,
    parameter_refs: Optional[set] = None,
    measure_refs: Optional[set] = None,
    lod_subs: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Replace ``{ FIXED [dims] : agg(X) }`` blocks with DAX
    ``CALCULATE`` + ``ALL`` / ``ALLEXCEPT`` equivalents.

    Patterns handled (innermost first; recursion repeats until none remain):

      * ``{ FIXED : agg(X) }`` — global, no dims:
        ``CALCULATE(<dax-agg>, ALL('Table'))``
      * ``{ FIXED [Dim] : agg(X) }`` — single dim:
        ``CALCULATE(<dax-agg>, ALLEXCEPT('Table', 'Table'[Dim]))``
      * ``{ FIXED [D1], [D2] : agg(X) }`` — multi dim: same with more
        ALLEXCEPT args.

    The aggregation expression on the right of ``:`` is recursively
    translated via the main ``translate_tableau_to_dax`` entry point so
    nested IFs / case-when / column refs all resolve correctly. The dim
    refs are translated in isolation to ``'Table'[Col]`` form.

    Returns the formula with all FIXED blocks substituted, or ``None``
    if any block had a sub-translation that failed (caller treats that
    as the whole formula being untranslatable).

    Tableau LOD has cross-datasource and context-filter semantics that
    don't map perfectly onto PBI relationships. The translation is a
    best-effort *single-table* lift: when the dim and the aggregation
    column live on the same TMDL table (the common case), the result
    is correct. Cross-table FIXED LODs would need TREATAS — flagged as
    a TODO in the emitted comment.
    """
    pat = re.compile(r"\{\s*FIXED\b", re.IGNORECASE)
    while True:
        m = pat.search(formula)
        if not m:
            break
        start = m.start()
        # Find matching '}' accounting for nested LOD blocks.
        depth = 1
        i = m.end()
        n = len(formula)
        while i < n and depth > 0:
            if formula[i] == "{":
                depth += 1
            elif formula[i] == "}":
                depth -= 1
            i += 1
        if depth != 0:
            return None
        end = i  # one past closing '}'
        inner = formula[m.end():end - 1]

        # Split inner at the first top-level ':'. Tableau allows
        # ``[Dim1], [Dim2] : agg``. The colon binds tightest at the
        # LOD level — anything inside (), [], or "" is shielded.
        colon_idx = _find_top_level_colon(inner)
        if colon_idx < 0:
            return None
        dims_part = inner[:colon_idx].strip()
        agg_part  = inner[colon_idx + 1:].strip()

        # Parse dim refs: comma-separated tokens. Two shapes are
        # supported:
        #   * ``[Field]``                      — bare column ref.
        #   * ``YEAR([X])`` / ``MONTH([X])`` /
        #     ``QUARTER([X])`` / ``DAY([X])``  — date-part dim. Maps to
        #     the synthesized hierarchy column the model writer emits
        #     for every dateTime column (``Year of X`` integer, etc.).
        # Other expressions (arbitrary functions, arithmetic) aren't
        # representable in PBI's ALLEXCEPT (which requires column refs)
        # so we fall through to None and the whole formula gets dropped
        # rather than emitting broken DAX.
        dim_pbi: List[str] = []
        if dims_part:
            for raw_dim in _split_top_level_commas(dims_part):
                raw_dim = raw_dim.strip()
                # Pattern 1: bare [Field].
                if raw_dim.startswith("[") and raw_dim.endswith("]"):
                    dim_dax = translate_tableau_to_dax(
                        raw_dim, table_name, col_aliases, datasource_columns,
                        field_to_pbi=field_to_pbi,
                        parameter_refs=parameter_refs,
                        measure_refs=measure_refs,
                    )
                    if not dim_dax:
                        return None
                    m_min = re.match(r"^MIN\((.+)\)$", dim_dax.strip())
                    if m_min:
                        dim_dax = m_min.group(1).strip()
                    dim_pbi.append(dim_dax)
                    continue

                # Pattern 2: YEAR/MONTH/QUARTER/DAY([X]) — redirect to
                # the synthesized integer hierarchy column.
                m_dp = re.match(
                    r"^(YEAR|MONTH|QUARTER|DAY)\s*\(\s*\[([^\]]+)\]\s*\)$",
                    raw_dim, re.IGNORECASE,
                )
                if m_dp:
                    fn_name   = m_dp.group(1).upper()
                    field_ref = m_dp.group(2).strip()
                    # Resolve the source date column to its (table, col)
                    # via translate-on-bare-ref to get '<table>'[<col>].
                    base_dax = translate_tableau_to_dax(
                        f"[{field_ref}]", table_name, col_aliases, datasource_columns,
                        field_to_pbi=field_to_pbi,
                        parameter_refs=parameter_refs,
                        measure_refs=measure_refs,
                    )
                    if not base_dax:
                        return None
                    m_resolved = re.match(
                        r"^(?:MIN\()?'([^']+)'\[([^\]]+)\]\)?$",
                        base_dax.strip(),
                    )
                    if not m_resolved:
                        return None
                    base_tbl  = m_resolved.group(1)
                    base_col  = m_resolved.group(2)
                    # Map fn -> hierarchy column name. Month uses the
                    # hidden "Month Number of X" int column (the visible
                    # "Month of X" is a string month name).
                    if fn_name == "YEAR":
                        hier_col = f"Year of {base_col}"
                    elif fn_name == "MONTH":
                        hier_col = f"Month Number of {base_col}"
                    elif fn_name == "QUARTER":
                        hier_col = f"Quarter of {base_col}"
                    elif fn_name == "DAY":
                        hier_col = f"Day of {base_col}"
                    else:
                        return None
                    dim_pbi.append(f"'{base_tbl}'[{hier_col}]")
                    continue

                # Anything else: refuse — ALLEXCEPT can't take a
                # function-call expression as an argument.
                return None

        # Translate the aggregation expression.
        agg_dax = translate_tableau_to_dax(
            agg_part, table_name, col_aliases, datasource_columns,
            field_to_pbi=field_to_pbi,
            parameter_refs=parameter_refs,
            measure_refs=measure_refs,
        )
        if not agg_dax:
            return None

        if not dim_pbi:
            dax_block = f"CALCULATE({agg_dax}, ALL('{table_name}'))"
        else:
            allexcept_args = ", ".join(
                [f"'{table_name}'"] + dim_pbi
            )
            dax_block = f"CALCULATE({agg_dax}, ALLEXCEPT({allexcept_args}))"

        if lod_subs is not None:
            # Placeholder substitution path (used by translate_tableau_to_dax).
            # The placeholder is a plain identifier the main translator
            # leaves alone; we substitute the DAX back at the end.
            ph = f"__LOD_BLOCK_{len(lod_subs)}__"
            lod_subs[ph] = dax_block
            formula = formula[:start] + ph + formula[end:]
        else:
            formula = formula[:start] + dax_block + formula[end:]

    return formula


def _find_top_level_colon(s: str) -> int:
    """Return the index of the first ``:`` at top-level (outside any
    quoted string / [field-ref] / parens). Returns -1 if none."""
    depth_paren = 0
    in_bracket = False
    in_squote = False
    in_dquote = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_squote:
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if c == '"':
                in_dquote = False
            i += 1
            continue
        if in_bracket:
            if c == "]":
                in_bracket = False
            i += 1
            continue
        if c == "[":
            in_bracket = True
        elif c == "'":
            in_squote = True
        elif c == '"':
            in_dquote = True
        elif c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
        elif c == ":" and depth_paren == 0:
            return i
        i += 1
    return -1


def _split_top_level_commas(s: str) -> List[str]:
    """Split a string on commas at top-level (outside paren/bracket/quote)."""
    parts: List[str] = []
    depth_paren = 0
    in_bracket = False
    in_squote = False
    in_dquote = False
    last = 0
    i = 0
    while i < len(s):
        c = s[i]
        if in_squote:
            if c == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if c == '"':
                in_dquote = False
            i += 1
            continue
        if in_bracket:
            if c == "]":
                in_bracket = False
            i += 1
            continue
        if c == "[":
            in_bracket = True
        elif c == "'":
            in_squote = True
        elif c == '"':
            in_dquote = True
        elif c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
        elif c == "," and depth_paren == 0:
            parts.append(s[last:i])
            last = i + 1
        i += 1
    parts.append(s[last:])
    return parts


def translate_tableau_to_dax(
    formula: str,
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]] = None,
    parameter_refs: Optional[set] = None,
    measure_refs: Optional[set] = None,
) -> Optional[str]:
    """Translate a Tableau calculation formula to DAX.

    `field_to_pbi` maps each Tableau field reference (whichever form
    appears in the formula — internal name, caption, suffixed) to the
    canonical PBI (table, column) pair from col_locator. Without it,
    field references would emit Tableau's original name (which often
    contains spaces, parentheses, or `(Object!Suffix)` decorations) and
    PBI would fail with "Column X cannot be found" at load time.

    Returns the DAX expression string, or None if the formula contains
    constructs we cannot safely translate.
    """
    if not formula:
        return None

    f = formula.strip()

    # Tableau's ``__tableau_internal_object_id__`` pseudo-field is the
    # row-counter Tableau auto-creates for every datasource. The XML
    # form is ``[__tableau_internal_object_id__].[<table-id>]`` and
    # users typically wrap it in COUNT() to get a row count. The DAX
    # equivalent is ``COUNTROWS('Table')`` but the substitution can't
    # be emitted directly into the source formula because the
    # tokenizer would parse ``'Table'`` as a string literal. We use a
    # placeholder substitution path identical to LOD blocks: replace
    # the COUNT(...) wrapper with ``__ROWCOUNT_PLACEHOLDER_N__`` and
    # restore the DAX (with proper single-quoted table name) after the
    # main translator returns.
    rowcount_subs: Dict[str, str] = {}
    def _rc_sub(m):
        ph = f"__ROWCOUNT_PLACEHOLDER_{len(rowcount_subs)}__"
        rowcount_subs[ph] = f"COUNTROWS('{table_name}')"
        return ph
    f = re.sub(
        r"COUNT\s*\(\s*\[__tableau_internal_object_id__\]\s*\.\s*\[[^\]]+\]\s*\)",
        _rc_sub,
        f,
        flags=re.IGNORECASE,
    )

    # LOD FIXED preprocessing — extract each ``{ FIXED [dims] : agg }``
    # block, translate it to DAX, replace it with a placeholder
    # identifier in the source formula, and record the substitution. The
    # main translator runs on the placeholder version (identifiers it
    # doesn't recognise pass through unchanged); the placeholders get
    # swapped back to their DAX after the main translator returns.
    # This keeps the LOD-translated DAX from being re-tokenised by the
    # Tableau-syntax token loop, which doesn't understand DAX shapes
    # like ``ALL('Table')`` or single-quoted table names.
    lod_subs: Dict[str, str] = {}
    f_lod = _translate_lod_fixed(
        f, table_name, col_aliases, datasource_columns,
        field_to_pbi, parameter_refs, measure_refs,
        lod_subs,
    )
    if f_lod is None:
        # LOD subblock wasn't translatable; fall through to the
        # unsupported-token check which will reject the whole formula.
        f_lod = f
    else:
        f = f_lod

    _UNSUPPORTED_TOKENS = {
        # LOD expressions we don't translate yet (FIXED is handled above)
        "include", "exclude",
        # Table calculations
        "lookup", "previous_value", "running_sum", "running_avg",
        "window_sum", "window_avg", "window_max", "window_min",
        "rank", "index",
        # Others we don't translate accurately. ATTR is now handled in
        # the token translator (maps to SELECTEDVALUE) so it's removed
        # from this list.
        "percentile",
    }
    f_lower = f.lower()
    for tok in _UNSUPPORTED_TOKENS:
        # Word-boundary check so 'rank' doesn't reject 'frank' columns.
        if re.search(rf"\b{re.escape(tok)}\b", f_lower):
            return None
    # If LOD preprocessing left a literal `{ FIXED` (because the inner
    # agg failed to translate), reject — better to drop with a clear
    # log than emit broken DAX.
    if re.search(r"\{\s*FIXED\b", f, re.IGNORECASE):
        return None

    global _ACTIVE_MEASURE_REFS
    _ACTIVE_MEASURE_REFS = measure_refs or set()
    tokens = _tokenize(f)
    try:
        result = _translate_tokens(
            tokens, 0, len(tokens),
            table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
        )
        # Final scalar-context safety: wrap any bare column ref left at
        # the top level in MIN(). Tableau lets calc fields return a
        # column value per row (logically a calculated column) but PBI
        # measures must be scalars — wrapping in MIN preserves the
        # intended single-value semantic.
        if result is not None:
            result = _wrap_scalar_columns(result)
            # Re-insert LOD-DAX subblocks now that the main translator
            # has run. Placeholders are simple identifiers (``__LOD_BLOCK_N__``)
            # that the translator passes through unchanged.
            if lod_subs:
                for ph, dax in lod_subs.items():
                    result = result.replace(ph, dax)
            if rowcount_subs:
                for ph, dax in rowcount_subs.items():
                    result = result.replace(ph, dax)
        return result
    finally:
        _ACTIVE_MEASURE_REFS = set()


# ---------------------------------------------------------------------------
# Core token translator
# ---------------------------------------------------------------------------

def _translate_tokens(
    tokens: List[str], start: int, end: int,
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]] = None,
    parameter_refs: Optional[set] = None,
) -> Optional[str]:
    def _resolve(field_ref: str, hinted_table: Optional[str] = None) -> Tuple[str, str, bool]:
        """Map a Tableau field reference to its PBI (table, column,
        is_parameter). Tries the raw token, the alias-translated form,
        and a few suffix-stripped fallbacks. Returns
        (table_name, field_ref, False) when nothing matches so the
        caller can still emit something."""
        if field_to_pbi:
            for k in (field_ref, col_aliases.get(field_ref, field_ref)):
                if k in field_to_pbi:
                    is_param = bool(parameter_refs and k in parameter_refs)
                    return (*field_to_pbi[k], is_param)
            stripped = re.sub(r"\s*\([^()]+!.+?\)\s*$", "", field_ref).strip()
            if stripped and stripped in field_to_pbi:
                is_param = bool(parameter_refs and stripped in parameter_refs)
                return (*field_to_pbi[stripped], is_param)
        resolved_col = col_aliases.get(field_ref, field_ref)
        return (hinted_table or table_name, resolved_col, False)

    def _emit_field_ref(t_out: str, c_out: str, is_param: bool) -> str:
        # Tableau parameters are scalars but their PBI backing is a
        # multi-row list-parameter table. Wrap with SELECTEDVALUE so the
        # measure reads whatever the user has slicer-selected — same
        # semantic as Tableau's parameter "current value".
        if is_param:
            return f"SELECTEDVALUE('{t_out}'[{c_out}])"
        return f"'{t_out}'[{c_out}]"
    dax_tokens: List[str] = []
    i = start
    while i < end:
        tok = tokens[i]
        low = tok.lower()

        # -- Qualified field reference [Table].[Column] -------------------
        if (tok.startswith("[") and tok.endswith("]")
                and i + 2 < end and tokens[i + 1] == "."
                and tokens[i + 2].startswith("[") and tokens[i + 2].endswith("]")):
            tbl = tok[1:-1].strip()
            col = tokens[i + 2][1:-1].strip()
            # Resolve through the mapping with the hinted table so we
            # land on the PBI column name (the mapping is keyed by the
            # Tableau field ref, not the qualified form).
            t_out, c_out, is_param = _resolve(col, hinted_table=tbl)
            dax_tokens.append(_emit_field_ref(t_out, c_out, is_param))
            i += 3
            continue

        # -- Bare field reference [Column] --------------------------------
        if tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1].strip()
            t_out, c_out, is_param = _resolve(inner)
            dax_tokens.append(_emit_field_ref(t_out, c_out, is_param))
            i += 1
            continue

        # -- String literal '...' or "..." -> "..." ----------------------
        if (len(tok) >= 2 and (
                (tok[0] == "'" and tok[-1] == "'")
                or (tok[0] == '"' and tok[-1] == '"'))):
            inner = tok[1:-1].replace('"', '""')
            dax_tokens.append(f'"{inner}"')
            i += 1
            continue

        # -- Block-style CASE (not function call) -------------------------
        if low == "case" and (i + 1 >= end or tokens[i + 1] != "("):
            r = _translate_block_case(
                tokens, i, end,
                table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
            )
            if r is None:
                return None
            dax, new_i = r
            dax_tokens.append(dax)
            i = new_i
            continue

        # -- Block-style IF (not function call) ---------------------------
        if low == "if" and (i + 1 >= end or tokens[i + 1] != "("):
            r = _translate_block_if(
                tokens, i, end,
                table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
            )
            if r is None:
                return None
            dax, new_i = r
            dax_tokens.append(dax)
            i = new_i
            continue

        # -- Function-call IF / IIF ---------------------------------------
        # Tableau:
        #   IF(test, then [, else])               — 2 or 3 args
        #   IIF(test, then, else [, unknown])     — 3 or 4 args; the 4th
        #                                            arg is what to return
        #                                            when test is NULL
        # DAX IF takes (cond, then, [else]). For IIF's 4-arg form we
        # nest: IF(ISBLANK(test), unknown, IF(test, then, else)).
        if low in ("if", "iif") and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 2:
                return None
            cond = _translate_token_list(parts[0], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            then_v = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            else_v = "BLANK()"
            unknown_v = None
            if len(parts) > 2:
                else_v = _translate_token_list(parts[2], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if low == "iif" and len(parts) > 3:
                unknown_v = _translate_token_list(parts[3], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if cond is None or then_v is None or else_v is None:
                return None
            if unknown_v is not None:
                dax_tokens.append(
                    f"IF(ISBLANK({cond}), {unknown_v}, IF({cond}, {then_v}, {else_v}))"
                )
            else:
                dax_tokens.append(f"IF({cond}, {then_v}, {else_v})")
            i = j + 1
            continue

        # -- IFNULL(expr, fallback) -> COALESCE(expr, fallback) ----------
        # Tableau IFNULL returns the second arg when the first is null.
        # DAX COALESCE has the same shape and type semantics, and unlike
        # IFERROR doesn't require both args to be the same data type at
        # parse time. Use COALESCE so a numeric column with a string
        # fallback (rare but legal in Tableau) doesn't trip the loader.
        if low == "ifnull" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) != 2:
                return None
            v = _translate_token_list(parts[0], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            f = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if v is None or f is None:
                return None
            dax_tokens.append(f"COALESCE({v}, {f})")
            i = j + 1
            continue

        # -- DATEDIFF('part', start, end) -> DATEDIFF(start, end, PART) --
        # Tableau order: ('day', start, end). DAX order: (start, end, DAY).
        # The interval keyword in DAX is a bare token, not a quoted
        # string. Without this dedicated handler the passthrough would
        # forward Tableau's signature unchanged and the loader would
        # reject the measure.
        if low == "datediff" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 3:
                return None
            part_tok = parts[0]
            if (len(part_tok) == 1
                    and len(part_tok[0]) >= 2
                    and part_tok[0][0] in ("'", '"')
                    and part_tok[0][-1] == part_tok[0][0]):
                interval = part_tok[0][1:-1].upper()
            else:
                # Non-literal first arg — too dynamic for safe translation.
                return None
            start = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            end_t = _translate_token_list(parts[2], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if start is None or end_t is None:
                return None
            dax_tokens.append(f"DATEDIFF({start}, {end_t}, {interval})")
            i = j + 1
            continue

        # -- DATEPART('part', date) -> YEAR/MONTH/...(date) -------------
        # Tableau DATEPART has a string-keyword first arg. DAX has no
        # generic DATEPART; map common parts to their dedicated function.
        if low == "datepart" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 2:
                return None
            part_tok = parts[0]
            if not (len(part_tok) == 1
                    and len(part_tok[0]) >= 2
                    and part_tok[0][0] in ("'", '"')
                    and part_tok[0][-1] == part_tok[0][0]):
                return None
            part_name = part_tok[0][1:-1].lower()
            mapping = {
                "year": "YEAR", "quarter": "QUARTER", "month": "MONTH",
                "week": "WEEKNUM", "weekday": "WEEKDAY", "day": "DAY",
                "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
            }
            fn = mapping.get(part_name)
            if not fn:
                return None
            inner = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f"{fn}({inner})")
            i = j + 1
            continue

        # -- DATETRUNC('part', date) -> period-truncated date ------------
        # Tableau truncates the date to the start of the period. DAX has
        # no direct equivalent; we emit the canonical period-start
        # arithmetic for each supported part. Unsupported parts return
        # None (drop with a clear log) rather than emit broken DAX.
        if low == "datetrunc" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 2:
                return None
            part_tok = parts[0]
            if not (len(part_tok) == 1
                    and len(part_tok[0]) >= 2
                    and part_tok[0][0] in ("'", '"')
                    and part_tok[0][-1] == part_tok[0][0]):
                return None
            part_name = part_tok[0][1:-1].lower()
            inner = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            # Tableau supports a 3rd arg (start-of-week); we ignore it
            # for week truncation (uses ISO Monday) — the dashboards in
            # our corpus don't customise this.
            if part_name == "year":
                expr = f"DATE(YEAR({inner}), 1, 1)"
            elif part_name == "quarter":
                expr = f"DATE(YEAR({inner}), (QUARTER({inner}) - 1) * 3 + 1, 1)"
            elif part_name == "month":
                expr = f"DATE(YEAR({inner}), MONTH({inner}), 1)"
            elif part_name == "week":
                # Monday-start week. WEEKDAY(date, 2) returns 1=Mon..7=Sun;
                # subtract (n-1) days to land on Monday. The DATEADD shape
                # works for date AND datetime inputs.
                expr = f"({inner}) - (WEEKDAY({inner}, 2) - 1)"
            elif part_name == "day":
                expr = f"INT({inner})"
            else:
                return None
            dax_tokens.append(expr)
            i = j + 1
            continue

        # -- DATENAME('part', date) -> string name of date part ----------
        # Tableau returns the localised name (e.g. 'January', 'Mon').
        # DAX uses FORMAT with the appropriate format string.
        if low == "datename" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 2:
                return None
            part_tok = parts[0]
            if not (len(part_tok) == 1
                    and len(part_tok[0]) >= 2
                    and part_tok[0][0] in ("'", '"')
                    and part_tok[0][-1] == part_tok[0][0]):
                return None
            part_name = part_tok[0][1:-1].lower()
            inner = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            mapping = {
                "year":     f'FORMAT({inner}, "YYYY")',
                "quarter":  f'"Q" & QUARTER({inner})',
                "month":    f'FORMAT({inner}, "MMMM")',
                "weekday":  f'FORMAT({inner}, "dddd")',
                "day":      f'FORMAT({inner}, "DD")',
            }
            expr = mapping.get(part_name)
            if not expr:
                return None
            dax_tokens.append(expr)
            i = j + 1
            continue

        # -- TOTAL(expr) -> CALCULATE(expr, ALLSELECTED()) -------------
        # Tableau TOTAL() aggregates ignoring the current partition.
        # DAX CALCULATE+ALLSELECTED removes filter context from the
        # current visual scope while respecting outer slicers — the
        # closest semantic match. Common pattern in dashboards is
        # ``COUNT([X]) / TOTAL(COUNT([X]))`` which becomes a percent-of-
        # total calculation. Tableau also accepts non-aggregated inner
        # expressions (rare) — we forward them as-is and rely on the
        # scalar-wrapping pass to MIN-wrap any bare column refs.
        if low == "total" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            inner = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f"CALCULATE({inner}, ALLSELECTED())")
            i = j + 1
            continue

        # -- SPLIT(string, delim, token-num) -> MID-based extraction ----
        # Tableau SPLIT returns the Nth token from a delimited string.
        # DAX has no native SPLIT, but PATHITEM works on a |-delimited
        # form. We emit:
        #   PATHITEM(SUBSTITUTE(<string>, <delim>, "|"), <n>)
        # which respects the 1-based indexing convention Tableau uses.
        # PATHITEM returns BLANK when the index is out of range, matching
        # Tableau's behaviour on missing tokens.
        if low == "split" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 3:
                return None
            s_str   = _translate_token_list(parts[0], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            s_delim = _translate_token_list(parts[1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            s_n     = _translate_token_list(parts[2], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if s_str is None or s_delim is None or s_n is None:
                return None
            dax_tokens.append(
                f'PATHITEM(SUBSTITUTE({s_str}, {s_delim}, "|"), {s_n})'
            )
            i = j + 1
            continue

        # -- TRIM / LTRIM / RTRIM -----------------------------------------
        # Tableau and DAX share these names but Tableau's TRIM strips
        # only spaces while DAX TRIM strips spaces and collapses internal
        # whitespace. For our usage (output of SPLIT chains) the
        # difference doesn't surface — straight passthrough is safe.
        if low in ("trim", "ltrim", "rtrim") and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            inner = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f"{low.upper()}({inner})")
            i = j + 1
            continue

        # -- ATTR(expr) -> SELECTEDVALUE -------------------------------
        # Tableau ATTR returns the single value when the field has the
        # same value across the partition (and an asterisk otherwise);
        # DAX SELECTEDVALUE has the same single-value-or-blank semantic.
        # When the inner expression resolves to a column ref we can call
        # SELECTEDVALUE on it directly; for non-column inner exprs (rare
        # but possible — e.g. ATTR([Sales]+[Tax])), we fall back to
        # MIN(...) which preserves the deterministic-scalar contract.
        if low == "attr" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            inner = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            # Detect bare column ref ('T'[C] or MIN('T'[C]) — the scalar
            # safety pass may have wrapped it). Strip a single MIN()
            # wrapper if present so SELECTEDVALUE gets the column ref.
            stripped = inner.strip()
            m_min = re.match(r"^MIN\(('[^']+'\[[^\]]+\])\)$", stripped)
            if m_min:
                stripped = m_min.group(1)
            if re.fullmatch(r"'[^']+'\[[^\]]+\]", stripped):
                dax_tokens.append(f"SELECTEDVALUE({stripped})")
            else:
                dax_tokens.append(f"MIN({inner})")
            i = j + 1
            continue

        # -- DATEPARSE / PARSEDATE -> DATEVALUE -------------------------
        # Tableau ``DATEPARSE(format, string)`` parses ``string`` using
        # the supplied format pattern. DAX has no direct format-string
        # parser; the closest equivalent is ``DATEVALUE`` which uses the
        # locale's default date format. We drop the format arg and emit
        # ``DATEVALUE(string)`` — works for ISO-formatted inputs and
        # most common locale formats; custom formats degrade to BLANK on
        # unrecognised inputs (and the user can replace with a Power
        # Query M ``Date.FromText(... [Format=...])`` step manually).
        # Tableau also accepts the ``PARSEDATE`` alias.
        if low in ("dateparse", "parsedate") and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            # Both 1-arg and 2-arg forms — pick the LAST arg which is the
            # string to parse (Tableau is ``DATEPARSE('format', string)``;
            # PARSEDATE alias is sometimes 1-arg).
            string_arg = parts[-1] if parts else None
            if not string_arg:
                return None
            inner = _translate_token_list(string_arg, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f"DATEVALUE({inner})")
            i = j + 1
            continue

        # -- DATE(expr) cast ---------------------------------------------
        # Tableau DATE(x) coerces a value to a date. DAX is duck-typed for
        # date arithmetic — the safe lift is DATEVALUE for strings and a
        # passthrough for already-date inputs. We emit DATEVALUE which
        # PBI accepts on string AND date inputs (no-op on the latter).
        if low == "date" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            inner = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f"DATEVALUE({inner})")
            i = j + 1
            continue

        # -- STR(expr) -> FORMAT(expr, "") ------------------------------
        # Tableau STR coerces any value to text. DAX FORMAT(value, "")
        # produces the value's default string rendering, which is the
        # closest 1-arg equivalent.
        if low == "str" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            inner = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if inner is None:
                return None
            dax_tokens.append(f'FORMAT({inner}, "")')
            i = j + 1
            continue

        # -- Function-call CASE -------------------------------------------
        # DAX SWITCH requires at least one Value/Result pair. Bare
        # SWITCH(expr) is invalid. We collect pairs and enforce the
        # minimum here so a malformed CASE returns None and the caller
        # emits a placeholder measure instead of broken DAX.
        if low == "case" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            parts = _split_args(sub_toks)
            if len(parts) < 3:
                return None
            expr = _translate_token_list(parts[0], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if expr is None:
                return None
            args = [expr]
            pairs_emitted = 0
            k = 1
            while k + 1 < len(parts):
                v = _translate_token_list(parts[k], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
                r2 = _translate_token_list(parts[k + 1], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
                if v is None or r2 is None:
                    return None
                args.extend([v, r2])
                pairs_emitted += 1
                k += 2
            if pairs_emitted == 0:
                return None
            if k < len(parts):
                e = _translate_token_list(parts[k], table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
                if e is None:
                    return None
                args.append(e)
            dax_tokens.append(f"SWITCH({', '.join(args)})")
            i = j + 1
            continue

        # -- Aggregations -------------------------------------------------
        if low in _AGG_MAP and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            sub = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if sub is None:
                return None
            # SUM/AVG/MIN/etc. require a column reference. If the inner
            # expression is JUST a reference to another measure (which
            # happens often: Tableau lets you write SUM([Sales]) where
            # [Sales] itself is `IF ... THEN [Amount] END` — a calc),
            # PBI rejects it with 'The SUM function only accepts a
            # column reference as an argument'. Detect that case by
            # matching the resolved `'Table'[Name]` token against the
            # measure_refs set, and emit the bare measure ref instead.
            stripped = sub.strip()
            m = re.match(r"^'([^']+)'\[([^\]]+)\]$", stripped)
            if m and (m.group(1), m.group(2)) in _ACTIVE_MEASURE_REFS:
                dax_tokens.append(stripped)
            else:
                dax_tokens.append(f"{_AGG_MAP[low]}({sub})")
            i = j + 1
            continue

        # -- ZN(expr) -> IFERROR(expr, 0). Tableau's ZN means "null/error
        # becomes zero". DAX IFERROR requires a fallback argument, so
        # passing it through as a single-arg call would emit invalid
        # DAX. We collect the inner expression here and emit the
        # 2-arg form explicitly.
        if low == "zn" and i + 1 < end and tokens[i + 1] == "(":
            j, sub_toks = _collect_paren(tokens, i + 1, end)
            if j is None:
                return None
            sub = _translate_token_list(sub_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs)
            if sub is None:
                return None
            dax_tokens.append(f"IFERROR({sub}, 0)")
            i = j + 1
            continue

        # -- Keyword-as-literal -------------------------------------------
        if low in _KW_LITERAL:
            dax_tokens.append(_KW_LITERAL[low])
            i += 1
            continue

        # -- Logical operators --------------------------------------------
        if low in _LOGICAL:
            dax_tokens.append(_LOGICAL[low])
            i += 1
            continue

        # -- Function passthrough (followed by paren handled by main loop) -
        if low in _FN_PASSTHROUGH:
            dax_tokens.append(_FN_PASSTHROUGH[low])
            i += 1
            continue

        # -- Comparison operators -----------------------------------------
        if tok in ("==", "="):
            dax_tokens.append("=")
            i += 1
            continue
        if tok in ("!=", "<>"):
            dax_tokens.append("<>")
            i += 1
            continue
        if tok in ("<", ">", "<=", ">="):
            dax_tokens.append(tok)
            i += 1
            continue

        # -- Parens / commas / arithmetic ---------------------------------
        if tok in ("(", ")", ",", "+", "-", "*", "/", "%", "^"):
            dax_tokens.append(tok)
            i += 1
            continue

        # -- Number -------------------------------------------------------
        if _is_number(tok):
            dax_tokens.append(tok)
            i += 1
            continue

        # -- LOD-block placeholder ----------------------------------------
        # ``__LOD_BLOCK_N__`` markers are emitted by ``_translate_lod_fixed``
        # when it extracts a ``{ FIXED ... }`` subexpression. The marker
        # passes through the main translator unchanged and gets swapped
        # back to its DAX equivalent in ``translate_tableau_to_dax`` after
        # the main translator returns.
        if re.fullmatch(r"__LOD_BLOCK_\d+__", tok):
            dax_tokens.append(tok)
            i += 1
            continue

        # -- Rowcount-placeholder ----------------------------------------
        # ``__ROWCOUNT_PLACEHOLDER_N__`` markers are emitted by the
        # ``__tableau_internal_object_id__`` preprocessor at the top of
        # ``translate_tableau_to_dax``. Same pass-through pattern as
        # LOD blocks — substitution back happens post-translation.
        if re.fullmatch(r"__ROWCOUNT_PLACEHOLDER_\d+__", tok):
            dax_tokens.append(tok)
            i += 1
            continue

        # -- DAX function passthrough (already-translated identifiers) ----
        # Some preprocessors substitute Tableau-isms with bare DAX
        # function calls like ``COUNTROWS(...)`` (the
        # ``__tableau_internal_object_id__`` row-counter pattern). The
        # tokenizer picks the function name as an identifier; we let
        # any all-uppercase identifier followed by ``(`` pass through as
        # a function name unchanged. This is conservative — the bareword
        # has to be UPPERCASE so user-named columns/measures (always
        # mixed-case in practice) don't accidentally pass through and
        # produce broken DAX.
        if tok.isupper() and tok.isalpha() and i + 1 < end and tokens[i + 1] == "(":
            dax_tokens.append(tok)
            i += 1
            continue

        # -- Unknown alpha identifier — REJECT ----------------------------
        # Wrapping it as a column ref ('Table'[CASE], 'Table'[WHEN], etc.)
        # produces broken DAX that fails at load time. Returning None tells
        # the caller to emit a placeholder measure instead.
        if tok and (tok[0].isalpha() or tok[0] == "_"):
            return None

        # -- Anything else passes through (rare) --------------------------
        dax_tokens.append(tok)
        i += 1

    return _join_tokens(dax_tokens)


# ---------------------------------------------------------------------------
# Block-style handlers
# ---------------------------------------------------------------------------

def _translate_block_case(
    tokens: List[str], start_idx: int, end_idx: int,
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]],
    parameter_refs: Optional[set] = None,
) -> Optional[Tuple[str, int]]:
    """CASE expr WHEN v1 THEN r1 [WHEN ...] [ELSE re] END
    →  SWITCH(expr, v1, r1, ..., re)
    """
    end_pos = _find_block_end(tokens, start_idx + 1, end_idx)
    if end_pos is None:
        return None
    body = tokens[start_idx + 1: end_pos]

    when_positions = [j for j, t in enumerate(body) if t.lower() == "when"]
    if not when_positions:
        return None

    expr_toks = body[:when_positions[0]]
    expr_dax = _translate_token_list(
        expr_toks, table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
    )
    if expr_dax is None:
        return None

    args = [expr_dax]
    for k, when_pos in enumerate(when_positions):
        then_pos = None
        for j in range(when_pos + 1, len(body)):
            if body[j].lower() == "then":
                then_pos = j
                break
        if then_pos is None:
            return None
        next_pos = len(body)
        if k + 1 < len(when_positions):
            next_pos = when_positions[k + 1]
        else:
            for j in range(then_pos + 1, len(body)):
                if body[j].lower() == "else":
                    next_pos = j
                    break
        when_dax = _translate_token_list(
            body[when_pos + 1: then_pos],
            table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
        )
        then_dax = _translate_token_list(
            body[then_pos + 1: next_pos],
            table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
        )
        if when_dax is None or then_dax is None:
            return None
        args.extend([when_dax, then_dax])

    last_when = when_positions[-1]
    for j in range(last_when + 1, len(body)):
        if body[j].lower() == "else":
            else_dax = _translate_token_list(
                body[j + 1:],
                table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
            )
            if else_dax is None:
                return None
            args.append(else_dax)
            break

    return (f"SWITCH({', '.join(args)})", end_pos + 1)


def _translate_block_if(
    tokens: List[str], start_idx: int, end_idx: int,
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]],
    parameter_refs: Optional[set] = None,
) -> Optional[Tuple[str, int]]:
    """IF c1 THEN r1 [ELSEIF c2 THEN r2] [ELSE re] END
    →  SWITCH(TRUE(), c1, r1, c2, r2, ..., re)
    """
    end_pos = _find_block_end(tokens, start_idx + 1, end_idx)
    if end_pos is None:
        return None
    body = tokens[start_idx + 1: end_pos]

    # Walk the body; alternating segments are conditions and results.
    args: List[str] = ["TRUE()"]
    cond_start = 0
    cond_end: Optional[int] = None  # set when we hit THEN
    result_start: Optional[int] = None
    j = 0
    while j < len(body):
        low = body[j].lower()
        if low == "then":
            cond_end = j
            result_start = j + 1
            j += 1
            continue
        if low in ("elseif", "else"):
            if cond_end is None or result_start is None:
                return None
            cond = _translate_token_list(
                body[cond_start: cond_end],
                table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
            )
            res = _translate_token_list(
                body[result_start: j],
                table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
            )
            if cond is None or res is None:
                return None
            args.extend([cond, res])
            if low == "elseif":
                cond_start = j + 1
                cond_end = None
                result_start = None
            else:  # else: the rest of body is the default value
                else_v = _translate_token_list(
                    body[j + 1:],
                    table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
                )
                if else_v is None:
                    return None
                args.append(else_v)
                return (f"SWITCH({', '.join(args)})", end_pos + 1)
            j += 1
            continue
        j += 1

    # Reached end of body without ELSE.
    if cond_end is None or result_start is None:
        return None
    cond = _translate_token_list(
        body[cond_start: cond_end],
        table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
    )
    res = _translate_token_list(
        body[result_start:],
        table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
    )
    if cond is None or res is None:
        return None
    args.extend([cond, res])
    return (f"SWITCH({', '.join(args)})", end_pos + 1)


def _find_block_end(
    tokens: List[str], start: int, end: int,
) -> Optional[int]:
    """Find the matching END for a CASE/IF block, accounting for nesting."""
    depth = 1
    j = start
    while j < end:
        low = tokens[j].lower()
        if low == "case" and (j + 1 >= end or tokens[j + 1] != "("):
            depth += 1
        elif low == "if" and (j + 1 >= end or tokens[j + 1] != "("):
            depth += 1
        elif low == "end":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _collect_paren(
    tokens: List[str], lparen_idx: int, end: int,
) -> Tuple[Optional[int], List[str]]:
    """Given tokens[lparen_idx] == '(', return (rparen_idx, inner_tokens)."""
    if tokens[lparen_idx] != "(":
        return None, []
    depth = 1
    inner: List[str] = []
    j = lparen_idx + 1
    while j < end:
        t = tokens[j]
        if t == "(":
            depth += 1
            inner.append(t)
        elif t == ")":
            depth -= 1
            if depth == 0:
                return j, inner
            inner.append(t)
        else:
            inner.append(t)
        j += 1
    return None, []


def _translate_token_list(
    tokens: List[str],
    table_name: str,
    col_aliases: Dict[str, str],
    datasource_columns: List[Dict[str, Any]],
    field_to_pbi: Optional[Dict[str, Tuple[str, str]]] = None,
    parameter_refs: Optional[set] = None,
) -> Optional[str]:
    return _translate_tokens(
        tokens, 0, len(tokens),
        table_name, col_aliases, datasource_columns, field_to_pbi, parameter_refs,
    )


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

def _tokenize(formula: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    n = len(formula)
    while i < n:
        c = formula[i]
        if c.isspace():
            i += 1
            continue
        # Single-quoted string
        if c == "'":
            j = i + 1
            while j < n and formula[j] != "'":
                j += 1
            tokens.append(formula[i:j + 1])
            i = j + 1
            continue
        # Double-quoted string
        if c == '"':
            j = i + 1
            while j < n and formula[j] != '"':
                j += 1
            tokens.append(formula[i:j + 1])
            i = j + 1
            continue
        # Field reference [Field Name]
        if c == "[":
            j = i + 1
            while j < n and formula[j] != "]":
                j += 1
            tokens.append(formula[i:j + 1])
            i = j + 1
            continue
        # Multi-char operators
        if c in "<>!=" and i + 1 < n and formula[i + 1] == "=":
            tokens.append(formula[i:i + 2])
            i += 2
            continue
        if c == "<" and i + 1 < n and formula[i + 1] == ">":
            tokens.append("<>")
            i += 2
            continue
        if c == "/" and i + 1 < n and formula[i + 1] == "/":
            while i < n and formula[i] != "\n":
                i += 1
            continue
        # Block comment ``/* ... */`` — Tableau allows multi-line block
        # comments inside formulas. Skip the entire span between the
        # opening ``/*`` and the matching ``*/``. Unterminated blocks
        # consume the rest of the formula (defensive).
        if c == "/" and i + 1 < n and formula[i + 1] == "*":
            i += 2
            while i + 1 < n and not (formula[i] == "*" and formula[i + 1] == "/"):
                i += 1
            i += 2  # past the closing '*/'
            continue
        if c in ("(", ")", ","):
            tokens.append(c)
            i += 1
            continue
        # Number
        if c.isdigit() or (c == "." and i + 1 < n and formula[i + 1].isdigit()):
            j = i
            while j < n and (formula[j].isdigit() or formula[j] == "."):
                j += 1
            tokens.append(formula[i:j])
            i = j
            continue
        # Identifier / keyword
        if c.isalpha() or c == "_":
            j = i
            while j < n and (formula[j].isalnum() or formula[j] == "_"):
                j += 1
            tokens.append(formula[i:j])
            i = j
            continue
        # Anything else (e.g. '.') as its own token
        tokens.append(c)
        i += 1
    return tokens


# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

_AGG_MAP: Dict[str, str] = {
    "sum":     "SUM",
    "avg":     "AVERAGE",
    "average": "AVERAGE",
    "count":   "COUNT",
    "cnt":     "COUNT",
    "countd":  "DISTINCTCOUNT",
    "cntd":    "DISTINCTCOUNT",
    "min":     "MIN",
    "max":     "MAX",
    "median":  "MEDIAN",
    "med":     "MEDIAN",
    "stdev":   "STDEV.P",
    "std":     "STDEV.P",
    "var":     "VAR.P",
}

# Tableau function name → DAX function name. Emitted as a bare token, so
# the following '(' and arguments are processed by the normal token loop.
_FN_PASSTHROUGH: Dict[str, str] = {
    "round":    "ROUND",
    "floor":   "FLOOR",
    "ceiling": "CEILING",
    "abs":     "ABS",
    "sqrt":    "SQRT",
    "power":   "POWER",
    "log":     "LOG",
    "ln":      "LN",
    "exp":     "EXP",
    "mod":     "MOD",
    "truncate": "TRUNC",
    "year":    "YEAR",
    "quarter": "QUARTER",
    "month":   "MONTH",
    "week":    "WEEKNUM",
    "day":     "DAY",
    "hour":    "HOUR",
    "minute":  "MINUTE",
    "second":  "SECOND",
    "date":    "DATE",
    "datediff": "DATEDIFF",
    "dateadd":  "DATEADD",
    # Tableau MAKEDATE(year, month, day) takes the same arg shape as DAX
    # DATE(year, month, day) — straight passthrough with rename.
    "makedate": "DATE",
    # Tableau MAKEDATETIME(date, time) → DAX has no direct equivalent;
    # the closest is ``date + time`` since DAX dates are floats.
    # Emit as a passthrough rename and let the user fix manually if it
    # doesn't behave as expected; better than dropping the entire calc.
    "makedatetime": "DATE",
    # Tableau MAKETIME(hour, minute, second) → DAX TIME(...). Same shape.
    "maketime": "TIME",
    "len":     "LEN",
    "upper":   "UPPER",
    "lower":   "LOWER",
    "trim":    "TRIM",
    "left":    "LEFT",
    "right":   "RIGHT",
    "mid":     "MID",
    "find":    "FIND",
    "replace": "SUBSTITUTE",
    "contains": "CONTAINSSTRING",
    "isnull":  "ISBLANK",
    # Note: 'zn' is handled separately via the dedicated ZN(expr) ->
    # IFERROR(expr, 0) branch above. Don't put it in this passthrough
    # table — IFERROR needs a 2-arg form and a bare passthrough would
    # emit invalid DAX.
}

_KW_LITERAL: Dict[str, str] = {
    "true":  "TRUE()",
    "false": "FALSE()",
    "null":  "BLANK()",
    "today": "TODAY()",
    "now":   "NOW()",
}

_LOGICAL: Dict[str, str] = {
    "and": "&&",
    "or":  "||",
    "not": "NOT",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except (ValueError, TypeError):
        return False


def _split_args(tokens: List[str]) -> List[List[str]]:
    args: List[List[str]] = []
    cur: List[str] = []
    depth = 0
    for t in tokens:
        if t == "(":
            depth += 1
            cur.append(t)
        elif t == ")":
            depth -= 1
            cur.append(t)
        elif t == "," and depth == 0:
            if cur:
                args.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        args.append(cur)
    return args


def _join_tokens(tokens: List[str]) -> str:
    """Render translated tokens, converting + to & for string concatenation."""
    out_tokens: List[str] = []
    for i, tok in enumerate(tokens):
        if tok == "+":
            left_str  = _looks_stringy(tokens, i - 1, -1)
            right_str = _looks_stringy(tokens, i + 1, 1)
            out_tokens.append("&" if (left_str or right_str) else "+")
        else:
            out_tokens.append(tok)

    out = ""
    for i, t in enumerate(out_tokens):
        if i == 0:
            out += t
            continue
        prev = out_tokens[i - 1]
        if prev == "(" or t == ")" or t == "," or prev == ",":
            out += t
        else:
            out += " " + t
    return out.strip()


def _looks_stringy(tokens: List[str], start_idx: int, direction: int) -> bool:
    i = start_idx
    while 0 <= i < len(tokens):
        t = tokens[i]
        if t in ("+", "-", "*", "/", "%", "^", "&&", "||", ",", "("):
            i += direction
            continue
        break
    if not (0 <= i < len(tokens)):
        return False
    t = tokens[i]
    if t.startswith('"') and t.endswith('"'):
        return True
    return False
