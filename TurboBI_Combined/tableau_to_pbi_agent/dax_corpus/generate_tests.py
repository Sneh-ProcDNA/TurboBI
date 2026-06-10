"""Generate ``tableau_to_pbi/tests/test_dax_corpus.py`` by running
curated formulas through the current translator and pinning the actual
output.

Each test case is ``(test_id, formula, table, ftp, prefs, mrefs,
expected)`` where ``expected`` is the actual DAX the translator emits
today (or ``None``). The generated file is a parameterized pytest
module; running it later catches drift in translator behavior — every
case stays green so long as the translator emits the same string.

Re-run this generator after intentional translator changes and inspect
the diff to confirm only the expected cases moved.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from tableau_to_pbi.dax_translator import translate_tableau_to_dax


HERE         = Path(__file__).parent            # tableau_to_pbi_agent/dax_corpus
PROJECT_ROOT = HERE.parent.parent               # TurboBI Version 0515
OUT_TEST_PATH = (
    PROJECT_ROOT / "tableau_to_pbi" / "tests" / "test_dax_corpus.py"
)


# (test_id, formula, table, field_to_pbi, parameter_refs, measure_refs)
# field_to_pbi maps Tableau ref → (table, col) — used to lock down the
# exact resolution path so the test isn't affected by inferred ordering.
CASES: List[Tuple] = [
    # ---- LITERAL_ONLY ----------------------------------------------------
    ("literal_string",
     "'Hello'",                                           "Sales", {}, None, None),
    ("literal_double_quoted",
     '"World"',                                           "Sales", {}, None, None),
    ("literal_int",
     "42",                                                "Sales", {}, None, None),
    ("literal_float",
     "3.14",                                              "Sales", {}, None, None),

    # ---- REF_ONLY --------------------------------------------------------
    ("ref_bare_col_min_wrap",
     "[Sales]",                                           "Sales", {}, None, None),

    # ---- ARITHMETIC + COMPARISON ----------------------------------------
    ("arith_add",
     "[Sales] + 100",                                     "Sales", {}, None, None),
    ("arith_mul",
     "[Sales] * 1.1",                                     "Sales", {}, None, None),
    ("arith_div",
     "[Profit] / [Sales]",                                "Sales", {}, None, None),
    ("compare_gt",
     "[Sales] > 1000",                                    "Sales", {}, None, None),
    ("compare_neq",
     "[Region] != 'West'",                                "Sales", {}, None, None),

    # ---- LOGICAL --------------------------------------------------------
    ("logical_and",
     "[Sales] > 0 AND [Profit] > 0",                      "Sales", {}, None, None),
    ("logical_or",
     "[Region] = 'East' OR [Region] = 'West'",            "Sales", {}, None, None),
    ("logical_not",
     "NOT [IsReturn]",                                    "Sales", {}, None, None),

    # ---- AGG_BASIC ------------------------------------------------------
    ("agg_sum",
     "SUM([Sales])",                                      "Sales", {}, None, None),
    ("agg_avg",
     "AVG([Sales])",                                      "Sales", {}, None, None),
    ("agg_count",
     "COUNT([Customer ID])",                              "Sales", {}, None, None),
    ("agg_countd",
     "COUNTD([Customer ID])",                             "Sales", {}, None, None),
    ("agg_min",
     "MIN([Sales])",                                      "Sales", {}, None, None),
    ("agg_max",
     "MAX([Sales])",                                      "Sales", {}, None, None),
    ("agg_median",
     "MEDIAN([Sales])",                                   "Sales", {}, None, None),

    # ---- DATE_FN --------------------------------------------------------
    ("date_year",
     "YEAR([Order Date])",                                "Sales", {}, None, None),
    ("date_month",
     "MONTH([Order Date])",                               "Sales", {}, None, None),
    ("date_datepart_year",
     "DATEPART('year', [Order Date])",                    "Sales", {}, None, None),
    ("date_datetrunc_month",
     "DATETRUNC('month', [Order Date])",                  "Sales", {}, None, None),
    ("date_datediff_day",
     "DATEDIFF('day', [Order Date], [Ship Date])",        "Sales", {}, None, None),
    ("date_dateadd_month",
     "DATEADD('month', 1, [Order Date])",                 "Sales", {}, None, None),
    ("date_makedate",
     "MAKEDATE(2024, 1, 1)",                              "Sales", {}, None, None),
    ("date_datename_month",
     "DATENAME('month', [Order Date])",                   "Sales", {}, None, None),
    ("date_today",
     "TODAY()",                                           "Sales", {}, None, None),

    # ---- STRING_FN ------------------------------------------------------
    ("str_left",
     "LEFT([Product Name], 3)",                           "Sales", {}, None, None),
    ("str_upper",
     "UPPER([Product Name])",                             "Sales", {}, None, None),
    ("str_trim",
     "TRIM([Product Name])",                              "Sales", {}, None, None),
    ("str_len",
     "LEN([Product Name])",                               "Sales", {}, None, None),
    ("str_split",
     "SPLIT([Product Name], '-', 1)",                     "Sales", {}, None, None),
    ("str_concat_plus",
     "'$' + STR([Sales])",                                "Sales", {}, None, None),

    # ---- BLOCK_IF / BLOCK_CASE / FN_IIF / FN_IF -------------------------
    ("block_if_simple",
     "IF [Sales] > 1000 THEN 'High' ELSE 'Low' END",      "Sales", {}, None, None),
    ("block_if_elseif",
     "IF [Sales] > 1000 THEN 'High' ELSEIF [Sales] > 500 THEN 'Med' ELSE 'Low' END",
     "Sales", {}, None, None),
    ("block_case",
     "CASE [Region] WHEN 'East' THEN 1 WHEN 'West' THEN 2 ELSE 0 END",
     "Sales", {}, None, None),
    # Nested: IF THEN (block CASE) — the body walker has to skip the
    # inner CASE's WHEN / THEN / END at depth>0 so the outer IF still
    # parses correctly.
    ("nested_if_then_case",
     "IF [Cat] = 'A' THEN CASE [P] WHEN 1 THEN 'first' WHEN 2 THEN 'second' END END",
     "Sales", {}, None, None),
    # Nested: CASE WHEN ... THEN (block IF). Same skip needed in the
    # CASE walker so the inner IF's THEN / ELSE don't confuse the outer
    # WHEN/THEN/ELSE delimiter scan.
    ("nested_case_when_if",
     "CASE [P] WHEN 1 THEN IF [X] > 0 THEN 'pos' ELSE 'neg' END END",
     "Sales", {}, None, None),
    ("fn_iif",
     "IIF([Sales] > 1000, 'High', 'Low')",                "Sales", {}, None, None),
    ("fn_if",
     "IF([Sales] > 1000, 'High', 'Low')",                 "Sales", {}, None, None),

    # ---- LOD_FIXED ------------------------------------------------------
    ("lod_fixed_single_dim",
     "{ FIXED [Region] : SUM([Sales]) }",                 "Sales", {}, None, None),
    ("lod_fixed_multi_dim",
     "{ FIXED [Region], [Category] : COUNT([Customer ID]) }",
     "Sales", {}, None, None),
    ("lod_fixed_global",
     "{ FIXED : SUM([Sales]) }",
     "Sales", {"Sales": ("Sales", "Sales")}, None, None),
    ("lod_fixed_with_year",
     "{ FIXED YEAR([Order Date]) : SUM([Sales]) }",       "Sales", {}, None, None),

    # ---- WINDOW_AGG -----------------------------------------------------
    ("window_sum",
     "WINDOW_SUM(SUM([Sales]))",                          "Sales", {}, None, None),
    ("window_avg",
     "WINDOW_AVG(MAX([Sales]))",                          "Sales", {}, None, None),
    ("window_max",
     "WINDOW_MAX([Sales])",                               "Sales", {}, None, None),

    # ---- AGG_ATTR -------------------------------------------------------
    ("attr_bare",
     "ATTR([Region])",                                    "Sales", {}, None, None),
    ("attr_in_if",
     "IF ATTR([Region]) = 'East' THEN 1 ELSE 0 END",      "Sales", {}, None, None),

    # ---- TYPE_CAST ------------------------------------------------------
    ("cast_int",
     "INT([Sales])",                                      "Sales", {}, None, None),
    ("cast_str",
     "STR([Sales])",                                      "Sales", {}, None, None),
    ("cast_float",
     "FLOAT([Sales])",                                    "Sales", {}, None, None),
    ("cast_date",
     "DATE([Order Date])",                                "Sales", {}, None, None),

    # ---- TOTAL / Tableau idioms ----------------------------------------
    ("total_to_allselected",
     "SUM([Sales]) / TOTAL(SUM([Sales]))",                "Sales", {}, None, None),
    ("rowcount_internal_object_id",
     "COUNT([__tableau_internal_object_id__].[Sales_XYZ])",
     "Sales", {}, None, None),

    # ---- Comments -------------------------------------------------------
    ("comment_line",
     "// caption\n[Sales]",                               "Sales", {}, None, None),
    ("comment_block",
     "/* note */ SUM([Sales])",                           "Sales", {}, None, None),

    # ---- Measure refs (don't wrap a measure ref in MIN) -----------------
    ("measure_ref_passthrough",
     "[ExistingMeasure] + 1",                             "Sales", {}, None,
     {("Sales", "ExistingMeasure")}),

    # ====================================================================
    # UNSUPPORTED — translator returns None
    # ====================================================================
    ("unsupported_rank_unique",
     "RANK_UNIQUE(SUM([Sales]), 'desc')",                 "Sales", {}, None, None),
    ("unsupported_rank_dense",
     "RANK_DENSE(MIN([Order ID]), 'asc')",                "Sales", {}, None, None),
    ("unsupported_rank",
     "RANK(SUM([Sales]))",                                "Sales", {}, None, None),
    ("unsupported_index",
     "INDEX()",                                           "Sales", {}, None, None),
    ("unsupported_lookup_prev",
     "LOOKUP(SUM([Sales]), -1)",                          "Sales", {}, None, None),
    ("unsupported_running_sum",
     "RUNNING_SUM(SUM([Sales]))",                         "Sales", {}, None, None),
    ("unsupported_running_avg",
     "RUNNING_AVG(SUM([Sales]))",                         "Sales", {}, None, None),
    ("unsupported_previous_value",
     "PREVIOUS_VALUE(0) + SUM([Sales])",                  "Sales", {}, None, None),
    ("unsupported_window_stdev",
     "WINDOW_STDEV(SUM([Sales]))",                        "Sales", {}, None, None),
    ("unsupported_window_var",
     "WINDOW_VAR(SUM([Sales]))",                          "Sales", {}, None, None),
    ("unsupported_percentile",
     "PERCENTILE([Sales], 0.95)",                         "Sales", {}, None, None),
    ("unsupported_regexp_extract",
     "REGEXP_EXTRACT([Sprint], '(\\d+)')",                "Sales", {}, None, None),
    ("unsupported_regexp_match",
     "REGEXP_MATCH([Name], '^[A-Z]+$')",                  "Sales", {}, None, None),
    ("unsupported_lod_include",
     "{ INCLUDE [Region] : SUM([Sales]) }",               "Sales", {}, None, None),
    ("unsupported_lod_exclude",
     "{ EXCLUDE [Region] : SUM([Sales]) }",               "Sales", {}, None, None),
    ("unsupported_in_list",
     "[Region] IN ('East', 'West')",                      "Sales", {}, None, None),
]


def run() -> int:
    """Write ``tableau_to_pbi/tests/test_dax_corpus.py`` with current
    translator outputs pinned as the expected values."""
    lines: list[str] = []
    lines.append('"""Regression corpus for the DAX translator.\n\n')
    lines.append("This file pins the translator's current behavior across the\n")
    lines.append("most common Tableau formula patterns seen in the workbook corpus,\n")
    lines.append("plus the constructs the translator explicitly rejects (returns\n")
    lines.append("None). Generated by\n")
    lines.append("``python -m tableau_to_pbi_agent.dax_corpus regen-tests``.\n\n")
    lines.append("After intentional changes to the translator, regenerate this\n")
    lines.append("file and inspect the diff — only the expected cases should move.\n")
    lines.append('"""\n\n')
    lines.append("import pytest\n\n")
    lines.append("from tableau_to_pbi.dax_translator import translate_tableau_to_dax\n\n\n")

    lines.append("# (test_id, formula, table, field_to_pbi, parameter_refs, measure_refs, expected)\n")
    lines.append("CASES = [\n")

    n_translated = 0
    n_none = 0
    for case in CASES:
        test_id, formula, table, ftp, prefs, mrefs = case
        actual = translate_tableau_to_dax(
            formula, table, {}, [],
            field_to_pbi=ftp,
            parameter_refs=prefs,
            measure_refs=mrefs,
        )
        if actual is None:
            n_none += 1
        else:
            n_translated += 1
        lines.append("    (\n")
        lines.append(f"        {test_id!r},\n")
        lines.append(f"        {formula!r},\n")
        lines.append(f"        {table!r},\n")
        lines.append(f"        {ftp!r},\n")
        lines.append(f"        {prefs!r},\n")
        lines.append(f"        {mrefs!r},\n")
        lines.append(f"        {actual!r},\n")
        lines.append("    ),\n")
    lines.append("]\n\n\n")

    lines.append("@pytest.mark.parametrize(\n")
    lines.append('    "test_id,formula,table,ftp,prefs,mrefs,expected",\n')
    lines.append("    CASES,\n")
    lines.append("    ids=[c[0] for c in CASES],\n")
    lines.append(")\n")
    lines.append("def test_translator(test_id, formula, table, ftp, prefs, mrefs, expected):\n")
    lines.append('    """Pinned behavior — translator output must match the captured value.\n\n')
    lines.append("    ``expected=None`` means we're asserting the translator rejects this\n")
    lines.append("    construct. Once a new rule lands that DOES translate it, regenerate\n")
    lines.append("    this file and the test for the previously-unsupported case will flip\n")
    lines.append("    from None to the new DAX string.\n")
    lines.append('    """\n')
    lines.append("    actual = translate_tableau_to_dax(\n")
    lines.append("        formula, table, {}, [],\n")
    lines.append("        field_to_pbi=ftp,\n")
    lines.append("        parameter_refs=prefs,\n")
    lines.append("        measure_refs=mrefs,\n")
    lines.append("    )\n")
    lines.append("    assert actual == expected\n")

    OUT_TEST_PATH.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_TEST_PATH}")
    print(f"  {len(CASES)} cases total: {n_translated} translated, {n_none} return None")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
