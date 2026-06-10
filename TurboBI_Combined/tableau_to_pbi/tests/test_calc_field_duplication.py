"""Regression: a single Tableau calc field must emit exactly one TMDL
artifact across the whole semantic model.

Two real bug shapes the converter could previously hit:

1. **Cross-group calc field with parentTable=""** — the partitioner in
   ``_partition_for_table`` appended such a calc to EVERY group whose
   columns intersected its formula references. The same calc field then
   flowed through ``_build_measures`` once per table, producing
   duplicate calc columns / measures on multiple tables. Corpus
   evidence: ``Pipeline Name`` calc on Production Report emitted as a
   DAX calc column on both ``MATILLION_PIPELINE_CANCELLED_LOGS_FINAL``
   and ``MATILLION_PIPELINE_LOGS_FINAL``; ``Region`` on
   FINAL_PLC_DEPLOYMENT_TRACKER + FINAL_PLC_PROD_RUN_LOGS; many more.

2. **Calc field with daxColumnExpr AND non-empty formula** — the
   parser emits it as a DAX calc column via the column-render path
   (line ~2960 of model.py), but ``_build_measures`` also iterated it
   and could emit a second column or a same-named measure. Tableau
   groups in the corpus naturally avoid this (their ``formula`` attr
   is empty), but an unusual XML shape could trigger it.

Both scenarios are pinned here so they can't silently regress.
"""

from __future__ import annotations

import unittest

from tableau_to_pbi.model import SemanticModel


def _bare_col(name, parent_table, role="dimension", tmdl="string", datatype="string"):
    return {
        "name":          name,
        "caption":       name,
        "datasource":    "ds1",
        "parentTable":   parent_table,
        "sourceName":    name,
        "tmdlType":      tmdl,
        "datatype":      datatype,
        "isCalc":        False,
        "hidden":        False,
        "format":        "",
        "role":          role,
        "semanticRole":  "",
        "daxColumnExpr": "",
        "formula":       "",
    }


def _calc_col(name, formula, parent_table="", role="dimension",
              tmdl="string", datatype="string", dax_column_expr=""):
    return {
        "name":          name,
        "caption":       name,
        "datasource":    "ds1",
        "parentTable":   parent_table,
        "sourceName":    "",
        "tmdlType":      tmdl,
        "datatype":      datatype,
        "isCalc":        True,
        "hidden":        False,
        "format":        "",
        "role":          role,
        "semanticRole":  "",
        "daxColumnExpr": dax_column_expr,
        "formula":       formula,
    }


def _ds(columns):
    return {
        "name":           "ds1",
        "caption":        "Test",
        "objects":        {},
        "columns":        columns,
        "relationships":  [],
        "connection":     {},
        "extracts":       [],
        "extractFilters": [],
        "customSql":      [],
        "colsMap":        {},
        "columnAliases":  {},
        "groupAliases":   {},
    }


class TestCalcFieldDuplication(unittest.TestCase):
    def test_cross_group_calc_emits_on_exactly_one_table(self):
        """A calc field whose formula references columns from TWO groups
        must not be partitioned onto BOTH. It should land on the group
        with the highest column-overlap (ties to first iteration order),
        and emit exactly once across the model.
        """
        ds = _ds([
            _bare_col("A_field", "TableA"),
            _bare_col("A_other", "TableA"),
            _bare_col("B_field", "TableB"),
            # Cross-group calc — references one column from each table.
            # Without parentTable set, the old partitioner would append
            # this calc to BOTH TableA and TableB's _pending_calc_fields.
            _calc_col(
                "CrossCalc",
                formula="IIF([A_field] > 0, [B_field], 0)",
                parent_table="",
                tmdl="string",
            ),
        ])
        model = SemanticModel([ds])
        model.build()

        # Across all tables, the calc must appear at most once.
        total_emissions = 0
        for t in model.tables:
            for c in t.get("columns") or []:
                if c["name"] == "CrossCalc":
                    total_emissions += 1
            for m in t.get("measures") or []:
                # Either the raw name or a deduped "(Measure)" variant.
                if m["name"] == "CrossCalc" or m["name"].startswith("CrossCalc "):
                    total_emissions += 1
        self.assertEqual(
            total_emissions, 1,
            f"Cross-group calc field emitted {total_emissions} times across "
            f"the model. Expected exactly 1."
        )

    def test_parent_table_explicit_wins(self):
        """When a calc field DOES have parentTable set, the partitioner
        respects it — no cross-overlap logic kicks in."""
        ds = _ds([
            _bare_col("A_field", "TableA"),
            _bare_col("B_field", "TableB"),
            _calc_col(
                "ExplicitCalc",
                formula="[B_field] * 2",
                parent_table="TableA",       # explicit anchor
                tmdl="int64",
            ),
        ])
        model = SemanticModel([ds])
        model.build()

        # Should end up on TableA (the explicit parentTable), NOT TableB
        # even though the formula references TableB's column.
        seen_on = []
        for t in model.tables:
            for c in t.get("columns") or []:
                if c["name"] == "ExplicitCalc":
                    seen_on.append((t["name"], "column"))
            for m in t.get("measures") or []:
                if m["name"] == "ExplicitCalc" or m["name"].startswith("ExplicitCalc "):
                    seen_on.append((t["name"], "measure"))
        self.assertEqual(len(seen_on), 1,
                         f"ExplicitCalc emitted {len(seen_on)} times: {seen_on}")
        self.assertTrue(any(t == "TableA" for t, _ in seen_on),
                        f"Expected ExplicitCalc on TableA; got {seen_on}")

    def test_dax_column_expr_calc_does_not_double_emit(self):
        """A calc field with daxColumnExpr set must NOT also produce a
        measure via _build_measures (the parser's column-render path
        will emit it as `column 'X' = <expr>`)."""
        ds = _ds([
            _bare_col("Region", "Sales"),
            # Group/categorical-bin-like calc: has daxColumnExpr set,
            # AND has a non-empty formula. The parser's normal group
            # path produces formula="" + daxColumnExpr=<switch>, but if
            # _merge_worksheet_calc_fields or any other path reattaches
            # a formula, we now skip the measure pass.
            _calc_col(
                "Region Group",
                formula="IF [Region] = 'East' THEN 'Americas' ELSE [Region] END",
                parent_table="Sales",
                tmdl="string",
                dax_column_expr=(
                    "SWITCH(TRUE(), 'Sales'[Region] = \"East\", \"Americas\", "
                    "'Sales'[Region])"
                ),
            ),
        ])
        model = SemanticModel([ds])
        model.build()
        sales = next(t for t in model.tables if t["name"] == "Sales")
        meas_names = [m["name"] for m in sales.get("measures") or []]
        # No same-named measure should appear.
        self.assertFalse(
            any("Region Group" in m for m in meas_names),
            f"Calc field with daxColumnExpr also emitted as measure: "
            f"{meas_names}"
        )

    def test_regular_calc_still_emits_a_measure(self):
        """Sanity: a regular aggregation calc still flows through the
        normal measure path."""
        ds = _ds([
            _bare_col("Sales Amount", "Sales", tmdl="double", datatype="real"),
            _calc_col(
                "Total Sales",
                formula="SUM([Sales Amount])",
                parent_table="Sales",
                tmdl="double",
                datatype="real",
                role="measure",
            ),
        ])
        model = SemanticModel([ds])
        model.build()
        sales = next(t for t in model.tables if t["name"] == "Sales")
        meas_names = [m["name"] for m in sales.get("measures") or []]
        self.assertIn("Total Sales", meas_names,
                      f"Regular calc didn't emit as measure. Measures: {meas_names}")


if __name__ == "__main__":
    unittest.main()
