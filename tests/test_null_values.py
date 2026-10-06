"""

Tests for bound values that are NULL.

build_sql_query() returns None for a variable whose value is None, and both
backends send None as SQL NULL. Request values stay text.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from typing import Any, AsyncIterator, Dict

import pytest

from api_dock.database_backends import DuckDBBackend
from api_dock.postgres_pools import PostgresPools
from api_dock.sql_builder import build_sql_query
from tests.conftest import PostgresServer


#
# CONSTANTS
#
ROUTE: Dict[str, Any] = {
    "route": "check/{{id}}",
    "sql": "SELECT {{value}} IS NULL AS is_null, {{id}} AS id",
    "query_params": [{"other": {"sql": "{{value}} IS NULL"}}],
}

# Requests don't give None values; resolvers will. Path params stand in here.
NULL_PARAMS: Dict[str, Any] = {"id": "7", "value": None}


#
# FIXTURES
#
@pytest.fixture
async def postgres_backend(running_postgres: PostgresServer) -> AsyncIterator[Any]:
    """Start a pool for the test database and give its backend.

    Args:
        running_postgres: The session server.

    Yields:
        A PostgreSQL backend, closed afterwards.
    """
    pools = PostgresPools({("shop", None): running_postgres.database_config()})
    await pools.start()
    yield pools.backend(("shop", None))
    await pools.aclose()


#
# PUBLIC
#
class TestBuilder:
    """build_sql_query() keeps None as None, in marker order."""

    def test_none_is_returned_as_none(self) -> None:
        """A None value is not turned into the text 'None'."""
        sql, values = build_sql_query(
            ROUTE, {}, path_params=NULL_PARAMS, query_params={"other": "x"}
        )
        assert sql == "SELECT ? IS NULL AS is_null, ? AS id WHERE ? IS NULL"
        assert values == [None, "7", None]

    def test_request_values_stay_text(self) -> None:
        """Path and query values are still text."""
        _, values = build_sql_query(ROUTE, {}, path_params={"id": "7", "value": "0"})
        assert values == ["0", "7"]


class TestBackends:
    """Both backends send None as SQL NULL."""

    @pytest.mark.anyio
    async def test_duckdb(self) -> None:
        """DuckDB reads None as NULL."""
        sql, values = build_sql_query(ROUTE, {}, path_params=NULL_PARAMS)
        assert await DuckDBBackend({}).execute(sql, values) == (["is_null", "id"], [(True, "7")])

    @pytest.mark.anyio
    async def test_postgres(self, postgres_backend: Any) -> None:
        """PostgreSQL reads None as NULL."""
        route = {**ROUTE, "sql": "SELECT {{value}}::text IS NULL AS is_null, {{id}}::text AS id"}
        sql, values = build_sql_query(
            route, {}, path_params=NULL_PARAMS, marker=postgres_backend.marker
        )
        assert await postgres_backend.execute(sql, values) == (["is_null", "id"], [(True, "7")])
