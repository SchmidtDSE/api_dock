"""

PostgreSQL Backend Module for API Dock

Runs routes natively on PostgreSQL through psycopg connection pools, one pool
per named connection (``database.connections`` in ``databases/config.yaml``).
This module imports psycopg and psycopg_pool, which are installed with
``api_dock[postgres]``; api_dock imports it only when connections are configured.

Adapted from PR #6 (PostgreSQL backend).

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from api_dock.database_backends import (
    DatabaseBackend,
    DatabaseLifecycleError,
    DatabaseUnavailableError,
    QueryResult,
)
from api_dock.postgres_config import (
    connection_options,
    libpq_fields,
    PostgresSettings,
    resolve_settings,
)
from api_dock.types import TableReference


#
# CONSTANTS
#
# psycopg's marker for a bound value. With it, each literal % in the SQL is %%.
POSTGRES_MARKER: str = "%s"

# Seconds the pool keeps retrying a lost connection before _replenish_pool
# starts a new round of retries.
RECONNECT_TIMEOUT_SECONDS: float = 300.0

# SQLSTATEs for a server that is shutting down or not yet accepting
# connections. Class 08 (connection exceptions) is also treated as unavailable.
UNAVAILABLE_SQLSTATES: frozenset = frozenset({"57P01", "57P02", "57P03"})
CONNECTION_SQLSTATE_CLASS: str = "08"

# A table's columns and their exact types, in order (for lining up unions).
COLUMNS_SQL: str = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
    "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
    "ORDER BY a.attnum"
)

logger = logging.getLogger(__name__)


#
# PUBLIC
#
class PostgresBackend(DatabaseBackend):
    """Runs SQL on a pooled PostgreSQL connection.

    Each query runs in its own read-only transaction (set on every connection,
    with the statement timeout and UTC), which is rolled back after the rows are
    fetched, so a query can't change settings for the next one. Queries are sent
    as prepared statements, so SQL with more than one statement fails. Queries
    are never retried.
    """

    marker: str = POSTGRES_MARKER

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Use an open pool.

        Args:
            pool: The connection's pool.
        """
        self.pool = pool

    async def execute(
            self,
            sql: str,
            values: List[Optional[str]],
            tables: List[TableReference]) -> QueryResult:
        """Run SQL on a pooled connection and return all of its rows.

        Args:
            sql: SQL with a ``%s`` marker per bound value and each literal ``%``
                written ``%%``.
            values: Values for the markers, in marker order (always sent, so
                ``%%`` is always read as ``%``).
            tables: Unused; PostgreSQL tables need no per-query setup.

        Returns:
            Tuple of the column names and every row.

        Raises:
            DatabaseUnavailableError: If no connection is available in time or
                the connection is lost.
            DatabaseLifecycleError: If the pool is closed.
            psycopg.Error: For other database errors, such as invalid SQL, a
                value of the wrong type or the statement timeout.
        """
        try:
            conn = await self.pool.getconn()
        except PoolClosed as error:
            raise DatabaseLifecycleError(f"Connection pool '{self.pool.name}' is closed") from error
        except (PoolTimeout, TooManyRequests) as error:
            raise DatabaseUnavailableError("Database unavailable") from error
        try:
            return await _run_and_roll_back(conn, sql, values)
        except psycopg.Error as error:
            if _is_unavailable_error(error, conn):
                raise DatabaseUnavailableError("Database unavailable") from error
            raise
        finally:
            await self.pool.putconn(conn)


class PostgresPools:
    """Opens, hands out and closes one connection pool per named connection.

    Pools belong to the event loop that started them; the FastAPI app starts
    them in its lifespan.
    """

    def __init__(self, connections: Dict[str, Dict[str, Any]]) -> None:
        """Keep the connection entries without opening anything.

        Args:
            connections: The checked ``database.connections`` mapping.
        """
        self._connections = connections
        self._backends: Dict[str, PostgresBackend] = {}
        self._columns: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    async def start(self) -> None:
        """Resolve every connection's settings, then open and probe a pool for each.

        All settings are resolved before any pool opens, so a config error
        (e.g. a missing environment variable) stops startup with nothing open.
        A database that can't be reached is logged as a warning; its pool
        keeps trying in the background and its routes return 503 meanwhile.

        Raises:
            ValueError: If a connection's settings are invalid. The message
                names the connection, never a value.
        """
        self._loop = asyncio.get_running_loop()
        settings = {name: _settings(name, entry) for name, entry in self._connections.items()}
        try:
            for name, pool_settings in settings.items():
                pool = _create_pool(pool_settings, name)
                await pool.open(wait=False)
                self._backends[name] = PostgresBackend(pool)
            results = await asyncio.gather(*(
                _probe(self._backends[name].pool, settings[name].startup_timeout)
                for name in settings
            ))
        except BaseException:
            await self.aclose()
            raise
        for name, connected in zip(settings, results):
            if not connected:
                logger.warning(
                    "Connection '%s': no connection obtained within %ss; its PostgreSQL routes "
                    "return 503 until a connection is available", name,
                    settings[name].startup_timeout,
                )

    async def aclose(self) -> None:
        """Close every pool. Safe to call more than once."""
        self._closed = True
        backends, self._backends = self._backends, {}
        for backend in backends.values():
            await backend.pool.close()

    async def columns(
            self, name: str, tables: Iterable[str]) -> Dict[str, List[Tuple[str, str]]]:
        """Each table's columns and types, read once per table and then cached.

        Columns are read from PostgreSQL the first time a table is needed and
        kept until restart (a changed table definition needs a restart).

        Args:
            name: Connection name.
            tables: PostgreSQL table names (``schema.table``) on that connection.

        Returns:
            Table name -> ``(column, type)`` pairs in column order.

        Raises:
            DatabaseUnavailableError: If the database can't be reached.
            DatabaseLifecycleError: See backend().
        """
        backend = self.backend(name)
        result = {}
        for table in tables:
            key = (name, table)
            if key not in self._columns:
                _, rows = await backend.execute(COLUMNS_SQL, [table], [])
                self._columns[key] = [(str(column), str(data_type)) for column, data_type in rows]
            result[table] = self._columns[key]
        return result

    def backend(self, name: str) -> PostgresBackend:
        """The backend for a named connection.

        Args:
            name: Connection name.

        Returns:
            The connection's PostgresBackend.

        Raises:
            DatabaseLifecycleError: After aclose(), from another event loop, or
                for a connection that wasn't configured at startup.
        """
        if self._closed:
            raise DatabaseLifecycleError("PostgreSQL pools are closed")
        if asyncio.get_running_loop() is not self._loop:
            raise DatabaseLifecycleError(
                "PostgreSQL pools can't be used from an event loop other than the one that "
                "started them"
            )
        if name not in self._backends:
            raise DatabaseLifecycleError(
                f"Connection '{name}' was added after startup; restart required"
            )
        return self._backends[name]


#
# INTERNAL
#
def _settings(name: str, entry: Dict[str, Any]) -> PostgresSettings:
    """Resolve a connection's settings and check its fields against libpq.

    Args:
        name: Connection name, for error messages.
        entry: The connection entry.

    Returns:
        The resolved settings.

    Raises:
        ValueError: If a value is invalid, a field isn't a libpq option, or the
            fields don't form a connection string.
    """
    try:
        settings = resolve_settings(entry)
        unknown = sorted(set(libpq_fields(entry)) - _libpq_keywords())
        if unknown:
            raise ValueError(
                f"{', '.join(repr(key) for key in unknown)} is not a libpq connection option"
            )
        make_conninfo("", **settings.fields)
    except psycopg.Error:
        raise ValueError(
            f"connection '{name}': the fields don't form a valid libpq connection string"
        ) from None
    except ValueError as error:
        raise ValueError(f"connection '{name}': {error}") from error
    return settings


def _create_pool(settings: PostgresSettings, name: str) -> AsyncConnectionPool:
    """Create an unopened pool with the enforced session options and recovery.

    Args:
        settings: Resolved connection settings.
        name: Connection name (the pool's name in psycopg's logs).

    Returns:
        The pool.
    """
    return AsyncConnectionPool(
        make_conninfo("", **settings.fields),
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


async def _probe(pool: AsyncConnectionPool, timeout: float) -> bool:
    """Try to take and return one connection within ``timeout`` seconds.

    Args:
        pool: The pool to probe.
        timeout: Seconds to wait.

    Returns:
        False on timeout (the pool stays open and keeps trying).
    """
    try:
        conn = await pool.getconn(timeout=timeout)
    except PoolTimeout:
        return False
    await pool.putconn(conn)
    return True


async def _run_and_roll_back(conn: Any, sql: str, values: List[Optional[str]]) -> QueryResult:
    """Execute one prepared query, fetch its columns and rows, and always roll back.

    Args:
        conn: A pooled connection.
        sql: SQL with ``%s`` markers.
        values: Values for the markers.

    Returns:
        Tuple of the column names and every row.
    """
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, values, prepare=True)
            if cursor.description is None:
                return [], []
            return [column.name for column in cursor.description], await cursor.fetchall()
    finally:
        try:
            await conn.rollback()
        except Exception:
            # The pool replaces closed connections instead of reusing them.
            await conn.close()


def _is_unavailable_error(error: psycopg.Error, conn: Any) -> bool:
    """Whether a query error means the database can't be reached.

    SQLSTATE class 08 and a server shutting down or starting count; an error
    without a SQLSTATE counts only if the connection is broken or closed. A
    statement timeout does not count.

    Args:
        error: The psycopg error.
        conn: The connection the query ran on.

    Returns:
        True if the database is unavailable (respond 503, not 500).
    """
    sqlstate = error.sqlstate
    if sqlstate:
        return sqlstate.startswith(CONNECTION_SQLSTATE_CLASS) or sqlstate in UNAVAILABLE_SQLSTATES
    return bool(conn.broken or conn.closed)


def _libpq_keywords() -> Set[str]:
    """The connection option names the installed libpq accepts.

    Returns:
        Set of libpq keywords.
    """
    return {option.keyword.decode() for option in psycopg.pq.Conninfo.get_defaults()}


async def _replenish_pool(pool: Any) -> None:
    """Start new connection attempts after the pool stops retrying.

    Args:
        pool: The pool whose reconnect attempts timed out.
    """
    if not pool.closed:
        await pool.check()
