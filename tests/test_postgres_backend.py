"""

Tests for the PostgreSQL database backend.

The backend runs each query in its own read-only transaction on a pooled
connection, sends it as a prepared statement and rolls the transaction back
afterwards, even on success. Errors that mean the database can't be reached
are raised as DatabaseUnavailableError; other errors are raised unchanged.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import PoolTimeout, TooManyRequests

from api_dock.database_backends import DatabaseLifecycleError, DatabaseUnavailableError
from api_dock.postgres_backend import (
    PostgresBackend,
    build_settings,
    _is_unavailable_error,
    _roll_back,
    create_pool,
    probe_pool,
)
from tests.conftest import PostgresServer


#
# CONSTANTS
#
SETTINGS_QUERY: str = (
    "SELECT current_setting('default_transaction_read_only'), "
    "current_setting('statement_timeout'), current_setting('TimeZone'), pg_backend_pid()"
)

PoolFactory = Callable[..., Awaitable[Any]]


#
# FIXTURES
#
@pytest.fixture
async def open_pool(running_postgres: PostgresServer) -> AsyncIterator[PoolFactory]:
    """Return a function that opens a pool for the test database.

    Pools are closed when the test ends.

    Args:
        running_postgres: The session server.

    Yields:
        Async function taking ``pool`` and ``statement_timeout_ms`` config values.
    """
    pools: List[Any] = []

    async def factory(pool: Optional[Dict[str, Any]] = None, **changes: Any) -> Any:
        config = running_postgres.database_config(pool=pool or {}, **changes)
        new_pool = create_pool(build_settings(config), name=f"test-{len(pools)}")
        pools.append(new_pool)
        await new_pool.open(wait=False)
        return new_pool

    yield factory
    for pool in pools:
        await pool.close()


async def _pid(backend: PostgresBackend) -> int:
    """Get the server process id of the connection a query runs on.

    Args:
        backend: Backend to query.

    Returns:
        The backend process id.
    """
    _, rows = await backend.execute("SELECT pg_backend_pid()", [])
    return rows[0][0]


async def _wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    """Wait until a condition is true, failing the test if it never is.

    Args:
        condition: Function to poll.
        seconds: Longest time to wait.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


class _FakeConnection:
    """Stands in for a psycopg connection in error classification tests."""

    def __init__(self, broken: bool = False, closed: bool = False) -> None:
        """Set the connection state.

        Args:
            broken: Whether the connection is broken.
            closed: Whether the connection is closed.
        """
        self.broken = broken
        self.closed = closed


class _FailingRollbackConnection:
    """Stands in for a psycopg connection whose rollback fails."""

    def __init__(self) -> None:
        """Start open."""
        self.closed = False

    async def rollback(self) -> None:
        """Fail as a lost connection would."""
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    async def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True


#
# PUBLIC
#
class TestQueries:
    """Queries return their rows; values are bound."""

    @pytest.mark.anyio
    async def test_columns_rows_and_bound_values(self, open_pool: PoolFactory) -> None:
        """Values are sent separately and matched as text, including quotes and %."""
        backend = PostgresBackend(await open_pool())
        assert backend.marker == "%s"
        sql = "SELECT id, name FROM catalog.items WHERE name = %s OR name = %s ORDER BY id"
        columns, rows = await backend.execute(sql, ["O'Brien", "%s literal"])
        assert columns == ["id", "name"]
        assert rows == [(2, "O'Brien"), (4, "%s literal")]

    @pytest.mark.anyio
    async def test_doubled_percent_without_values(self, open_pool: PoolFactory) -> None:
        """With an empty value list, %% is read as a single % ."""
        backend = PostgresBackend(await open_pool())
        _, rows = await backend.execute("SELECT name FROM catalog.items WHERE name LIKE '50%%'", [])
        assert rows == [("50% off",)]

    @pytest.mark.anyio
    async def test_delete_fails_and_changes_nothing(
            self, open_pool: PoolFactory, running_postgres: PostgresServer) -> None:
        """The reader may DELETE from scratch, but the transaction is read-only."""
        backend = PostgresBackend(await open_pool())
        with pytest.raises(errors.ReadOnlySqlTransaction):
            await backend.execute("DELETE FROM catalog.scratch", [])
        assert running_postgres.admin_query("SELECT count(*) FROM catalog.scratch") == [(2,)]

    @pytest.mark.anyio
    @pytest.mark.parametrize("values", [[], ["1"]])
    async def test_several_statements_fail(
            self, open_pool: PoolFactory, running_postgres: PostgresServer,
            values: List[str]) -> None:
        """Prepared statements allow one statement, so read-only can't be switched off."""
        backend = PostgresBackend(await open_pool())
        where = " WHERE id = %s" if values else ""
        with pytest.raises(psycopg.errors.SyntaxError):
            await backend.execute(
                f"COMMIT; BEGIN READ WRITE; DELETE FROM catalog.scratch{where}", values
            )
        assert running_postgres.admin_query("SELECT count(*) FROM catalog.scratch") == [(2,)]

    @pytest.mark.anyio
    async def test_statement_without_result_set(self, open_pool: PoolFactory) -> None:
        """A statement that returns no result set gives no columns and no rows."""
        backend = PostgresBackend(await open_pool())
        assert await backend.execute("SET LOCAL TimeZone = 'UTC'", []) == ([], [])

    @pytest.mark.anyio
    async def test_value_of_wrong_type_is_a_query_error(self, open_pool: PoolFactory) -> None:
        """A value that isn't a valid integer fails as a query error."""
        backend = PostgresBackend(await open_pool())
        with pytest.raises(errors.InvalidTextRepresentation):
            await backend.execute("SELECT * FROM catalog.items WHERE id = %s", ["0 OR true"])


class TestSessionSettings:
    """Each query runs in its own transaction, rolled back afterwards."""

    @pytest.mark.anyio
    async def test_set_config_does_not_reach_next_query(self, open_pool: PoolFactory) -> None:
        """Settings changed by one query are undone before the connection is reused."""
        backend = PostgresBackend(await open_pool(pool={"max_size": 1}))
        _, rows = await backend.execute(
            "SELECT set_config('default_transaction_read_only', 'off', false), "
            "set_config('statement_timeout', '0', false), "
            "set_config('TimeZone', 'America/New_York', false), pg_backend_pid()", []
        )
        _, after = await backend.execute(SETTINGS_QUERY, [])
        assert after[0][:3] == ("on", "10s", "UTC")
        assert after[0][3] == rows[0][3]

    @pytest.mark.anyio
    async def test_replacement_connection_gets_settings(
            self, open_pool: PoolFactory, running_postgres: PostgresServer) -> None:
        """A connection that replaces a lost one is read-only, with the timeout and UTC."""
        backend = PostgresBackend(await open_pool(pool={"max_size": 1}, statement_timeout_ms=1500))
        first_pid = await _pid(backend)
        running_postgres.admin_query(f"SELECT pg_terminate_backend({first_pid})")
        _, rows = await backend.execute(SETTINGS_QUERY, [])
        assert rows[0][:3] == ("on", "1500ms", "UTC")
        assert rows[0][3] != first_pid

    @pytest.mark.anyio
    async def test_statement_timeout_then_success(self, open_pool: PoolFactory) -> None:
        """A query over the time limit fails as a query error; the next one succeeds."""
        backend = PostgresBackend(await open_pool(pool={"max_size": 1}, statement_timeout_ms=100))
        with pytest.raises(errors.QueryCanceled):
            await backend.execute("SELECT pg_sleep(2)", [])
        assert (await backend.execute("SELECT 1", []))[1] == [(1,)]

    @pytest.mark.anyio
    async def test_cancelled_request_then_success(self, open_pool: PoolFactory) -> None:
        """Cancelling a request cancels its query; the pool still serves the next one."""
        backend = PostgresBackend(await open_pool(pool={"max_size": 1}))
        query = asyncio.create_task(backend.execute("SELECT pg_sleep(5)", []))
        await asyncio.sleep(0.3)
        query.cancel()
        with pytest.raises(asyncio.CancelledError):
            await query
        started = time.monotonic()
        assert (await backend.execute("SELECT 1", []))[1] == [(1,)]
        assert time.monotonic() - started < 3


class TestUnavailable:
    """Errors that mean the database can't be reached become DatabaseUnavailableError."""

    @pytest.mark.anyio
    async def test_full_queue_and_checkout_timeout(self, open_pool: PoolFactory) -> None:
        """With the only connection taken, one request waits and times out; the next is refused."""
        pool = await open_pool(pool={"max_size": 1, "max_waiting": 1, "timeout": 0.5})
        backend = PostgresBackend(pool)
        held = await pool.getconn()
        try:
            waiting = asyncio.create_task(backend.execute("SELECT 1", []))
            await _wait_for(lambda: pool.get_stats().get("requests_waiting", 0) == 1)
            with pytest.raises(DatabaseUnavailableError) as refused:
                await backend.execute("SELECT 1", [])
            assert isinstance(refused.value.__cause__, TooManyRequests)
            with pytest.raises(DatabaseUnavailableError) as timed_out:
                await waiting
            assert isinstance(timed_out.value.__cause__, PoolTimeout)
        finally:
            await pool.putconn(held)

    @pytest.mark.anyio
    async def test_terminated_during_query_is_not_retried(
            self, open_pool: PoolFactory, running_postgres: PostgresServer) -> None:
        """A query whose connection is ended fails at once and is not run again."""
        backend = PostgresBackend(await open_pool(pool={"max_size": 1}))
        query = asyncio.create_task(backend.execute("SELECT pg_sleep(3)", []))
        sleeping = "SELECT pid FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(3)%'"
        await _wait_for(lambda: bool(running_postgres.admin_query(sleeping)))
        running_postgres.admin_query(f"SELECT pg_terminate_backend(({sleeping}))")
        started = time.monotonic()
        with pytest.raises(DatabaseUnavailableError):
            await query
        assert time.monotonic() - started < 2
        assert running_postgres.admin_query(sleeping) == []

    @pytest.mark.anyio
    async def test_server_restart_with_idle_connections(
            self, open_pool: PoolFactory, running_postgres: PostgresServer) -> None:
        """After a restart, the checkout check replaces the stale idle connection."""
        pool = await open_pool(pool={"min_size": 2, "max_size": 2})
        backend = PostgresBackend(pool)
        await _wait_for(lambda: pool.get_stats().get("pool_available", 0) == 2)
        running_postgres.stop()
        running_postgres.start()
        assert (await backend.execute("SELECT 1", []))[1] == [(1,)]

    @pytest.mark.anyio
    async def test_closed_pool_is_a_lifecycle_error(self, open_pool: PoolFactory) -> None:
        """A closed pool is reported as an api_dock lifecycle problem, not unavailability."""
        pool = await open_pool()
        await pool.close()
        with pytest.raises(DatabaseLifecycleError):
            await PostgresBackend(pool).execute("SELECT 1", [])


class TestRollBack:
    """Every query's transaction is rolled back before the connection is reused."""

    @pytest.mark.anyio
    async def test_failed_rollback_closes_connection(self) -> None:
        """If the rollback fails, the connection is closed so the pool replaces it."""
        conn = _FailingRollbackConnection()
        await _roll_back(conn)
        assert conn.closed


class TestErrorClassification:
    """_is_unavailable_error separates availability problems from query errors."""

    @pytest.mark.parametrize("sqlstate", ["08000", "08003", "08006", "57P01", "57P02", "57P03"])
    def test_unavailable_sqlstates(self, sqlstate: str) -> None:
        """Connection-class and shutdown errors mean the database is unavailable."""
        assert _is_unavailable_error(errors.lookup(sqlstate)("x"), _FakeConnection())

    @pytest.mark.parametrize("sqlstate", ["57014", "42601", "25006", "22P02", "42501", "53300"])
    def test_query_error_sqlstates(self, sqlstate: str) -> None:
        """Statement timeout, syntax, read-only, value and permission errors are query errors."""
        assert not _is_unavailable_error(errors.lookup(sqlstate)("x"), _FakeConnection())

    @pytest.mark.parametrize("broken, closed, expected", [
        (True, False, True), (False, True, True), (False, False, False),
    ])
    def test_transport_error_without_sqlstate(
            self, broken: bool, closed: bool, expected: bool) -> None:
        """An error without SQLSTATE is unavailability only if the connection is gone."""
        error = psycopg.OperationalError("server closed the connection unexpectedly")
        assert _is_unavailable_error(error, _FakeConnection(broken, closed)) is expected


class TestProbe:
    """The startup probe tries one checkout within a time limit."""

    @pytest.mark.anyio
    async def test_probe_succeeds(self, open_pool: PoolFactory) -> None:
        """A reachable database gives a connection, which goes back to the pool."""
        pool = await open_pool()
        assert await probe_pool(pool, 3.0)
        assert pool.get_stats().get("pool_available", 0) >= 1

    @pytest.mark.anyio
    async def test_probe_times_out_and_pool_stays_open(
            self, open_pool: PoolFactory, running_postgres: PostgresServer) -> None:
        """An unreachable database fails the probe within its time limit; the pool stays open."""
        running_postgres.stop()
        pool = await open_pool()
        started = time.monotonic()
        assert not await probe_pool(pool, 0.5)
        assert time.monotonic() - started < 2
        assert not pool.closed
