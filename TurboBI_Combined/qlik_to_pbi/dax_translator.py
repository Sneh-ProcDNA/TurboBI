"""Qlik expression -> DAX translator.

Scope is deliberately narrow. Qlik's expression language (set analysis,
$-expansion, AGGR, etc.) does not map 1:1 to DAX. We translate the
patterns that show up most often in the corpus:

  * Bare aggregations:    Sum(Field), Count(Field), Avg(Field/16)
  * Count distinct:       Count(distinct Field)        -> DISTINCTCOUNT
  * Simple arithmetic:    Sum(A) / Sum(B), A * (1 - B)
  * Set analysis:         Sum({<Flag={1}>} Field)      -> CALCULATE(SUM, FILTER)
  * Variable expansion:   $(varName)                    -> $varName (inlined)
  * Date / number literals are passed through.

Everything else is returned as a DAX comment block that loads but
evaluates to BLANK. The user keeps the original formula in the body
so they can hand-translate after open.
"""

import re
from typing import Callable, Dict, Optional

from ._logging import get_logger
from .dax_translator_v2 import ExpressionTranslator as _ExpressionTranslatorV2

_log = get_logger("DAX")


def _strip_comments(s: str) -> str:
    """Remove Qlik ``//`` line comments and ``/* */`` block comments,
    respecting string literals so a ``//`` inside a quoted value (e.g.
    ``'http://...'``) is preserved. Qlik measure bodies frequently
    carry trailing ``//`` comments that would otherwise break the
    tokenizer / regex passes."""
    out = []
    i, n = 0, len(s)
    quote = None
    while i < n:
        c = s[i]
        if quote:
            out.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_QLIK_AGG_TO_DAX = {
    "sum":   "SUM",
    "avg":   "AVERAGE",
    "min":   "MIN",
    "max":   "MAX",
    "count": "COUNTA",
    "only":  "SELECTEDVALUE",
    "first": "MIN",
    "last":  "MAX",
}

# Aggregation names the legacy simple-agg path knows how to translate.
# Used to confirm the OUTERMOST call really is an aggregation before the
# legacy path claims the expression (see ``_translate_qlik_to_dax_legacy``).
_LEGACY_AGG_NAMES = set(_QLIK_AGG_TO_DAX.keys())


# Qlik {<Field={'val'}, Other={1}>} pattern
_SET_RE = re.compile(r"\{\s*<([^<>]*)>\s*\}", re.DOTALL)
# Matches both the plain ``$(var)`` reference and the ``$(=var)``
# evaluation form Qlik uses to force immediate evaluation. Both expand
# to the variable's definition body; the optional ``=`` is consumed so
# no ``$(`` remnant survives to trip _looks_like_valid_dax.
_VAR_RE = re.compile(r"\$\(\s*=?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")
_FIELD_REF_RE = re.compile(r"\[([^\[\]\r\n]+)\]")
_AGG_CALL_RE = re.compile(
    r"\b(Sum|Avg|Min|Max|Count|Only|First|Last)\s*\(",
    re.IGNORECASE,
)


def translate_qlik_to_dax(
    expr: str,
    table_name: str,
    variable_lookup: Optional[Callable[[str], Optional[str]]] = None,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Translate a Qlik expression into a DAX expression.

    ``field_resolver(field_name)`` (optional) maps a Qlik field to the
    fully-qualified DAX reference of the column that actually OWNS it,
    e.g. ``"Region"`` -> ``"'DimGeography'[Region]"``. When supplied, a
    field reference resolves to its real home table instead of being
    pinned to ``table_name`` (the measure's home). This is what makes a
    measure like ``Sum([Sales]) / Sum([Budget])`` -- whose operands live
    on different tables -- emit correct column refs rather than
    ``'Fact'[Budget]`` for a column that isn't on ``Fact``. The resolver
    returns ``None`` for unknown fields, which fall back to
    ``table_name``.

    Pipeline:

    1. Try the legacy regex translator below. If it returns valid DAX,
       use it -- the corpus tests are calibrated to its output for the
       common patterns.
    2. If the legacy path emits a ``BLANK() /* qlik: ... */`` stub,
       retry through the v2 tokenizer-based translator
       (:mod:`qlik_to_pbi.dax_translator_v2`). v2 handles set analysis,
       If chains, Year/Month/Day, Len/Upper/Lower, Alt/Coalesce, Pick,
       Match, and other patterns the regex pipeline misses.
    3. If v2 also fails, return the legacy stub. The user keeps the
       original Qlik formula in the comment for manual rewrite.

    Stubs always load cleanly in PBI Desktop.
    """
    # Comment-strip + leading-``=`` removal + variable expansion is
    # identical for the legacy and v2 paths and is the most expensive
    # string work in the translator. Compute it ONCE here and hand the
    # prepared body to both, so the legacy->v2 fallback no longer
    # re-strips comments and re-expands variables on the same expression
    # (previously every stubbed expression paid that cost twice).
    prepared = _prepare_expr(expr, variable_lookup)

    legacy = _translate_qlik_to_dax_legacy(
        expr, table_name, variable_lookup, measure_lookup, field_resolver,
        prepared=prepared,
    )
    if not legacy.startswith("BLANK() /* qlik:"):
        return _finalize_dax(legacy, table_name)

    # Legacy gave up. Try v2 (reusing the same prepared body).
    v2 = _try_v2(
        expr, table_name, variable_lookup, measure_lookup, field_resolver,
        prepared=prepared,
    )
    if v2 is not None:
        _log.debug(f"v2 translator picked up: {expr[:60]}")
        return _finalize_dax(v2, table_name)
    return legacy


def _prepare_expr(
    expr: str,
    variable_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Shared pre-processing for both translator stages.

    Performs, in order, the exact sequence both the legacy and v2 paths
    used to run independently:

    1. Strip Qlik comments (``//`` line + ``/* */`` block) FIRST -- a
       body may lead with a ``//`` comment whose newline precedes the
       ``=`` that introduces the expression.
    2. Strip the leading ``=`` Qlik uses on label/conditional exprs.
    3. Expand ``$(var)`` / ``$(=var)`` recursively (depth 6).
    4. Strip comments again to catch comments inside expanded bodies.

    Returns the prepared expression body (``src``). The ORIGINAL ``expr``
    is preserved by the callers for the ``BLANK() /* qlik: ... */`` stub.
    """
    if not expr:
        return ""
    src = expr.strip()
    src = _strip_comments(src).strip()
    if src.startswith("="):
        src = src[1:].strip()
    src = _expand_variables(src, variable_lookup, depth=6)
    src = _strip_comments(src).strip()
    return src


def _translate_qlik_to_dax_legacy(
    expr: str,
    table_name: str,
    variable_lookup: Optional[Callable[[str], Optional[str]]] = None,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
    prepared: Optional[str] = None,
) -> str:
    """Original regex-driven translator. Preserved verbatim so the
    snapshot/corpus tests stay calibrated.

    ``prepared`` is the comment-stripped, ``=``-stripped, variable-
    expanded body (see ``_prepare_expr``). The public entry computes it
    once and passes it to both translator stages so the work isn't
    repeated. When ``None`` (direct callers / tests), it's computed here
    -- the exact same sequence the function used inline before.
    """
    if not expr:
        return "BLANK()"

    # Comment strip + leading-``=`` removal + variable expansion. Shared
    # with the v2 path via ``_prepare_expr`` so it runs once per
    # expression rather than once per stage. The ORIGINAL ``expr`` is
    # still used for the stub comment below.
    src = prepared if prepared is not None else _prepare_expr(expr, variable_lookup)

    # If after expansion the expression is just a single $(missingVar)
    # placeholder, fall through to the stub path.
    if not src or _VAR_RE.fullmatch(src or "") is not None:
        return _stub(expr)

    # Set analysis. Two failure paths feed `_stub`:
    #   (a) `_SET_RE` matches but the body shape is non-trivial — the
    #       translator raises `_UnsupportedSet`.
    #   (b) `_SET_RE` doesn't match (the set-block contains a `>` or `<`
    #       inside a string literal so the no-angle-bracket character
    #       class breaks early). In that case the code falls through to
    #       the `_AGG_CALL_RE` arm, but we detect the un-translated `{<`
    #       / `>}` in the final validation pass and stub it.
    set_match = _SET_RE.search(src)
    if set_match:
        try:
            translated = _translate_set_analysis(src, table_name, field_resolver)
            return translated if _looks_like_valid_dax(translated) else _stub(expr)
        except _UnsupportedSet:
            return _stub(expr)

    # Bare aggregate function. Translate the head; rewrite field refs.
    # Only when the OUTERMOST call is itself an aggregation. A Qlik
    # wrapper like `Date(Max(Date), 'MM/DD/YYYY')` or `If(Sum(x)>0, ...)`
    # also contains an `Agg(` token, but the legacy path would keep the
    # outer `Date(` / `If(` as a literal DAX call and emit broken DAX --
    # `DATE(<date>, "MM/DD/YYYY")` makes PBI complain "Too few arguments
    # ... the minimum argument count for the function is 3". Defer those
    # to v2, which strips Qlik's Date/Num/Time formatting wrappers (the
    # format belongs on the measure, not the expression) and parses
    # If/Pick/Alt correctly.
    _outer = re.match(r"\s*([A-Za-z_]\w*)\s*\(", src)
    _outer_is_nonagg = bool(_outer) and _outer.group(1).lower() not in _LEGACY_AGG_NAMES
    if _AGG_CALL_RE.search(src) and not _outer_is_nonagg:
        translated = _translate_simple_agg(src, table_name, measure_lookup, field_resolver)
        return translated if _looks_like_valid_dax(translated) else _stub(expr)

    # Pure number or string literal.
    if re.fullmatch(r"-?\d+(\.\d+)?", src):
        return src
    if re.fullmatch(r"'[^']*'", src) or re.fullmatch(r'"[^"]*"', src):
        return src.replace("'", '"')

    # Bare ``[measureName]`` after variable expansion -- common when
    # a master measure is just ``$(varX)`` and varX was materialised.
    if re.fullmatch(r"\s*\[[^\[\]]+\]\s*", src):
        inner = src.strip()[1:-1].strip()
        if measure_lookup and measure_lookup(inner):
            return f"[{inner}]"

    # Arithmetic with bare fields - wrap each bare ref in SUM() so the
    # expression at least evaluates. Mirrors PBI's default-aggregation
    # convention for naked numeric column references.
    if re.search(r"[+\-*/]", src):
        rewritten = _rewrite_field_refs_with_sum(
            src, table_name, measure_lookup, field_resolver,
        )
        if rewritten and _looks_like_valid_dax(rewritten):
            return rewritten

    return _stub(expr)


def _try_v2(
    expr: str,
    table_name: str,
    variable_lookup: Optional[Callable[[str], Optional[str]]] = None,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
    prepared: Optional[str] = None,
) -> Optional[str]:
    """Run the v2 translator on the raw Qlik expression.

    Returns the v2 DAX iff the parse succeeded and produced output that
    survives ``_looks_like_valid_dax``. Returns ``None`` otherwise so
    the caller can fall back to the legacy stub.

    The v2 field resolver binds bare and bracketed field names to the
    current home table -- UNLESS ``measure_lookup`` recognises the
    name, in which case it emits the bare measure-reference form
    ``[Name]`` (DAX's measure-ref syntax). This lets variable
    materialisation (model._materialize_variables_as_measures) expose
    each variable as a real measure and have other expressions
    reference it cleanly instead of inlining the body every time.

    ``prepared`` is the shared comment-stripped / ``=``-stripped /
    variable-expanded body (``_prepare_expr``); when supplied by the
    public entry it's reused so v2 doesn't redo that work after the
    legacy path already did it. When ``None``, it's computed here -- the
    same sequence v2 ran inline before, matching the legacy ordering.
    """
    if not expr:
        return None
    src = prepared if prepared is not None else _prepare_expr(expr, variable_lookup)

    def resolve(field_name: str) -> str:
        clean = (field_name or "").strip().strip("[]")
        if not clean:
            return "BLANK()"
        if measure_lookup and measure_lookup(clean):
            return f"[{clean}]"
        if field_resolver:
            full = field_resolver(clean)
            if full:
                return full
        return f"'{table_name}'[{clean}]"

    try:
        result = _ExpressionTranslatorV2(resolve).translate(src)
    except Exception as exc:
        _log.debug(f"v2 translator raised: {exc}")
        return None
    if not result.success:
        return None
    if not _looks_like_valid_dax(result.dax):
        return None
    return result.dax


_QLIK_KNOWN_FUNCS = {
    # These names appear in Qlik formulas but have no direct DAX equivalent.
    # When the translated body still references one, fall back to the stub.
    "YEARSTART", "ADDMONTHS", "ABOVE", "BELOW", "AGGR", "RANGESUM",
    "RANGEMIN", "RANGEMAX", "GETOBJECTFIELD", "GETPOSSIBLECOUNT",
    "ONLY", "ROWNO", "MINSTRING", "MAXSTRING", "REPLACE", "COLORMIX1",
    "CLASS", "RGB", "WHITE", "BLACK", "PEEK", "NUM",
    "FRACTILE", "FIRSTSORTEDVALUE", "MATCH", "WILDMATCH", "AUTOGENERATE",
    "INTERVAL", "SUBSTRINGCOUNT", "TIMESTAMP",
}


def _looks_like_valid_dax(translated: str) -> bool:
    """Reject output that still contains Qlik-only syntax.

    The DAX engine rejects any of these tokens at compile time:

    * `{<` / `>}`  - Qlik set-analysis block.
    * Stray `{` or `}` outside of an explicit literal (we never emit them).
    * `$(`         - un-expanded Qlik variable.
    * Function names not in DAX (Yearstart, Addmonths, Aggr, ...).

    Returning False here promotes the measure to `_stub`, which always
    loads. Better to lose a translation than crash the entire report.
    """
    if not translated:
        return False
    if "{<" in translated or ">}" in translated:
        return False
    if "$(" in translated:
        return False
    # Curly braces are legitimate ONLY as DAX table constructors in an
    # ``IN {a, b, c}`` clause (multi-value set analysis, Match/WildMatch).
    # First blank out double-quoted string literals so a brace inside a
    # literal (a Qlik label like ``"{special}"``) doesn't trip the check.
    # Then strip ``IN {...}`` constructors; any surviving brace is a Qlik
    # set-analysis block the legacy path failed to translate (``{$}``
    # current selection, ``{1}`` all-data, or a ``{<...>}`` remnant) that
    # would otherwise pass as garbage like ``SUMX('T', {$} 'T'[X])``.
    # Rejecting routes it to v2 / the stub instead.
    no_str = re.sub(r'"(?:[^"\\]|\\.)*"', " ", translated)
    without_in = re.sub(r"\bIN\s*\{[^{}]*\}", " ", no_str, flags=re.IGNORECASE)
    if "{" in without_in or "}" in without_in:
        return False
    upper = translated.upper()
    for fn in _QLIK_KNOWN_FUNCS:
        if re.search(r"\b" + re.escape(fn) + r"\s*\(", upper):
            return False
    # Balanced parens check - a missing close-paren is a compile failure.
    if translated.count("(") != translated.count(")"):
        return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _UnsupportedSet(Exception):
    pass


def _stub(original: str) -> str:
    flat = (original or "").replace("\r", " ").replace("\n", " ")
    flat = re.sub(r"\s+", " ", flat).strip()
    if len(flat) > 240:
        flat = flat[:240] + "..."
    flat = flat.replace("*/", "* /")
    return f"BLANK() /* qlik: {flat} */"


def _strip_dollar_eval(s: str) -> str:
    """Rewrite Qlik's ``$(=<expr>)`` immediate-evaluation form to ``(<expr>)``
    so the inner Qlik expression survives to the translator.

    Only the GENERAL form (inner is a function call / operator expression, e.g.
    ``$(=MakeDate(X,1,1))``) is rewritten. The bare-identifier form
    ``$(=var)`` is left untouched -- it's a variable reference that
    :data:`_VAR_RE` / ``_expand_variables`` expands to the variable's body. Was
    previously unhandled, so a surviving ``$(`` stubbed the whole measure (this
    is what blanked the date-range financial measures)."""
    if "$(=" not in s:
        return s
    i = s.find("$(=")
    while i != -1:
        j = i + 3
        depth = 1
        while j < len(s) and depth > 0:
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
            j += 1
        if depth != 0:           # unbalanced -> leave the rest alone
            break
        inner = s[i + 3:j - 1]
        if re.fullmatch(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*", inner):
            # Bare ``$(=var)`` -- let the variable expander handle it.
            i = s.find("$(=", i + 3)
            continue
        s = s[:i] + "(" + inner + ")" + s[j:]
        i = s.find("$(=")
    return s


def _is_search_string_value(v: str) -> bool:
    """True when a (comment/``=``-stripped) value is a Qlik search/range
    string -- after dropping one layer of surrounding quotes it begins with a
    comparison operator (``>``/``<``), e.g. ``'>=2020<=2021'``. Such a value is
    Qlik text substituted verbatim inside a set modifier ``Field={"$(var)"}``,
    NOT an arithmetic value -- so it must not be paren-wrapped on expansion."""
    if not v:
        return False
    s = v.strip()
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return s[:1] in ("<", ">")


def _expand_variables(
    src: str,
    lookup: Optional[Callable[[str], Optional[str]]],
    depth: int,
) -> str:
    if not lookup or depth <= 0:
        return src
    changed = True
    out = src
    while changed and depth > 0:
        changed = False
        depth -= 1

        # ``$(=<expr>)`` evaluation form first (a search-string variable body
        # is full of these); rewriting to ``(<expr>)`` exposes any inner
        # ``$(var)`` for the substitution pass below.
        stripped = _strip_dollar_eval(out)
        if stripped != out:
            changed = True
            out = stripped

        def repl(m: re.Match) -> str:
            name = m.group(1)
            val = lookup(name)
            if val is None:
                return m.group(0)
            nonlocal changed
            changed = True
            v = val.strip()
            if v.startswith("="):
                v = v[1:].strip()
            # Strip comments from the body BEFORE wrapping in parens --
            # a trailing `//` comment in the variable would otherwise
            # swallow the closing `)` we append (they end up on the
            # same line), truncating the expression.
            v = _strip_comments(v).strip()
            # A search/range-string macro (``vD_YTD = '>=...<=...'``) is Qlik
            # text substituted verbatim inside a set modifier -- its quotes are
            # delimiters, not content, and paren-wrapping it would break the
            # range parser. Substitute the unquoted body as-is.
            if _is_search_string_value(v):
                if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
                    v = v[1:-1]
                return v
            return f"({v})"

        out = _VAR_RE.sub(repl, out)
    return out


def _qualify_field(
    name: str,
    table_name: str,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Qualify a bare/bracketed reference.

    Resolution order:
      1. ``measure_lookup`` recognises the name as a DAX measure ->
         bare measure-ref ``[Name]`` (no table prefix). Introduced by
         variable materialisation; must not be re-qualified as a column.
      2. ``field_resolver`` maps the field to the column on its OWNING
         table -> e.g. ``'DimGeography'[Region]``. This is what lets an
         expression reference columns from several tables correctly
         instead of pinning every field to ``table_name``.
      3. Fallback: ``'table_name'[name]`` (the measure's home table)."""
    n = name.strip().strip("[]")
    if measure_lookup and measure_lookup(n):
        return f"[{n}]"
    if field_resolver:
        full = field_resolver(n)
        if full:
            return full
    return f"'{table_name}'[{n}]"


def _rewrite_field_refs_with_sum(
    src: str,
    table_name: str,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Replace bare/bracketed field references with SUM(Table[Field]).

    Cheap rewrite for arithmetic expressions where each operand is a
    field. We do not try to be perfect; if any token survives that
    looks like a Qlik function we don't recognise, return None and let
    the caller fall back to the stub.

    Bails (returns None) on string concatenation (``&``) or single-
    quoted string literals: this helper only handles NUMERIC arithmetic
    on bracketed fields. Those other shapes are row-level / string
    expressions the v2 translator handles correctly -- and a stray ``-``
    inside a literal like ``'-wk'`` must not trick us into returning the
    raw expression as if it were valid DAX.
    """
    if "&" in src or "'" in src:
        return None
    def _wrap(m: re.Match) -> str:
        name = m.group(1).strip()
        if measure_lookup and measure_lookup(name):
            # Bare measure ref is already scalar -- no SUM wrap.
            return f"[{name}]"
        return f"SUM({_qualify_field(name, table_name, measure_lookup, field_resolver)})"
    out = _FIELD_REF_RE.sub(_wrap, src)
    # Drop anything still looking like a Qlik call we don't translate.
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", out):
        return None
    # If no bracketed field was rewritten and bare identifiers remain,
    # this isn't a pure numeric-arithmetic-on-fields expression we can
    # safely claim -- let v2 handle it. Bare measure refs are OK too.
    if "SUM(" not in out and "[" not in out and re.search(r"[A-Za-z_]", out):
        return None
    return out


def _qlik_strings_to_dax(s: str) -> str:
    """Convert Qlik single-quoted string literals to DAX double-quoted
    literals. Qlik uses ``'...'`` for strings and ``[...]`` for field
    names -- it never single-quotes identifiers -- so every ``'...'``
    in a Qlik source fragment is a string literal. DAX requires
    double quotes for strings (single quotes denote table names), so
    ``Goal_Flag = 'Met'`` must become ``... = "Met"`` or it parses as a
    reference to a table named ``Met`` and errors at load.

    Safe to run ONLY before DAX table-name qualifiers (``'Table'[Col]``)
    are introduced -- i.e. on raw Qlik fragments, not on already-emitted
    DAX. Doubled ``''`` Qlik escapes collapse to a single quote inside
    the resulting double-quoted literal."""
    def repl(m: re.Match) -> str:
        inner = m.group(1).replace("''", "'").replace('"', '""')
        return f'"{inner}"'
    return re.sub(r"'((?:[^']|'')*)'", repl, s)


def _translate_simple_agg(
    src: str,
    table_name: str,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Translate a top-level Sum/Avg/Min/Max/Count(...) expression."""
    # Qlik string literals -> DAX double-quoted. This runs before any
    # 'Table'[Col] qualifier is added, so all single quotes here are
    # unambiguously Qlik strings (the set-analysis path is handled
    # separately and never reaches here).
    src = _qlik_strings_to_dax(src)

    def repl_agg(m: re.Match) -> str:
        fn = m.group(1).lower()
        return _QLIK_AGG_TO_DAX.get(fn, fn.upper()) + "("

    out = _AGG_CALL_RE.sub(repl_agg, src)

    # COUNT(distinct Field) -> DISTINCTCOUNT(Field)
    out = re.sub(
        r"COUNTA\(\s*distinct\s+",
        "DISTINCTCOUNT(",
        out,
        flags=re.IGNORECASE,
    )

    out = _FIELD_REF_RE.sub(
        lambda m: _qualify_field(m.group(1), table_name, measure_lookup, field_resolver),
        out,
    )

    # Bare identifiers that look like field names - only do this for
    # identifiers that appear inside the function-arg list, not for DAX
    # function names we just emitted.
    out = _rewrite_bare_identifiers(out, table_name, measure_lookup, field_resolver)

    # Promote `AGG(<expr-with-operator>)` to the iterator form
    # `AGGX('<table>', <expr>)`. DAX SUM/AVG/MIN/MAX accept only a
    # bare column reference; the X variants accept any scalar.
    out = _promote_agg_to_iterator(out, table_name)

    if "'" + table_name + "'" not in out and "[" not in out:
        return _stub(src)
    return out


_AGG_TO_ITERATOR = {
    "SUM":     "SUMX",
    "AVERAGE": "AVERAGEX",
    "MIN":     "MINX",
    "MAX":     "MAXX",
    "COUNTA":  "COUNTAX",
}


def _promote_agg_to_iterator(src: str, table_name: str) -> str:
    """Rewrite `AGG(<expr>)` to `AGGX('<table>', <expr>)` when the body
    is an expression, not a single column ref.

    DAX validators reject `AVERAGE([Col]/16)` at compile because AVERAGE
    only accepts a column. The iterator form accepts the same expression.
    Leaves `AGG(<col>)` untouched - those are valid as-is and switching
    them would change the storage-engine optimisation envelope.
    """
    out_parts: list[str] = []
    i = 0
    n = len(src)
    pattern = re.compile(
        r"\b(SUM|AVERAGE|MIN|MAX|COUNTA)\(", re.IGNORECASE,
    )
    while i < n:
        m = pattern.search(src, i)
        if not m:
            out_parts.append(src[i:])
            break
        out_parts.append(src[i:m.start()])
        agg = m.group(1).upper()
        # Find the matching close paren.
        depth = 1
        j = m.end()
        while j < n and depth > 0:
            ch = src[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        if depth != 0:
            # Unbalanced - emit the rest as-is and let the validator
            # reject (translate_qlik_to_dax will catch this and stub).
            out_parts.append(src[m.start():])
            break
        body = src[m.end():j - 1]
        body_stripped = body.strip()
        # If body is a single qualified column ref ('T'[C]) or a bare
        # column ref, leave it alone.
        if re.fullmatch(r"'[^']+'\[[^\[\]]+\]", body_stripped) or re.fullmatch(
            r"\[[^\[\]]+\]", body_stripped,
        ):
            out_parts.append(src[m.start():j])
        else:
            new_fn = _AGG_TO_ITERATOR.get(agg, agg + "X")
            # Iterate the table the body's columns actually live on
            # (e.g. v2 set-analysis bodies reference the fact table even
            # when the measure's home is elsewhere); fall back to the
            # measure's home table when the body has no qualified ref.
            iter_table = _table_of_first_ref(body_stripped) or table_name
            out_parts.append(f"{new_fn}('{iter_table}', {body_stripped})")
        i = j
    return "".join(out_parts)


def _table_of_first_ref(expr: str) -> Optional[str]:
    """Table name of the first ``'Table'[Col]`` reference in ``expr``,
    or None when it carries no qualified column reference."""
    m = re.search(r"'([^']+)'\[", expr)
    return m.group(1) if m else None


def _is_bare_column_ref(expr: str) -> bool:
    """True when ``expr`` is a single column reference -- ``'T'[C]`` or
    ``[C]`` -- the only argument DAX SUM / AVERAGE / DISTINCTCOUNT take."""
    e = expr.strip()
    return bool(
        re.fullmatch(r"'[^']+'\[[^\[\]]+\]", e)
        or re.fullmatch(r"\[[^\[\]]+\]", e)
    )


def _paren_span(s: str, open_idx: int) -> int:
    """Index just past the ``)`` matching the ``(`` at ``open_idx``
    (returns len(s)+1-ish past the end when unbalanced)."""
    depth, j, n = 1, open_idx + 1, len(s)
    while j < n and depth:
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
        j += 1
    return j


def _split_top_commas(s: str) -> list:
    parts: list = []
    depth = 0
    cur: list = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


_COUNT_OF_RE = re.compile(r"\b(DISTINCTCOUNT|COUNT)\s*\(", re.IGNORECASE)


def _fix_count_of_if(src: str) -> str:
    """Rewrite ``COUNT``/``DISTINCTCOUNT(IF(cond, col[, else]))`` to
    ``CALCULATE(<count>(col), FILTER('table', cond))``.

    Qlik's ``Count(distinct If(cond, Field))`` counts ``Field`` only on
    rows where ``cond`` holds. The literal ``DISTINCTCOUNT(IF(...))`` is
    rejected by DAX at query time ("... only accepts a column
    reference"). Only the ``IF``-with-a-bare-column-value shape is
    rewritten; any other non-column argument is left untouched (so the
    measure still loads -- it just can't be auto-fixed)."""
    i = 0
    while True:
        m = _COUNT_OF_RE.search(src, i)
        if not m:
            return src
        cnt = m.group(1).upper()
        open_idx = m.end() - 1
        end = _paren_span(src, open_idx)
        if end > len(src):
            return src
        arg = src[open_idx + 1:end - 1].strip()
        rewritten = None
        if re.match(r"(?i)^IF\s*\(", arg):
            if_open = arg.index("(")
            if_end = _paren_span(arg, if_open)
            if if_end == len(arg):  # the IF spans the entire argument
                if_args = _split_top_commas(arg[if_open + 1:if_end - 1])
                if len(if_args) >= 2:
                    cond = if_args[0].strip()
                    col = if_args[1].strip()
                    if _is_bare_column_ref(col):
                        tbl = _table_of_first_ref(col) or _table_of_first_ref(cond)
                        if tbl:
                            rewritten = (
                                f"CALCULATE({cnt}({col}), FILTER('{tbl}', {cond}))"
                            )
        if rewritten is not None:
            src = src[:m.start()] + rewritten + src[end:]
            i = m.start() + len(rewritten)
        else:
            i = end


def _finalize_dax(dax: str, table_name: str) -> str:
    """Normalisation applied to the chosen translation (legacy OR v2) so
    an aggregation over an EXPRESSION emits valid DAX no matter which
    stage produced it:

    * ``SUM``/``AVERAGE``/``MIN``/``MAX``/``COUNTA`` of an expression ->
      iterator form (``SUMX('table', expr)`` ...). The legacy stage does
      this internally already; re-running is idempotent and ALSO covers
      v2 output (set-analysis ``CALCULATE(SUM(a * b), ...)`` was the gap
      that produced "The SUM function only accepts a column reference").
    * ``COUNT``/``DISTINCTCOUNT(IF(cond, col))`` -> ``CALCULATE`` form.
    """
    if not dax or dax.startswith("BLANK() /* qlik:"):
        return dax
    dax = _promote_agg_to_iterator(dax, table_name)
    dax = _fix_count_of_if(dax)
    return dax


_DAX_FUNCS = {
    "SUM", "AVERAGE", "MIN", "MAX", "COUNTA", "COUNT", "DISTINCTCOUNT",
    "SELECTEDVALUE", "CALCULATE", "FILTER", "ALL", "BLANK", "TRIM", "IF",
    "DIVIDE", "RELATED", "NOT", "AND", "OR",
}


def _rewrite_bare_identifiers(
    src: str,
    table_name: str,
    measure_lookup: Optional[Callable[[str], Optional[str]]] = None,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Wrap bare numeric identifiers in `'Table'[Field]`.

    Skip anything already part of a qualified reference (between single
    quotes or inside the `[...]` portion of a qualified ref) and any
    double-quoted string literal. The token walk respects those zones
    explicitly so we don't double-qualify a field that's already been
    wrapped by `_FIELD_REF_RE` -- or mangle a string literal like
    ``"Met"`` into ``"'Table'[Met]"``.
    """
    out_parts: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        # Skip table name 'Foo' (single-quoted region).
        if c == "'":
            end = src.find("'", i + 1)
            if end == -1:
                out_parts.append(src[i:])
                break
            out_parts.append(src[i:end + 1])
            i = end + 1
            continue
        # Skip double-quoted string literal "Foo".
        if c == '"':
            end = src.find('"', i + 1)
            if end == -1:
                out_parts.append(src[i:])
                break
            out_parts.append(src[i:end + 1])
            i = end + 1
            continue
        # Skip column body `[Foo Bar]`.
        if c == "[":
            end = src.find("]", i + 1)
            if end == -1:
                out_parts.append(src[i:])
                break
            out_parts.append(src[i:end + 1])
            i = end + 1
            continue
        # Identifier? Greedy-match while alphanumeric/underscore.
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            ident = src[i:j]
            # Function call? Pass through.
            k = j
            while k < n and src[k] == " ":
                k += 1
            if k < n and src[k] == "(":
                out_parts.append(ident)
                i = j
                continue
            if ident.upper() in _DAX_FUNCS:
                out_parts.append(ident)
                i = j
                continue
            out_parts.append(_qualify_field(ident, table_name, measure_lookup, field_resolver))
            i = j
            continue
        out_parts.append(c)
        i += 1
    return "".join(out_parts)


# Recognise the simplest set-analysis filter: <Field={'val'}> or <Field={1}>,
# optionally a list of such pairs separated by commas. Field names may
# start with a digit when bracketed (e.g. [30-day Readmission]) so the
# bracketed-and-bare forms are matched as two alternations.
_SET_PAIR_RE = re.compile(
    r"(?:\[([^\[\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*=\s*\{\s*('([^']*)'|(-?\d+(?:\.\d+)?))\s*\}",
)


def _translate_set_analysis(
    src: str,
    table_name: str,
    field_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Handle a lone ``Sum({<F1={'v'}, F2={1}>} Field)``.

    This path emits a single ``CALCULATE(<agg>(<field>), <filters>)``
    with per-field boolean filters -- each field (the measured operand
    and every set-filter field) resolves through ``field_resolver`` to
    the column on its OWNING table, so a set like ``{<DimRegion={'EU'}>}``
    filtering a fact aggregation lands on ``'DimRegion'[Region]`` rather
    than a non-existent column on the fact table. The boolean-filter form
    (``CALCULATE(agg, 'T'[F] = v)``) also matches Qlik's set semantics
    more closely than ``FILTER(ALL('T'), ...)`` -- it overrides only the
    named fields' selections instead of wiping every filter on the table.
    It can represent ONLY one aggregation over one set block with nothing
    else around it. Compound shapes -- a ratio of two
    set-analysis aggregations (``Sum({s1} A)/Sum({s2} B)``), trailing
    arithmetic (``Sum({s} A) * 100``), or a DISTINCT/TOTAL qualifier --
    raise :class:`_UnsupportedSet` so the caller stubs and the v2
    tokenizer (which walks the full arithmetic tree) takes over. Before
    these guards were added, such expressions silently collapsed to just
    their first aggregation, dropping the denominator entirely.
    """
    match = _SET_RE.search(src)
    if not match:
        raise _UnsupportedSet()

    inside = match.group(1).strip()
    pairs = []
    for m in _SET_PAIR_RE.finditer(inside):
        field = (m.group(1) or m.group(2) or "").strip()
        if not field:
            continue
        if m.group(4) is not None:
            value_literal = '"' + m.group(4).replace('"', '""') + '"'
        else:
            value_literal = m.group(5)
        pairs.append((field, value_literal))

    if not pairs:
        raise _UnsupportedSet()

    # The aggregator wrapping the set analysis - we only handle one.
    head_match = _AGG_CALL_RE.search(src)
    if not head_match:
        raise _UnsupportedSet()

    fn = _QLIK_AGG_TO_DAX.get(head_match.group(1).lower())
    if not fn:
        raise _UnsupportedSet()

    # DISTINCT / TOTAL change the aggregation's meaning (distinct count,
    # ignore-context). This single-CALCULATE path can neither encode them
    # nor even parse past them (the qualifier is mis-read as the measured
    # field), so defer to v2. Blank out bracketed field names first so a
    # field such as ``[Total Cost]`` can't trip the check.
    if re.search(r"\b(distinct|total)\b", _FIELD_REF_RE.sub(" ", src), re.IGNORECASE):
        raise _UnsupportedSet()

    # Pull the trailing field reference after the closing `}` of the set.
    raw_tail = src[match.end():]
    # Strip the immediate `}` -> something like `Admitted)` or `Field)`
    strip_len = len(raw_tail) - len(raw_tail.lstrip("} "))
    tail = raw_tail[strip_len:]
    body_match = re.match(r"\s*\[?([A-Za-z_][A-Za-z0-9_ \-]*)\]?\s*\)", tail)
    if not body_match:
        raise _UnsupportedSet()

    # Faithfulness guard: the matched ``Agg({set} field)`` must span the
    # whole expression. Any content before the aggregator or after its
    # close paren (a division denominator, a second aggregation, trailing
    # arithmetic) would be silently dropped by the single CALCULATE we
    # emit here, so hand the full expression to v2 instead.
    agg_close = match.end() + strip_len + body_match.end()
    if src[:head_match.start()].strip() or src[agg_close:].strip():
        raise _UnsupportedSet()

    measured_field = body_match.group(1).strip()
    measured_ref = _qualify_field(measured_field, table_name, None, field_resolver)
    filter_parts = [
        f"{_qualify_field(f, table_name, None, field_resolver)} = {v}"
        for f, v in pairs
    ]
    return f"CALCULATE({fn}({measured_ref}), {', '.join(filter_parts)})"
