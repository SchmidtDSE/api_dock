"""

Tests for starting and closing PostgreSQL connection pools.

``RouteMapper.start()`` opens one pool per PostgreSQL database version and
``aclose()`` closes them. The FastAPI app does both in its lifespan. Using a
PostgreSQL route before start(), after aclose() or from another event loop is an
error. Flask rejects configs with PostgreSQL databases.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask

from api_dock import postgres_backend, postgres_pools
from api_dock.cli import cli
from api_dock.database_backends import DatabaseLifecycleError
from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.route_mapper import RouteMapper
from tests.conftest import PostgresServer, write_config
from tests.test_postgres_requests import duckdb_shop, postgres_shop


#
# CONSTANTS
#
FLASK_ERROR: str = (
    "PostgreSQL requires the FastAPI server; use --backbone fastapi, or use RouteMapper "
    "in an async application with start()/aclose()."
)


#
# FIXTURES
#
@pytest.fixture
def created_pools(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Record every pool created.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        List that each new pool is added to.
    """
    pools: List[Any] = []
    real_create_pool = postgres_backend.create_pool

    def create_pool(*args: Any, **kwargs: Any) -> Any:
        pool = real_create_pool(*args, **kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(postgres_backend, "create_pool", create_pool)
    return pools


async def _query(mapper: RouteMapper, path: str = "items/1", database: str = "shop") -> Any:
    """Send one database request.

    Args:
        mapper: Route mapper.
        path: Route path.
        database: Database name.

    Returns:
        The response.
    """
    return await mapper.map_database_route(database_name=database, path=path)


#
# PUBLIC
#
class TestStandaloneLifecycle:
    """start() and aclose() on one event loop."""

    @pytest.mark.anyio
    async def test_start_query_close(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any]) -> None:
        """Repeated start() keeps one pool; requests share it; repeated aclose() is safe."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        assert created_pools == []
        await mapper.start()
        await mapper.start()
        assert len(created_pools) == 1
        for _ in range(3):
            assert (await _query(mapper)).status_code == 200
        await mapper.aclose()
        await mapper.aclose()
        assert created_pools[0].closed

    @pytest.mark.anyio
    async def test_query_before_start(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any]) -> None:
        """A PostgreSQL route before start() is a clear error, and no pool is opened."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        with pytest.raises(DatabaseLifecycleError, match="start()"):
            await _query(mapper)
        assert created_pools == []

    @pytest.mark.anyio
    async def test_after_close(self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """After aclose(), requests and a new start() are errors."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        await mapper.start()
        await mapper.aclose()
        with pytest.raises(DatabaseLifecycleError, match="closed"):
            await _query(mapper)
        with pytest.raises(DatabaseLifecycleError, match="new RouteMapper"):
            await mapper.start()

    @pytest.mark.anyio
    async def test_other_event_loop(self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """A request from another event loop is an error, raised before checkout."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        await mapper.start()
        try:
            with pytest.raises(DatabaseLifecycleError, match="event loop"):
                await asyncio.to_thread(asyncio.run, _query(mapper))
        finally:
            await mapper.aclose()

    @pytest.mark.anyio
    async def test_duckdb_needs_no_start(self, tmp_path: Path) -> None:
        """DuckDB-only mappers serve requests without start()."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": duckdb_shop(tmp_path)}))
        assert (await _query(mapper)).status_code == 200

    @pytest.mark.anyio
    async def test_versions_get_own_pools_and_latest_shares(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any]) -> None:
        """Each version has its own pool; /latest uses the latest version's pool."""
        config = postgres_shop(running_postgres)
        databases = {"shop": {"versions": {"1.0": config, "2.0": config}}}
        mapper = RouteMapper(write_config(tmp_path, databases))
        await mapper.start()
        try:
            assert len(created_pools) == 2
            latest_pool = [pool for pool in created_pools if pool.name == "shop/2.0"][0]
            before = latest_pool.get_stats().get("requests_num", 0)
            assert (await _query(mapper, "latest/items/1")).status_code == 200
            assert (await _query(mapper, "2.0/items/1")).status_code == 200
            assert latest_pool.get_stats().get("requests_num", 0) == before + 2
        finally:
            await mapper.aclose()


class TestStartupErrors:
    """Configuration and dependency errors stop start(); no pool is left open."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("connection", [{"sslmode": "verfy-full"}, {"port": "abc"}])
    async def test_invalid_values_stop_startup_before_pool_creation(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            connection: Dict[str, str]) -> None:
        """Validate every database before opening even the first valid pool."""
        def unexpected_pool(*args: Any, **kwargs: Any) -> None:
            pytest.fail("Pool created before connection values were validated")

        monkeypatch.setattr(postgres_backend, "create_pool", unexpected_pool)
        mapper = RouteMapper(write_config(tmp_path, {
            "good": {"backend": "postgres", "connection": {}, "routes": []},
            "bad": {"backend": "postgres", "connection": connection, "routes": []},
        }))
        with pytest.raises(ValueError, match="Database 'bad'.*" + next(iter(connection))):
            await mapper.start()

    @pytest.mark.anyio
    async def test_settings_checked_for_all_databases_before_any_pool(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any]) -> None:
        """A bad option in the second database stops start() before any pool is created."""
        bad = postgres_shop(running_postgres)
        bad["connection"] = {**bad["connection"], "autocommit": "true"}
        mapper = RouteMapper(write_config(
            tmp_path, {"good": postgres_shop(running_postgres), "bad": bad}
        ))
        with pytest.raises(ValueError, match="Database 'bad'.*autocommit"):
            await mapper.start()
        assert created_pools == []

    @pytest.mark.anyio
    async def test_missing_environment_variable(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing env: variable stops start(), naming the database and variable."""
        monkeypatch.delenv("SHOP_DB_PASSWORD", raising=False)
        config = postgres_shop(running_postgres)
        config["connection"] = {**config["connection"], "password": "env:SHOP_DB_PASSWORD"}
        mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
        with pytest.raises(ValueError, match="Database 'shop'.*SHOP_DB_PASSWORD"):
            await mapper.start()

    @pytest.mark.anyio
    async def test_password_from_environment(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """The password is read from the environment when the mapper starts."""
        monkeypatch.setenv("SHOP_DB_PASSWORD", "reader_password")
        config = postgres_shop(running_postgres)
        config["connection"] = {**config["connection"], "password": "env:SHOP_DB_PASSWORD"}
        mapper = RouteMapper(write_config(tmp_path, {"shop": config}))
        await mapper.start()
        try:
            assert (await _query(mapper)).status_code == 200
        finally:
            await mapper.aclose()

    @pytest.mark.anyio
    async def test_failure_while_opening_closes_created_pools(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch, created_pools: List[Any]) -> None:
        """If opening the second pool fails, the first is closed and the mapper is closed."""
        recording_create_pool = postgres_backend.create_pool

        def create_pool(*args: Any, **kwargs: Any) -> Any:
            if created_pools:
                raise RuntimeError("cannot create pool")
            return recording_create_pool(*args, **kwargs)

        monkeypatch.setattr(postgres_backend, "create_pool", create_pool)
        databases = {"a": postgres_shop(running_postgres), "b": postgres_shop(running_postgres)}
        mapper = RouteMapper(write_config(tmp_path, databases))
        with pytest.raises(RuntimeError, match="cannot create pool"):
            await mapper.start()
        assert len(created_pools) == 1 and created_pools[0].closed
        with pytest.raises(DatabaseLifecycleError):
            await _query(mapper, database="a")

    @pytest.mark.anyio
    async def test_missing_dependencies(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """Without psycopg_pool, start() says to install api_dock[postgres]."""
        monkeypatch.delitem(sys.modules, "api_dock.postgres_backend")
        monkeypatch.setitem(sys.modules, "psycopg_pool", None)
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        with pytest.raises(RuntimeError, match=r"api_dock\[postgres\].*psycopg_pool"):
            await mapper.start()

    @pytest.mark.anyio
    async def test_missing_libpq(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """If psycopg finds no libpq, start() says to install api_dock[postgres]."""
        def no_libpq(name: str) -> None:
            raise ImportError("no pq wrapper available")

        monkeypatch.setattr(postgres_pools, "importlib", SimpleNamespace(import_module=no_libpq))
        mapper = RouteMapper(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        with pytest.raises(RuntimeError, match=r"api_dock\[postgres\].*psycopg found no libpq"):
            await mapper.start()


class TestFastAPI:
    """The FastAPI app starts and closes the mapper in its lifespan."""

    def test_lifespan_opens_pools_once_and_closes_them(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any]) -> None:
        """Creating the app opens nothing; the lifespan opens one pool and closes it."""
        app = create_fastapi_app(write_config(tmp_path, {"shop": postgres_shop(running_postgres)}))
        assert created_pools == []
        with TestClient(app) as client:
            for _ in range(3):
                response = client.get("/shop/items/2")
                assert response.json() == [{"id": 2, "name": "O'Brien"}]
            assert len(created_pools) == 1
        assert created_pools[0].closed

    def test_mounted_app_with_parent_lifespan(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """A mounted app works when the parent starts and closes its mapper."""
        path = write_config(tmp_path, {"shop": postgres_shop(running_postgres)})
        child = create_fastapi_app(path)

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            await child.state.route_mapper.start()
            yield
            await child.state.route_mapper.aclose()

        parent = FastAPI(lifespan=lifespan)
        parent.mount("/api", child)
        with TestClient(parent) as client:
            assert client.get("/api/shop/items/1").status_code == 200

    def test_mounted_app_without_parent_lifespan(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """Without the parent's lifespan, a PostgreSQL route reports the lifecycle error."""
        path = write_config(tmp_path, {"shop": postgres_shop(running_postgres)})
        child = create_fastapi_app(path)
        parent = FastAPI()
        parent.mount("/api", child)
        with TestClient(parent) as client:
            with pytest.raises(DatabaseLifecycleError, match="start()"):
                client.get("/api/shop/items/1")


class TestFlask:
    """Flask serves DuckDB and remote configs, and rejects PostgreSQL configs."""

    @pytest.mark.parametrize("databases, label", [
        ({"shop": "postgres"}, "Database 'shop'"),
        ({"shop": {"versions": {"1.0": "duckdb", "2.0": "postgres"}}},
         "Database 'shop' version '2.0'"),
    ])
    def test_factory_rejects_postgres(
            self, tmp_path: Path, running_postgres: PostgresServer,
            created_pools: List[Any], databases: Dict[str, Any], label: str) -> None:
        """Any PostgreSQL version, including in a mixed database, stops app creation."""
        path = write_config(tmp_path, _configs(databases, tmp_path, running_postgres))
        with pytest.raises(RuntimeError) as error:
            create_flask_app(path)
        assert label in str(error.value)
        assert FLASK_ERROR in str(error.value)
        assert created_pools == []

    def test_duckdb_still_served(self, tmp_path: Path) -> None:
        """A DuckDB-only config works with Flask."""
        app = create_flask_app(write_config(tmp_path, {"shop": duckdb_shop(tmp_path)}))
        response = app.test_client().get("/shop/items/1")
        assert response.get_json() == [{"id": 1, "name": "apple"}]

    def test_cli_backbone_flask(
            self, tmp_path: Path, running_postgres: PostgresServer,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """api-dock start --backbone flask fails before serving, naming the database."""
        config_dir = tmp_path / "api_dock_config"
        write_config(config_dir, {"shop": postgres_shop(running_postgres)})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Flask, "run", lambda *args, **kwargs: None)
        result = CliRunner().invoke(cli, ["start", "--backbone", "flask"])
        assert result.exit_code == 1
        assert "Database 'shop'" in result.output
        assert "use --backbone fastapi" in result.output


class TestWithoutDriver:
    """Configs are checked, and Flask rejects PostgreSQL, without psycopg installed."""

    def test_construction_and_flask_check_need_no_driver(
            self, tmp_path: Path, running_postgres: PostgresServer) -> None:
        """With psycopg blocked, construction works, Flask rejects, start() asks for the extra."""
        path = write_config(tmp_path, {"shop": postgres_shop(running_postgres)})
        code = textwrap.dedent(f"""
            import asyncio, sys
            sys.modules["psycopg"] = None
            sys.modules["psycopg_pool"] = None
            from api_dock import RouteMapper, create_fastapi_app, create_flask_app

            create_fastapi_app({path!r})
            try:
                create_flask_app({path!r})
            except RuntimeError as error:
                assert "use --backbone fastapi" in str(error), error
            else:
                raise AssertionError("Flask accepted a PostgreSQL config")
            try:
                asyncio.run(RouteMapper({path!r}).start())
            except RuntimeError as error:
                assert "api_dock[postgres]" in str(error), error
            else:
                raise AssertionError("start() worked without psycopg")
            assert "psycopg" not in [name for name, module in sys.modules.items() if module]
        """)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                timeout=60)
        assert result.returncode == 0, result.stderr


#
# INTERNAL
#
def _configs(
        databases: Dict[str, Any], root: Path, server: PostgresServer) -> Dict[str, Any]:
    """Replace "postgres" and "duckdb" placeholders with database configs.

    Args:
        databases: Database names to a placeholder, or to versions of placeholders.
        root: Folder for DuckDB Parquet files.
        server: The test server.

    Returns:
        Databases mapping for write_config.
    """
    def build(kind: str) -> Dict[str, Any]:
        return postgres_shop(server) if kind == "postgres" else duckdb_shop(root)

    return {
        name: {"versions": {v: build(k) for v, k in value["versions"].items()}}
        if isinstance(value, dict) else build(value)
        for name, value in databases.items()
    }
