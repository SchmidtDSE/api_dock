"""

Tests for multivalue query parameter support in the SQL builder.

Covers the multivalue_sql feature: when a query parameter key is repeated in
the URL (e.g. ?recording_id=1&recording_id=4), a param configured with a
multivalue_sql template renders that template with {{param}} expanded to a
parenthesized SQL value list, instead of the single-value sql template.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from api_dock.route_mapper import collect_multi_query_params
from api_dock.sql_builder import build_sql_query, build_where_clause_from_params


#
# CONSTANTS
#
DATABASE_CONFIG = {
    "tables": {"detections": "data/detections.parquet"},
}

ROUTE_CONFIG = {
    "route": "detections",
    "sql": "SELECT [[detections]].* FROM [[detections]]",
    "query_params": [
        {
            "recording_id": {
                "sql": "[[detections]].recording_id == {{recording_id}}",
                "multivalue_sql": "[[detections]].recording_id in {{recording_id}}",
            }
        },
        {"scientific_name": {"sql": "[[detections]].scientific_name = '{{scientific_name}}'"}},
    ],
}


#
# PUBLIC
#
class TestCollectMultiQueryParams:
    """Tests for grouping repeated query string pairs."""

    def test_groups_repeated_keys(self) -> None:
        """Repeated keys are grouped into a list preserving URL order."""
        items = [("recording_id", "4"), ("recording_id", "1"), ("name", "Gryllus")]
        result = collect_multi_query_params(items)
        assert result == {"recording_id": ["4", "1"], "name": ["Gryllus"]}

    def test_empty_items(self) -> None:
        """An empty iterable yields an empty mapping."""
        assert collect_multi_query_params([]) == {}


class TestMultivalueWhereClause:
    """Tests for build_where_clause_from_params multivalue handling."""

    def test_multiple_values_use_multivalue_sql(self) -> None:
        """More than one value triggers the multivalue_sql IN-list template."""
        query_params = {"recording_id": "1"}  # collapsed last-wins value
        multi = {"recording_id": ["4", "1"]}
        fragments = build_where_clause_from_params(
            ROUTE_CONFIG, query_params, {}, multi
        )
        assert fragments == ["[[detections]].recording_id in ('4', '1')"]

    def test_single_value_uses_sql(self) -> None:
        """A single value falls back to the single-value sql template."""
        query_params = {"recording_id": "4"}
        multi = {"recording_id": ["4"]}
        fragments = build_where_clause_from_params(
            ROUTE_CONFIG, query_params, {}, multi
        )
        assert fragments == ["[[detections]].recording_id == '4'"]

    def test_no_multi_dict_uses_sql(self) -> None:
        """Omitting multi_query_params preserves the original single-value behavior."""
        query_params = {"recording_id": "4"}
        fragments = build_where_clause_from_params(ROUTE_CONFIG, query_params, {})
        assert fragments == ["[[detections]].recording_id == '4'"]

    def test_multivalue_escapes_quotes(self) -> None:
        """Each value in the list is quote-escaped like single values are."""
        route_config = {
            "query_params": [
                {
                    "name": {
                        "sql": "name = '{{name}}'",
                        "multivalue_sql": "name in {{name}}",
                    }
                }
            ]
        }
        multi = {"name": ["O'Brien", "Smith"]}
        fragments = build_where_clause_from_params(
            route_config, {"name": "Smith"}, {}, multi
        )
        assert fragments == ["name in ('O''Brien', 'Smith')"]


class TestBuildSqlQueryMultivalue:
    """End-to-end SQL assembly with multivalue params."""

    def test_multivalue_combined_with_other_param(self) -> None:
        """Multivalue IN-list combines with other WHERE fragments via AND."""
        query_params = {"recording_id": "1", "scientific_name": "Gryllus fultoni"}
        multi = {
            "recording_id": ["4", "1"],
            "scientific_name": ["Gryllus fultoni"],
        }
        sql = build_sql_query(
            ROUTE_CONFIG, DATABASE_CONFIG, {}, query_params, {}, multi
        )
        expected = (
            "SELECT detections.* FROM 'data/detections.parquet' AS detections "
            "WHERE detections.recording_id in ('4', '1') "
            "AND detections.scientific_name = 'Gryllus fultoni'"
        )
        assert sql == expected

    def test_single_value_end_to_end(self) -> None:
        """A single recording_id uses the == template end to end."""
        query_params = {"recording_id": "4"}
        multi = {"recording_id": ["4"]}
        sql = build_sql_query(
            ROUTE_CONFIG, DATABASE_CONFIG, {}, query_params, {}, multi
        )
        expected = (
            "SELECT detections.* FROM 'data/detections.parquet' AS detections "
            "WHERE detections.recording_id == '4'"
        )
        assert sql == expected
