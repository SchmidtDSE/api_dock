"""

Tests for the DuckDB database backend.

The backend runs each query on a new in-memory DuckDB connection in a worker
thread. The connection is created, used and closed in that thread, so a slow
query does not block the event loop, and the connection is closed after
errors and after the awaiting request is cancelled.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import threading
from typing import Any, Dict, List, Optional

import duckdb
import pytest

from api_dock import database_backends
from api_dock.database_backends import DuckDBBackend


#
# CONSTANTS
#
# Seconds to wait for a worker thread before failing instead of hanging.
WAIT_SECONDS: float = 5.0


#
# FIXTURES
#
class _RecordingConnection:
    """Wraps a DuckDB connection and records the thread of each call."""

    def __init__(
            self,
            connection: Any,
            threads: Dict[str, int],
            release: Optional[threading.Event] = None) -> None:
        """Wrap a connection.

        Args:
            connection: Real DuckDB connection.
            threads: Dictionary to record call names and thread ids in.
            release: If given, execute waits for this event before running and
                records in ``released`` whether it was set in time.
        """
        self._connection = connection
        self._threads = threads
        self._release = release
        self.started = threading.Event()
        self.closed = threading.Event()
        self.released = False

    @property
    def description(self) -> Any:
        """Return the wrapped connection's description."""
        return self._connection.description

    def execute(self, sql: str, values: Optional[List[str]] = None) -> Any:
        """Record the thread, optionally wait for release, then execute.

        Args:
            sql: SQL text.
            values: Bound values.

        Returns:
            The wrapped connection's execute result.
        """
        self._threads["execute"] = threading.get_ident()
        self.started.set()
        if self._release is not None:
            self.released = self._release.wait(WAIT_SECONDS)
        return self._connection.execute(sql, values)

    def close(self) -> None:
        """Record the thread and close the wrapped connection."""
        self._threads["close"] = threading.get_ident()
        self._connection.close()
        self.closed.set()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Make DuckDB connections recordable and return what they record.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Dictionary with ``threads`` (call name to thread id), ``release`` (an
        event that blocking executes wait for) and ``connections``.
    """
    real_connect = duckdb.connect
    state: Dict[str, Any] = {
        "threads": {}, "release": None, "connections": [],
    }

    def connect(*args: Any, **kwargs: Any) -> _RecordingConnection:
        state["threads"]["connect"] = threading.get_ident()
        connection = _RecordingConnection(
            real_connect(*args, **kwargs), state["threads"], state["release"]
        )
        state["connections"].append(connection)
        return connection

    monkeypatch.setattr(database_backends.duckdb, "connect", connect)
    return state


#
# PUBLIC
#
class TestDuckDBBackend:
    """The DuckDB backend runs a query and returns its columns and rows."""

    def test_marker_is_question_mark(self) -> None:
        """DuckDB uses ? for bound values."""
        assert DuckDBBackend({}).marker == "?"

    @pytest.mark.anyio
    async def test_returns_columns_and_rows(self) -> None:
        """Column names and all rows are returned, with values bound."""
        columns, rows = await DuckDBBackend({}).execute(
            "SELECT ? AS name, 2 AS n UNION ALL SELECT 'b', 3 ORDER BY n", ["a"]
        )
        assert columns == ["name", "n"]
        assert rows == [("a", 2), ("b", 3)]

    @pytest.mark.anyio
    async def test_connection_stays_in_one_worker_thread(
            self, recorded: Dict[str, Any]) -> None:
        """Connect, execute and close all run in one thread that isn't the event loop's."""
        await DuckDBBackend({}).execute("SELECT 1", [])
        threads = recorded["threads"]
        assert set(threads) == {"connect", "execute", "close"}
        assert len(set(threads.values())) == 1
        assert threads["connect"] != threading.get_ident()

    @pytest.mark.anyio
    async def test_connection_closed_after_error(self, recorded: Dict[str, Any]) -> None:
        """A failing query raises and its connection is still closed."""
        with pytest.raises(duckdb.Error):
            await DuckDBBackend({}).execute("SELECT * FROM missing_table", [])
        assert recorded["connections"][0].closed.is_set()

    @pytest.mark.anyio
    async def test_slow_query_does_not_block_event_loop(
            self, recorded: Dict[str, Any]) -> None:
        """Other tasks run while a query waits; here one of them releases it."""
        recorded["release"] = threading.Event()
        heartbeats: List[str] = []

        async def heartbeat() -> None:
            await asyncio.sleep(0.01)
            heartbeats.append("beat")
            recorded["release"].set()

        query = asyncio.create_task(DuckDBBackend({}).execute("SELECT 1 AS n", []))
        await heartbeat()
        columns, rows = await asyncio.wait_for(query, WAIT_SECONDS)
        assert heartbeats == ["beat"]
        assert recorded["connections"][0].released
        assert (columns, rows) == (["n"], [(1,)])

    @pytest.mark.anyio
    async def test_cancelled_request_still_closes_connection(
            self, recorded: Dict[str, Any]) -> None:
        """Cancelling the awaiting task leaves the worker to finish and close."""
        recorded["release"] = threading.Event()
        query = asyncio.create_task(DuckDBBackend({}).execute("SELECT 1", []))
        while not recorded["connections"]:
            await asyncio.sleep(0.001)
        connection = recorded["connections"][0]
        assert await asyncio.to_thread(connection.started.wait, WAIT_SECONDS)

        query.cancel()
        with pytest.raises(asyncio.CancelledError):
            await query
        assert not connection.closed.is_set()

        recorded["release"].set()
        assert await asyncio.to_thread(connection.closed.wait, WAIT_SECONDS)
        assert recorded["threads"]["close"] == recorded["threads"]["connect"]
