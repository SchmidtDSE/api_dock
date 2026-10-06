"""

Tests for building SQL for PostgreSQL.

psycopg uses ``%s`` for bound values, so every literal ``%`` in the config's SQL
is doubled exactly once before markers are written. Values are never changed.
``[[table]]`` references become quoted identifiers. DuckDB SQL is unchanged.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from typing import Any, Dict, Optional

import pytest

from api_dock.sql_builder import build_sql_query


#
# CONSTANTS
#
POSTGRES: Dict[str, Any] = {
    "backend": "postgres",
    "tables": {"items": "catalog.items", "order": "catalog.order", "notes": "notes"},
    "queries": {"discounted": "SELECT * FROM [[items]] WHERE label LIKE '%off'"},
}

DUCKDB: Dict[str, Any] = {"tables": {"items": "data/items.parquet"}}


#
# FIXTURES
#
def _build(route: Dict[str, Any], query: Dict[str, str], config: Dict[str, Any] = POSTGRES,
           marker: str = "%s", multi: Optional[Dict[str, Any]] = None) -> Any:
    """Build SQL for a route.

    Args:
        route: Route config.
        query: Query parameters.
        config: Database config.
        marker: Marker for bound values.
        multi: Repeated query parameters.

    Returns:
        Tuple of SQL text and values.
    """
    return build_sql_query(route, config, {}, query, {}, multi or {}, marker=marker)


#
# PUBLIC
#
class TestPercentEscaping:
    """Literal % is doubled once in every SQL source; values are untouched."""

    def test_like_pattern_around_value(self) -> None:
        """The README's ILIKE form gets %% around a %s marker; the value keeps its %."""
        route = {"route": "r",
                 "sql": "SELECT * FROM [[items]] WHERE name ILIKE '%' || {{q}} || '%'"}
        sql, values = _build(route, {"q": "50%"})
        assert sql == (
            'SELECT * FROM "catalog"."items" AS "items" '
            "WHERE name ILIKE '%%' || %s || '%%'"
        )
        assert values == ["50%"]

    def test_literal_percent_s_existing_double_and_modulo(self) -> None:
        """'%s' text, an existing %% and a modulo operator are each doubled once."""
        route = {"route": "r", "sql": "SELECT '%s', '%%', id % 2 FROM [[items]]"}
        sql, values = _build(route, {})
        assert sql == "SELECT '%%s', '%%%%', id %% 2 FROM \"catalog\".\"items\" AS \"items\""
        assert values == []

    def test_comment_and_named_query(self) -> None:
        """Percent signs in comments and in an inlined named query are doubled."""
        route = {"route": "r", "sql": "[[discounted]]"}
        sql, _ = _build(route, {})
        assert sql == (
            "SELECT * FROM \"catalog\".\"items\" AS \"items\" WHERE label LIKE '%%off'"
        )
        route = {"route": "r", "sql": "SELECT 1 /* 100% */ FROM [[items]]"}
        assert _build(route, {})[0] == 'SELECT 1 /* 100%% */ FROM "catalog"."items" AS "items"'

    def test_where_fragments_each_escaped_once(self) -> None:
        """sql, conditional and multivalue fragments are escaped once, then joined."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[items]] WHERE id % 2 = 0",
            "query_params": [
                {"q": {"sql": "name LIKE {{q}} || '%'"}},
                {"kind": {"conditional": {"sale": {"sql": "label LIKE '%sale%'"}}}},
                {"id": {"sql": "id = {{id}}", "multivalue_sql": "id IN {{id}} AND id % 3 = 0"}},
            ],
        }
        sql, values = _build(route, {"q": "a%", "kind": "sale", "id": "2"},
                             multi={"id": ["2", "4"]})
        assert sql == (
            'SELECT * FROM "catalog"."items" AS "items" WHERE id %% 2 = 0 '
            "AND name LIKE %s || '%%' AND label LIKE '%%sale%%' "
            "AND id IN (%s, %s) AND id %% 3 = 0"
        )
        assert values == ["a%", "2", "4"]

    def test_selector_leaf_and_append_fragments(self) -> None:
        """Selector leaves, their sql_append and route sql_append are escaped."""
        route = {
            "route": "r",
            "sql": [{"else": {"sql": "SELECT id % 2 AS odd FROM [[items]]",
                              "sql_append": "GROUP BY id % 2"}}],
            "query_params": [
                {"sort": {"sql_append": "ORDER BY id % 5, {{sort}}", "default": "id"}},
            ],
        }
        sql, values = _build(route, {})
        assert sql == (
            'SELECT id %% 2 AS odd FROM "catalog"."items" AS "items" '
            "GROUP BY id %% 2 ORDER BY id %% 5, id"
        )
        assert values == []

    def test_append_value_with_percent_is_refused(self) -> None:
        """sql_append values are written into the SQL, so % in a value is refused."""
        route = {"route": "r", "sql": "SELECT * FROM [[items]]",
                 "query_params": [{"sort": {"sql_append": "ORDER BY {{sort}}"}}]}
        with pytest.raises(ValueError):
            _build(route, {"sort": "id%"})

    def test_duckdb_unchanged(self) -> None:
        """DuckDB SQL keeps single percent signs and quoted file paths."""
        route = {"route": "r", "sql": "SELECT * FROM [[items]] WHERE name LIKE '%' || {{q}}"}
        sql, values = _build(route, {"q": "x%"}, config=DUCKDB, marker="?")
        assert sql == "SELECT * FROM 'data/items.parquet' AS items WHERE name LIKE '%' || ?"
        assert values == ["x%"]


class TestTableReferences:
    """[[table]] becomes quoted identifiers for PostgreSQL."""

    def test_from_join_and_column_references(self) -> None:
        """After FROM or JOIN the table and alias are written; elsewhere just the alias."""
        route = {
            "route": "r",
            "sql": (
                "SELECT [[items]].name, [[order]].label FROM [[items]] "
                "JOIN [[order]] USING (id) WHERE [[order]].id > 0"
            ),
            "query_params": [{"n": {"sql": "[[items]].name = {{n}}"}}],
        }
        sql, values = _build(route, {"n": "apple"})
        assert sql == (
            'SELECT "items".name, "order".label FROM "catalog"."items" AS "items" '
            'JOIN "catalog"."order" AS "order" USING (id) WHERE "order".id > 0 '
            'AND "items".name = %s'
        )
        assert values == ["apple"]

    def test_unqualified_table(self) -> None:
        """A table without a schema is written as one quoted name."""
        route = {"route": "r", "sql": "SELECT * FROM [[notes]]"}
        assert _build(route, {})[0] == 'SELECT * FROM "notes" AS "notes"'
