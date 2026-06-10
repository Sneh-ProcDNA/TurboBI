import unittest

from tableau_to_pbi.model import SemanticModel
from tableau_to_pbi.report import ReportBuilder


def _date_datasource():
    return {
        "name": "ds1",
        "caption": "Sales",
        "objects": {},
        "columns": [{
            "name": "Order Date",
            "caption": "Order Date",
            "datasource": "ds1",
            "parentTable": "Sales",
            "sourceName": "Order Date",
            "tmdlType": "dateTime",
            "isCalc": False,
            "hidden": False,
            "format": "",
            "role": "dimension",
            "semanticRole": "",
        }],
        "relationships": [],
        "connection": {},
        "extracts": [],
        "extractFilters": [],
        "customSql": [],
        "colsMap": {},
        "columnAliases": {},
        "groupAliases": {},
    }


class TestModelOrder(unittest.TestCase):
    def test_date_hierarchy_columns_exist_before_report_binding(self):
        model = SemanticModel([_date_datasource()])
        model.build()

        self.assertTrue(model.has_column("Sales", "Year-Month of Order Date"))
        self.assertTrue(model.has_column("Sales", "Month of Order Date"))
        self.assertEqual(
            model.resolve_field("ds1", "Year-Month of Order Date"),
            ("Sales", "Year-Month of Order Date"),
        )

        rb = ReportBuilder([], [], [], model)
        projections = {}
        rb.resolver.add_proj(
            projections,
            "Category",
            {"field": "Order Date", "agg": "tmn"},
            "ds1",
            prefer_table="Sales",
        )

        prop = (
            projections["Category"]["projections"][0]
            ["field"]["Column"]["Property"]
        )
        self.assertEqual(prop, "Year-Month of Order Date")

    def test_date_hierarchy_columns_are_not_duplicated_in_tmdl(self):
        model = SemanticModel([_date_datasource()])
        model.build()
        table = next(t for t in model.tables if t["name"] == "Sales")

        tmdl = SemanticModel._render_table_tmdl(table)

        self.assertEqual(tmdl.count("column 'Year-Month of Order Date' ="), 1)
        self.assertIn("hierarchy 'Order Date Hierarchy'", tmdl)
        self.assertIn("column: 'Month of Order Date'", tmdl)
        self.assertIn("sortByColumn: 'Month Number of Order Date'", tmdl)

    def test_direct_generated_date_visual_ref_does_not_create_stub_column(self):
        worksheets = [{
            "name": "Sheet1",
            "datasourceRef": "ds1",
            "rows": [{
                "datasource": "ds1",
                "field": "Year-Month of Order Date",
            }],
        }]
        model = SemanticModel([_date_datasource()], worksheets=worksheets)
        model.build()
        table = next(t for t in model.tables if t["name"] == "Sales")

        cols = [
            c for c in table["columns"]
            if c["name"] == "Year-Month of Order Date"
        ]
        self.assertEqual(len(cols), 1)
        self.assertTrue(cols[0].get("generatedDateHierarchy"))
        self.assertIn("DATE(YEAR(", cols[0].get("daxColumnExpr", ""))


if __name__ == "__main__":
    unittest.main()
