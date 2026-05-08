"""End-to-end pipeline:

    1. Build the model snapshot in-process (parser + SemanticModel).
    2. Run the deterministic converter, capture stdout.
    3. Parse warnings.
    4. Resolve field warnings via Claude (parallel).
    5. Merge with any existing hints sidecar; persist.
    6. Re-run the converter with hints applied.
    7. Report before/after warning counts.

Step 6 is skipped when no hints were produced — there's no point
re-running the converter for zero gain.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tableau_to_pbi.converter import (
    Converter,
    extract_twbx,
)
from tableau_to_pbi.model  import SemanticModel
from tableau_to_pbi.parser import TWBParser

from . import hints as hints_io
from .calc_index       import build_calc_index
from .claude_client    import ClaudeClient
from .context_builder  import model_snapshot
from .resolvers.field_resolver   import FieldResolver
from .resolvers.formula_resolver import resolve_all_via_formula
from .warnings_parser  import parse as parse_warnings, summarize


def _resolve_inputs(input_path: str) -> Tuple[str, List[str], Path, str, bool]:
    """Mirror Converter's mode-detection — return (twb_path, hyper_paths,
    output_dir, twb_stem, stub_only)."""
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")
    ext = src.suffix.lower()
    stem = src.stem
    if ext == ".twbx":
        work_dir = str(src.parent / f"_{stem}_extracted")
        twb_path, hypers = extract_twbx(str(src), work_dir)
        out = src.parent / f"{stem}_pbip"
        return twb_path, hypers, out, stem, False
    elif ext == ".twb":
        out = src.parent / f"{stem}_pbip"
        return str(src), [], out, stem, True
    raise ValueError(f"Unsupported extension: {ext}")


def _build_model_snapshot(
    twb_path: str, hyper_paths: List[str], stub_only: bool,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Re-run parser + SemanticModel just for the snapshot.

    Re-doing this work outside the converter is cheap (sub-second on
    the workbooks we've tested) and keeps the converter's run() method
    untouched. Returns (snapshot, datasources, worksheets).
    """
    parser = TWBParser(twb_path)
    parser.parse()

    hyper_data_by_ds: Dict[str, Any] = {}
    if hyper_paths and not stub_only:
        from tableau_to_pbi.hyper import HyperRegistry
        # Silence the registry's chatty prints — the agent already
        # echoes the converter's stdout once on the real run.
        with redirect_stdout(io.StringIO()):
            registry = HyperRegistry(hyper_paths)
            hyper_data_by_ds = registry.bind(parser.datasources)

    with redirect_stdout(io.StringIO()):
        model = SemanticModel(
            parser.datasources,
            parameters=parser.parameters,
            stub_only=stub_only,
            hyper_data_by_ds=hyper_data_by_ds,
        )
        model.build()

    snapshot = model_snapshot(parser.datasources, model.tables)
    return snapshot, parser.datasources, parser.worksheets


def _run_converter(
    twb_path: str,
    hyper_paths: List[str],
    output_dir: Path,
    stub_only: bool,
    hints: Optional[Dict[str, Any]] = None,
) -> str:
    """Run the deterministic converter; return its captured stdout."""
    buf = io.StringIO()
    conv = Converter(
        twb_path,
        output=str(output_dir),
        stub_only=stub_only,
        hyper_paths=hyper_paths,
        hints=hints or {},
    )
    with redirect_stdout(buf):
        conv.run()
    text = buf.getvalue()
    # Echo to the user so they see the same output they'd see without
    # the agent wrapper.
    print(text, end="")
    return text


def _validate_hints_against_snapshot(
    raw_hints: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]],
    twb: str,
    hypers: List[str],
    stub_only: bool,
) -> Tuple[Dict[str, Any], int]:
    """Drop any (ds, field) -> (table, column) entry whose target
    no longer exists in the model. Returns (clean_hints, dropped_count).

    Building the model snapshot is the expensive part of this function
    (~5-15s on real workbooks), so we skip it entirely when raw_hints
    is empty. When `snapshot` is passed in we reuse it; otherwise we
    build one on demand here. The snapshot's columns list also
    includes measures (suffixed with '(measure)'), so a hint pointing
    at a translated calc field still validates.
    """
    if not raw_hints:
        return {}, 0

    if snapshot is None:
        snapshot, _ds, _ws = _build_model_snapshot(twb, hypers, stub_only)

    clean: Dict[str, Any] = {}
    dropped = 0
    for ds_name, mapping in raw_hints.items():
        if not isinstance(mapping, dict):
            continue
        ds_tables = (snapshot.get(ds_name, {}) or {}).get("tables", {})
        if not ds_tables:
            # Datasource itself disappeared from the model — drop the
            # whole bucket, not just individual entries.
            dropped += len(mapping)
            continue
        ds_clean: Dict[str, List[str]] = {}
        for field, loc in mapping.items():
            if (not isinstance(loc, list) or len(loc) != 2
                    or not all(isinstance(v, str) for v in loc)):
                dropped += 1
                continue
            tbl, col = loc
            cols_in_table = ds_tables.get(tbl) or []
            # Match against bare column name AND the '(measure)' marker
            # that model_snapshot uses for translated calc fields.
            if col in cols_in_table or f"{col} (measure)" in cols_in_table:
                ds_clean[field] = [tbl, col]
            else:
                dropped += 1
        if ds_clean:
            clean[ds_name] = ds_clean

    return clean, dropped


async def _agent_resolve(
    warnings: List[Dict[str, Any]],
    snapshot: Dict[str, Any],
    worksheets: List[Dict[str, Any]],
    datasources: List[Dict[str, Any]],
    calc_index: Dict[str, Dict[str, Any]],
    model_name: Optional[str] = None,
) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """Build a Claude client and resolve all field warnings."""
    client = (ClaudeClient(model=model_name) if model_name
              else ClaudeClient())
    resolver = FieldResolver(client)
    return await resolver.resolve_all(warnings, worksheets, snapshot,
                                      datasources=datasources,
                                      calc_index=calc_index)


def run_with_agent(
    input_path: str,
    *,
    model: Optional[str] = None,
    skip_llm: bool = False,
) -> Dict[str, Any]:
    """Convert with agent assistance. Synchronous wrapper around the
    async resolution step.

    Returns a small report dict the CLI can pretty-print:
        {warnings_before, warnings_after, hints_total, hints_added}
    """
    twb, hypers, out_dir, stem, stub_only = _resolve_inputs(input_path)

    print(f"\n=== Pass 1: deterministic conversion ===")
    stdout1 = _run_converter(twb, hypers, out_dir, stub_only, hints=None)
    warnings_before = parse_warnings(stdout1)
    summary_before  = summarize(warnings_before)
    print(f"\n[AGENT] warnings: {summary_before or 'none'}")

    if not warnings_before:
        return {
            "warnings_before": 0,
            "warnings_after":  0,
            "hints_total":     0,
            "hints_added":     0,
        }

    # Existing hints from a prior agent run, if any. We validate them
    # against the current model snapshot before trusting — stale or
    # hallucinated (table, column) pairs from earlier LLM runs would
    # otherwise sit in the file forever and silently fail at convert
    # time. The runtime hint validator in report.py drops the same
    # entries; doing it here keeps the file itself clean.
    sidecar = hints_io.sidecar_path(out_dir, stem)
    raw_hints = hints_io.load(sidecar)
    existing_hints, dropped_stale = _validate_hints_against_snapshot(
        raw_hints,
        snapshot=None,  # built lazily below if needed
        twb=twb,
        hypers=hypers,
        stub_only=stub_only,
    )
    if dropped_stale:
        print(f"[AGENT] dropped {dropped_stale} stale hint(s) "
              f"(table/column no longer exists in model)")

    new_hints: Dict[Tuple[str, str], Tuple[str, str]] = {}

    print(f"\n=== Building model snapshot ===")
    snapshot, datasources, worksheets = _build_model_snapshot(
        twb, hypers, stub_only,
    )
    ds_count = len(snapshot)
    col_count = sum(len(c) for ds in snapshot.values()
                    for c in ds["tables"].values())
    print(f"[AGENT] snapshot: {ds_count} datasource(s), "
          f"{col_count} columns total")

    # Pull worksheet-local calc-field captions/formulas straight from
    # the twb XML — these unlock Calculation_<id> warnings the parser
    # doesn't surface.
    calc_index = build_calc_index(twb)
    print(f"[AGENT] calc-field index: {len(calc_index)} entries")

    # Free pass — deterministic resolver for trivial calc aliases like
    # `[REGION]` or `[REGION (DIM_REGION)]`. Anything it cracks doesn't
    # need to go to the LLM. Passing `worksheets` enables the multi-
    # table primary-table tiebreak for ambiguous column names.
    free_hints = resolve_all_via_formula(warnings_before, snapshot,
                                         calc_index, worksheets=worksheets)
    if free_hints:
        print(f"[AGENT] formula resolver: {len(free_hints)} resolved "
              f"deterministically (zero tokens)")
    new_hints.update(free_hints)

    field_kind_count = sum(
        1 for w in warnings_before if w["kind"] in ("resolve", "filter")
    )
    remaining = [
        w for w in warnings_before
        if w["kind"] in ("resolve", "filter")
        and (w["ds"], w["field"]) not in free_hints
    ]

    if not remaining:
        print(f"[AGENT] all {field_kind_count} warning(s) resolved without "
              f"calling Claude")
    elif skip_llm or not os.environ.get("ANTHROPIC_API_KEY"):
        if not skip_llm:
            print(f"[AGENT] ANTHROPIC_API_KEY not set; "
                  f"{len(remaining)} unresolved warning(s) left for LLM step.")
    else:
        print(f"\n=== Resolving {len(remaining)} remaining warning(s) "
              f"via Claude ===")
        llm_hints = asyncio.run(
            _agent_resolve(remaining, snapshot, worksheets,
                          datasources, calc_index, model_name=model)
        )
        print(f"[AGENT] LLM resolved {len(llm_hints)}/{len(remaining)} "
              f"remaining warning(s)")
        new_hints.update(llm_hints)

    # Merge new hints into the existing sidecar.
    merged = dict(existing_hints)
    for (ds, field), (tbl, col) in new_hints.items():
        hints_io.add(merged, ds, field, tbl, col)

    if not new_hints and not existing_hints:
        return {
            "warnings_before": len(warnings_before),
            "warnings_after":  len(warnings_before),
            "hints_total":     0,
            "hints_added":     0,
        }

    # Persist before re-running so a crash mid-run doesn't lose the
    # learned hints.
    hints_io.save(sidecar, merged)
    print(f"\n[AGENT] hints saved -> {sidecar} "
          f"(total {hints_io.hint_count(merged)}, added {len(new_hints)})")

    print(f"\n=== Pass 2: re-converting with hints ===")
    stdout2 = _run_converter(twb, hypers, out_dir, stub_only, hints=merged)
    warnings_after = parse_warnings(stdout2)
    summary_after  = summarize(warnings_after)
    print(f"\n[AGENT] warnings after hints: {summary_after or 'none'}")

    return {
        "warnings_before": len(warnings_before),
        "warnings_after":  len(warnings_after),
        "hints_total":     hints_io.hint_count(merged),
        "hints_added":     len(new_hints),
    }
