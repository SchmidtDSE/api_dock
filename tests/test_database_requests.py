"""

Request-level tests for database routes with bound parameters.

Each test writes a small config and Parquet tables to a temp directory, then
sends requests through ``RouteMapper.map_database_route`` against an in-memory
DuckDB. Caller-supplied values must be matched as data and never change what a
filter does.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pytest
import yaml

from api_dock.route_mapper import RouteMapper
from api_dock.types import ProxyResponse


#
# CONSTANTS
#
ITEM_ROWS: str = (
    "(1, 42, 0.9, 'Alpha', 3, DATE '2024-05-01', true, 1.25), "
    "(2, 42, 0.6, 'O''Brien', 1, DATE '2024-06-01', false, 2.50), "
    "(3, 7, 0.95, 'Gamma', 2, DATE '2024-07-01', true, 3.75), "
    "(4, 42, 0.85, '{{cookies.session_token}}', 2, DATE '2024-08-01', true, 4.00), "
    "(5, 42, 0.7, '''; DROP TABLE x; --', 1, DATE '2024-09-01', false, 5.00)"
)

ITEM_COLUMNS: str = (
    "id, group_id, weight, name, rank, day, verified, price"
)

USER_ROWS: str = "('alice', 'tok-a'), ('bob', 'tok-b')"

# Versioned items route with a filter for each column type.
CATALOG_CONFIG: Dict[str, Any] = {
    "name": "catalog",
    "cookies": ["session_token"],
    "routes": [{
        "route": "/groups/{{group_id}}/items/",
        "sql": (
            "SELECT [[items]].* FROM [[items]] "
            "WHERE [[items]].group_id = {{group_id}}"
        ),
        "query_params": [
            {"weight": {"sql": "[[items]].weight >= {{weight}}"}},
            {"name": {"sql": "[[items]].name = {{name}}"}},
            {"rank": {"sql": "[[items]].rank = {{rank}}"}},
            {"since": {"sql": "[[items]].day >= {{since}}"}},
            {"verified": {"sql": "[[items]].verified = {{verified}}"}},
            {"min_price": {"sql": "[[items]].price >= {{min_price}}"}},
            {"id": {
                "sql": "[[items]].id = {{id}}",
                "multivalue_sql": "[[items]].id IN {{id}}",
            }},
            {"sort": {"sql_append": "ORDER BY {{sort}} {{direction}}", "default": "id"}},
            {"direction": {"default": "ASC"}},
            {"limit": {"sql_append": "LIMIT {{limit}}"}},
        ],
    }, {
        "route": "missing",
        "sql": "SELECT * FROM [[items]] WHERE id = {{nope}}",
    }, {
        # DuckDB ignores the marker in the comment, but its value is still sent.
        "route": "commented",
        "sql": "SELECT * FROM [[items]] /* {{name}} */",
    }],
}

# README user-settings route with the quotes removed from its variables.
USERS_CONFIG: Dict[str, Any] = {
    "name": "users",
    "cookies": ["user_id", "session_token"],
    "routes": [{
        "route": "user-settings",
        "sql": (
            "SELECT * FROM [[user_activity]]\n"
            "WHERE user_id = {{cookies.user_id}}\n"
            "AND session_token = {{cookies.session_token}}"
        ),
    }],
}

# Same route, with session_token injected by the server from an env var.
INJECTED_CONFIG: Dict[str, Any] = {
    **USERS_CONFIG,
    "name": "injected",
    "cookies": ["user_id", {"key": "session_token", "value": "env:TEST_SESSION_TOKEN"}],
}


#
# FIXTURES
#
@pytest.fixture
def route_mapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RouteMapper:
    """Write configs and Parquet tables to tmp_path and return a RouteMapper.

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        RouteMapper loaded from the temp config.
    """
    items = tmp_path / "items.parquet"
    users = tmp_path / "user_activity.parquet"
    duckdb.sql(
        f"COPY (SELECT * FROM (VALUES {ITEM_ROWS}) v({ITEM_COLUMNS})) "
        f"TO '{items}' (FORMAT parquet)")
    duckdb.sql(
        f"COPY (SELECT * FROM (VALUES {USER_ROWS}) v(user_id, session_token)) "
        f"TO '{users}' (FORMAT parquet)")

    config_dir = tmp_path / "api_dock_config"
    (config_dir / "databases" / "catalog").mkdir(parents=True)
    _write_yaml(config_dir / "config.yaml", {
        "name": "test", "databases": ["catalog", "users", "injected"],
    })
    _write_yaml(config_dir / "databases" / "catalog" / "1.0.yaml",
                {**CATALOG_CONFIG, "tables": {"items": str(items)}})
    for database in [USERS_CONFIG, INJECTED_CONFIG]:
        _write_yaml(config_dir / "databases" / f"{database['name']}.yaml",
                    {**database, "tables": {"user_activity": str(users)}})

    monkeypatch.chdir(tmp_path)
    return RouteMapper(str(config_dir / "config.yaml"))


#
# PUBLIC
#
class TestFilterValues:
    """A caller can't change what a filter does."""

    @pytest.mark.anyio
    async def test_numeric_filter_matches(self, route_mapper: RouteMapper) -> None:
        """?weight=0.8 returns group 42's rows with weight >= 0.8."""
        result = await _items(route_mapper, {"weight": "0.8"})
        assert result.status_code == 200
        assert _ids(result) == [1, 4]

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", ["0.5 + 0.3", "0 OR true"])
    async def test_sql_in_value_is_rejected(self, route_mapper: RouteMapper, value: str) -> None:
        """A value that is SQL, not a number, gives an error and no rows."""
        result = await _items(route_mapper, {"weight": value})
        assert result.status_code == 500
        assert json.loads(result.content) == {"error": "Database query error"}

    @pytest.mark.anyio
    @pytest.mark.parametrize("name, expected", [
        ("O'Brien", [2]),
        ("{{cookies.session_token}}", [4]),
        ("'; DROP TABLE x; --", [5]),
    ])
    async def test_text_value_matches_exactly(
            self, route_mapper: RouteMapper, name: str, expected: List[int]) -> None:
        """Quotes and SQL-like text in a value are matched as that literal text."""
        result = await _items(
            route_mapper, {"name": name}, cookies={"session_token": "secret"}
        )
        assert result.status_code == 200
        assert _ids(result) == expected

    @pytest.mark.anyio
    @pytest.mark.parametrize("params, expected", [
        ({"rank": "2"}, [4]),
        ({"since": "2024-08-01"}, [4, 5]),
        ({"verified": "false"}, [2, 5]),
        ({"min_price": "4.5"}, [5]),
    ])
    async def test_typed_filters_convert_bound_strings(
            self, route_mapper: RouteMapper, params: Dict[str, str],
            expected: List[int]) -> None:
        """INTEGER, DATE, BOOLEAN and DECIMAL columns compare against bound strings."""
        result = await _items(route_mapper, params)
        assert result.status_code == 200
        assert _ids(result) == expected

    @pytest.mark.anyio
    async def test_multivalue_matches_each_value(self, route_mapper: RouteMapper) -> None:
        """?id=1&id=4 returns both rows."""
        result = await _items(
            route_mapper, {"id": "4"}, multi={"id": ["1", "4"]}
        )
        assert result.status_code == 200
        assert _ids(result) == [1, 4]

    @pytest.mark.anyio
    async def test_sort_and_limit_unchanged(self, route_mapper: RouteMapper) -> None:
        """sql_append sort, direction and limit behave as before."""
        result = await _items(
            route_mapper, {"sort": "id", "direction": "DESC", "limit": "2"}
        )
        assert result.status_code == 200
        assert [row["id"] for row in json.loads(result.content)] == [5, 4]

    @pytest.mark.anyio
    async def test_bad_sql_append_value_is_sql_query_error(
            self, route_mapper: RouteMapper) -> None:
        """An sql_append value that fails the character check gives 500 SQL query error."""
        result = await _items(route_mapper, {"limit": "1; DROP TABLE x"})
        assert result.status_code == 500
        assert json.loads(result.content) == {"error": "SQL query error"}

    @pytest.mark.anyio
    async def test_missing_variable_is_sql_query_error(self, route_mapper: RouteMapper) -> None:
        """A {{var}} with no value available gives 500 SQL query error."""
        result = await route_mapper.map_database_route("catalog", "latest/missing", {}, {})
        assert result.status_code == 500
        assert json.loads(result.content) == {"error": "SQL query error"}

    @pytest.mark.anyio
    async def test_commented_variable_is_database_query_error(
            self, route_mapper: RouteMapper) -> None:
        """A commented {{var}} leaves an unused value, so DuckDB rejects the query."""
        result = await route_mapper.map_database_route(
            "catalog", "latest/commented", {"name": "Alpha"}, {}
        )
        assert result.status_code == 500
        assert json.loads(result.content) == {"error": "Database query error"}


class TestCookieValues:
    """Cookie checks work, and crafted cookies get nothing."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("cookies, expected", [
        ({"user_id": "alice", "session_token": "tok-a"}, [["alice", "tok-a"]]),
        ({"user_id": "alice", "session_token": "guess"}, []),
        ({"user_id": "' OR true --", "session_token": "' OR true --"}, []),
    ])
    async def test_cookie_route(
            self, route_mapper: RouteMapper, cookies: Dict[str, str],
            expected: List[List[str]]) -> None:
        """Only the matching user and token return a row."""
        result = await route_mapper.map_database_route("users", "user-settings", {}, cookies)
        assert result.status_code == 200
        assert _rows(result) == expected

    @pytest.mark.anyio
    async def test_injected_cookie_is_bound(
            self, route_mapper: RouteMapper, monkeypatch: pytest.MonkeyPatch) -> None:
        """An injected cookie is bound like a client cookie and wins over the client's."""
        monkeypatch.setenv("TEST_SESSION_TOKEN", "tok-a")
        result = await route_mapper.map_database_route(
            "injected", "user-settings", {},
            {"user_id": "alice", "session_token": "' OR true --"},
        )
        assert result.status_code == 200
        assert _rows(result) == [["alice", "tok-a"]]

    @pytest.mark.anyio
    async def test_crafted_injected_cookie_gets_nothing(
            self, route_mapper: RouteMapper, monkeypatch: pytest.MonkeyPatch) -> None:
        """An injected value containing SQL is matched as text."""
        monkeypatch.setenv("TEST_SESSION_TOKEN", "' OR true --")
        result = await route_mapper.map_database_route(
            "injected", "user-settings", {}, {"user_id": "' OR true --"},
        )
        assert result.status_code == 200
        assert _rows(result) == []


#
# INTERNAL
#
def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write data to path as YAML.

    Args:
        path: File to write.
        data: Data to serialize.
    """
    path.write_text(yaml.safe_dump(data, sort_keys=False))


async def _items(
        route_mapper: RouteMapper,
        query_params: Dict[str, str],
        cookies: Optional[Dict[str, str]] = None,
        multi: Optional[Dict[str, List[str]]] = None) -> ProxyResponse:
    """Request group 42's items from the catalog database.

    Args:
        route_mapper: RouteMapper under test.
        query_params: Query parameters for the request.
        cookies: Cookies for the request.
        multi: Repeated query parameters, if any.

    Returns:
        The route mapper's response.
    """
    return await route_mapper.map_database_route(
        "catalog", "latest/groups/42/items/", query_params, cookies or {}, multi
    )


def _ids(result: ProxyResponse) -> List[int]:
    """Return the sorted item ids in a response.

    Args:
        result: Response with a JSON list of item rows.

    Returns:
        Sorted ids.
    """
    return sorted(row["id"] for row in json.loads(result.content))


def _rows(result: ProxyResponse) -> List[List[Any]]:
    """Return the rows of a response as lists of values.

    Args:
        result: Response with a JSON list of rows.

    Returns:
        Each row's values in column order.
    """
    return [list(row.values()) for row in json.loads(result.content)]
