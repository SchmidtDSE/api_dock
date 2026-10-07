"""

Request tests for resolvers.

A route that lists a resolver in ``resolve:`` first calls a route of an
internal database and gets exactly one row back. The route then uses the
row's values as ``{{<resolver>.<column>}}`` in its SQL, its table URIs and its
headers. These tests use internal DuckDB targets; test_resolver_postgres.py
covers PostgreSQL targets.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.resolvers import ResolverError, resolver_text
from api_dock.route_mapper import RouteMapper
from tests.chaining_fixtures import (
    NOT_FOUND,
    RESOLVER_ERROR,
    body,
    catalog,
    chain,
    default_releases,
    get,
    observations,
    readings_route,
    release_row,
    scenario,
    write_parquet,
    write_releases,
)
from tests.conftest import write_config


#
# CONSTANTS
#
READINGS_PATH: str = "2.0/projects/7/sites/42/readings"

HOURLY_ROWS: List[Dict[str, Any]] = [
    {"site_id": 42, "value": 1.5, "region": "north"},
    {"site_id": 42, "value": 3.0, "region": "north"},
]

ROUTE_LOGGER: str = "api_dock.route_mapper"


#
# FIXTURES
#
def _sorted(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort rows by value, for comparing results without ORDER BY.

    Args:
        rows: Response rows.

    Returns:
        Sorted rows.
    """
    return sorted(rows, key=lambda row: row["value"])


async def _value(root: Path, target_sql: str, **query: str) -> Any:
    """Return the shop's single row for a resolver whose target runs target_sql.

    The shop route returns the resolver's ``v`` value as it was bound.

    Args:
        root: Test folder.
        target_sql: SQL of the internal lookup route; it must return a ``v`` column.
        **query: Query params of the shop request.

    Returns:
        The response.
    """
    config_path = chain(root, target_sql, ["v"], "SELECT {{res.v}} AS v")
    return await get(RouteMapper(config_path), "shop", "1.0/items/1", **query)


#
# PUBLIC
#
class TestCurrentRelease:
    """A route reads the files of the release that the catalog names."""

    @pytest.mark.anyio
    async def test_reads_current_release(self, tmp_path: Path) -> None:
        """The route reads the hourly release's folder, with its partition column."""
        mapper = RouteMapper(scenario(tmp_path))
        response = await get(mapper, "observations", READINGS_PATH)
        assert response.status_code == 200
        assert _sorted(body(response)) == HOURLY_ROWS
        assert response.headers == {"ETag": '"r-19:abc"', "X-Release-Id": "r-19"}

    @pytest.mark.anyio
    async def test_caller_overrides_default(self, tmp_path: Path) -> None:
        """?dataset=daily changes the value that the resolver sends."""
        mapper = RouteMapper(scenario(tmp_path))
        response = await get(mapper, "observations", READINGS_PATH, dataset="daily")
        assert body(response) == [{"site_id": 42, "value": 7.0, "region": "south"}]
        assert response.headers["X-Release-Id"] == "r-20"

    @pytest.mark.anyio
    async def test_query_fragment_uses_table_alias(self, tmp_path: Path) -> None:
        """A query-param fragment can use the templated table's alias."""
        mapper = RouteMapper(scenario(tmp_path))
        response = await get(mapper, "observations", READINGS_PATH, min_value="2")
        assert body(response) == [HOURLY_ROWS[1]]

    @pytest.mark.anyio
    async def test_no_current_release(self, tmp_path: Path) -> None:
        """No row from the catalog gives 404 Not found."""
        mapper = RouteMapper(scenario(tmp_path))
        response = await get(mapper, "observations", "2.0/projects/8/sites/42/readings")
        assert (response.status_code, body(response)) == (404, NOT_FOUND)
        assert response.headers == {}

    @pytest.mark.anyio
    async def test_no_caching(self, tmp_path: Path) -> None:
        """Each request runs the resolver again, so a new release is used at once."""
        mapper = RouteMapper(scenario(tmp_path))
        assert (await get(mapper, "observations", READINGS_PATH)).headers["X-Release-Id"] == "r-19"
        rows = [release_row(tmp_path, "project:7", "hourly", "r-20")]
        write_releases(tmp_path, rows)
        response = await get(mapper, "observations", READINGS_PATH)
        assert response.headers["X-Release-Id"] == "r-20"
        assert body(response) == [{"site_id": 42, "value": 7.0, "region": "south"}]


class TestAdapters:
    """Both adapters serve resolver routes; neither runs resolvers itself."""

    def test_fastapi(self, tmp_path: Path) -> None:
        """FastAPI serves the route and sends the headers."""
        client = TestClient(create_fastapi_app(scenario(tmp_path)))
        response = client.get(f"/observations/{READINGS_PATH}")
        assert response.status_code == 200
        assert _sorted(response.json()) == HOURLY_ROWS
        assert response.headers["etag"] == '"r-19:abc"'
        assert response.headers["x-release-id"] == "r-19"

    def test_flask_with_duckdb_target(self, tmp_path: Path) -> None:
        """A Flask app with an internal DuckDB target serves a resolver route."""
        client = create_flask_app(scenario(tmp_path)).test_client()
        response = client.get(f"/observations/{READINGS_PATH}")
        assert response.status_code == 200
        assert _sorted(response.get_json()) == HOURLY_ROWS
        assert response.headers["ETag"] == '"r-19:abc"'

    def test_target_stays_hidden(self, tmp_path: Path) -> None:
        """The resolver's target still answers 404 over HTTP."""
        client = TestClient(create_fastapi_app(scenario(tmp_path)))
        response = client.get("/catalog/1.0/releases/current?project=project:7&dataset=hourly")
        assert (response.status_code, response.json()) == (
            404, {"error": "Remote 'catalog' not found"}
        )

    @pytest.mark.anyio
    async def test_target_hidden_from_direct_call(self, tmp_path: Path) -> None:
        """map_database_route() answers 404 for the target."""
        response = await get(
            RouteMapper(scenario(tmp_path)), "catalog", "1.0/releases/current",
            project="project:7", dataset="hourly",
        )
        assert (response.status_code, body(response)) == (
            404, {"error": "Database 'catalog' not found"}
        )


class TestParams:
    """Resolver params are filled from path and query values as text."""

    @pytest.mark.anyio
    async def test_missing_optional_param_is_not_sent(self, tmp_path: Path) -> None:
        """A param whose variable has no value is not sent; the target default applies."""
        config_path = chain(
            tmp_path, "SELECT {{kind}} AS v", ["v"], "SELECT {{res.v}} AS v",
            resolver={"params": {"kind": "{{kind}}"}},
            route={"query_params": [{"kind": {"sql": "1 = 1"}}]},
            target_route={"query_params": [{"kind": {"default": "target default"}}]},
        )
        mapper = RouteMapper(config_path)
        assert body(await get(mapper, "shop", "1.0/items/1")) == [{"v": "target default"}]
        assert body(await get(mapper, "shop", "1.0/items/1", kind="sent")) == [{"v": "sent"}]

    @pytest.mark.anyio
    async def test_missing_param_needed_by_target(self, tmp_path: Path) -> None:
        """If the target needs a param that was not sent, the caller gets 500 Resolver error."""
        config_path = chain(
            tmp_path, "SELECT 1 AS v", ["v"], "SELECT {{res.v}} AS v",
            resolver={"params": {"kind": "{{kind}}"}},
            route={"query_params": [{"kind": {"sql": "1 = 1"}}]},
            target_route={"query_params": [{"kind": {"required": True}}]},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)

    @pytest.mark.anyio
    async def test_caller_query_does_not_reach_target(self, tmp_path: Path) -> None:
        """A caller's query param reaches the target only through params:."""
        config_path = chain(
            tmp_path, "SELECT {{project}} AS v", ["v"], "SELECT {{res.v}} AS v",
            target_route={"query_params": [{"project": {"default": "unset"}}]},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1", project="x")
        assert body(response) == [{"v": "unset"}]

    @pytest.mark.anyio
    async def test_param_template_is_text(self, tmp_path: Path) -> None:
        """A param value with quotes and braces reaches the target as it is."""
        config_path = chain(
            tmp_path, "SELECT {{key}} AS v", ["v"], "SELECT {{res.v}} AS v",
            resolver={"params": {"key": "id:{{id}}"}},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/O'Brien {{id}}")
        assert body(response) == [{"v": "id:O'Brien {{id}}"}]

    @pytest.mark.anyio
    async def test_fixed_param(self, tmp_path: Path) -> None:
        """A param without variables is sent as written."""
        config_path = chain(
            tmp_path, "SELECT {{kind}} AS v", ["v"], "SELECT {{res.v}} AS v",
            resolver={"params": {"kind": "detections"}},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [
            {"v": "detections"}
        ]


class TestTargetCall:
    """The call to the target skips its authentication and sends no caller cookies."""

    @pytest.mark.anyio
    async def test_target_authentication_is_skipped(self, tmp_path: Path) -> None:
        """A target with authentication: still answers the resolver."""
        authentication = {"key": "token", "value": "secret", "encrypted": False}
        config_path = chain(
            tmp_path, "SELECT 'ok' AS v", ["v"], "SELECT {{res.v}} AS v",
            target={"authentication": authentication},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [{"v": "ok"}]

    @pytest.mark.anyio
    async def test_caller_cookies_are_not_sent(self, tmp_path: Path) -> None:
        """A target that accepts all cookies gets none of the caller's cookies."""
        target_sql: Any = [
            {"when": "cookies.session", "then": "SELECT 'sent' AS v"},
            "SELECT 'not sent' AS v",
        ]
        config_path = chain(
            tmp_path, target_sql, ["v"], "SELECT {{res.v}} AS v",
            target={"cookies": True}, shop={"cookies": True},
        )
        response = await get(
            RouteMapper(config_path), "shop", "1.0/items/1", cookies={"session": "abc"}
        )
        assert body(response) == [{"v": "not sent"}]

    @pytest.mark.anyio
    async def test_injected_cookie_is_used(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A target's injected cookie supplies its SQL value."""
        monkeypatch.setenv("CHAIN_TEST_TOKEN", "server-token")
        config_path = chain(
            tmp_path, "SELECT {{cookies.token}} AS v", ["v"], "SELECT {{res.v}} AS v",
            target={"cookies": [{"key": "token", "value": "env:CHAIN_TEST_TOKEN"}]},
        )
        response = await get(
            RouteMapper(config_path), "shop", "1.0/items/1", cookies={"token": "caller"}
        )
        assert body(response) == [{"v": "server-token"}]

    @pytest.mark.anyio
    async def test_missing_injected_value_fails(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            caplog: pytest.LogCaptureFixture) -> None:
        """An injected cookie whose variable is not set fails the call."""
        monkeypatch.delenv("CHAIN_TEST_TOKEN", raising=False)
        config_path = chain(
            tmp_path, "SELECT {{cookies.token}} AS v", ["v"], "SELECT {{res.v}} AS v",
            target={"cookies": [{"key": "token", "value": "env:CHAIN_TEST_TOKEN"}]},
        )
        with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
            response = await get(
                RouteMapper(config_path), "shop", "1.0/items/1", cookies={"token": "caller"}
            )
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)
        assert "resolver 'res'" in caplog.text
        assert "caller" not in caplog.text

    @pytest.mark.anyio
    async def test_target_path_variable(self, tmp_path: Path) -> None:
        """A path variable of the target route gets its value from the literal via path."""
        config_path = chain(
            tmp_path, "SELECT {{kind}} AS v", ["v"], "SELECT {{res.v}} AS v",
            target_route={"route": "lookup/{{kind}}"},
            resolver={"via": "catalog/1.0/lookup/hourly"},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [
            {"v": "hourly"}
        ]

    @pytest.mark.anyio
    async def test_target_query_default(self, tmp_path: Path) -> None:
        """A target query-param default supplies its SQL value."""
        config_path = chain(
            tmp_path, "SELECT {{kind}} AS v", ["v"], "SELECT {{res.v}} AS v",
            target_route={"query_params": [{"kind": {"default": "hourly"}}]},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [
            {"v": "hourly"}
        ]


class TestWhenResolversRun:
    """Resolvers run after early responses and in resolve: order."""

    @pytest.mark.parametrize("query, status", [
        ({"help": "1"}, 200), ({"mode": "fixed"}, 200), ({}, 400),
    ])
    @pytest.mark.anyio
    async def test_early_response_makes_no_call(
            self, tmp_path: Path, query: Dict[str, str], status: int) -> None:
        """response:, conditional: and a missing required param return before the call.

        The target's SQL fails, so a resolver call would give 500.
        """
        route = {"query_params": [
            {"help": {"response": {"help": "text"}}},
            {"mode": {"conditional": {"fixed": {"response": {"mode": "fixed"}}}}},
            {"need": {"required": True}},
        ]}
        config_path = chain(
            tmp_path, "SELECT * FROM no_such_table", ["v"], "SELECT {{res.v}} AS v", route=route,
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1", **query)
        assert response.status_code == status

    @pytest.mark.anyio
    async def test_second_resolver_fails(self, tmp_path: Path) -> None:
        """If the second resolver gets no row, the caller gets its 404."""
        config_path = chain(
            tmp_path, "SELECT {{k}} AS v WHERE {{k}} = 'one'", ["v"],
            "SELECT {{res.v}} AS a, {{other.v}} AS b",
            resolver={"params": {"k": "one"}},
            route={"resolve": ["res", "other"]},
            shop={"resolvers": {
                "res": {"via": "catalog/1.0/lookup", "params": {"k": "one"}, "bind": ["v"]},
                "other": {"via": "catalog/1.0/lookup", "params": {"k": "two"}, "bind": ["v"]},
            }},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (404, NOT_FOUND)

    @pytest.mark.anyio
    async def test_two_resolvers(self, tmp_path: Path) -> None:
        """Two resolvers on one route each supply their own values."""
        config_path = chain(
            tmp_path, "SELECT {{k}} AS v", ["v"], "SELECT {{res.v}} AS a, {{other.v}} AS b",
            route={"resolve": ["res", "other"]},
            shop={"resolvers": {
                "res": {"via": "catalog/1.0/lookup", "params": {"k": "one"}, "bind": ["v"]},
                "other": {"via": "catalog/1.0/lookup", "params": {"k": "two"}, "bind": ["v"]},
            }},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert body(response) == [{"a": "one", "b": "two"}]


class TestErrors:
    """Each failure gives a fixed response; the log names the resolver and target."""

    @pytest.mark.parametrize("target_sql, bind, status, expected", [
        ("SELECT 'x' AS v WHERE false", ["v"], 404, NOT_FOUND),
        ("SELECT 'x' AS v UNION ALL SELECT 'y'", ["v"], 500, RESOLVER_ERROR),
        ("SELECT * FROM no_such_table", ["v"], 500, RESOLVER_ERROR),
        ("SELECT 'x' AS v", ["v", "w"], 500, RESOLVER_ERROR),
        ("SELECT 'nan'::DOUBLE AS v", ["v"], 500, RESOLVER_ERROR),
    ])
    @pytest.mark.anyio
    async def test_error_response(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture, target_sql: str,
            bind: List[str], status: int, expected: Dict[str, str]) -> None:
        """The body is fixed and the log names the resolver and the target."""
        config_path = chain(tmp_path, target_sql, bind, "SELECT {{res.v}} AS v")
        with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
            response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (status, expected)
        assert response.headers == {}
        assert "resolver 'res'" in caplog.text
        assert "catalog/1.0/lookup" in caplog.text

    @pytest.mark.anyio
    async def test_target_error_response(self, tmp_path: Path) -> None:
        """A 400 from the target gives 500 Resolver error, without the target's message."""
        config_path = chain(
            tmp_path, "SELECT 1 AS v", ["v"], "SELECT {{res.v}} AS v",
            target_route={"query_params": [{"need": {
                "required": True, "missing_response": {"error": "need is missing"},
            }}]},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)

    @pytest.mark.anyio
    async def test_target_unavailable(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A target that answers 503 gives 503 Database unavailable."""
        from api_dock.database_backends import DatabaseUnavailableError, DuckDBBackend

        async def unavailable(self: Any, sql: str, values: List[Any]) -> Any:
            raise DatabaseUnavailableError("down")

        config_path = chain(tmp_path, "SELECT 1 AS v", ["v"], "SELECT {{res.v}} AS v")
        mapper = RouteMapper(config_path)
        monkeypatch.setattr(DuckDBBackend, "execute", unavailable)
        response = await get(mapper, "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (503, {"error": "Database unavailable"})

    @pytest.mark.anyio
    async def test_query_error_after_resolver_is_not_resolver_error(
            self, tmp_path: Path) -> None:
        """The route's own query error stays 500 Database query error."""
        config_path = chain(
            tmp_path, "SELECT 'x' AS v", ["v"], "SELECT {{res.v}} FROM no_such_table",
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (500, {"error": "Database query error"})


class TestValues:
    """Resolver values are converted to text, or refused."""

    @pytest.mark.parametrize("target_sql, expected", [
        ("SELECT 'abc' AS v", "abc"),
        ("SELECT 42 AS v", "42"),
        ("SELECT 1.10::DECIMAL(5, 2) AS v", "1.10"),
        ("SELECT 0.1::DOUBLE AS v", "0.1"),
        ("SELECT true AS v", "true"),
        ("SELECT false AS v", "false"),
        ("SELECT DATE '2026-01-02' AS v", "2026-01-02"),
        ("SELECT TIMESTAMP '2026-01-02 03:04:05' AS v", "2026-01-02T03:04:05"),
        ("SELECT 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::UUID AS v",
         "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
    ])
    @pytest.mark.anyio
    async def test_converted(self, tmp_path: Path, target_sql: str, expected: str) -> None:
        """Each supported type is bound as text."""
        assert body(await _value(tmp_path, target_sql)) == [{"v": expected}]

    @pytest.mark.parametrize("target_sql", [
        "SELECT 'nan'::DOUBLE AS v",
        "SELECT 'inf'::DOUBLE AS v",
        "SELECT '-inf'::DOUBLE AS v",
        "SELECT [1, 2] AS v",
        "SELECT {'a': 1} AS v",
        "SELECT 'x'::BLOB AS v",
    ])
    @pytest.mark.anyio
    async def test_refused(self, tmp_path: Path, target_sql: str) -> None:
        """Non-finite numbers and unsupported types give 500 Resolver error."""
        response = await _value(tmp_path, target_sql)
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)

    @pytest.mark.anyio
    async def test_null_is_bound_as_null(self, tmp_path: Path) -> None:
        """NULL is bound as SQL NULL."""
        config_path = chain(
            tmp_path, "SELECT NULL AS v", ["v"], "SELECT {{res.v}} IS NULL AS is_null",
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert body(response) == [{"is_null": True}]

    @pytest.mark.parametrize("value, expected", [
        ("text", "text"), (7, "7"), (True, "true"), (False, "false"), (None, None),
        (Decimal("1.50"), "1.50"), (2.5, "2.5"), (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
        (UUID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"), "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"),
    ])
    def test_resolver_text(self, value: Any, expected: Any) -> None:
        """resolver_text converts each supported type."""
        assert resolver_text(value) == expected

    @pytest.mark.parametrize("value", [
        float("nan"), float("inf"), float("-inf"), Decimal("NaN"), Decimal("Infinity"),
        [1], {"a": 1}, b"x",
    ])
    def test_resolver_text_refuses(self, value: Any) -> None:
        """resolver_text refuses non-finite numbers and unsupported types."""
        with pytest.raises(ResolverError):
            resolver_text(value)


class TestPrecedence:
    """A request value with a resolver value's dotted name can't replace it."""

    @pytest.mark.anyio
    async def test_query_value_in_base_sql(self, tmp_path: Path) -> None:
        """A query param named res.v does not change the base SQL value."""
        response = await _value(tmp_path, "SELECT 'good' AS v", **{"res.v": "evil"})
        assert body(response) == [{"v": "good"}]

    @pytest.mark.anyio
    async def test_null_is_not_replaced(self, tmp_path: Path) -> None:
        """A NULL resolver value stays NULL when a query value has its name."""
        config_path = chain(tmp_path, "SELECT NULL AS v", ["v"], "SELECT {{res.v}} AS v")
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1", **{"res.v": "evil"})
        assert body(response) == [{"v": None}]

    @pytest.mark.anyio
    async def test_default_value(self, tmp_path: Path) -> None:
        """A default for a param named res.v does not change the value."""
        config_path = chain(
            tmp_path, "SELECT 'good' AS v", ["v"], "SELECT {{res.v}} AS v",
            route={"query_params": [{"res.v": {"default": "evil"}}]},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [{"v": "good"}]

    @pytest.mark.anyio
    async def test_path_value(self, tmp_path: Path) -> None:
        """A path variable named res.v does not change the SQL or the header value."""
        config_path = chain(
            tmp_path, "SELECT 'good' AS v", ["v"], "SELECT {{res.v}} AS v",
            route={"route": "items/{{res.v}}", "headers": {"X-Value": "{{res.v}}"}},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/evil")
        assert body(response) == [{"v": "good"}]
        assert response.headers == {"X-Value": "good"}

    @pytest.mark.parametrize("query_param, query, multi", [
        ({"filter": {"sql": "t.v = {{res.v}}"}}, {"filter": "1", "res.v": "evil"}, None),
        ({"res.v": {"sql": "t.v = {{res.v}}", "default": "evil"}}, {}, None),
        ({"res.v": {"sql": "t.v = {{res.v}}", "default": "evil"}}, {"res.v": "evil"}, None),
        ({"res.v": {"sql": "t.v = {{res.v}}", "multivalue_sql": "t.v IN {{res.v}}"}},
         {"res.v": "b"}, {"res.v": ["a", "b"]}),
        ({"mode": {"conditional": {"on": {"sql": "t.v = {{res.v}}"}}}},
         {"mode": "on", "res.v": "evil"}, None),
    ])
    @pytest.mark.anyio
    async def test_sql_fragments(
            self, tmp_path: Path, query_param: Dict[str, Any], query: Dict[str, str],
            multi: Any) -> None:
        """Query, default, repeated and conditional values don't replace it in fragments."""
        config_path = chain(
            tmp_path, "SELECT 'good' AS v", ["v"], "SELECT t.v FROM (SELECT 'good' AS v) t",
            route={"query_params": [query_param]},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1", multi=multi, **query)
        assert body(response) == [{"v": "good"}]

    @pytest.mark.anyio
    async def test_uri_and_header(self, tmp_path: Path) -> None:
        """Query values named like the resolver values don't change the URI or headers."""
        mapper = RouteMapper(scenario(tmp_path))
        evil = {
            "current_release.data_uri": str(tmp_path / "releases" / "r-20") + "/",
            "current_release.release_id": "evil",
        }
        response = await get(mapper, "observations", READINGS_PATH, **evil)
        assert _sorted(body(response)) == HOURLY_ROWS
        assert response.headers["X-Release-Id"] == "r-19"


class TestBoundValues:
    """A resolver value is never read as SQL or as a template."""

    TRICKY: str = "O'Brien {{id}} '); DROP TABLE x; --"

    # Builds TRICKY; braces in a quoted string would fail the startup check.
    TRICKY_SQL: str = (
        "SELECT 'O''Brien ' || repeat(chr(123), 2) || 'id' || repeat(chr(125), 2) "
        "|| ' ''); DROP TABLE x; --' AS v"
    )

    @pytest.mark.anyio
    async def test_in_sql(self, tmp_path: Path) -> None:
        """A value with quotes, braces and SQL text is returned as it is."""
        target_sql = self.TRICKY_SQL
        config_path = chain(tmp_path, target_sql, ["v"], "SELECT {{res.v}} AS v")
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert body(response) == [{"v": self.TRICKY}]

    @pytest.mark.anyio
    async def test_in_header(self, tmp_path: Path) -> None:
        """A header value with quotes and braces is sent as it is."""
        target_sql = self.TRICKY_SQL
        config_path = chain(
            tmp_path, target_sql, ["v"], "SELECT 1 AS one",
            route={"headers": {"X-Value": "{{res.v}}"}},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert response.headers == {"X-Value": self.TRICKY}

    @pytest.mark.anyio
    async def test_in_uri(self, tmp_path: Path) -> None:
        """A URI with a quote and braces is bound, not written into the SQL."""
        folder = tmp_path / "releases" / "it's {{x}}"
        write_parquet(folder / "part-0.parquet", [{"site_id": 42, "value": 5.0}])
        rows = default_releases(tmp_path)
        rows[0] = release_row(tmp_path, "project:7", "hourly", "r-19", f"{folder}/")
        releases_csv = write_releases(tmp_path, rows)
        config_path = write_config(tmp_path, {
            "catalog": {"versions": {"1.0": catalog(releases_csv)}},
            "observations": {"versions": {"2.0": observations(tmp_path)}},
        })
        response = await get(RouteMapper(config_path), "observations", READINGS_PATH)
        assert body(response) == [{"site_id": 42, "value": 5.0}]


class TestHeaders:
    """Headers can use resolver values; NULL leaves a header out."""

    @pytest.mark.anyio
    async def test_null_leaves_header_out(self, tmp_path: Path) -> None:
        """A NULL value leaves its header out; other headers are still sent."""
        config_path = chain(
            tmp_path, "SELECT NULL AS v, 'x' AS w", ["v", "w"], "SELECT 1 AS one",
            route={"headers": {"X-V": "{{res.v}}", "X-W": "{{res.w}}"}},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert response.status_code == 200
        assert response.headers == {"X-W": "x"}

    @pytest.mark.anyio
    async def test_line_feed_is_refused(self, tmp_path: Path) -> None:
        """A value with a line feed gives 500 and no header."""
        config_path = chain(
            tmp_path, "SELECT 'a' || chr(10) || 'b' AS v", ["v"], "SELECT 1 AS one",
            route={"headers": {"X-V": "{{res.v}}"}},
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)
        assert response.headers == {}

    @pytest.mark.parametrize("adapter", ["fastapi", "flask"])
    def test_adapters(self, tmp_path: Path, adapter: str) -> None:
        """Both adapters send resolver headers and leave out NULL ones."""
        config_path = chain(
            tmp_path, "SELECT NULL AS v, 'x' AS w", ["v", "w"], "SELECT 1 AS one",
            route={"headers": {"X-V": "{{res.v}}", "X-W": "{{res.w}}"}},
        )
        if adapter == "fastapi":
            client: Any = TestClient(create_fastapi_app(config_path))
        else:
            client = create_flask_app(config_path).test_client()
        response = client.get("/shop/1.0/items/1")
        assert response.status_code == 200
        assert response.headers.get("X-W") == "x"
        assert "X-V" not in response.headers

    @pytest.mark.parametrize("adapter", ["fastapi", "flask"])
    def test_adapters_refuse_non_ascii(self, tmp_path: Path, adapter: str) -> None:
        """A non-ASCII value gives 500 Resolver error through both adapters."""
        config_path = chain(
            tmp_path, "SELECT 'é' AS v", ["v"], "SELECT 1 AS one",
            route={"headers": {"X-V": "{{res.v}}"}},
        )
        if adapter == "fastapi":
            response: Any = TestClient(create_fastapi_app(config_path)).get("/shop/1.0/items/1")
            payload = response.json()
        else:
            response = create_flask_app(config_path).test_client().get("/shop/1.0/items/1")
            payload = response.get_json()
        assert (response.status_code, payload) == (500, RESOLVER_ERROR)
        assert "X-V" not in response.headers
