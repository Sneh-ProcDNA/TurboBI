import unittest
from xml.etree import ElementTree as ET

from tableau_to_pbi.hyper import match_extract_filters_to_hyper_table
from tableau_to_pbi.model import SemanticModel
from tableau_to_pbi.parser import TWBParser


class TestExtractFilters(unittest.TestCase):
    def test_parse_field_ref_strips_role_suffix_without_datasource(self):
        role, field = TWBParser._parse_field_ref("[none:Region:nk]")

        self.assertEqual(role, "none")
        self.assertEqual(field, "Region")

    def test_parse_extract_filter_uses_groupfilter_level_fallback(self):
        ds = ET.fromstring(
            """
            <datasource>
              <extract>
                <filter class="categorical">
                  <groupfilter function="member"
                               level="[none:Region:nk]"
                               member="&quot;West&quot;" />
                </filter>
              </extract>
            </datasource>
            """
        )
        parser = TWBParser("unused.twb")

        filters = parser._parse_extract_filters(
            ds,
            {"Region": ("Sales", "Region")},
        )

        self.assertEqual(filters, [{
            "column": "Region",
            "table": "Sales",
            "class": "categorical",
            "rawColumn": "[none:Region:nk]",
            "operator": "in",
            "values": ["West"],
        }])

    def test_disabled_extract_metadata_does_not_mark_live_datasource_extract(self):
        ds = ET.fromstring(
            """
            <datasource>
              <extract enabled="false">
                <connection class="hyper"
                            dbname="Data/TableauTemp/Stale.hyper" />
                <filter class="categorical" column="[none:Region:nk]">
                  <groupfilter function="member"
                               level="[none:Region:nk]"
                               member="&quot;West&quot;" />
                </filter>
              </extract>
            </datasource>
            """
        )
        parser = TWBParser("unused.twb")

        self.assertEqual(TWBParser._parse_hyper_extracts(ds), [])
        self.assertEqual(
            parser._parse_extract_filters(ds, {"Region": ("Sales", "Region")}),
            [],
        )

    def test_parse_datasource_filter_outside_extract(self):
        ds = ET.fromstring(
            """
            <datasource>
              <filter class="categorical" column="[none:Region:nk]">
                <groupfilter function="member"
                             level="[none:Region:nk]"
                             member="&quot;West&quot;" />
              </filter>
            </datasource>
            """
        )
        parser = TWBParser("unused.twb")

        filters = parser._parse_datasource_filters(
            ds,
            {"Region": ("Sales", "Region")},
        )

        self.assertEqual(filters, [{
            "column": "Region",
            "table": "Sales",
            "class": "categorical",
            "rawColumn": "[none:Region:nk]",
            "operator": "in",
            "values": ["West"],
        }])

    def test_hyper_filter_table_hint_can_match_tmdl_table(self):
        filters = [{
            "column": "Region",
            "table": "Sales",
            "operator": "in",
            "values": ["West"],
        }]

        matched = match_extract_filters_to_hyper_table(
            "Extract.Extract",
            ["Region"],
            filters,
            "Sales",
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["column"], "Region")

    def test_hyper_filter_table_hint_still_rejects_wrong_table(self):
        filters = [{
            "column": "Region",
            "table": "Other Table",
            "operator": "in",
            "values": ["West"],
        }]

        matched = match_extract_filters_to_hyper_table(
            "Extract.Extract",
            ["Region"],
            filters,
            "Sales",
        )

        self.assertEqual(matched, [])

    def test_live_partition_applies_extract_filters(self):
        table = {
            "name": "Sales",
            "caption": "Sales",
            "lineageTag": "00000000-0000-0000-0000-000000000001",
            "connection": {
                "class": "sqlserver",
                "server": "localhost",
                "dbname": "Warehouse",
                "schema": "dbo",
            },
            "columns": [{
                "name": "Region",
                "sourceCol": "Region",
                "tmdlType": "string",
                "lineageTag": "00000000-0000-0000-0000-000000000002",
                "role": "dimension",
                "hidden": False,
            }],
            "measures": [],
        }

        tmdl = SemanticModel._render_table_tmdl(
            table,
            csv_path=None,
            hyper_cols=None,
            extract_filters=[{
                "column": "Region",
                "operator": "in",
                "values": ["West"],
            }],
        )

        self.assertIn("mode: directQuery", tmdl)
        self.assertIn("Table.SelectRows(ExtractFilterSource", tmdl)
        self.assertIn('List.Contains({"West"}, [#"Region"])', tmdl)

    def test_source_filters_map_to_live_table_without_hyper(self):
        model = SemanticModel.__new__(SemanticModel)
        model.datasources = [{
            "name": "ds1",
            "extractFilters": [{
                "column": "Region",
                "table": "Sales",
                "operator": "in",
                "values": ["West"],
            }],
        }]
        table = {
            "name": "Sales",
            "caption": "Sales",
            "datasource": "ds1",
            "columns": [{
                "name": "Region",
                "sourceCol": "Region",
                "tmdlType": "string",
            }],
        }

        filters = model._source_filters_for_table(table)

        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]["column"], "Region")

    def test_extract_datasource_live_partition_uses_import_mode(self):
        table = {
            "name": "Sales",
            "caption": "Sales",
            "lineageTag": "00000000-0000-0000-0000-000000000003",
            "connection": {
                "class": "sqlserver",
                "server": "localhost",
                "dbname": "Warehouse",
                "schema": "dbo",
            },
            "extracts": ["Data/TableauTemp/Sales.hyper"],
            "columns": [{
                "name": "Region",
                "sourceCol": "Region",
                "tmdlType": "string",
                "lineageTag": "00000000-0000-0000-0000-000000000004",
                "role": "dimension",
                "hidden": False,
            }],
            "measures": [],
        }

        tmdl = SemanticModel._render_table_tmdl(table, csv_path=None)

        self.assertIn("mode: import", tmdl)
        self.assertNotIn("mode: directQuery", tmdl)


if __name__ == "__main__":
    unittest.main()
