"""

Tests for how long database configs are used once loaded.

A database with any PostgreSQL version is loaded once, when the route mapper is
created: its versions, configs and /latest stay fixed until a new mapper is
created, and catalog listings report the same versions. Databases with only
DuckDB versions are still read from their files on each request, but a file
that now uses PostgreSQL is refused with a restart-required error.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import pytest
from fastapi.testclient import TestClient

from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.route_mapper import RouteMapper
from tests.conftest import PostgresServer, write_config, write_yaml
from tests.test_postgres_requests import duckdb_shop, postgres_shop


#
# CONSTANTS
#
RESTART_REQUIRED: Dict[str, str] = {"error": "Database configuration changed; restart required"}


#
# FIXTURES
#
@pytest.fixture
async def mixed(tmp_path: Path, running_postgres: PostgresServer) -> AsyncIterator[RouteMapper]:
    """Start a mapper for ``shop`` with DuckDB version 1.0 and PostgreSQL version 2.0.

    Args:
        tmp_path: Pytest temp directory.
        running_postgres: The session server.

    Yields:
        A started mapper, closed afterwards.
    """
    databases = {"shop": {"versions": {
        "1.0": duckdb_shop(tmp_path), "2.0": postgres_shop(running_postgres),
    }}}
    mapper = RouteMapper(write_config(tmp_path, databases, expose=True))
    await mapper.start()
    yield mapper
    await mapper.aclose()


async def _get(mapper: RouteMapper, path: str, database: str = "shop") -> Any:
    """Send a database request and decode the response.

    Args:
        mapper: Route mapper.
        path: Path after the database name.
        database: Database name.

    Returns:
        Tuple of status code and decoded JSON body.
    """
    response = await mapper.map_database_route(database_name=database, path=path)
    return response.status_code, json.loads(response.content)


def _listing(mapper: RouteMapper) -> List[Any]:
    """Get the /databases catalog listing.

    Args:
        mapper: Route mapper with ``expose: true``.

    Returns:
        Listing rows.
    """
    spec = [spec for spec in mapper.listing_specs if spec.kind == "databases"][0]
    return mapper.get_listing(spec)


#
# PUBLIC
#
class TestSnapshottedDatabase:
    """A database with a PostgreSQL version keeps its startup config."""

    @pytest.mark.anyio
    async def test_both_backends_serve(self, mixed: RouteMapper) -> None:
        """Each version uses its own backend; /latest is the PostgreSQL version."""
        assert await _get(mixed, "1.0/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert await _get(mixed, "2.0/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert await _get(mixed, "") == (200, {"versions": ["1.0", "2.0"]})

    @pytest.mark.anyio
    async def test_file_changes_are_ignored(self, mixed: RouteMapper, tmp_path: Path) -> None:
        """Added, edited and deleted version files change nothing until restart."""
        folder = tmp_path / "databases" / "shop"
        write_yaml(folder / "3.0.yaml", duckdb_shop(tmp_path))
        write_yaml(folder / "2.0.yaml", {"name": "shop", "routes": []})
        (folder / "1.0.yaml").unlink()

        assert await _get(mixed, "") == (200, {"versions": ["1.0", "2.0"]})
        assert await _get(mixed, "latest/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert await _get(mixed, "1.0/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert (await _get(mixed, "3.0/items/1"))[0] == 404
        assert _listing(mixed) == [
            {"model": "shop", "version": "1.0"}, {"model": "shop", "version": "2.0"},
        ]

    @pytest.mark.anyio
    async def test_connection_and_environment_changes_are_ignored(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """Connection settings and env: values are read once, at start()."""
        monkeypatch.setenv("SHOP_DB_PASSWORD", "reader_password")
        config = postgres_shop(running_postgres)
        config["connection"] = {**config["connection"], "password": "env:SHOP_DB_PASSWORD"}
        mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
        await mapper.start()
        try:
            monkeypatch.setenv("SHOP_DB_PASSWORD", "wrong")
            changed = {**config, "connection": {**config["connection"], "port": "1"},
                       "tables": {"items": "catalog.missing"}}
            write_yaml(tmp_path / "databases" / "shop.yaml", changed)
            assert await _get(mapper, "items/1") == (200, [{"id": 1, "name": "apple"}])
        finally:
            await mapper.aclose()


class TestDuckDBOnlyDatabase:
    """A DuckDB-only database is read live, but can't switch to PostgreSQL."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["latest", "latest/items/1"])
    async def test_latest_rejects_new_older_postgres_version(
            self, tmp_path: Path, path: str) -> None:
        """Latest scans reject PostgreSQL additions even when DuckDB stays latest."""
        databases = {"shop": {"versions": {"2.0": duckdb_shop(tmp_path)}}}
        mapper = RouteMapper(write_config(tmp_path, databases, expose=True))
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", {
            "backend": "postgres", "connection": {}, "routes": [],
        })
        assert await _get(mapper, path) == (500, RESTART_REQUIRED)
        assert await _get(mapper, "2.0/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert _listing(mapper) == [{"model": "shop", "version": "2.0"}]

    @pytest.mark.anyio
    async def test_unreadable_version_file_does_not_need_restart(self, tmp_path: Path) -> None:
        """A version file that fails to load is not a PostgreSQL change; latest still works."""
        databases = {"shop": {"versions": {"2.0": duckdb_shop(tmp_path)}}}
        mapper = RouteMapper(write_config(tmp_path, databases))
        (tmp_path / "databases" / "shop" / "1.0.yaml").write_text("routes: [\n")
        assert await _get(mapper, "latest/items/1") == (200, [{"id": 1, "name": "apple"}])

    @pytest.mark.anyio
    async def test_switch_to_postgres_needs_restart(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """Editing an unversioned DuckDB config to PostgreSQL is refused before running SQL."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": duckdb_shop(tmp_path)}))
        assert (await _get(mapper, "items/1"))[0] == 200
        write_yaml(tmp_path / "databases" / "shop.yaml", postgres_shop(running_postgres))
        assert await _get(mapper, "items/1") == (500, RESTART_REQUIRED)

    @pytest.mark.anyio
    async def test_new_postgres_version_needs_restart(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """A PostgreSQL version added while running is refused, as are scans that find it."""
        databases = {"shop": {"versions": {"1.0": duckdb_shop(tmp_path)}}}
        mapper = RouteMapper(write_config(tmp_path, databases, expose=True))
        write_yaml(tmp_path / "databases" / "shop" / "2.0.yaml", postgres_shop(running_postgres))

        assert await _get(mapper, "2.0/items/1") == (500, RESTART_REQUIRED)
        assert await _get(mapper, "latest/items/1") == (500, RESTART_REQUIRED)
        assert await _get(mapper, "") == (500, RESTART_REQUIRED)
        assert await _get(mapper, "1.0/items/1") == (200, [{"id": 1, "name": "apple"}])
        assert _listing(mapper) == [{"model": "shop", "version": "1.0"}]

    def test_listing_keeps_other_databases(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """A changed database doesn't stop the listing of the others."""
        databases = {"shop": duckdb_shop(tmp_path), "other": duckdb_shop(tmp_path)}
        mapper = RouteMapper(write_config(tmp_path, databases, expose=True))
        write_yaml(tmp_path / "databases" / "shop.yaml", postgres_shop(running_postgres))
        assert _listing(mapper) == [{"model": "other", "version": None}]

    def test_guard_in_both_apps(self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """FastAPI and Flask both refuse a switch to PostgreSQL."""
        path = write_config(tmp_path, {"shop": duckdb_shop(tmp_path)})
        fastapi_client = TestClient(create_fastapi_app(path))
        flask_client = create_flask_app(path).test_client()
        write_yaml(tmp_path / "databases" / "shop.yaml", postgres_shop(running_postgres))
        fastapi_response = fastapi_client.get("/shop/items/1")
        flask_response = flask_client.get("/shop/items/1")
        assert (fastapi_response.status_code, fastapi_response.json()) == (500, RESTART_REQUIRED)
        assert (flask_response.status_code, flask_response.get_json()) == (500, RESTART_REQUIRED)
