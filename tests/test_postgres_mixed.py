"""

End-to-end tests for queries mixing PostgreSQL tables with files (and with
tables on other connections), which run on DuckDB.

Uses the ``postgres_server`` fixture (skipped without PostgreSQL/psycopg). Two
connection names point at the same test server so cross-connection queries can
be tested. Covers ``[[*.table]]`` / group unions across PostgreSQL schemas and
Parquet, ``source_columns`` and ``{{self.*}}``, joins of PostgreSQL and Parquet
tables, query params on mixed queries, and 503 when an attached connection is
unreachable.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any, Dict, Iterator, List

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api
from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
SETUP_SQL: str = """
DROP SCHEMA IF EXISTS mixed CASCADE;
CREATE SCHEMA mixed;
CREATE TABLE mixed.dets_a (id text, recording_id int, start_time float8, end_time float8,
                           common_name text, confidence float8);
CREATE TABLE mixed.dets_b (id text, recording_id int, start_time float8, end_time float8,
                           confidence float8, label text);
INSERT INTO mixed.dets_a VALUES ('a1', 1, 0, 3, 'Owl', 0.9), ('a2', 2, 0, 3, 'Frog', 0.2);
INSERT INTO mixed.dets_b VALUES ('b1', 1, 2, 5, 0.7, 'L1'), ('b2', 1, 10, 13, 0.4, 'L2');
"""

OVERLAPS_SQL: str = """
WITH src AS (
  SELECT recording_id, start_time, end_time FROM [[detections]] WHERE id = {{id}}
)
SELECT d.id, d.schema_name, d.name FROM [[*.detections]] d
JOIN src ON d.recording_id = src.recording_id
        AND d.start_time < src.end_time AND d.end_time > src.start_time
WHERE NOT (d.schema_name = {{self.schema}} AND d.id = {{id}})
"""


#
# PUBLIC
#
@pytest.fixture(scope="module")
def database(postgres_server: Dict[str, str]) -> Dict[str, str]:
    """Create the PostgreSQL tables once per module."""
    import psycopg

    with psycopg.connect(**postgres_server, autocommit=True) as conn:
        conn.execute(SETUP_SQL)
    return postgres_server


@pytest.fixture
def config_path(tmp_path: Path, database: Dict[str, str]) -> str:
    """Write the mixed-source config (PostgreSQL on two connections plus Parquet)."""
    return _write_config(tmp_path, database)


@pytest.fixture
def client(config_path: str) -> Iterator[TestClient]:
    """A FastAPI test client with its lifespan running."""
    with TestClient(fast_api.create_app(config_path)) as test_client:
        yield test_client


class TestMixedQueries:
    """Queries that mix sources run on DuckDB with PostgreSQL attached."""

    def test_union_across_postgres_schemas_and_parquet(self, client: TestClient) -> None:
        rows = client.get("/models/1.0/all").json()
        assert sorted((r["schema_name"], r["id"]) for r in rows) == [
            ("model_a", "a1"), ("model_a", "a2"), ("model_b", "b1"), ("model_b", "b2"),
            ("model_c", "c1"),
        ]
        by_id = {r["id"]: r for r in rows}
        assert by_id["a1"]["common_name"] == "Owl" and by_id["a1"]["label"] is None
        assert by_id["b1"]["label"] == "L1" and by_id["b1"]["common_name"] is None
        # name: the database whose version uses the schema (model_c is used by none)
        assert by_id["a1"]["name"] == "models" and by_id["c1"]["name"] is None

    def test_query_params_on_union(self, client: TestClient) -> None:
        rows = client.get("/models/1.0/all", params={"confidence": "0.5"}).json()
        assert sorted(r["id"] for r in rows) == ["a1", "b1", "c1"]

    def test_overlaps_across_sources(self, client: TestClient) -> None:
        rows = client.get("/models/1.0/detections/a1/overlaps").json()
        assert sorted((r["schema_name"], r["id"]) for r in rows) == [
            ("model_b", "b1"), ("model_c", "c1"),
        ]

    def test_group_union_on_two_connections(self, client: TestClient) -> None:
        rows = client.get("/models/1.0/postgres").json()
        assert sorted(r["id"] for r in rows) == ["a1", "a2", "b1", "b2"]

    def test_join_postgres_and_parquet(self, client: TestClient) -> None:
        assert client.get("/models/1.0/joined").json() == [{"id": "a1", "parquet_id": "c1"}]

    def test_single_connection_route_is_native(self, client: TestClient) -> None:
        assert [r["id"] for r in client.get("/models/1.0/detections/a2").json()] == ["a2"]

    def test_unreachable_attached_connection_is_503(
            self, tmp_path: Path, database: Dict[str, str]) -> None:
        unreachable = {**database, "port": "1", "connect_timeout": 1,
                       "pool": {"timeout": 0.5, "startup_timeout": 0.5}}
        config = _write_config(tmp_path, database, second=unreachable)
        with TestClient(fast_api.create_app(config)) as test_client:
            response = test_client.get("/models/1.0/all")
        assert (response.status_code, response.json()) == (503, {"error": "Database unavailable"})

    def test_engines_chosen_per_route(self, config_path: str) -> None:
        mapper = RouteMapper(config_path)
        assert mapper.duckdb_backend.connections.keys() == {"core", "core2"}


#
# INTERNAL
#
def _write_config(tmp_path: Path, database: Dict[str, str],
                  second: Dict[str, Any] = None) -> str:
    """Write a config: model_a on ``core``, model_b on ``core2``, model_c Parquet.

    Args:
        tmp_path: Directory to write into.
        database: Connection fields for the test server.
        second: Entry for the ``core2`` connection (default: the test server).

    Returns:
        Path to the main config file.
    """
    parquet = tmp_path / "c.parquet"
    duckdb.sql(
        "COPY (SELECT 'c1' AS id, 1 AS recording_id, 1.0 AS start_time, 4.0 AS end_time, "
        f"'Heron' AS common_name, 0.8 AS confidence) TO '{parquet}'"
    )
    config_dir = tmp_path / "api_dock_config"
    (config_dir / "databases").mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"name": "t", "databases": ["models"]}))
    routes: List[Dict[str, Any]] = [
        {"route": "all", "source_columns": ["schema", "name"],
         "sql": "SELECT u.* FROM [[*.detections]] u",
         "query_params": [{"confidence": {"sql": "u.confidence >= {{confidence}}"}}]},
        {"route": "detections/{{id}}/overlaps", "source_columns": ["schema", "name"],
         "sql": OVERLAPS_SQL},
        {"route": "postgres", "sql": "SELECT p.id FROM [[postgres_models.detections]] p"},
        {"route": "joined",
         "sql": "SELECT [[detections]].id, c.id AS parquet_id FROM [[detections]] "
                "JOIN [[model_c.detections]] c ON [[detections]].recording_id = c.recording_id "
                "WHERE [[detections]].confidence > 0.5"},
        {"route": "detections/{{id}}", "engine": "postgres",
         "sql": "SELECT [[detections]].id FROM [[detections]] WHERE [[detections]].id = {{id}}"},
    ]
    (config_dir / "databases" / "config.yaml").write_text(yaml.safe_dump({
        "database": {
            "connections": {"core": database, "core2": second or database},
            "schema": {
                "model_a": {"detections": {"connection": "core", "table": "mixed.dets_a"}},
                "model_b": {"detections": {"connection": "core2", "table": "mixed.dets_b"}},
                "model_c": {"detections": str(parquet)},
            },
        },
        "schema_groups": {"postgres_models": ["model_a", "model_b"]},
    }))
    (config_dir / "databases" / "models").mkdir(exist_ok=True)
    (config_dir / "databases" / "models" / "1.0.yaml").write_text(yaml.safe_dump({
        "name": "models", "schema": "model_a", "routes": routes,
    }))
    return str(config_dir / "config.yaml")
