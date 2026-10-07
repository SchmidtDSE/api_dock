"""

Resolver tests with PostgreSQL.

The same resolver config gives the same result with an internal DuckDB target
and an internal PostgreSQL target. A PostgreSQL target that is down gives 503.
A public PostgreSQL database can use a resolver, and a NULL value is bound as
SQL NULL there too.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from api_dock.route_mapper import RouteMapper
from tests.chaining_fixtures import (
    CATALOG_SQL,
    UNAVAILABLE,
    body,
    catalog,
    chain,
    default_releases,
    get,
    observations,
    write_releases,
)
from tests.conftest import PostgresServer, write_config


#
# CONSTANTS
#
READINGS_PATH: str = "2.0/projects/7/sites/42/readings"


#
# FIXTURES
#
@pytest.fixture
def releases_table(running_postgres: PostgresServer, tmp_path: Path) -> PostgresServer:
    """Create ``catalog.releases`` with the default releases of this test's folder.

    Args:
        running_postgres: The session server.
        tmp_path: Pytest temp directory.

    Returns:
        The server.
    """
    rows = default_releases(tmp_path)
    values = ", ".join(
        "('{project}', '{dataset}', '{release_id}', '{data_uri}', 3, '{validator}')".format(**row)
        for row in rows
    )
    running_postgres.admin_query(
        "DROP TABLE IF EXISTS catalog.releases; "
        "CREATE TABLE catalog.releases (project TEXT, dataset TEXT, release_id TEXT, "
        "data_uri TEXT, schema_version INTEGER, validator TEXT); "
        f"INSERT INTO catalog.releases VALUES {values}; "
        "GRANT SELECT ON catalog.releases TO reader;"
    )
    return running_postgres


def _postgres_catalog(server: PostgresServer, **changes: Any) -> Dict[str, Any]:
    """Build an internal PostgreSQL catalog with the same route as the DuckDB one.

    Args:
        server: The test server.
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config = server.database_config(
        name="catalog", internal=True, tables={"current_release": "catalog.releases"},
        routes=[{"route": "releases/current", "sql": CATALOG_SQL}],
    )
    config.update(changes)
    return config


async def _readings(config_path: str) -> Any:
    """Start a mapper, request the readings and close the mapper.

    Args:
        config_path: Path of config.yaml.

    Returns:
        Tuple of status, sorted rows and headers.
    """
    mapper = RouteMapper(config_path)
    await mapper.start()
    try:
        response = await get(mapper, "observations", READINGS_PATH)
    finally:
        await mapper.aclose()
    rows: List[Dict[str, Any]] = body(response)
    return response.status_code, sorted(rows, key=lambda row: row["value"]), response.headers


#
# PUBLIC
#
class TestPostgresTarget:
    """A PostgreSQL target answers the resolver as a DuckDB target does."""

    @pytest.mark.anyio
    async def test_same_result_as_duckdb(
            self, tmp_path: Path, releases_table: PostgresServer) -> None:
        """The same resolver config gives the same response with either target."""
        releases_csv = write_releases(tmp_path)
        duckdb_path = write_config(tmp_path / "duckdb", {
            "catalog": {"versions": {"1.0": catalog(releases_csv)}},
            "observations": {"versions": {"2.0": observations(tmp_path)}},
        })
        postgres_path = write_config(tmp_path / "postgres", {
            "catalog": {"versions": {"1.0": _postgres_catalog(releases_table)}},
            "observations": {"versions": {"2.0": observations(tmp_path)}},
        })
        duckdb_result = await _readings(duckdb_path)
        postgres_result = await _readings(postgres_path)
        assert duckdb_result[0] == 200
        assert duckdb_result[2] == {"ETag": '"r-19:abc"', "X-Release-Id": "r-19"}
        assert postgres_result == duckdb_result

    @pytest.mark.anyio
    async def test_target_down(
            self, tmp_path: Path, releases_table: PostgresServer,
            caplog: pytest.LogCaptureFixture) -> None:
        """A PostgreSQL target that is down gives 503; startup continues."""
        write_releases(tmp_path)
        target = _postgres_catalog(releases_table, pool={"timeout": 0.5, "startup_timeout": 0.5})
        config_path = write_config(tmp_path, {
            "catalog": {"versions": {"1.0": target}},
            "observations": {"versions": {"2.0": observations(tmp_path)}},
        })
        releases_table.stop()
        mapper = RouteMapper(config_path)
        try:
            with caplog.at_level(logging.WARNING, logger="api_dock"):
                await mapper.start()
                response = await get(mapper, "observations", READINGS_PATH)
            assert (response.status_code, body(response)) == (503, UNAVAILABLE)
            assert response.headers == {}
            assert "reader_password" not in caplog.text
            assert "resolver 'current_release'" in caplog.text
        finally:
            await mapper.aclose()


class TestPostgresRoute:
    """A public PostgreSQL route can use a resolver value."""

    @pytest.mark.parametrize("target_sql, expected", [
        ("SELECT NULL AS v", [{"is_null": True, "v": None}]),
        ("SELECT 'O''Brien' AS v", [{"is_null": False, "v": "O'Brien"}]),
    ])
    @pytest.mark.anyio
    async def test_value_is_bound(
            self, tmp_path: Path, running_postgres: PostgresServer, target_sql: str,
            expected: List[Dict[str, Any]]) -> None:
        """The value is bound on PostgreSQL, and NULL is sent as NULL."""
        shop = running_postgres.database_config()
        del shop["routes"]
        config_path = chain(
            tmp_path, target_sql, ["v"],
            "SELECT {{res.v}}::text IS NULL AS is_null, {{res.v}}::text AS v",
            shop=shop,
        )
        mapper = RouteMapper(config_path)
        await mapper.start()
        try:
            assert body(await get(mapper, "shop", "1.0/items/1")) == expected
        finally:
            await mapper.aclose()
