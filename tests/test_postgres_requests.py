"""

End-to-end tests for routes running natively on PostgreSQL.

Uses the ``postgres_server`` fixture (skipped without PostgreSQL/psycopg).
Covers native routes through the FastAPI app (pools opened in its lifespan),
bound values and literal ``%``, PostgreSQL types in JSON, read-only and
multi-statement protection, 500 vs 503 errors, the start()/aclose()
lifecycle, and Flask refusing PostgreSQL configs.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api
from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
SETUP_SQL: str = """
DROP SCHEMA IF EXISTS catalog CASCADE;
CREATE SCHEMA catalog;
CREATE TABLE catalog.items (
    id integer PRIMARY KEY, name text, price numeric(6, 2), uid uuid,
    attrs jsonb, created timestamptz, tags text[]
);
INSERT INTO catalog.items VALUES
    (1, 'Alpha owl', 3.50, '8f14e45f-ceea-467f-a8f1-9f1e2c4c6a3b', '{"a": 1}',
     '2024-05-01 12:00+00', '{x,y}'),
    (2, 'Beta frog', 10.00, NULL, NULL, NULL, '{}'),
    (3, '100% Gamma', 1.00, NULL, NULL, NULL, '{}');
"""

ROUTES: List[Dict[str, Any]] = [
    {"route": "items", "engine": "postgres",
     "sql": "SELECT [[items]].id, [[items]].name FROM [[items]]",
     "query_params": [
         {"q": {"sql": "[[items]].name ILIKE '%' || {{q}} || '%'"}},
         {"min_price": {"sql": "[[items]].price >= {{min_price}}"}},
         {"order": {"sql_append": "ORDER BY [[items]].id", "default": "id"}},
     ]},
    {"route": "items/{{id}}", "sql": "SELECT * FROM [[items]] WHERE [[items]].id = {{id}}"},
    {"route": "percent", "sql": "SELECT name FROM [[items]] WHERE name LIKE '100%%%'"},
    {"route": "delete", "sql": "DELETE FROM [[items]] WHERE id = 2 RETURNING id"},
    {"route": "broken", "sql": "SELECT no_such_column FROM [[items]]"},
]


#
# PUBLIC
#
@pytest.fixture(scope="module")
def database(postgres_server: Dict[str, str]) -> Dict[str, str]:
    """Create the test tables once per module."""
    import psycopg

    with psycopg.connect(**postgres_server, autocommit=True) as conn:
        conn.execute(SETUP_SQL)
    return postgres_server


@pytest.fixture
def client(tmp_path: Path, database: Dict[str, str]) -> Iterator[TestClient]:
    """A FastAPI test client (lifespan running) for a PostgreSQL-only config."""
    with TestClient(fast_api.create_app(_write_config(tmp_path, database))) as test_client:
        yield test_client


class TestNativeRoutes:
    """Routes whose tables are all on one connection run on PostgreSQL."""

    def test_rows(self, client: TestClient) -> None:
        assert client.get("/shop/items").json() == [
            {"id": 1, "name": "Alpha owl"}, {"id": 2, "name": "Beta frog"},
            {"id": 3, "name": "100% Gamma"},
        ]

    def test_bound_values_and_percent(self, client: TestClient) -> None:
        assert [r["id"] for r in client.get("/shop/items", params={"q": "OWL"}).json()] == [1]
        assert [r["id"] for r in client.get("/shop/items", params={"q": "100%"}).json()] == [3]
        assert [r["id"] for r in client.get("/shop/items",
                                            params={"min_price": "5"}).json()] == [2]
        assert client.get("/shop/percent").json() == [{"name": "100% Gamma"}]

    def test_injection_is_just_a_value(self, client: TestClient) -> None:
        assert client.get("/shop/items", params={"q": "' OR '1'='1"}).json() == []
        response = client.get("/shop/items", params={"min_price": "0 OR 1=1"})
        assert response.status_code == 400 and "0 OR 1=1" in response.json()["detail"]

    def test_postgres_types_as_json(self, client: TestClient) -> None:
        row = client.get("/shop/items/1").json()[0]
        assert row["price"] == 3.5
        assert row["uid"] == "8f14e45f-ceea-467f-a8f1-9f1e2c4c6a3b"
        assert row["attrs"] == {"a": 1}
        assert row["created"] == "2024-05-01T12:00:00+00:00"
        assert row["tags"] == ["x", "y"]

    def test_writes_are_refused(self, client: TestClient, database: Dict[str, str]) -> None:
        assert client.get("/shop/delete").status_code == 500
        assert len(client.get("/shop/items").json()) == 3

    def test_sql_error_is_500(self, client: TestClient) -> None:
        response = client.get("/shop/broken")
        assert (response.status_code, response.json()) == (500, {"error": "Database query error"})


class TestAvailabilityAndLifecycle:
    """503 when unreachable, start()/aclose(), Flask refusal."""

    def test_unreachable_database_is_503(self, tmp_path: Path, database: Dict[str, str]) -> None:
        unreachable = {**database, "port": "1", "pool": {"timeout": 0.5, "startup_timeout": 0.5}}
        app = fast_api.create_app(_write_config(tmp_path, unreachable))
        with TestClient(app) as test_client:
            response = test_client.get("/shop/items")
        assert (response.status_code, response.json()) == (503, {"error": "Database unavailable"})

    @pytest.mark.anyio
    async def test_start_and_aclose(self, tmp_path: Path, database: Dict[str, str]) -> None:
        mapper = RouteMapper(_write_config(tmp_path, database))
        before = await mapper.map_database_route("shop", "items")
        assert before.status_code == 500 and b"start()" in before.content
        await mapper.start()
        try:
            result = await mapper.map_database_route("shop", "items/2")
            assert json.loads(result.content)[0]["name"] == "Beta frog"
        finally:
            await mapper.aclose()
            await mapper.aclose()

    def test_missing_env_stops_startup(self, tmp_path: Path, database: Dict[str, str],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_DOCK_TEST_PG_PASSWORD", raising=False)
        config = _write_config(tmp_path, {**database, "password": "env:API_DOCK_TEST_PG_PASSWORD"})
        with pytest.raises(ValueError, match="API_DOCK_TEST_PG_PASSWORD, which is not set"):
            with TestClient(fast_api.create_app(config)):
                pass

    def test_flask_refuses_postgres(self, tmp_path: Path, database: Dict[str, str]) -> None:
        with pytest.raises(ValueError, match="need the FastAPI server"):
            flask_api.create_app(_write_config(tmp_path, database))


#
# INTERNAL
#
def _write_config(tmp_path: Path, connection: Dict[str, Any]) -> str:
    """Write a config with one PostgreSQL connection and a ``shop`` database.

    Args:
        tmp_path: Directory to write into.
        connection: The ``core`` connection entry.

    Returns:
        Path to the main config file.
    """
    config_dir = tmp_path / "api_dock_config"
    (config_dir / "databases").mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"name": "t", "databases": ["shop"]}))
    (config_dir / "databases" / "config.yaml").write_text(yaml.safe_dump({"database": {
        "connections": {"core": connection},
        "schema": {"shop_v1": {"items": {"connection": "core", "table": "catalog.items"}}},
    }}))
    (config_dir / "databases" / "shop.yaml").write_text(yaml.safe_dump({
        "name": "shop", "schema": "shop_v1", "routes": ROUTES,
    }))
    return str(config_dir / "config.yaml")
