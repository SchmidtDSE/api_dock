"""

Tests for the conditional SQL selector in the SQL builder.

Covers ``resolve_route_sql`` (the decision-tree resolver) and its end-to-end
composition with the existing query_params WHERE/append pipeline in
``build_sql_query``, plus the SqlSelectionError path through
``RouteMapper.map_database_route``.

The selector lets a route's ``sql`` be a plain string (today's behavior) or a
first-match-wins list of rules that pick a base SQL node from the presence and
value of path/query/cookie params.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from unittest.mock import patch

import pytest

from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import build_sql_query, resolve_route_sql, SqlSelectionError


#
# CONSTANTS
#
DATABASE_CONFIG = {"tables": {"detections": "data/detections.parquet"}}

COUNT_ROUTE = {
    "route": "detections",
    "sql": [
        {
            "when": "count",
            "then": {
                "sql": (
                    "SELECT [[detections]].common_name, [[detections]].scientific_name, "
                    "COUNT(*) AS count FROM [[detections]]"
                ),
                "sql_append": (
                    "GROUP BY [[detections]].common_name, [[detections]].scientific_name"
                ),
            },
        },
        {"else": "SELECT [[detections]].* FROM [[detections]]"},
    ],
    "query_params": [
        {
            "recording": {
                "sql": "[[detections]].recording_id = {{recording}}",
                "multivalue_sql": "[[detections]].recording_id IN {{recording}}",
            }
        },
        {"limit": {"sql_append": "LIMIT {{limit}}"}},
    ],
}


#
# PUBLIC
#
class TestResolveRouteSql:
    """Unit tests for the decision-tree resolver."""

    def test_plain_string_passthrough(self) -> None:
        """A plain string sql resolves to itself with no branch appends."""
        assert resolve_route_sql({"sql": "SELECT 1"}, {}) == ("SELECT 1", [])

    def test_missing_sql_returns_empty(self) -> None:
        """A route with no sql resolves to an empty base (legacy behavior)."""
        assert resolve_route_sql({}, {}) == ("", [])

    def test_when_truthy_fires(self) -> None:
        """A bare when fires on a truthy value."""
        rc = {"sql": [{"when": "count", "then": "HIST"}, {"else": "BASE"}]}
        assert resolve_route_sql(rc, {"count": "True"}) == ("HIST", [])

    def test_when_absent_falls_to_else(self) -> None:
        """An absent query param skips the rule and falls to else."""
        rc = {"sql": [{"when": "count", "then": "HIST"}, {"else": "BASE"}]}
        assert resolve_route_sql(rc, {}) == ("BASE", [])

    def test_when_falsy_falls_to_else(self) -> None:
        """A falsy value does not fire a bare when."""
        rc = {"sql": [{"when": "count", "then": "HIST"}, {"else": "BASE"}]}
        assert resolve_route_sql(rc, {"count": "false"}) == ("BASE", [])

    def test_leaf_object_with_append(self) -> None:
        """A leaf object contributes base sql plus post-WHERE append clauses."""
        rc = {
            "sql": [
                {"when": "count", "then": {"sql": "HIST", "sql_append": "GROUP BY x"}},
                "BASE",
            ]
        }
        assert resolve_route_sql(rc, {"count": "1"}) == ("HIST", ["GROUP BY x"])

    def test_trailing_string_default(self) -> None:
        """A trailing bare string acts as the default (implicit else)."""
        rc = {"sql": [{"when": "count", "then": "HIST"}, "BASE"]}
        assert resolve_route_sql(rc, {}) == ("BASE", [])

    def test_equals_literal_case_insensitive(self) -> None:
        """equals matches a specific value, case-insensitively, and only that value."""
        rc = {"sql": [{"when": "mode", "equals": "true", "then": "T"}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"mode": "True"}) == ("T", [])
        assert resolve_route_sql(rc, {"mode": "1"}) == ("E", [])

    def test_value_map_precedence_literal_over_truthy(self) -> None:
        """A value map prefers an exact literal over _truthy regardless of order."""
        rc = {"sql": [{"when": "count", "match": {"_truthy": "TRU", "true": "LIT"}}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"count": "true"}) == ("LIT", [])
        assert resolve_route_sql(rc, {"count": "1"}) == ("TRU", [])

    def test_value_map_default_requires_presence(self) -> None:
        """_default fires for any present value but not when the param is absent."""
        rc = {"sql": [{"when": "count", "match": {"_default": "D"}}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"count": "anything"}) == ("D", [])
        assert resolve_route_sql(rc, {}) == ("E", [])

    def test_value_map_absent(self) -> None:
        """_absent fires only when the param is absent."""
        rc = {"sql": [{"when": "count", "match": {"_absent": "A", "_truthy": "T"}}, {"else": "E"}]}
        assert resolve_route_sql(rc, {}) == ("A", [])
        assert resolve_route_sql(rc, {"count": "1"}) == ("T", [])

    def test_value_map_falsy(self) -> None:
        """_falsy fires when a present value is falsy."""
        rc = {"sql": [{"when": "flag", "match": {"_falsy": "F", "_truthy": "T"}}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"flag": "0"}) == ("F", [])
        assert resolve_route_sql(rc, {"flag": "yes"}) == ("T", [])

    def test_multi_when_all_truthy(self) -> None:
        """A list when with then fires only when every param is truthy (AND)."""
        rc = {"sql": [{"when": ["a", "b"], "then": "BOTH"}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"a": "1", "b": "1"}) == ("BOTH", [])
        assert resolve_route_sql(rc, {"a": "1"}) == ("E", [])

    def test_multi_match_case_list_with_any(self) -> None:
        """A positional case list matches top-to-bottom, honoring _any wildcards."""
        rc = {
            "sql": [
                {
                    "when": ["count", "something"],
                    "match": [
                        {"values": ["_any", "x"], "then": "SX"},
                        {"values": ["_truthy", "_absent"], "then": "CT"},
                        {"default": "DEF"},
                    ],
                },
                {"else": "E"},
            ]
        }
        assert resolve_route_sql(rc, {"something": "x"}) == ("SX", [])
        assert resolve_route_sql(rc, {"count": "1"}) == ("CT", [])
        assert resolve_route_sql(rc, {"count": "1", "something": "y"}) == ("DEF", [])

    def test_nested_rule_list(self) -> None:
        """A branch payload may itself be a nested rule list."""
        rc = {
            "sql": [
                {
                    "when": "some_value",
                    "match": {
                        "4": [{"when": "count", "then": "FOUR_COUNT"}, {"else": "FOUR"}],
                        "_default": "OTHER",
                    },
                },
                {"else": "E"},
            ]
        }
        assert resolve_route_sql(rc, {"some_value": "4", "count": "1"}) == ("FOUR_COUNT", [])
        assert resolve_route_sql(rc, {"some_value": "4"}) == ("FOUR", [])
        assert resolve_route_sql(rc, {"some_value": "9"}) == ("OTHER", [])

    def test_cookie_param_selection(self) -> None:
        """Cookies participate in selection via the cookies.<name> key."""
        rc = {"sql": [{"when": "cookies.role", "equals": "admin", "then": "A"}, {"else": "E"}]}
        assert resolve_route_sql(rc, {"cookies.role": "admin"}) == ("A", [])
        assert resolve_route_sql(rc, {"cookies.role": "user"}) == ("E", [])

    def test_no_match_raises_default_400(self) -> None:
        """No branch and no default raises a 400 SqlSelectionError."""
        rc = {"sql": [{"when": "count", "then": "HIST"}]}
        with pytest.raises(SqlSelectionError) as exc:
            resolve_route_sql(rc, {})
        assert exc.value.status_code == 400
        assert "error" in exc.value.response

    def test_no_match_custom_response(self) -> None:
        """A no_match rule provides a custom error body and status."""
        rc = {
            "sql": [
                {"when": "recording", "then": "R"},
                {"no_match": {"error": "recording required", "http_status": 422}},
            ]
        }
        with pytest.raises(SqlSelectionError) as exc:
            resolve_route_sql(rc, {})
        assert exc.value.status_code == 422
        assert exc.value.response["error"] == "recording required"


class TestSelectorComposition:
    """End-to-end SQL assembly combining the selector with query_params."""

    def test_count_mode_composes_where_and_group_by(self) -> None:
        """count=True selects the histogram base; recording filter + GROUP BY compose."""
        sql, values = build_sql_query(
            COUNT_ROUTE, DATABASE_CONFIG, {}, {"recording": "1", "count": "True"}, {}, {}
        )
        expected = (
            "SELECT detections.common_name, detections.scientific_name, COUNT(*) AS count "
            "FROM 'data/detections.parquet' AS detections "
            "WHERE (detections.recording_id = ?) "
            "GROUP BY detections.common_name, detections.scientific_name"
        )
        assert sql == expected
        assert values == ["1"]

    def test_count_mode_with_shared_limit(self) -> None:
        """A shared LIMIT append lands after the branch GROUP BY."""
        sql, _ = build_sql_query(
            COUNT_ROUTE, DATABASE_CONFIG, {},
            {"recording": "1", "count": "1", "limit": "20"}, {}, {}
        )
        assert sql.endswith("GROUP BY detections.common_name, detections.scientific_name LIMIT 20")

    def test_default_mode_returns_rows(self) -> None:
        """Without count, the else branch returns rows with the recording filter."""
        sql, values = build_sql_query(
            COUNT_ROUTE, DATABASE_CONFIG, {}, {"recording": "1"}, {}, {}
        )
        expected = (
            "SELECT detections.* FROM 'data/detections.parquet' AS detections "
            "WHERE (detections.recording_id = ?)"
        )
        assert sql == expected
        assert values == ["1"]

    def test_no_match_raises_through_build(self) -> None:
        """A selector with no default raises SqlSelectionError from build_sql_query."""
        route = {"route": "detections", "sql": [{"when": "count", "then": "SELECT 1"}]}
        with pytest.raises(SqlSelectionError):
            build_sql_query(route, DATABASE_CONFIG, {}, {}, {}, {})


class TestMapDatabaseRouteSelection:
    """The SqlSelectionError path surfaces as a proper URL error response."""

    def _make_rm(self):
        rm = RouteMapper.__new__(RouteMapper)
        rm.config_dir = "api_dock_config"
        rm.remote_names = []
        rm.database_names = ["mydb"]
        rm.config = {}
        rm.settings = {}
        return rm

    @pytest.mark.anyio
    async def test_selection_error_returns_400(self) -> None:
        """No matching branch (and no default) yields a 400 with an error body."""
        rm = self._make_rm()
        route = {"route": "detections", "sql": [{"when": "count", "then": "SELECT 1"}]}
        db_cfg = {"tables": {}, "routes": [route]}
        with patch("api_dock.route_mapper.is_versioned_database", return_value=False), \
             patch("api_dock.route_mapper.load_database_config", return_value=db_cfg), \
             patch("api_dock.route_mapper.merge_inherited_config", side_effect=lambda c, p: c), \
             patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}), \
             patch("api_dock.route_mapper.get_authentication_config", return_value=None), \
             patch("api_dock.route_mapper.find_database_route", return_value=route), \
             patch("api_dock.route_mapper.merge_query_params", side_effect=lambda r, d: r):
            result = await rm.map_database_route("mydb", "detections", {}, {})

        assert result.status_code == 400
        parsed = json.loads(result.content)
        assert "error" in parsed
