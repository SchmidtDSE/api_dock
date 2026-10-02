"""

Tests for bound parameters in the SQL builder.

Every caller-supplied value (path variable, query parameter, default, cookie)
must reach DuckDB as a bound value: ``build_sql_query`` returns the SQL text with
a ``?`` marker for each ``{{var}}`` and a list of values in marker order.
``sql_append`` fragments are the exception; their values are checked against an
allowed-character pattern and written into the SQL text.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from typing import Any, Dict

import pytest

from api_dock.sql_builder import build_sql_query, process_query_parameters


#
# CONSTANTS
#
DATABASE_CONFIG: Dict[str, Any] = {"tables": {"d": "data/d.parquet"}}

TABLE_SQL: str = "'data/d.parquet' AS d"


#
# PUBLIC
#
class TestBuildSqlQueryValues:
    """build_sql_query writes markers and returns values in marker order."""

    def test_path_and_query_values_are_bound(self) -> None:
        """Path and query values become markers; [[table]] expands as before."""
        route = {
            "route": "groups/{{group_id}}/items",
            "sql": "SELECT * FROM [[d]] WHERE d.group_id = {{group_id}}",
            "query_params": [{"weight": {"sql": "[[d]].weight >= {{weight}}"}}],
        }
        sql, values = build_sql_query(
            route, DATABASE_CONFIG, {"group_id": "42"}, {"weight": "0 OR true"}
        )
        assert sql == (
            f"SELECT * FROM {TABLE_SQL} WHERE d.group_id = ? AND d.weight >= ?"
        )
        assert values == ["42", "0 OR true"]

    def test_variable_used_twice_is_bound_twice(self) -> None:
        """Each use of a variable gets its own marker and list entry."""
        route = {"route": "r", "sql": "SELECT * FROM [[d]] WHERE a = {{x}} OR b = {{x}}"}
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"x": "v"})
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE a = ? OR b = ?"
        assert values == ["v", "v"]

    def test_values_follow_marker_order_across_pieces(self) -> None:
        """Base SQL values come first, then WHERE fragments in declaration order."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]] WHERE user_id = {{cookies.user_id}}",
            "query_params": [
                {"b": {"sql": "b = {{b}}"}},
                {"a": {"sql": "a = {{a}}"}},
            ],
        }
        sql, values = build_sql_query(
            route, DATABASE_CONFIG, {}, {"a": "A", "b": "B"}, {"user_id": "alice"}
        )
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE user_id = ? AND b = ? AND a = ?"
        assert values == ["alice", "B", "A"]

    def test_value_is_not_scanned_for_variables(self) -> None:
        """A value that looks like {{var}} is bound as that literal text."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]] WHERE name = {{name}}",
            "query_params": [{"token": {"sql": "token = {{cookies.session_token}}"}}],
        }
        sql, values = build_sql_query(
            route, DATABASE_CONFIG, {},
            {"name": "{{cookies.session_token}}", "token": "1"},
            {"session_token": "secret"},
        )
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE name = ? AND token = ?"
        assert values == ["{{cookies.session_token}}", "secret"]

    def test_quote_in_value_is_bound_unchanged(self) -> None:
        """Quotes and SQL text in a value are not escaped or interpreted."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]]",
            "query_params": [{"name": {"sql": "name = {{name}}"}}],
        }
        for value in ["O'Brien", "'; DROP TABLE x; --"]:
            sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"name": value})
            assert sql == f"SELECT * FROM {TABLE_SQL} WHERE name = ?"
            assert values == [value]

    def test_multivalue_writes_one_marker_per_value(self) -> None:
        """multivalue_sql with n values writes (?, ..., ?) and the values in order."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]]",
            "query_params": [{
                "id": {"sql": "id = {{id}}", "multivalue_sql": "id IN {{id}}"},
            }],
        }
        sql, values = build_sql_query(
            route, DATABASE_CONFIG, {}, {"id": "4"}, {}, {"id": ["1", "4", "2"]}
        )
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE id IN (?, ?, ?)"
        assert values == ["1", "4", "2"]

    def test_default_value_is_bound_as_string(self) -> None:
        """A config default is converted to a string and bound."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]]",
            "query_params": [{"min_weight": {"sql": "weight >= {{min_weight}}", "default": 0.5}}],
        }
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {})
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE weight >= ?"
        assert values == ["0.5"]

    def test_conditional_sql_is_bound(self) -> None:
        """A conditional branch's sql binds its variables."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]]",
            "query_params": [
                {"since": {"default": "2024-01-01"}},
                {"mode": {"conditional": {
                    "recent": {"sql": "ts >= {{since}}"},
                    "all": {"sql": ""},
                }}},
            ],
        }
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"mode": "recent"})
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE ts >= ?"
        assert values == ["2024-01-01"]

        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"mode": "all"})
        assert sql == f"SELECT * FROM {TABLE_SQL}"
        assert values == []

    def test_named_query_is_bound(self) -> None:
        """Variables inside a [[query]] reference are bound."""
        queries = {"by_id": "SELECT * FROM [[d]] WHERE id = {{id}}"}
        database = {**DATABASE_CONFIG, "queries": queries}
        route = {"route": "items/{{id}}", "sql": "[[by_id]]"}
        sql, values = build_sql_query(route, database, {"id": "7"})
        assert sql == f"SELECT * FROM {TABLE_SQL} WHERE id = ?"
        assert values == ["7"]

    def test_config_text_is_left_unchanged(self) -> None:
        """A % or ? already in config text is not treated as a marker or value."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]] WHERE name LIKE 'a%' AND note = '?' AND id = {{id}}",
        }
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"id": "1"})
        assert sql == (
            f"SELECT * FROM {TABLE_SQL} WHERE name LIKE 'a%' AND note = '?' AND id = ?"
        )
        assert values == ["1"]

    def test_missing_variable_raises_value_error(self) -> None:
        """A {{var}} with no value available is an error, not left in the SQL."""
        route = {"route": "r", "sql": "SELECT * FROM [[d]] WHERE id = {{nope}}"}
        with pytest.raises(ValueError):
            build_sql_query(route, DATABASE_CONFIG, {}, {})

    def test_marker_argument_is_written_for_every_value(self) -> None:
        """The given marker replaces ? in base SQL, WHERE and multivalue fragments."""
        route = {
            "route": "groups/{{group_id}}/items",
            "sql": "SELECT * FROM [[d]] WHERE d.group_id = {{group_id}}",
            "query_params": [
                {"weight": {"sql": "d.weight >= {{weight}}"}},
                {"id": {"sql": "id = {{id}}", "multivalue_sql": "id IN {{id}}"}},
            ],
        }
        sql, values = build_sql_query(
            route, DATABASE_CONFIG, {"group_id": "42"}, {"weight": "0.5", "id": "2"},
            {}, {"id": ["1", "2"]}, marker="%s",
        )
        assert sql == (
            f"SELECT * FROM {TABLE_SQL} WHERE d.group_id = %s"
            " AND d.weight >= %s AND id IN (%s, %s)"
        )
        assert values == ["42", "0.5", "1", "2"]


class TestSqlAppendValues:
    """sql_append values are checked and written into the SQL text, not bound."""

    def test_route_sql_append_is_written_into_text(self) -> None:
        """sort/direction/limit behave as before and add no bound values."""
        route = {
            "route": "r",
            "sql": "SELECT * FROM [[d]]",
            "query_params": [
                {"direction": {"default": "DESC"}},
                {"sort": {"sql_append": "ORDER BY {{sort}} {{direction}}", "default": "id"}},
                {"limit": {"sql_append": "LIMIT {{limit}}"}},
            ],
        }
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"limit": "20"})
        assert sql == f"SELECT * FROM {TABLE_SQL} ORDER BY id DESC LIMIT 20"
        assert values == []

    def test_branch_sql_append_uses_character_check(self) -> None:
        """A selector branch's sql_append is checked like route-level sql_append."""
        route = {
            "route": "r",
            "sql": [
                {"when": "group", "then": {
                    "sql": "SELECT name, COUNT(*) FROM [[d]]",
                    "sql_append": "GROUP BY {{group}}",
                }},
            ],
        }
        sql, values = build_sql_query(route, DATABASE_CONFIG, {}, {"group": "name"})
        assert sql == f"SELECT name, COUNT(*) FROM {TABLE_SQL} GROUP BY name"
        assert values == []

        with pytest.raises(ValueError):
            build_sql_query(route, DATABASE_CONFIG, {}, {"group": "name; DROP TABLE x"})


class TestResponseSubstitution:
    """response bodies are JSON, so {{var}} is replaced as plain text."""

    def test_response_value_is_not_sql_quoted(self) -> None:
        """A body containing an SQL-like word does not quote the value."""
        route = {
            "route": "r",
            "query_params": [{"q": {"response": {"message": "results for {{q}}"}}}],
        }
        early, body, status, _ = process_query_parameters(route, {"q": "O'Brien"}, {})
        assert early is True
        assert status == 200
        assert body == {"message": "results for O'Brien"}

    def test_conditional_response_value_is_not_sql_quoted(self) -> None:
        """A conditional response substitutes values as plain text."""
        route = {
            "route": "r",
            "query_params": [
                {"name": {"default": "x"}},
                {"mode": {"conditional": {
                    "info": {"response": {"text": "SELECT help for {{name}}"}},
                }}},
            ],
        }
        _, body, _, _ = process_query_parameters(route, {"mode": "info", "name": "bob"}, {})
        assert body == {"text": "SELECT help for bob"}
