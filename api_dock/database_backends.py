"""

Database Backends Module for API Dock

Runs a database route's SQL and returns its columns and rows. Each backend
states the marker it writes for bound values, so the SQL builder can write SQL
for it. DuckDB is the default backend: it reads file tables (Parquet, CSV, ...)
from local disk or cloud storage.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from api_dock.postgres_config import connection_options, conninfo, resolve_settings
from api_dock.sql_builder import build_schema_view_statements, postgres_catalog, SQL_MARKER
from api_dock.storage_auth import setup_table_storage_authentication
from api_dock.types import TableReference


#
# CONSTANTS
#
# `settings.duckdb` options. Every key is applied to each query's DuckDB
# connection as `SET <key> = <value>` (memory_limit, threads, temp_directory,
# ...), except max_concurrent_queries, which caps how many database queries run
# at once in this process (others wait their turn).
DUCKDB_SETTINGS_KEY: str = "duckdb"
MAX_CONCURRENT_QUERIES_KEY: str = "max_concurrent_queries"

DUCKDB_OPTION_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Column names and rows returned by a backend.
QueryResult = Tuple[List[str], List[Tuple[Any, ...]]]


#
# PUBLIC
#
class DatabaseUnavailableError(Exception):
    """The database can't be reached right now (respond 503, not 500)."""


class DatabaseLifecycleError(Exception):
    """A backend was used before it was started, after it closed, or on the wrong loop."""


class DatabaseBackend(ABC):
    """Runs SQL with bound values and returns every row.

    Attributes:
        marker: Text written into SQL for each bound value.
    """

    marker: str = SQL_MARKER

    @abstractmethod
    async def execute(
            self,
            sql: str,
            values: List[Optional[str]],
            tables: List[TableReference]) -> QueryResult:
        """Run SQL and return all of its rows.

        Errors from the database are raised for the caller to handle.

        Args:
            sql: SQL text with one marker per bound value.
            values: Values for the markers, in marker order.
            tables: The tables the query may read (for storage access).

        Returns:
            Tuple of the column names and every row.
        """


class DuckDBBackend(DatabaseBackend):
    """Runs SQL on a new in-memory DuckDB connection for each query.

    The DuckDB driver blocks, so each query runs in a worker thread, letting
    other requests (and health checks) be served meanwhile. The ``settings.duckdb``
    options, storage authentication for every table, and views for
    ``[[schema.table]]`` references are set up on the connection first. Each
    PostgreSQL connection a query reads is attached read-only (with the
    connection's statement timeout), so DuckDB can mix PostgreSQL tables with
    files and with tables on other connections.

    Attributes:
        statements: ``SET`` statements from ``settings.duckdb``.
        max_concurrent_queries: Cap on queries running at once, or None.
    """

    def __init__(self, options: Any = None,
                 connections: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """Create the backend from the ``settings.duckdb`` options.

        Args:
            options: The ``settings.duckdb`` mapping, or None.
            connections: The ``database.connections`` mapping (for attaching
                PostgreSQL tables), or None.

        Raises:
            ValueError: If the options are invalid (see parse_duckdb_settings).
        """
        self.connections = connections or {}
        self.statements, self.max_concurrent_queries = parse_duckdb_settings(options)
        self._slots = (
            threading.BoundedSemaphore(self.max_concurrent_queries)
            if self.max_concurrent_queries else None
        )

    async def execute(
            self,
            sql: str,
            values: List[Optional[str]],
            tables: List[TableReference]) -> QueryResult:
        """Run SQL in a worker thread and return all of its rows.

        Args:
            sql: SQL text with a ``?`` marker per bound value.
            values: Values for the markers, in marker order.
            tables: The tables the query may read: storage authentication is set
                up for each, and qualified ones are exposed as views.

        Returns:
            Tuple of the column names and every row.
        """
        return await asyncio.to_thread(self._execute_in_thread, sql, values, tables)

    def _execute_in_thread(
            self,
            sql: str,
            values: List[Optional[str]],
            tables: List[TableReference]) -> QueryResult:
        """Open a connection, set it up, run SQL, fetch every row and close it.

        Honors max_concurrent_queries by waiting for a free slot first.

        Args:
            sql: SQL text with a ``?`` marker per bound value.
            values: Values for the markers, in marker order.
            tables: The tables the query may read.

        Returns:
            Tuple of the column names and every row.
        """
        import duckdb

        if self._slots is not None:
            self._slots.acquire()
        try:
            conn = duckdb.connect(database=':memory:')
            try:
                for statement in self.statements:
                    conn.execute(statement)
                self._attach_postgres(conn, tables)
                setup_table_storage_authentication(
                    conn, [table for table in tables if not table.is_postgres]
                )
                for statement in build_schema_view_statements(tables):
                    conn.execute(statement)
                rows = conn.execute(sql, values).fetchall()
                columns = [desc[0] for desc in conn.description] if conn.description else []
                return columns, rows
            finally:
                conn.close()
        finally:
            if self._slots is not None:
                self._slots.release()

    def _attach_postgres(self, conn: Any, tables: List[TableReference]) -> None:
        """Attach each PostgreSQL connection the query's tables use, read-only.

        Args:
            conn: The query's DuckDB connection.
            tables: The tables the query may read.

        Raises:
            ValueError: If a connection's settings can't be resolved (e.g. an
                unset ``env:`` variable).
            DatabaseUnavailableError: If a connection can't be attached.
        """
        names = sorted({table.connection for table in tables if table.is_postgres})
        if not names:
            return
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
        for name in names:
            settings = resolve_settings(self.connections[name])
            dsn = conninfo({
                **settings.fields,
                "options": connection_options(settings.statement_timeout_ms),
            })
            try:
                conn.execute(
                    f"ATTACH '{dsn.replace(chr(39), chr(39) * 2)}' AS {postgres_catalog(name)} "
                    "(TYPE postgres, READ_ONLY)"
                )
            except Exception as error:
                raise DatabaseUnavailableError("Database unavailable") from error


def parse_duckdb_settings(options: Any) -> Tuple[List[str], Optional[int]]:
    """Turn ``settings.duckdb`` into SET statements and a concurrency cap.

    Args:
        options: Mapping of DuckDB option -> value, plus optional
            max_concurrent_queries; or None.

    Returns:
        Tuple of (SET statements, max concurrent queries or None).

    Raises:
        ValueError: If options isn't a mapping, an option name isn't a plain
            identifier, or max_concurrent_queries isn't a positive integer.
    """
    if not options:
        return ([], None)
    if not isinstance(options, dict):
        raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY} must be a mapping")

    max_queries = options.get(MAX_CONCURRENT_QUERIES_KEY)
    if max_queries is not None and (not isinstance(max_queries, int) or max_queries < 1):
        raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY}.{MAX_CONCURRENT_QUERIES_KEY} "
                         "must be a positive integer")

    statements = []
    for name, value in options.items():
        if name == MAX_CONCURRENT_QUERIES_KEY:
            continue
        if not DUCKDB_OPTION_PATTERN.match(str(name)):
            raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY}: invalid option name '{name}'")
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = "'" + str(value).replace("'", "''") + "'"
        statements.append(f"SET {name} = {literal}")
    return (statements, max_queries)
