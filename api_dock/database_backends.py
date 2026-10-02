"""

Database Backends Module for API Dock

Runs a database route's SQL and returns its columns and rows. Each backend
states the marker it uses for bound values, so the SQL builder can write SQL
for it.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

import duckdb

from api_dock.sql_builder import SQL_MARKER
from api_dock.storage_auth import (
    detect_required_backends,
    extract_table_metadata_by_backend,
    extract_table_uris,
    setup_storage_authentication,
)


#
# PUBLIC
#
class DatabaseBackend(ABC):
    """Runs SQL with bound values against one database.

    Attributes:
        marker: Text written into SQL for each bound value.
    """

    marker: str

    @abstractmethod
    async def execute(self, sql: str, values: List[str]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Run SQL and return all of its rows.

        Errors from the database are raised for the caller to handle.

        Args:
            sql: SQL text with one marker per bound value.
            values: Values for the markers, in marker order.

        Returns:
            Tuple of the column names and every row.
        """


class DuckDBBackend(DatabaseBackend):
    """Runs SQL on a new in-memory DuckDB connection for each query.

    The DuckDB driver blocks, so each query runs in a worker thread. The
    connection is created, used and closed in that thread. If the awaiting
    request is cancelled, the query is not stopped; the worker closes the
    connection when the query finishes.
    """

    marker: str = SQL_MARKER

    def __init__(self, database_config: Dict[str, Any]) -> None:
        """Create a backend for one request.

        Args:
            database_config: Database configuration, used to set up access to
                its tables' storage (S3, GCS, Azure or HTTP).
        """
        self.database_config = database_config

    async def execute(self, sql: str, values: List[str]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Run SQL in a worker thread and return all of its rows.

        Args:
            sql: SQL text with a ``?`` marker per bound value.
            values: Values for the markers, in marker order.

        Returns:
            Tuple of the column names and every row.
        """
        return await asyncio.to_thread(self._execute_in_thread, sql, values)

    def _execute_in_thread(
            self, sql: str, values: List[str]) -> Tuple[List[str], List[Tuple[Any, ...]]]:
        """Open a connection, run SQL, fetch every row and close the connection.

        Args:
            sql: SQL text with a ``?`` marker per bound value.
            values: Values for the markers, in marker order.

        Returns:
            Tuple of the column names and every row.
        """
        conn = duckdb.connect(database=':memory:')
        try:
            self._setup_storage(conn)
            rows = conn.execute(sql, values).fetchall()
            columns = [desc[0] for desc in conn.description] if conn.description else []
            return columns, rows
        finally:
            conn.close()

    def _setup_storage(self, conn: Any) -> None:
        """Set up access to the storage backends the config's tables use.

        Args:
            conn: DuckDB connection.
        """
        table_uris = extract_table_uris(self.database_config)
        required_backends = detect_required_backends(table_uris)
        backend_metadata = extract_table_metadata_by_backend(self.database_config)
        setup_storage_authentication(conn, required_backends, backend_metadata)
