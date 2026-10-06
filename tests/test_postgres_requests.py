"""

Request-level tests for PostgreSQL database routes.

Requests go through ``RouteMapper.map_database_route`` after ``start()``, against
the local test server. Routes, query params and ``[[table]]`` references work as
they do for DuckDB, values are bound, and rows are returned as JSON. A database
that can't be reached returns 503; query errors return 500.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import duckdb
import pytest

from api_dock import postgres_backend
from api_dock.database_backends import DuckDBBackend
from api_dock.route_mapper import RouteMapper
from api_dock.types import ProxyResponse
from tests.conftest import PostgresServer, write_config


#
# CONSTANTS
#
ITEM_ROWS: List[Dict[str, Any]] = [
    {"id": 1, "name": "apple"},
    {"id": 2, "name": "O'Brien"},
    {"id": 3, "name": "50% off"},
    {"id": 4, "name": "%s literal"},
]

# The same routes are served from PostgreSQL and from a DuckDB Parquet copy.
ROUTES: List[Dict[str, Any]] = [
    {"route": "items/{{id}}",
     "sql": "SELECT [[items]].id, [[items]].name FROM [[items]] WHERE [[items]].id = {{id}}"},
    {"route": "search",
     "sql": "SELECT [[items]].id, [[items]].name FROM [[items]]",
     "query_params": [
         {"name": {"sql": "[[items]].name = {{name}}"}},
         {"q": {"sql": "[[items]].name ILIKE '%' || {{q}} || '%'"}},
         {"twice": {"sql": "([[items]].name = {{twice}} OR [[items]].name = {{twice}})"}},
         {"id": {"sql": "[[items]].id = {{id}}", "multivalue_sql": "[[items]].id IN {{id}}"}},
         {"sort": {"sql_append": "ORDER BY {{sort}}", "default": "id"}},
         {"limit": {"sql_append": "LIMIT {{limit}}"}},
     ]},
]

POSTGRES_ONLY_ROUTES: List[Dict[str, Any]] = [
    {"route": "kinds", "sql": "SELECT * FROM [[kinds]]"},
    {"route": "delete", "sql": "DELETE FROM [[scratch]] RETURNING id"},
    {"route": "settings",
     "sql": "SELECT current_setting('default_transaction_read_only') AS read_only"},
]

KINDS_ROW: Dict[str, Any] = {
    "id": 1,
    "u": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "n": 1.25,
    "d": "2026-01-02",
    "ts": "2026-01-02T03:04:05",
    "tstz": "2026-01-02T01:04:05+00:00",
    "j": {"a": [1, 2]},
    "jb": {"b": {"c": None}},
    "iv": 93600.0,
    "ip": "192.168.0.1",
    "net": "10.0.0.0/8",
    "uuids": ["a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"],
    "nums": [1.5, 2.25],
    "dates": ["2026-01-02"],
    "tstzs": ["2026-01-02T01:04:05+00:00"],
    "grid": [[1, 2], [3, 4]],
    "with_null": ["x", None],
}


#
# FIXTURES
#
def postgres_shop(server: PostgresServer, **changes: Any) -> Dict[str, Any]:
    """Build the PostgreSQL ``shop`` database config.

    Args:
        server: The test server.
        **changes: Top-level config keys to replace.

    Returns:
        Database config.
    """
    config = server.database_config(
        tables={"items": "catalog.items", "kinds": "catalog.kinds", "scratch": "catalog.scratch"},
        routes=ROUTES + POSTGRES_ONLY_ROUTES,
    )
    config.update(changes)
    return config


def duckdb_shop(root: Path) -> Dict[str, Any]:
    """Write the items table to Parquet and build a DuckDB ``shop`` config for it.

    Args:
        root: Folder for the Parquet file.

    Returns:
        Database config.
    """
    path = root / "items.parquet"
    values = ", ".join(f"({row['id']}, '{row['name'].replace(chr(39), chr(39) * 2)}')"
                       for row in ITEM_ROWS)
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {values}) v(id, name)) TO '{path}' (FORMAT parquet)")
    return {"name": "shop", "tables": {"items": str(path)}, "routes": ROUTES}


@pytest.fixture
async def mapper(tmp_path: Path, running_postgres: PostgresServer) -> AsyncIterator[RouteMapper]:
    """Start a mapper serving the PostgreSQL shop database.

    Args:
        tmp_path: Pytest temp directory.
        running_postgres: The session server.

    Yields:
        A started mapper, closed afterwards.
    """
    route_mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
    await route_mapper.start()
    yield route_mapper
    await route_mapper.aclose()


@pytest.fixture(params=["postgres", "duckdb"])
async def either_mapper(
        request: pytest.FixtureRequest, tmp_path: Path,
        running_postgres: PostgresServer) -> AsyncIterator[RouteMapper]:
    """Start a mapper serving the shop database from PostgreSQL or from DuckDB.

    Args:
        request: Pytest request, whose param names the backend.
        tmp_path: Pytest temp directory.
        running_postgres: The session server.

    Yields:
        A started mapper, closed afterwards.
    """
    if request.param == "postgres":
        config = postgres_shop(running_postgres)
    else:
        config = duckdb_shop(tmp_path)
    route_mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
    await route_mapper.start()
    yield route_mapper
    await route_mapper.aclose()


async def _get(mapper: RouteMapper, path: str, database: str = "shop",
               multi: Optional[Dict[str, List[str]]] = None, **query: str) -> ProxyResponse:
    """Send a database request.

    Args:
        mapper: Started mapper.
        path: Route path.
        database: Database name.
        multi: Repeated query parameters.
        **query: Query parameters.

    Returns:
        The response.
    """
    return await mapper.map_database_route(
        database_name=database, path=path, query_params=query,
        cookies={"session": "secret-cookie"}, multi_query_params=multi or {},
    )


def _body(response: ProxyResponse) -> Any:
    """Decode a JSON response body.

    Args:
        response: The response.

    Returns:
        Decoded JSON.
    """
    return json.loads(response.content)


#
# PUBLIC
#
class TestBoundValuesOnBothBackends:
    """The same routes give the same results from PostgreSQL and DuckDB."""

    @pytest.mark.anyio
    async def test_path_value(self, either_mapper: RouteMapper) -> None:
        """A path variable is bound."""
        assert _body(await _get(either_mapper, "items/2")) == [ITEM_ROWS[1]]

    @pytest.mark.anyio
    @pytest.mark.parametrize("name, expected", [
        ("O'Brien", [ITEM_ROWS[1]]),
        ("' OR '1'='1", []),
        ("' OR true --", []),
        ("{{cookies.session}}", []),
        ("%s literal", [ITEM_ROWS[3]]),
    ])
    async def test_values_match_as_text(
            self, either_mapper: RouteMapper, name: str, expected: List[Dict[str, Any]]) -> None:
        """Quotes, SQL, variable-like text and %s in values are matched as text."""
        assert _body(await _get(either_mapper, "search", name=name)) == expected

    @pytest.mark.anyio
    async def test_like_pattern_with_percent(self, either_mapper: RouteMapper) -> None:
        """A literal % in the route and a % in the value both work."""
        assert _body(await _get(either_mapper, "search", q="0%")) == [ITEM_ROWS[2]]
        assert _body(await _get(either_mapper, "search", q="P")) == [ITEM_ROWS[0]]

    @pytest.mark.anyio
    async def test_repeated_and_reused_values(self, either_mapper: RouteMapper) -> None:
        """multivalue_sql binds each repeated value; a variable used twice is bound twice."""
        response = await _get(either_mapper, "search", multi={"id": ["1", "3"]}, id="3")
        assert _body(response) == [ITEM_ROWS[0], ITEM_ROWS[2]]
        assert _body(await _get(either_mapper, "search", twice="apple")) == [ITEM_ROWS[0]]

    @pytest.mark.anyio
    async def test_sort_and_limit(self, either_mapper: RouteMapper) -> None:
        """sql_append clauses work as with DuckDB."""
        response = await _get(either_mapper, "search", sort="id DESC", limit="2")
        assert _body(response) == [ITEM_ROWS[3], ITEM_ROWS[2]]

    @pytest.mark.anyio
    async def test_value_of_wrong_type(self, either_mapper: RouteMapper) -> None:
        """A value that can't be an integer gives 500 Database query error."""
        response = await _get(either_mapper, "items/abc")
        assert response.status_code == 500
        assert _body(response) == {"error": "Database query error"}


class TestPostgresRoutes:
    """PostgreSQL-specific behavior of routes."""

    @pytest.mark.anyio
    async def test_column_types(self, mapper: RouteMapper) -> None:
        """Each PostgreSQL type, including arrays and JSON, converts as documented."""
        assert _body(await _get(mapper, "kinds")) == [KINDS_ROW]

    @pytest.mark.anyio
    async def test_delete_fails_and_changes_nothing(
            self, mapper: RouteMapper, running_postgres: PostgresServer) -> None:
        """A writing route fails with 500 because every transaction is read-only."""
        response = await _get(mapper, "delete")
        assert response.status_code == 500
        assert _body(response) == {"error": "Database query error"}
        assert running_postgres.admin_query("SELECT count(*) FROM catalog.scratch") == [(2,)]

    @pytest.mark.anyio
    async def test_routes_are_listed(self, mapper: RouteMapper) -> None:
        """The database root lists its routes, as for DuckDB."""
        routes = _body(await _get(mapper, ""))["routes"]
        assert "kinds" in routes and "search" in routes


class TestAvailability:
    """An unreachable database gives 503 and recovers without a restart."""

    @pytest.mark.anyio
    async def test_down_at_startup_then_recovers(
            self, tmp_path: Path, running_postgres: PostgresServer,
            caplog: pytest.LogCaptureFixture) -> None:
        """Startup warns and continues; routes give 503 until the server is back."""
        running_postgres.stop()
        config = postgres_shop(running_postgres, pool={"timeout": 0.5, "startup_timeout": 0.5})
        databases = {"shop": config, "local": duckdb_shop(tmp_path)}
        mapper = RouteMapper(write_config(tmp_path, databases))
        try:
            with caplog.at_level(logging.WARNING, logger="api_dock"):
                await mapper.start()
            assert (
                "Database 'shop': No connection obtained within 0.5s; PostgreSQL routes "
                "will return 503 until a connection is available"
            ) in caplog.text
            assert "reader_password" not in caplog.text
            response = await _get(mapper, "items/1")
            assert response.status_code == 503
            assert _body(response) == {"error": "Database unavailable"}
            assert _body(await _get(mapper, "items/1", database="local")) == [ITEM_ROWS[0]]
            running_postgres.start()
            assert await _eventually_ok(mapper, "items/1")
        finally:
            await mapper.aclose()

    @pytest.mark.anyio
    async def test_outage_longer_than_reconnect_window_with_traffic(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """Requests during a long outage give 503; the database serves again after it."""
        monkeypatch.setattr(postgres_backend, "RECONNECT_TIMEOUT_SECONDS", 1.0)
        config = postgres_shop(running_postgres, pool={"timeout": 0.3})
        mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
        await mapper.start()
        try:
            assert (await _get(mapper, "items/1")).status_code == 200
            running_postgres.stop()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                assert (await _get(mapper, "items/1")).status_code == 503
                await asyncio.sleep(0.2)
            running_postgres.start()
            assert await _eventually_ok(mapper, "items/1")
        finally:
            await mapper.aclose()

    @pytest.mark.anyio
    async def test_pool_closed_while_serving(
            self, mapper: RouteMapper, caplog: pytest.LogCaptureFixture) -> None:
        """A pool closed outside the mapper's lifecycle gives 500 and an error log."""
        await mapper._postgres.backend(("shop", None)).pool.close()
        with caplog.at_level(logging.ERROR, logger="api_dock"):
            response = await _get(mapper, "items/1")
        assert response.status_code == 500
        assert _body(response) == {"error": "Database query error"}
        assert "Database 'shop': connection pool closed unexpectedly" in caplog.text

    @pytest.mark.anyio
    async def test_statement_timeout_is_a_query_error(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """A query over the statement timeout gives 500, not 503."""
        config = postgres_shop(
            running_postgres, statement_timeout_ms=100,
            routes=[{"route": "slow", "sql": "SELECT pg_sleep(1)"}],
        )
        mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
        await mapper.start()
        try:
            response = await _get(mapper, "slow")
            assert response.status_code == 500
            assert _body(response) == {"error": "Database query error"}
        finally:
            await mapper.aclose()

    @pytest.mark.anyio
    async def test_concurrent_startup_probes(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """Probes of several unreachable databases run at the same time."""
        running_postgres.stop()
        pool = {"startup_timeout": 1}
        databases = {name: postgres_shop(running_postgres, pool=pool) for name in ["a", "b", "c"]}
        mapper = RouteMapper(write_config(tmp_path, databases))
        started = time.monotonic()
        try:
            await mapper.start()
            assert time.monotonic() - started < 2.0
        finally:
            await mapper.aclose()


class TestDuckDBAlongsidePostgres:
    """A slow DuckDB query runs in a worker thread and doesn't hold up PostgreSQL."""

    @pytest.mark.anyio
    async def test_postgres_request_completes_while_duckdb_waits(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """A PostgreSQL request and a heartbeat finish while a DuckDB query is held."""
        release = threading.Event()
        real_execute = DuckDBBackend._execute_in_thread

        def held_execute(self: DuckDBBackend, sql: str, values: List[str]) -> Any:
            assert release.wait(10)
            return real_execute(self, sql, values)

        monkeypatch.setattr(DuckDBBackend, "_execute_in_thread", held_execute)
        databases = {"shop": postgres_shop(running_postgres), "local": duckdb_shop(tmp_path)}
        mapper = RouteMapper(write_config(tmp_path, databases))
        await mapper.start()
        try:
            slow = asyncio.create_task(_get(mapper, "items/1", database="local"))
            await asyncio.sleep(0.01)
            assert _body(await _get(mapper, "items/2")) == [ITEM_ROWS[1]]
            assert not slow.done()
            release.set()
            assert _body(await slow) == [ITEM_ROWS[0]]
        finally:
            release.set()
            await mapper.aclose()


#
# INTERNAL
#
async def _eventually_ok(mapper: RouteMapper, path: str, seconds: float = 10.0) -> bool:
    """Repeat a request until it succeeds.

    Args:
        mapper: Started mapper.
        path: Route path.
        seconds: Longest time to keep trying.

    Returns:
        Whether a request returned 200 in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (await _get(mapper, path)).status_code == 200:
            return True
        await asyncio.sleep(0.2)
    return False
