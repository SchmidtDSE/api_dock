"""

PostgreSQL Backend Module for API Dock

Runs database routes against PostgreSQL through a psycopg connection pool. This
module imports psycopg and psycopg_pool, which are installed with
``api_dock[postgres]``; api_dock imports it only when a PostgreSQL database is
configured.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from api_dock.database_backends import (
    DatabaseBackend,
    DatabaseLifecycleError,
    DatabaseUnavailableError,
)
from api_dock.postgres_config import (
    PostgresSettings,
    connection_options,
    pool_limits,
    resolve_connection_fields,
)
from api_dock.sql_builder import POSTGRES_MARKER


#
# CONSTANTS
#
# Seconds the pool keeps retrying a lost connection before calling
# _replenish_pool, which starts a new round of retries.
RECONNECT_TIMEOUT_SECONDS: float = 300.0

# SQLSTATEs for a server that is shutting down or not yet accepting
# connections. Class 08 (connection exceptions) is also treated as unavailable.
UNAVAILABLE_SQLSTATES: frozenset = frozenset({"57P01", "57P02", "57P03"})
CONNECTION_SQLSTATE_CLASS: str = "08"


#
# PUBLIC
#
class PostgresBackend(DatabaseBackend):
    """Runs SQL on a pooled PostgreSQL connection.

    Each query runs in its own read-only transaction, which is rolled back
    after the rows are fetched, even on success, so settings a query changes
    don't reach the next query on that connection. Queries are sent as
    prepared statements, so SQL with more than one statement is refused.
    Queries are never retried.
    """

    marker: str = POSTGRES_MARKER

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Use an open pool for one database version."""
        self.pool = pool

    async def execute(
            self, sql: str,
            values: List[Optional[str]]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Run SQL on a pooled connection and return all of its rows.

        Args:
            sql: SQL text with a ``%s`` marker per bound value and each literal
                ``%`` written ``%%``.
            values: Values for the markers, in marker order. Always sent, even
                when empty, so ``%%`` is always read as ``%``.

        Returns:
            Tuple of the column names and every row.

        Raises:
            DatabaseUnavailableError: If no connection is available in time or
                the connection is lost.
            DatabaseLifecycleError: If the pool is closed.
            psycopg.Error: For other database errors, such as invalid SQL, a
                value of the wrong type or the statement timeout.
        """
        conn = await self._checkout()
        try:
            return await _run_and_roll_back(conn, sql, values)
        except psycopg.Error as error:
            if _is_unavailable_error(error, conn):
                raise DatabaseUnavailableError("Database unavailable") from error
            raise
        finally:
            await self.pool.putconn(conn)

    async def _checkout(self) -> Any:
        """Take a connection checked by the pool, replacing broken connections.

        Raise DatabaseUnavailableError if no connection is free in time or the
        wait queue is full (max_waiting). Raise DatabaseLifecycleError if the
        pool is closed.
        """
        try:
            return await self.pool.getconn()
        except PoolClosed as error:
            raise DatabaseLifecycleError(f"Connection pool '{self.pool.name}' is closed") from error
        except (PoolTimeout, TooManyRequests) as error:
            raise DatabaseUnavailableError("Database unavailable") from error


async def probe_pool(pool: AsyncConnectionPool, timeout: float) -> bool:
    """Try one checkout within timeout seconds, then immediately return the connection.

    Return False on timeout. The pool stays open for background recovery.
    """
    try:
        conn = await pool.getconn(timeout=timeout)
    except PoolTimeout:
        return False
    await pool.putconn(conn)
    return True


def build_settings(database_config: Dict[str, Any]) -> PostgresSettings:
    """Resolve and validate a config before building its connection string and limits.

    Raise ValueError for missing environment values, invalid connection values,
    unknown libpq keys or malformed syntax. Errors name fields, never values.
    """
    fields = resolve_connection_fields(database_config)
    unknown = sorted(set(fields) - _libpq_keywords())
    if unknown:
        raise ValueError(
            f"connection: {', '.join(repr(key) for key in unknown)} is not a libpq "
            "connection option"
        )
    try:
        conninfo = make_conninfo("", **fields)
    except psycopg.Error:
        raise ValueError(
            "connection: the fields don't form a valid libpq connection string"
        ) from None
    return PostgresSettings(conninfo=conninfo, **pool_limits(database_config))


def create_pool(settings: PostgresSettings, name: str) -> AsyncConnectionPool:
    """Create an unopened pool with enforced session options and recovery callbacks."""
    return AsyncConnectionPool(
        settings.conninfo,
        open=False,
        name=name,
        min_size=settings.min_size,
        max_size=settings.max_size,
        timeout=settings.timeout,
        max_waiting=settings.max_waiting,
        kwargs={
            "autocommit": False,
            "options": connection_options(settings.statement_timeout_ms),
        },
        check=AsyncConnectionPool.check_connection,
        reconnect_timeout=RECONNECT_TIMEOUT_SECONDS,
        reconnect_failed=_replenish_pool,
    )


#
# INTERNAL
#
def _is_unavailable_error(error: psycopg.Error, conn: Any) -> bool:
    """Return whether a query error means the database cannot be reached.

    These errors count: SQLSTATE class 08, and a server that is shutting down or
    starting. An error without a SQLSTATE counts only if the connection is broken
    or closed. A statement timeout does not count. Pool errors are handled when
    the connection is taken, so they never reach this function.
    """
    sqlstate = error.sqlstate
    if sqlstate:
        return sqlstate.startswith(CONNECTION_SQLSTATE_CLASS) or sqlstate in UNAVAILABLE_SQLSTATES
    return bool(conn.broken or conn.closed)


def _libpq_keywords() -> Set[str]:
    """Return the connection option names accepted by the installed libpq."""
    return {option.keyword.decode() for option in psycopg.pq.Conninfo.get_defaults()}


async def _run_and_roll_back(
        conn: Any, sql: str,
        values: List[Optional[str]]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Execute one prepared query, fetch its columns and rows, and always roll back."""
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, values, prepare=True)
            if cursor.description is None:
                return [], []
            return [column.name for column in cursor.description], await cursor.fetchall()
    finally:
        await _roll_back(conn)


async def _roll_back(conn: Any) -> None:
    """Roll back the transaction, closing the connection if rollback fails.

    The pool replaces closed connections instead of reusing them.
    """
    try:
        await conn.rollback()
    except Exception:
        await conn.close()


async def _replenish_pool(pool: Any) -> None:
    """Start new connection attempts after the pool stops retrying.

    psycopg_pool stops retrying a lost connection after RECONNECT_TIMEOUT_SECONDS
    and then calls this function. pool.check() starts one new connection attempt
    if the pool has fewer than max_size connections. It also tests the idle
    connections. It does not wait for the new connection, and it does not run any
    route's SQL again.
    """
    if not pool.closed:
        await pool.check()
