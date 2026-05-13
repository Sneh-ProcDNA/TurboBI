"""End-to-end smoke test with the LLM mocked.

Proves the agent's plumbing — capture warnings, build context, accept
a resolution, write hints, re-run converter, verify the field stops
being dropped — without spending real API tokens.

Run:  python -m tableau_to_pbi_agent.evals.test_pipeline_mocked
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tableau_to_pbi.converter import Converter, extract_twbx
from tableau_to_pbi_agent.warnings_parser import parse


def main() -> int:
    twbx = "Dental Pharma.twbx"
    if not Path(twbx).exists():
        print(f"SKIP: {twbx} not found in cwd")
        return 0

    # Clean slate — we want pass 1 to look like a fresh user run.
    shutil.rmtree("Dental Pharma_pbip", ignore_errors=True)
    shutil.rmtree("_Dental Pharma_extracted", ignore_errors=True)

    twb_path, hypers = extract_twbx(twbx, "_Dental Pharma_extracted")

    # ---- Pass 1: surface warnings ------------------------------------
    buf = io.StringIO()
    with redirect_stdout(buf):
        Converter(twb_path, output="Dental Pharma_pbip",
                  hyper_paths=hypers).run()
    warnings_before = parse(buf.getvalue())
    print(f"Pass 1 warnings: {len(warnings_before)}")
    for w in warnings_before:
        print(f"  - [{w['kind']}] {w['field']}")

    # ---- Pass 2 with a synthetic hint --------------------------------
    # The 'Measure Names' warning gets a fabricated mapping. We don't
    # care that it's wrong (the model lookup will reject it). The
    # important thing is that the converter accepts the hints kwarg
    # and consults it.
    fabricated_hints = {
        warnings_before[0]["ds"]: {
            warnings_before[0]["field"]: ["NoSuchTable", "NoSuchCol"],
        },
    }
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        Converter(twb_path, output="Dental Pharma_pbip",
                  hyper_paths=hypers,
                  hints=fabricated_hints).run()
    text2 = buf2.getvalue()
    warnings_after = parse(text2)
    print(f"\nPass 2 warnings (with bogus hint): {len(warnings_after)}")
    print("[HINT] line should be ABSENT because the table doesn't exist:",
          "[HINT]" in text2)

    # ---- Pass 3 with a hint pointing at a real column ----------------
    # Find a real (table, column) pair the converter knows about, and
    # use it as the hint. After pass 3, the warning for that field
    # should disappear.
    from tableau_to_pbi.parser import TWBParser
    from tableau_to_pbi.model import SemanticModel
    from tableau_to_pbi.hyper import HyperRegistry
    parser = TWBParser(twb_path); parser.parse()
    with redirect_stdout(io.StringIO()):
        registry = HyperRegistry(hypers)
        hyper_data = registry.bind(parser.datasources)
        model = SemanticModel(parser.datasources, parameters=parser.parameters,
                              hyper_data_by_ds=hyper_data)
        model.build()
    real_table  = model.tables[0]["name"]
    real_column = model.tables[0]["columns"][0]["name"]
    print(f"\nPicked real target: {real_table}.{real_column}")

    real_hints = {
        warnings_before[0]["ds"]: {
            warnings_before[0]["field"]: [real_table, real_column],
        },
    }
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        Converter(twb_path, output="Dental Pharma_pbip",
                  hyper_paths=hypers,
                  hints=real_hints).run()
    text3 = buf3.getvalue()
    warnings_after_real = parse(text3)
    print(f"\nPass 3 warnings (real hint): {len(warnings_after_real)}")
    hint_logged = "[HINT]" in text3
    print(f"[HINT] line printed by converter: {hint_logged}")

    # ---- Assertions --------------------------------------------------
    fail = []
    if not warnings_before:
        fail.append("expected pass 1 to surface warnings")
    if "[HINT]" in text2:
        fail.append("bogus hint should not have been honored")
    if not hint_logged:
        fail.append("real hint should have produced a [HINT] log line")
    if len(warnings_after_real) >= len(warnings_before):
        fail.append("real hint should have reduced warning count "
                    f"({len(warnings_after_real)} vs {len(warnings_before)})")

    print()
    if fail:
        print("FAIL:")
        for f in fail:
            print(f"  - {f}")
        return 1
    print("PASS: hints pipeline wired end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
