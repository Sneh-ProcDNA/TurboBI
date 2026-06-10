"""Qlik text-expression evaluation for the unbuild step + offline fallback.

Qlik text objects (``sn-text`` / ``text-image``) and visual titles can
embed *expressions* -- ``='Top ' & $(vBrokers) & ' Brokers'`` or
``=Num(Sum(Sales), '#,##0')``. Power BI textboxes are static, so a
faithful conversion must capture the **evaluated values** while the
Qlik engine is still on the line and bake those snapshots into the
converted report. This module owns that:

* :func:`collect_text_expressions` -- walk the written unbuild JSON and
  find every expression a textbox / title carries.
* :func:`evaluate_unbuilt_expressions` -- called by the engine unbuild
  (cloud or Desktop) right after the object files are written. Each
  ``sn-text`` object is evaluated **in object context** via
  ``GetObject -> GetLayout`` (so number formatting, master measures and
  object-scoped functions resolve exactly as Qlik shows them); any
  leftover title-level expression is evaluated with ``EvaluateEx``.
  Results land in an ``evaluated-expressions.json`` sidecar next to the
  other unbuild artefacts:

  ::

      {"objects":     {"<objectId>": {"<cId>": "<text>"}},
       "expressions": {"<raw expression>": "<text>"}}

  The parser loads the sidecar into ``ir["evaluated"]`` and the report
  builder substitutes the snapshot text wherever Qlik referenced the
  expression.

* :func:`eval_static_expression` -- a tiny local evaluator for the
  no-engine paths (an old unbuild folder, direct QVF parse): resolves
  pure literals (``'ALTERNATIVES'``), literal concatenations
  (``'A' & ' ' & 'B'``), ``chr()`` and ``$(variable)`` expansions whose
  definitions are themselves literal. Anything data-driven returns
  ``None`` and the caller keeps its existing label fallback.

Selection state caveat: evaluation must run on a CLEAR selection state
(the unbuild's bookmark capture finishes with ``ClearAll``); a saved
selection would otherwise skew every aggregate snapshot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._logging import get_logger

_log = get_logger("TEXTEVAL")

SIDECAR_FILENAME = "evaluated-expressions.json"

# Object types whose hypercube measures feed text content (the Lexical
# ``qlik.expression.node`` references them by cId).
_TEXT_VIZ_TYPES = {"sn-text", "text-image"}

# Keys whose string values are title-ish expressions when they start
# with "=" (visual titles, button labels, footnotes...).
_TITLE_KEYS = {"title", "subtitle", "footnote", "label"}

# Caps so a pathological app can't stall the unbuild.
_MAX_DOC_EXPRESSIONS = 500
_MAX_TEXT_OBJECTS = 200


# ---------------------------------------------------------------------------
# Collection -- walk the written unbuild JSON
# ---------------------------------------------------------------------------

def collect_text_expressions(
    objects_dir: Path,
) -> Tuple[Dict[str, List[Tuple[str, Optional[str]]]], List[str]]:
    """Scan ``objects/*.json`` for text expressions.

    Returns ``(obj_exprs, doc_exprs)``:

    * ``obj_exprs`` -- ``{objectId: [(cId, rawExpr | None), ...]}`` in
      hypercube-measure order, one entry per ``sn-text`` / ``text-image``
      object that carries measures. ``rawExpr`` is ``None`` for master-
      measure refs (``qLibraryId``) -- object-context evaluation still
      resolves those.
    * ``doc_exprs`` -- unique title-level expression strings (leading
      ``=`` form preserved) found anywhere in the object tree:
      ``qStringExpression`` bodies, ``=``-prefixed title/label strings
      and ``qLabelExpression`` values.
    """
    obj_exprs: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    doc_seen: Dict[str, None] = {}

    def add_doc(expr: Any) -> None:
        if not isinstance(expr, str):
            return
        e = expr.strip()
        if not e or len(e) > 4000 or len(doc_seen) >= _MAX_DOC_EXPRESSIONS:
            return
        doc_seen.setdefault(e, None)

    def harvest_text_object(prop: Dict[str, Any]) -> None:
        qid = ((prop.get("qInfo") or {}).get("qId") or "").strip()
        if not qid or qid in obj_exprs or len(obj_exprs) >= _MAX_TEXT_OBJECTS:
            return
        measures = ((prop.get("qHyperCubeDef") or {}).get("qMeasures")) or []
        entries: List[Tuple[str, Optional[str]]] = []
        for meas in measures:
            if not isinstance(meas, dict):
                continue
            mdef = meas.get("qDef") or {}
            cid = (mdef.get("cId") or "").strip()
            raw = mdef.get("qDef")
            expr = raw.strip() if isinstance(raw, str) and raw.strip() else None
            if meas.get("qLibraryId") and not expr:
                expr = None  # library measure -- object eval handles it
            if cid:
                entries.append((cid, expr))
        if entries:
            obj_exprs[qid] = entries

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            viz = node.get("visualization")
            if isinstance(viz, str) and viz.strip().lower() in _TEXT_VIZ_TYPES:
                harvest_text_object(node)
            # qStringExpression bodies are expressions by definition.
            qse = node.get("qStringExpression")
            if isinstance(qse, dict):
                add_doc(qse.get("qExpr"))
            elif isinstance(qse, str):
                add_doc(qse)
            for key, val in node.items():
                if isinstance(val, str):
                    if key == "qLabelExpression" and val.strip():
                        add_doc(val)
                    elif key in _TITLE_KEYS and val.strip().startswith("="):
                        add_doc(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if objects_dir.is_dir():
        for fp in sorted(objects_dir.glob("*.json")):
            lname = fp.name.lower()
            # loadmodel / engine-schema are big and carry no text content.
            if lname.startswith("loadmodel") or lname == "engine-schema.json":
                continue
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            walk(data)

    # Object-level expressions are also useful doc-wide (same string
    # reused as a title elsewhere); the engine pass keys results by both.
    return obj_exprs, list(doc_seen.keys())


# ---------------------------------------------------------------------------
# Engine evaluation (called from the unbuild while the socket is open)
# ---------------------------------------------------------------------------

def _format_number(num: Any) -> Optional[str]:
    if not isinstance(num, (int, float)):
        return None
    if isinstance(num, float) and num.is_integer():
        return str(int(num))
    return str(num)


def _cell_text(cell: Any) -> Optional[str]:
    """Pull display text from a hypercube cell ({qText, qNum, ...})."""
    if not isinstance(cell, dict):
        return None
    txt = cell.get("qText")
    if isinstance(txt, str):
        return txt
    return _format_number(cell.get("qNum"))


def _evaluate_object_text(
    client: Any, obj_id: str, n_measures: int,
) -> Optional[List[Optional[str]]]:
    """Evaluate one text object's measures in object context.

    ``GetObject -> GetLayout`` returns the hypercube with measures
    evaluated exactly as the Qlik client shows them (number format,
    master measures, object-scoped functions). Values come from
    ``qGrandTotalRow`` (always present for a 0-dimension cube) or the
    first data-page row; as a last resort one explicit
    ``GetHyperCubeData`` page is requested. Returns the per-measure
    text list (index-aligned with the property tree's ``qMeasures``),
    or ``None`` when the object can't be opened.
    """
    try:
        res = client.request("GetObject", client.app_handle, [obj_id])
        handle = (res.get("qReturn") or {}).get("qHandle")
        if not isinstance(handle, int) or handle < 0:
            return None
        lay = client.request("GetLayout", handle, []).get("qLayout") or {}
    except RuntimeError as exc:
        _log.info(f"  text object {obj_id}: layout unavailable ({exc})")
        return None

    hc = lay.get("qHyperCube") or {}
    n_dims = len(hc.get("qDimensionInfo") or [])

    cells: List[Any] = list(hc.get("qGrandTotalRow") or [])
    if not cells:
        for page in hc.get("qDataPages") or []:
            matrix = page.get("qMatrix") or []
            if matrix:
                cells = list(matrix[0][n_dims:])
                break
    if not cells and n_measures:
        try:
            pages = client.request(
                "GetHyperCubeData", handle,
                ["/qHyperCubeDef",
                 [{"qLeft": 0, "qTop": 0,
                   "qWidth": n_dims + n_measures, "qHeight": 1}]],
            )
            matrix = ((pages.get("qDataPages") or [{}])[0].get("qMatrix")) or []
            if matrix:
                cells = list(matrix[0][n_dims:])
        except RuntimeError:
            pass

    if not cells:
        return None
    return [_cell_text(c) for c in cells]


def _evaluate_doc_expression(client: Any, expr: str) -> Optional[str]:
    """Doc-level ``EvaluateEx`` of one expression (leading ``=`` stripped).

    Returns the engine's text result, or ``None`` when the engine
    rejects the expression (object-scoped functions, syntax the doc
    scope can't resolve...). Callers keep their label fallback then.
    """
    body = expr.strip()
    if body.startswith("="):
        body = body[1:].strip()
    if not body:
        return None
    try:
        res = client.request("EvaluateEx", client.app_handle, [body])
    except RuntimeError:
        return None
    val = res.get("qValue") or {}
    txt = val.get("qText")
    if isinstance(txt, str):
        return txt
    return _format_number(val.get("qNumber"))


def evaluate_unbuilt_expressions(client: Any, output_dir: Path) -> Optional[Path]:
    """Evaluate every collected text expression and write the sidecar.

    Call AFTER all object files are written and the selection state is
    clear. Never raises -- a failed evaluation just leaves that entry
    out and downstream falls back to the expression label.
    """
    output_dir = Path(output_dir)
    try:
        obj_exprs, doc_exprs = collect_text_expressions(output_dir / "objects")
    except Exception as exc:  # noqa: BLE001 -- defensive: never fail the unbuild
        _log.warning(f"Text-expression collection failed: {exc}")
        return None
    if not obj_exprs and not doc_exprs:
        return None

    objects_out: Dict[str, Dict[str, str]] = {}
    expr_out: Dict[str, str] = {}
    leftovers: List[str] = []

    for obj_id, entries in obj_exprs.items():
        values = _evaluate_object_text(client, obj_id, len(entries))
        per_obj: Dict[str, str] = {}
        for idx, (cid, raw_expr) in enumerate(entries):
            val = values[idx] if values and idx < len(values) else None
            if val is None and raw_expr:
                leftovers.append(raw_expr)
                continue
            if val is not None:
                per_obj[cid] = val
                # The same expression string may title another visual.
                if raw_expr and raw_expr not in expr_out:
                    expr_out[raw_expr] = val
        if per_obj:
            objects_out[obj_id] = per_obj

    for expr in list(doc_exprs) + leftovers:
        if expr in expr_out:
            continue
        val = _evaluate_doc_expression(client, expr)
        if val is not None:
            expr_out[expr] = val

    sidecar = {"objects": objects_out, "expressions": expr_out}
    out_path = output_dir / SIDECAR_FILENAME
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(sidecar, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        _log.warning(f"Could not write {SIDECAR_FILENAME}: {exc}")
        return None
    _log.info(
        f"Evaluated text expressions: {len(objects_out)} text object(s), "
        f"{len(expr_out)} expression value(s) -> {SIDECAR_FILENAME}"
    )
    return out_path


# ---------------------------------------------------------------------------
# Local static evaluator (no engine)
# ---------------------------------------------------------------------------

_DOLLAR_VAR_RE = re.compile(r"\$\(\s*([A-Za-z_][\w.]*)\s*\)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CHR_RE = re.compile(r"chr\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _expand_variables(
    expr: str, variables: Optional[Dict[str, str]], depth: int = 0,
) -> Optional[str]:
    """Textually expand ``$(var)`` references (Qlik dollar expansion).

    A definition that itself starts with ``=`` is an *evaluated*
    variable -- resolvable here only when its body is statically
    evaluable. Returns ``None`` when any reference can't be resolved,
    so the caller bails out instead of mangling the expression.
    """
    if depth > 5:
        return None
    out = expr
    for _ in range(10):
        m = _DOLLAR_VAR_RE.search(out)
        if not m:
            return out
        name = m.group(1)
        definition = (variables or {}).get(name)
        if definition is None:
            return None
        definition = definition.strip()
        if definition.startswith("="):
            resolved = eval_static_expression(definition, variables, _depth=depth + 1)
            if resolved is None:
                return None
            definition = resolved
        elif "$(" in definition:
            expanded = _expand_variables(definition, variables, depth + 1)
            if expanded is None:
                return None
            definition = expanded
        out = out[:m.start()] + definition + out[m.end():]
    return None


def eval_static_expression(
    expr: Any,
    variables: Optional[Dict[str, str]] = None,
    _depth: int = 0,
) -> Optional[str]:
    """Best-effort local evaluation of a Qlik expression to plain text.

    Handles exactly the shapes that are static by construction:

    * string literals: ``'ALTERNATIVES'``, ``"text"`` (doubled-quote
      escapes honoured);
    * number literals;
    * ``chr(n)``;
    * ``&`` concatenations of the above;
    * ``$(variable)`` expansion when the variable's definition is
      itself statically evaluable;
    * ``//`` and ``/* */`` comments (outside string literals).

    Anything data-driven (field refs, aggregations, set analysis)
    returns ``None`` -- the caller keeps its existing fallback. Never
    raises.
    """
    if not isinstance(expr, str) or _depth > 5:
        return None
    s = expr.strip()
    if s.startswith("="):
        s = s[1:]
    if "$(" in s:
        expanded = _expand_variables(s, variables, _depth)
        if expanded is None:
            return None
        s = expanded
    if not s.strip():
        return None

    parts: List[str] = []
    i, n = 0, len(s)
    expect_term = True
    while i < n:
        ch = s[i]
        if ch in " \t\r\n":
            i += 1
            continue
        # Comments (only reachable outside string literals).
        if ch == "/" and i + 1 < n and s[i + 1] == "/":
            nl = s.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        if ch == "/" and i + 1 < n and s[i + 1] == "*":
            end = s.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            continue
        if expect_term:
            if ch in ("'", '"'):
                quote = ch
                j = i + 1
                buf: List[str] = []
                while j < n:
                    if s[j] == quote:
                        if j + 1 < n and s[j + 1] == quote:  # doubled escape
                            buf.append(quote)
                            j += 2
                            continue
                        break
                    buf.append(s[j])
                    j += 1
                if j >= n:
                    return None  # unterminated literal
                parts.append("".join(buf))
                i = j + 1
                expect_term = False
                continue
            m = _CHR_RE.match(s, i)
            if m:
                try:
                    parts.append(chr(int(m.group(1))))
                except (ValueError, OverflowError):
                    return None
                i = m.end()
                expect_term = False
                continue
            m = _NUMBER_RE.match(s, i)
            if m:
                parts.append(m.group(0))
                i = m.end()
                expect_term = False
                continue
            return None  # field ref / function call / anything dynamic
        else:
            if ch == "&":
                i += 1
                expect_term = True
                continue
            return None  # operator we don't model (+, -, comparison...)

    if expect_term and parts:
        return None  # dangling '&'
    if not parts:
        return None
    return "".join(parts)
