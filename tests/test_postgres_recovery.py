"""

Tests for PostgreSQL connection pool recovery after long outages.

The pool stops retrying a connection after its reconnect window. api_dock then
asks the pool to check itself, which starts a new attempt, so the pool keeps
trying in the background without needing requests. These tests shorten the
reconnect window to about a second.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import time
from typing import Any, Callable

import pytest

from api_dock import postgres_backend
from api_dock.postgres_backend import create_pool
from api_dock.postgres_config import PostgresSettings
from tests.conftest import PostgresServer


#
# CONSTANTS
#
# Shortened reconnect window, in seconds.
SHORT_RECONNECT_SECONDS: float = 1.0

# Outage length: several reconnect windows.
OUTAGE_SECONDS: float = 4.0

# Seconds to wait for the pool to refill after the server is back.
REFILL_SECONDS: float = 10.0


#
# FIXTURES
#
@pytest.fixture
def short_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the pool's reconnect window.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(postgres_backend, "RECONNECT_TIMEOUT_SECONDS", SHORT_RECONNECT_SECONDS)


def _settings(server: PostgresServer) -> PostgresSettings:
    """Build pool settings for the test database.

    Args:
        server: The test server.

    Returns:
        Settings with one connection kept open.
    """
    fields = " ".join(f"{key}={value}" for key, value in server.connection().items())
    return PostgresSettings(
        conninfo=f"{fields} connect_timeout=2", min_size=1, max_size=2, timeout=1.0,
        max_waiting=4, startup_timeout=1.0, statement_timeout_ms=10000,
    )


async def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    """Wait for a condition to become true.

    Args:
        condition: Function to poll.
        seconds: Longest time to wait.

    Returns:
        Whether the condition became true in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


def _available(pool: Any) -> int:
    """Count the idle connections in a pool.

    Args:
        pool: An open pool.

    Returns:
        Number of connections ready for checkout.
    """
    return pool.get_stats().get("pool_available", 0)


#
# PUBLIC
#
class TestLongOutageRecovery:
    """The pool refills by itself after outages longer than its reconnect window."""

    @pytest.mark.anyio
    async def test_refills_after_outage_from_startup_with_no_requests(
            self, running_postgres: PostgresServer, short_reconnect: None) -> None:
        """A pool opened while the server is down refills once it is back, with no requests."""
        running_postgres.stop()
        pool = create_pool(_settings(running_postgres), name="outage-at-start")
        await pool.open(wait=False)
        try:
            await asyncio.sleep(OUTAGE_SECONDS)
            assert _available(pool) == 0
            running_postgres.start()
            assert await _wait_until(lambda: _available(pool) >= 1, REFILL_SECONDS)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_refills_after_lost_connections_with_no_requests(
            self, running_postgres: PostgresServer, short_reconnect: None) -> None:
        """Connections lost in an outage are replaced after it, with no requests during it."""
        pool = create_pool(_settings(running_postgres), name="outage-later")
        await pool.open(wait=False)
        try:
            assert await _wait_until(lambda: _available(pool) >= 1, REFILL_SECONDS)
            running_postgres.stop()
            await pool.check()
            await asyncio.sleep(OUTAGE_SECONDS)
            assert _available(pool) == 0
            running_postgres.start()
            assert await _wait_until(lambda: _available(pool) >= 1, REFILL_SECONDS)
            async with pool.connection() as conn:
                assert await (await conn.execute("SELECT 1")).fetchone() == (1,)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_close_stops_recovery(
            self, running_postgres: PostgresServer, short_reconnect: None) -> None:
        """Closing the pool during an outage stops its background tasks."""
        running_postgres.stop()
        pool = create_pool(_settings(running_postgres), name="outage-closed")
        await pool.open(wait=False)
        await asyncio.sleep(SHORT_RECONNECT_SECONDS * 2)
        await pool.close()
        running_postgres.start()
        await asyncio.sleep(SHORT_RECONNECT_SECONDS * 2)
        assert pool.closed
        assert _available(pool) == 0
        pool_tasks = [task for task in asyncio.all_tasks() if "outage-closed" in task.get_name()]
        assert pool_tasks == []
