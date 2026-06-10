"""Tokenizer + recursive-descent Qlik-expression -> DAX translator.

Merged in from a sibling project. Where :mod:`qlik_to_pbi.dax_translator`
is regex-driven and narrow, this module parses the expression into a
token stream and walks an actual grammar. The result handles:

* Nested aggregations / arithmetic sub-expressions
* Multi-field, multi-operator set analysis
  (``Sum({<Year={2024}, Region={'EU','US'}>} Sales)``)
* The ``{1}`` (all-data) and ``{$}`` (current selection) set blocks
* ``If(cond, true, false)`` with recursive args
* ``Only`` / ``Only({...})`` -> ``SELECTEDVALUE``
* ``Num(expr, format)`` / ``Date(expr, format)`` -> ``expr`` (format
  belongs on the measure)
* Date-part scalars: ``Year``, ``Month``, ``Day``, ``Hour``, ``Minute``,
  ``Second``
* Text scalars: ``Len``, ``Upper``, ``Lower``, ``Trim``, ``LTrim``, ``RTrim``
* Math scalars: ``Abs``, ``Ceil``, ``Floor``, ``Round``, ``Sqrt``, ``Log``,
  ``Log10``, ``Exp``, ``Sign``
* Predicates: ``IsNull``, ``IsNum``, ``IsText``
* ``Alt`` / ``Coalesce`` -> nested ``IF(ISBLANK(...))`` chains
* ``Concat(a, b)`` -> ``a & b``
* ``Pick(idx, a, b, c)`` -> nested ``IF``
* ``Match(field, v1, v2)`` -> ``IF(field IN {v1, v2}, 1, 0)``

It is exposed as a **secondary translator** so the existing regex-driven
:mod:`qlik_to_pbi.dax_translator` keeps its precedence on patterns it
already gets right (the corpus tests are calibrated against it).
``dax_translator.translate_qlik_to_dax`` calls this module ONLY when
its own pipeline would emit a stub -- so a v2 success replaces an
otherwise-broken stub, but a v2 failure leaves the stub in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class TranslationResult:
    dax: str
    success: bool
    notes: List[str] = field(default_factory=list)


# Qlik aggregator -> DAX aggregator
AGG_MAP = {
    "sum":    "SUM",
    "avg":    "AVERAGE",
    "min":    "MIN",
    "max":    "MAX",
    "count":  "COUNT",
    "only":   "SELECTEDVALUE",
    "median": "MEDIAN",
    "stdev":  "STDEV.P",
    "stdevp": "STDEV.P",
    "mode":     None,  # no direct DAX equivalent
    "npv":      None,
    "fractile": "PERCENTILE.INC",
}

# 1-arg scalar functions
SCALAR_1_MAP = {
    # Date components
    "year":     "YEAR",
    "month":    "MONTH",
    "day":      "DAY",
    "hour":     "HOUR",
    "minute":   "MINUTE",
    "second":   "SECOND",
    "week":     "WEEKNUM",
    "weekday":  "WEEKDAY",
    "quarter":  "QUARTER",
    # NOTE: the Qlik date pivots (Yearstart/Monthstart/Quarterstart and
    # their *end variants) are SCALAR -- they take a date value and
    # return a date value. DAX's STARTOFYEAR / STARTOFMONTH / ... are
    # column-only time-intelligence functions ("The first argument to
    # 'STARTOFYEAR' must specify a column."), so they are handled
    # separately via _DATE_PIVOT_SCALAR in _parse_function, which emits
    # the scalar DATE(...) form instead.
    # String
    "len":      "LEN",
    "upper":    "UPPER",
    "lower":    "LOWER",
    "trim":     "TRIM",
    "ltrim":    "LTRIM",
    "rtrim":    "RTRIM",
    "capitalize": "PROPER",
    "evaluate": None,        # passthrough; the arg is already a number
    # Numeric
    "abs":      "ABS",
    "ceil":     "CEILING",
    "floor":    "FLOOR",
    "round":    "ROUND",
    "sqrt":     "SQRT",
    "sqr":      None,        # Qlik sqr(x) = x*x; handled inline below
    "log":      "LN",        # Qlik log() is natural log
    "log10":    "LOG10",
    "exp":      "EXP",
    "sign":     "SIGN",
    "frac":     None,        # x - INT(x)
    "fabs":     "ABS",
    "even":     None,
    "odd":      None,
    # Type tests
    "isnull":   "ISBLANK",
    "isnum":    "ISNUMBER",
    "istext":   "ISTEXT",
    "today":    "TODAY",     # 0-arg in Qlik too but tokenizer treats it as fn
    "now":      "NOW",
}


# Multi-arg scalar functions: variable arity, special handling.
# Each handler takes (args: List[str]) and returns DAX text.
SCALAR_N_MAP = {
    # Strings
    "left":     lambda a: f"LEFT({a[0]}, {a[1]})"            if len(a) >= 2 else f"LEFT({a[0]}, 1)",
    "right":    lambda a: f"RIGHT({a[0]}, {a[1]})"           if len(a) >= 2 else f"RIGHT({a[0]}, 1)",
    "mid":      lambda a: (f"MID({a[0]}, {a[1]}, {a[2]})"    if len(a) >= 3
                            else f"MID({a[0]}, {a[1]}, 99999)" if len(a) >= 2
                            else f"{a[0]}"),
    "replace":  lambda a: f"SUBSTITUTE({a[0]}, {a[1]}, {a[2]})" if len(a) >= 3 else f"{a[0]}",
    "substringcount": lambda a: (f"(LEN({a[0]}) - LEN(SUBSTITUTE({a[0]}, {a[1]}, \"\"))) / LEN({a[1]})"
                                  if len(a) >= 2 else "0"),
    "index":    lambda a: f"FIND({a[1]}, {a[0]}, 1, 0)"      if len(a) >= 2 else "0",
    "text":     lambda a: f"FORMAT({a[0]}, {a[1]})"          if len(a) >= 2 else f"FORMAT({a[0]}, \"General\")",
    "subfield": lambda a: _subfield_dax(a),
    # Dual(displayText, sortValue): Qlik pairs a display string with a
    # numeric. A DAX measure returns a single value, and these are
    # almost always used for their *display* (status arrows, custom
    # sort labels), so keep the display arg.
    "dual":     lambda a: a[0] if a else "BLANK()",
    # Numeric
    "pow":      lambda a: f"POWER({a[0]}, {a[1]})"           if len(a) >= 2 else f"{a[0]}",
    "div":      lambda a: f"DIVIDE({a[0]}, {a[1]}, 0)"       if len(a) >= 2 else f"{a[0]}",
    "mod":      lambda a: f"MOD({a[0]}, {a[1]})"             if len(a) >= 2 else f"{a[0]}",
    "fmod":     lambda a: f"MOD({a[0]}, {a[1]})"             if len(a) >= 2 else f"{a[0]}",
    # Date arithmetic
    "addmonths":      lambda a: f"EDATE({a[0]}, {a[1]})"           if len(a) >= 2 else f"{a[0]}",
    "addyears":       lambda a: f"EDATE({a[0]}, {a[1]} * 12)"      if len(a) >= 2 else f"{a[0]}",
    "adddays":        lambda a: f"({a[0]} + {a[1]})"               if len(a) >= 2 else f"{a[0]}",
    "monthsbetween":  lambda a: f"DATEDIFF({a[1]}, {a[0]}, MONTH)" if len(a) >= 2 else "0",
    "age":            lambda a: f"DATEDIFF({a[1]}, {a[0]}, YEAR)"  if len(a) >= 2 else "0",
    "makedate":       lambda a: (f"DATE({a[0]}, {a[1] if len(a) >= 2 else '1'}, "
                                  f"{a[2] if len(a) >= 3 else '1'})"),
    # Date-name formatting (Qlik returns dual text; the display string is
    # what a dimension shows). Row-level -> usable in a calculated column.
    "monthname":      lambda a: f'FORMAT({a[0]}, "MMM yyyy")' if a else "BLANK()",
    "dayname":        lambda a: f'FORMAT({a[0]}, "ddd")'      if a else "BLANK()",
    # Comparisons / logical
    "max":      lambda a: "MAX(" + ", ".join(a) + ")" if len(a) >= 2 else (a[0] if a else "BLANK()"),
    "min":      lambda a: "MIN(" + ", ".join(a) + ")" if len(a) >= 2 else (a[0] if a else "BLANK()"),
    # Bucketing
    "class":    lambda a: _class_dax(a),
    # Colour functions: render as hex string. Closest DAX equivalent is
    # a string literal that's typed as Color by the visual binding.
    "rgb":      lambda a: _rgb_dax(a),
    "argb":     lambda a: _rgb_dax(a, has_alpha=True),
    "color":    lambda a: f"{a[0]}" if a else "BLANK()",
    # Named colour shortcuts -- Qlik nullary calls.
    "white":    lambda a: '"#FFFFFF"',
    "black":    lambda a: '"#000000"',
    "red":      lambda a: '"#FF0000"',
    "green":    lambda a: '"#00FF00"',
    "blue":     lambda a: '"#0000FF"',
    "yellow":   lambda a: '"#FFFF00"',
    "magenta":  lambda a: '"#FF00FF"',
    "cyan":     lambda a: '"#00FFFF"',
    "darkgray": lambda a: '"#A9A9A9"',
    "lightgray": lambda a: '"#D3D3D3"',
    "colormix1": lambda a: a[1] if len(a) >= 2 else '"#000000"',
    "colormix2": lambda a: a[2] if len(a) >= 3 else (a[1] if len(a) >= 2 else '"#000000"'),
}


def _subfield_dax(a: List[str]) -> str:
    """``SubField(text, sep)`` -> first item before sep.
    ``SubField(text, sep, n)`` -> nth item (1-indexed)."""
    if len(a) >= 3:
        # Approximate: DAX has no PATHITEM for arbitrary separators,
        # but PATHITEM works with '|' so we substitute first.
        return f"PATHITEM(SUBSTITUTE({a[0]}, {a[1]}, \"|\"), {a[2]})"
    if len(a) >= 2:
        return f"LEFT({a[0]}, FIND({a[1]}, {a[0]}, 1, LEN({a[0]})+1) - 1)"
    return a[0] if a else "BLANK()"


def _class_dax(a: List[str]) -> str:
    """``Class(value, interval, label, offset)`` buckets a number.
    Approximate with ``FLOOR / interval * interval``."""
    if len(a) >= 2:
        return f'FORMAT(FLOOR({a[0]} / {a[1]}, 1) * {a[1]}, "0") & "-" & FORMAT(FLOOR({a[0]} / {a[1]}, 1) * {a[1]} + {a[1]}, "0")'
    return a[0] if a else "BLANK()"


def _rgb_dax(a: List[str], has_alpha: bool = False) -> str:
    """Convert RGB(r,g,b) / ARGB(a,r,g,b) numeric args to a hex string.
    DAX has no native colour type; the string is consumed by visuals
    that accept hex (data colours, conditional formatting)."""
    if has_alpha and len(a) >= 4:
        return (f'"#" & RIGHT("00" & DEC2HEX({a[0]}), 2) & RIGHT("00" & DEC2HEX({a[1]}), 2) '
                f'& RIGHT("00" & DEC2HEX({a[2]}), 2) & RIGHT("00" & DEC2HEX({a[3]}), 2)')
    if len(a) >= 3:
        return (f'"#" & RIGHT("00" & DEC2HEX({a[0]}), 2) & RIGHT("00" & DEC2HEX({a[1]}), 2) '
                f'& RIGHT("00" & DEC2HEX({a[2]}), 2)')
    return '"#000000"'


# Qlik date-pivot SCALARS (Yearstart(date) -> first day of that date's
# year, etc.). Qlik returns a scalar date; the DAX equivalent must also
# be scalar, NOT the column-only time-intelligence STARTOFYEAR family.
# Each builder takes the already-translated date argument.
_DATE_PIVOT_SCALAR = {
    "yearstart":    lambda a: f"DATE(YEAR({a}), 1, 1)",
    "yearend":      lambda a: f"DATE(YEAR({a}), 12, 31)",
    "monthstart":   lambda a: f"DATE(YEAR({a}), MONTH({a}), 1)",
    "monthend":     lambda a: f"EOMONTH({a}, 0)",
    "quarterstart": lambda a: f"DATE(YEAR({a}), (QUARTER({a}) - 1) * 3 + 1, 1)",
    "quarterend":   lambda a: f"EOMONTH(DATE(YEAR({a}), QUARTER({a}) * 3, 1), 0)",
}


# Date-pivot functions that DAX exposes only as table-returning
# time-intelligence functions. Inside a scalar comparison (a set-
# analysis range bound) we need their scalar equivalents instead.
_PIVOT_SCALAR = {
    "STARTOFYEAR":    lambda a: f"DATE(YEAR({a}), 1, 1)",
    "ENDOFYEAR":      lambda a: f"DATE(YEAR({a}), 12, 31)",
    "STARTOFMONTH":   lambda a: f"DATE(YEAR({a}), MONTH({a}), 1)",
    "ENDOFMONTH":     lambda a: f"EOMONTH({a}, 0)",
}

_SEARCH_OP_RE = re.compile(r"(>=|<=|<>|>|<)")


def _scalarize_date_pivots(dax: str) -> Optional[str]:
    """Rewrite table-returning date-pivot funcs to scalar DATE() forms.

    Returns None if a pivot we can't scalarize (quarter pivots) is
    present, so the caller falls back to a clean stub rather than
    emitting DAX that loads but mis-evaluates.
    """
    if any(p in dax.upper() for p in ("STARTOFQUARTER", "ENDOFQUARTER")):
        return None
    for fn, builder in _PIVOT_SCALAR.items():
        upper = dax.upper()
        idx = upper.find(fn + "(")
        while idx >= 0:
            argstart = idx + len(fn) + 1
            depth, i = 1, argstart
            while i < len(dax) and depth > 0:
                if dax[i] == "(":
                    depth += 1
                elif dax[i] == ")":
                    depth -= 1
                i += 1
            if depth != 0:
                return None
            arg = dax[argstart:i - 1]
            dax = dax[:idx] + builder(arg) + dax[i:]
            upper = dax.upper()
            idx = upper.find(fn + "(")
    return dax


def _split_search_bounds(content: str) -> List[tuple]:
    """Parse a Qlik numeric/date search string into (op, bound_text)
    pairs. ``">a <b"`` -> [(">", "a"), ("<", "b")]; ``">=2020"`` ->
    [(">=", "2020")]. Returns [] when the string isn't a comparison
    search (so the caller treats it as a plain equality value)."""
    s = content.strip()
    if not s or s[0] not in "<>":
        return []
    matches = list(_SEARCH_OP_RE.finditer(s))
    if not matches:
        return []
    bounds: List[tuple] = []
    for j, m in enumerate(matches):
        start = m.end()
        end = matches[j + 1].start() if j + 1 < len(matches) else len(s)
        bound_text = s[start:end].strip()
        if bound_text:
            bounds.append((m.group(1), bound_text))
    return bounds


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<NUM>   -?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)  |
    (?P<STR>   '(?:[^'\\]|\\.)*'  |  "(?:[^"\\]|\\.)*" )  |
    (?P<BRACKETID>  \[[^\[\]]*\] )  |
    (?P<ID>    \$?[\wÀ-ɏ][\wÀ-ɏ]* )  |
    (?P<LBKT>  \[)  |
    (?P<RBKT>  \])  |
    (?P<LPAR>  \()  |
    (?P<RPAR>  \))  |
    (?P<LBRC>  \{)  |
    (?P<RBRC>  \})  |
    (?P<LT>    <)   |
    (?P<GT>    >)   |
    (?P<EQ>    =)   |
    (?P<NEQ>   <>)  |
    (?P<GEQ>   >=)  |
    (?P<LEQ>   <=)  |
    (?P<COMMA> ,)   |
    (?P<SEMI>  ;)   |
    (?P<OP>    [+\-*/%&|^~])  |
    (?P<WS>    \s+)
    """,
    re.X | re.I,
)


@dataclass
class _Token:
    kind: str
    value: str


# A token that produces a VALUE -- after one of these a leading-``-`` number
# is a BINARY subtraction, not a negative literal (see ``_tokenize``).
_VALUE_TOKENS = {"NUM", "ID", "STR", "RPAR", "RBKT", "BRACKETID"}


def _tokenize(expr: str) -> List[_Token]:
    tokens: List[_Token] = []
    for m in _TOKEN_RE.finditer(expr):
        kind = m.lastgroup
        if kind == "WS":
            continue
        # The ``NUM`` pattern greedily eats a leading ``-`` (so ``-1`` is one
        # negative literal). That's correct in UNARY position (start, after an
        # operator / comma / ``(``) but WRONG after a value -- ``(a)-1`` must be
        # ``(a) - 1`` and ``MakeDate(([Year])-1,...)`` must subtract, not lex a
        # bare negative that derails the parser (the reason the "LY" financial
        # measures fell back to a broken ``Date = "text"`` filter). When a ``-``
        # number directly follows a value token, split it into ``-`` + number.
        if (kind == "NUM" and m.group().startswith("-")
                and tokens and tokens[-1].kind in _VALUE_TOKENS):
            tokens.append(_Token("OP", "-"))
            tokens.append(_Token("NUM", m.group()[1:]))
            continue
        tokens.append(_Token(kind or "", m.group()))
    return tokens


# ---------------------------------------------------------------------------
# Recursive descent parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: List[_Token], resolve: Callable[[str], str]):
        self.tokens = tokens
        self.pos = 0
        self.resolve = resolve
        self.notes: List[str] = []

    def peek(self, offset: int = 0) -> Optional[_Token]:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else None

    def consume(self) -> _Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: str) -> _Token:
        t = self.peek()
        if t is None or t.kind != kind:
            raise SyntaxError(f"Expected {kind}, got {t}")
        return self.consume()

    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    def match_id(self, *names: str) -> bool:
        t = self.peek()
        return t is not None and t.kind == "ID" and t.value.lower() in names

    def parse_expr(self) -> str:
        return self._parse_comparison()

    def _parse_additive(self) -> str:
        left = self._parse_multiplicative()
        while not self.at_end() and self.peek() \
                and self.peek().kind == "OP" \
                and self.peek().value in ("+", "-", "&"):
            op = self.consume().value
            right = self._parse_multiplicative()
            left = f"{left} {op} {right}"
        return left

    def _parse_multiplicative(self) -> str:
        left = self._parse_unary()
        while not self.at_end() and self.peek() \
                and self.peek().kind == "OP" \
                and self.peek().value in ("*", "/", "%"):
            op = self.consume().value
            right = self._parse_unary()
            left = f"{left} {op} {right}"
        return left

    def _parse_unary(self) -> str:
        if self.peek() and self.peek().kind == "OP" \
                and self.peek().value == "-":
            self.consume()
            return f"-{self._parse_primary()}"
        return self._parse_primary()

    def _parse_primary(self) -> str:
        t = self.peek()
        if t is None:
            return "BLANK()"

        if t.kind == "LPAR":
            self.consume()
            inner = self.parse_expr()
            if self.peek() and self.peek().kind == "RPAR":
                self.consume()
            return f"({inner})"

        if t.kind == "STR":
            self.consume()
            val = t.value[1:-1].replace('"', '""')
            return f'"{val}"'

        if t.kind == "NUM":
            self.consume()
            return t.value

        if t.kind == "BRACKETID":
            self.consume()
            return self.resolve(t.value[1:-1].strip())

        if t.kind == "LBKT":
            return self._parse_bracketed_field()

        if t.kind == "ID":
            name = t.value
            if self.peek(1) and self.peek(1).kind == "LPAR":
                return self._parse_function(name)
            self.consume()
            return self.resolve(name)

        return self.consume().value

    def _parse_bracketed_field(self) -> str:
        self.consume()  # [
        parts: List[str] = []
        while self.peek() and self.peek().kind != "RBKT":
            parts.append(self.consume().value)
        if self.peek():
            self.consume()  # ]
        field_name = "".join(parts).strip()
        return self.resolve(field_name)

    def _parse_function(self, name: str) -> str:
        lower = name.lower()
        self.consume()  # function name
        self.expect("LPAR")

        if lower in AGG_MAP:
            return self._parse_aggregator(lower)

        if lower == "if":
            return self._parse_if()

        if lower in _DATE_PIVOT_SCALAR:
            # Qlik date pivots are scalar. Use only the date argument;
            # any extra Qlik args (period_no shift, first_month_of_year)
            # are approximated away rather than crashing the parse.
            args = self._parse_arg_list()
            self.expect("RPAR")
            base = args[0] if args else "BLANK()"
            return _DATE_PIVOT_SCALAR[lower](base)

        if lower in SCALAR_1_MAP:
            dax_name = SCALAR_1_MAP[lower]
            # Some 1-arg Qlik functions have no direct DAX match;
            # handled inline below.
            if dax_name is None:
                if lower == "sqr":
                    arg = self.parse_expr()
                    self.expect("RPAR")
                    return f"({arg} * {arg})"
                if lower == "frac":
                    arg = self.parse_expr()
                    self.expect("RPAR")
                    return f"({arg} - INT({arg}))"
                if lower == "even":
                    arg = self.parse_expr()
                    self.expect("RPAR")
                    return f"IF(MOD({arg}, 2) = 0, TRUE(), FALSE())"
                if lower == "odd":
                    arg = self.parse_expr()
                    self.expect("RPAR")
                    return f"IF(MOD({arg}, 2) = 1, TRUE(), FALSE())"
                if lower == "evaluate":
                    arg = self.parse_expr()
                    self.expect("RPAR")
                    return arg
                # Fallthrough -- shouldn't happen.
                arg = self.parse_expr()
                self.expect("RPAR")
                return arg
            # 0-arg cases (Today/Now). Qlik allows them with no
            # parens but the tokenizer wrapped us in LPAR..RPAR.
            if lower in ("today", "now") and self.peek() and self.peek().kind == "RPAR":
                self.consume()
                return f"{dax_name}()"
            arg = self.parse_expr()
            self.expect("RPAR")
            if dax_name in ("CEILING", "FLOOR"):
                return f"{dax_name}({arg}, 1)"
            return f"{dax_name}({arg})"

        if lower in SCALAR_N_MAP:
            args = self._parse_arg_list()
            self.expect("RPAR")
            try:
                return SCALAR_N_MAP[lower](args)
            except (IndexError, KeyError):
                # Fall back to placeholder rather than crashing.
                joined = ", ".join(args)
                self.notes.append(
                    f"Qlik function '{name}' translation failed; kept original arg list."
                )
                return f"/* TODO: {name}({joined}) */ BLANK()"

        if lower in ("num", "date", "time", "timestamp", "interval"):
            arg = self.parse_expr()
            while self.peek() and self.peek().kind == "COMMA":
                self.consume()
                self.parse_expr()
            self.expect("RPAR")
            return arg

        if lower == "null":
            if self.peek() and self.peek().kind == "RPAR":
                self.consume()
            return "BLANK()"

        if lower in ("alt", "coalesce"):
            args = self._parse_arg_list()
            self.expect("RPAR")
            if not args:
                return "BLANK()"
            result = args[-1]
            for a in reversed(args[:-1]):
                result = f"IF(ISBLANK({a}), {result}, {a})"
            return result

        if lower == "concat":
            args = self._parse_arg_list()
            self.expect("RPAR")
            return " & ".join(args)

        if lower == "dual":
            args = self._parse_arg_list()
            self.expect("RPAR")
            if len(args) >= 2:
                return args[1]
            return args[0] if args else "BLANK()"

        if lower == "pick":
            args = self._parse_arg_list()
            self.expect("RPAR")
            if len(args) >= 2:
                idx = args[0]
                choices = args[1:]
                result = "BLANK()"
                for i, c in enumerate(reversed(choices), 1):
                    result = f"IF({idx} = {len(choices) - i + 1}, {c}, {result})"
                return result
            return "BLANK()"

        if lower in ("match", "wildmatch"):
            args = self._parse_arg_list()
            self.expect("RPAR")
            if len(args) >= 2:
                field_ref = args[0]
                vals = ", ".join(args[1:])
                return f"IF({field_ref} IN {{{vals}}}, 1, 0)"
            return "0"

        # Unknown function: emit a placeholder rather than crashing.
        args = self._parse_arg_list()
        self.expect("RPAR")
        self.notes.append(
            f"Unsupported Qlik function '{name}' -- kept as-is for review."
        )
        joined = ", ".join(args)
        return f"/* TODO: {name}({joined}) */ BLANK()"

    def _parse_aggregator(self, lower: str) -> str:
        dax_agg = AGG_MAP[lower]

        distinct = False
        # Qlik's grammar is  Agg([{set}] [DISTINCT] [TOTAL] expr).
        # DISTINCT/TOTAL can appear before the set block (rare) or
        # after it (standard, e.g. ``Count({set} distinct field)``);
        # accept both so the qualifier is never mistaken for the
        # aggregated expression.
        if self.match_id("distinct", "total"):
            if self.consume().value.lower() == "distinct":
                distinct = True

        set_filters: List[str] = []
        if self.peek() and self.peek().kind == "LBRC":
            set_filters = self._parse_set_analysis()

        if self.match_id("distinct", "total"):
            if self.consume().value.lower() == "distinct":
                distinct = True

        agg_expr = self.parse_expr()
        self.expect("RPAR")

        if dax_agg is None:
            self.notes.append(
                f"Qlik function '{lower}' has no DAX equivalent."
            )
            return f"/* TODO: {lower}({agg_expr}) */ BLANK()"

        if distinct and lower == "count":
            core = f"DISTINCTCOUNT({agg_expr})"
        else:
            core = f"{dax_agg}({agg_expr})"

        if set_filters:
            filters_str = ", ".join(set_filters)
            return f"CALCULATE({core}, {filters_str})"
        return core

    def _parse_set_analysis(self) -> List[str]:
        """Parse  {<Field={v1,v2}, ...>}  or  {1}  or  {$<...>}  or  {$}"""
        self.consume()  # {
        filters: List[str] = []

        if self.peek() and self.peek().kind == "NUM" \
                and self.peek().value == "1":
            self.consume()
            self.expect("RBRC")
            filters.append("ALL()")
            return filters

        # ``$`` is the current-selection set identifier. ``{$}`` means
        # "respect the current selections" -> no added CALCULATE filters.
        # ``{$<...>}`` is current-selection THEN the modifier; since
        # Qlik's default set identifier is already ``$``, that's
        # equivalent to ``{<...>}``, so we just consume the ``$`` and let
        # the ``<...>`` modifier block below produce the filters.
        if self.peek() and self.peek().kind == "ID" \
                and self.peek().value == "$":
            self.consume()
            if self.peek() and self.peek().kind == "RBRC":
                self.consume()
                return []

        if self.peek() and self.peek().kind == "LT":
            self.consume()
            while True:
                if self.peek() and self.peek().kind == "GT":
                    self.consume()
                    break
                if self.at_end():
                    break
                f = self._parse_set_field_filter()
                if f:
                    filters.append(f)
                if self.peek() and self.peek().kind == "COMMA":
                    self.consume()

        if self.peek() and self.peek().kind == "RBRC":
            self.consume()
        return filters

    def _parse_set_field_filter(self) -> Optional[str]:
        t = self.peek()
        if t is None:
            return None
        if t.kind == "BRACKETID":
            field_name = self.consume().value[1:-1].strip()
        elif t.kind == "ID":
            field_name = self.consume().value
        elif t.kind == "LBKT":
            self.consume()
            parts: List[str] = []
            while self.peek() and self.peek().kind != "RBKT":
                parts.append(self.consume().value)
            if self.peek():
                self.consume()
            field_name = " ".join(parts).strip()
        else:
            self.consume()
            return None

        op_tok = self.peek()
        if op_tok is None or op_tok.kind not in (
            "EQ", "NEQ", "LT", "GT", "GEQ", "LEQ",
        ):
            return None
        op = self.consume().value

        if self.peek() and self.peek().kind == "LBRC":
            self.consume()
            values: List[str] = []
            str_contents: List[str] = []
            while self.peek() and self.peek().kind != "RBRC":
                if self.peek().kind == "COMMA":
                    self.consume()
                    continue
                vt = self.peek()
                if vt.kind == "STR":
                    val = self.consume().value[1:-1]
                    str_contents.append(val)
                    values.append(f'"{val}"')
                elif vt.kind == "NUM":
                    values.append(self.consume().value)
                elif vt.kind == "ID":
                    values.append(f'"{self.consume().value}"')
                else:
                    self.consume()
            if self.peek():
                self.consume()

            field_ref = self.resolve(field_name)
            if not values:
                return f"ALL({field_ref})"
            # Range / comparison search, e.g. Date={">a <b"} or
            # Year={">=2020"}. Translate to a FILTER over the field.
            if len(values) == 1 and len(str_contents) == 1:
                rng = self._search_string_filter(field_ref, str_contents[0])
                if rng is not None:
                    return rng
            # ``{"*"}`` is Qlik's "any value" wildcard -- it selects
            # every value of the field, i.e. removes the filter. The
            # DAX equivalent inside CALCULATE is ALL(field).
            if values == ['"*"']:
                return f"ALL({field_ref})"
            if len(values) == 1:
                if op == "<>":
                    return f"{field_ref} <> {values[0]}"
                return f"{field_ref} {op} {values[0]}"
            in_clause = f"{field_ref} IN {{{', '.join(values)}}}"
            return f"NOT({in_clause})" if op == "<>" else in_clause

        if self.peek() and self.peek().kind in ("NUM", "STR", "ID"):
            vt = self.consume()
            val = vt.value[1:-1] if vt.kind == "STR" else vt.value
            field_ref = self.resolve(field_name)
            fmt_val = f'"{val}"' if vt.kind in ("STR", "ID") else val
            return f"{field_ref} {op} {fmt_val}"

        field_ref = self.resolve(field_name)
        return f"ALL({field_ref})"

    def _translate_bound(self, text: str) -> Optional[str]:
        """Translate one Qlik range bound (e.g. ``(Yearstart(Max(Date)))``
        or ``2020`` or ``01/01/2023``) to scalar DAX. Returns None when
        the bound can't be fully translated, so the search is dropped."""
        text = text.strip()
        # Bare date literal like 01/01/2023 -> DATE(2023, 1, 1).
        m = re.fullmatch(r"\(?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*\)?", text)
        if m:
            mm, dd, yy = m.group(1), m.group(2), m.group(3)
            return f"DATE({yy}, {int(mm)}, {int(dd)})"
        try:
            sub = _Parser(_tokenize(text), self.resolve)
            dax = sub.parse_expr()
        except Exception:
            return None
        if not sub.at_end():
            return None
        dax = _scalarize_date_pivots(dax)
        if dax is None or "TODO" in dax or dax.count("(") != dax.count(")"):
            return None
        return dax

    def _search_string_filter(self, field_ref: str, content: str) -> Optional[str]:
        bounds = _split_search_bounds(content)
        if not bounds:
            return None
        conds: List[str] = []
        for op, btext in bounds:
            bdax = self._translate_bound(btext)
            if bdax is None:
                return None
            conds.append(f"{field_ref} {op} {bdax}")
        return f"FILTER(ALL({field_ref}), {' && '.join(conds)})"

    def _parse_if(self) -> str:
        cond = self.parse_expr()
        self.expect("COMMA")
        true_val = self.parse_expr()
        false_val = "BLANK()"
        if self.peek() and self.peek().kind == "COMMA":
            self.consume()
            false_val = self.parse_expr()
        self.expect("RPAR")
        return f"IF({cond}, {true_val}, {false_val})"

    def _parse_comparison(self) -> str:
        left = self._parse_additive()
        t = self.peek()
        if t and t.kind in ("EQ", "NEQ", "LT", "GT", "GEQ", "LEQ"):
            op = self.consume().value
            right = self._parse_additive()
            return f"{left} {op} {right}"
        return left

    def _parse_arg_list(self) -> List[str]:
        args: List[str] = []
        if self.peek() and self.peek().kind == "RPAR":
            return args
        args.append(self.parse_expr())
        while self.peek() and self.peek().kind == "COMMA":
            self.consume()
            args.append(self.parse_expr())
        return args


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class ExpressionTranslator:
    """Translate a Qlik expression to DAX.

    The constructor takes a ``field_resolver`` callback that resolves a
    bare Qlik field name to a fully-qualified DAX reference, e.g.::

        resolve("Sales") -> "'Fact Sales'[Sales]"

    Call :py:meth:`translate` with the source expression. The result
    carries the DAX text plus a success flag — callers use the flag to
    decide whether to keep the v2 result or fall back to the legacy
    regex translator.
    """

    def __init__(self, field_resolver: Callable[[str], str]):
        self.resolve = field_resolver

    def translate(self, qlik_expr: str) -> TranslationResult:
        expr = (qlik_expr or "").strip()
        if not expr:
            return TranslationResult("BLANK()", True, [])

        # Strip leading `=` Qlik sometimes uses on label / conditional
        # expressions.
        if expr.startswith("="):
            expr = expr[1:].strip()

        # Drop $-expansion to a comment. The legacy translator handles
        # variable expansion itself and re-invokes us on the expanded
        # body, so we don't need to do it here too.
        expr = re.sub(r"\$\(([^)]+)\)", lambda m: f"/* $({m.group(1)}) */", expr)

        try:
            tokens = _tokenize(expr)
            parser = _Parser(tokens, self.resolve)
            dax = parser.parse_expr()
            notes = parser.notes
            success = "/* TODO" not in dax and (
                "BLANK()" not in dax or dax == "BLANK()"
            )
            if not parser.at_end():
                remaining = "".join(
                    t.value for t in parser.tokens[parser.pos:]
                )
                notes.append(f"Partial parse -- unparsed tail: {remaining[:60]}")
                success = False
            return TranslationResult(dax, success, notes)
        except Exception as exc:
            placeholder = (
                f"/* TODO: translate Qlik expression: "
                f"{qlik_expr[:80].replace('*/', '* /')} */ BLANK()"
            )
            return TranslationResult(placeholder, False, [str(exc)])
