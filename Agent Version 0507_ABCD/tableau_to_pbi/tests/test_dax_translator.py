"""Quick sanity tests for the DAX translator.

The translator wraps bare column refs in MIN() so the result is a
scalar that PBI measures can return. Tests assert the post-wrap form.
"""

import unittest
from tableau_to_pbi.dax_translator import translate_tableau_to_dax


class TestDAXTranslator(unittest.TestCase):
    def _tr(self, formula, table="Sales", aliases=None):
        return translate_tableau_to_dax(formula, table, aliases or {}, [])

    def test_basic_arithmetic(self):
        # Bare column ref gets MIN-wrapped for measure-context safety.
        self.assertEqual(
            self._tr("[Sales] * 1.1"),
            "MIN('Sales'[Sales]) * 1.1",
        )

    def test_sum_aggregation(self):
        # Inside SUM the MIN wrapping is suppressed.
        self.assertEqual(
            self._tr("SUM([Sales])"),
            "SUM('Sales'[Sales])",
        )

    def test_block_if_then_else(self):
        # Tableau block-form IF/THEN/ELSE/END becomes DAX SWITCH(TRUE(),...).
        self.assertEqual(
            self._tr("IF [Sales] > 1000 THEN 'High' ELSE 'Low' END"),
            'SWITCH(TRUE(), MIN(\'Sales\'[Sales]) > 1000, "High", "Low")',
        )

    def test_function_call_if(self):
        # Function-call IF: arguments get the same scalar wrapping.
        self.assertEqual(
            self._tr("IF([Sales] > 1000, 'High', 'Low')"),
            'IF(MIN(\'Sales\'[Sales]) > 1000, "High", "Low")',
        )

    def test_lod_fixed_with_dim(self):
        # FIXED LOD with a dim → CALCULATE + ALLEXCEPT.
        self.assertEqual(
            self._tr("{ FIXED [Region] : SUM([Sales]) }"),
            "CALCULATE(SUM('Sales'[Sales]), ALLEXCEPT('Sales', 'Sales'[Region]))",
        )

    def test_lod_fixed_global(self):
        # Global LOD with explicit field_to_pbi.
        ftp = {"Sales": ("Sales", "Sales")}
        result = translate_tableau_to_dax(
            "{ FIXED : SUM([Sales]) }", "Sales", {}, [], field_to_pbi=ftp,
        )
        self.assertEqual(
            result,
            "CALCULATE(SUM('Sales'[Sales]), ALL('Sales'))",
        )

    def test_unsupported_include_lod(self):
        # INCLUDE/EXCLUDE LODs are still unsupported.
        self.assertIsNone(self._tr("{ INCLUDE [Region] : SUM([Sales]) }"))

    def test_unsupported_table_calc(self):
        self.assertIsNone(self._tr("RUNNING_SUM(SUM([Sales]))"))

    def test_count_distinct(self):
        self.assertEqual(
            self._tr("COUNTD([Customer ID])"),
            "DISTINCTCOUNT('Sales'[Customer ID])",
        )

    def test_year_function(self):
        # YEAR() outer fn with bare col arg: column gets MIN-wrapped.
        self.assertEqual(
            self._tr("YEAR([Order Date])"),
            "YEAR (MIN('Sales'[Order Date]))",
        )

    def test_total_to_allselected(self):
        # TOTAL(expr) -> CALCULATE(expr, ALLSELECTED())
        self.assertEqual(
            self._tr("SUM([Sales]) / TOTAL(SUM([Sales]))"),
            "SUM('Sales'[Sales]) / CALCULATE(SUM('Sales'[Sales]), ALLSELECTED())",
        )

    def test_attr_to_selectedvalue(self):
        # ATTR([col]) -> SELECTEDVALUE('Table'[col])
        self.assertEqual(
            self._tr("ATTR([Region])"),
            "SELECTEDVALUE('Sales'[Region])",
        )

    def test_datetrunc_month(self):
        # DATETRUNC('month', X) -> DATE(YEAR(X), MONTH(X), 1)
        self.assertEqual(
            self._tr("DATETRUNC('month', [Order Date])"),
            "DATE(YEAR(MIN('Sales'[Order Date])), MONTH(MIN('Sales'[Order Date])), 1)",
        )


if __name__ == "__main__":
    unittest.main()
