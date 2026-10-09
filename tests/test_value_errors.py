"""

Tests for request values that can't be converted to a column's type

A value that can't be read as the type it's compared with (e.g. a timestamp
for a number column) is the caller's mistake: 400 with the database's first
message line. A conversion error caused by data in a table (no request value
involved) stays a 500. Checked on DuckDB (Parquet) and native PostgreSQL.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import duckdb
import pytest
import yaml

from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
TIMESTAMP: str = "2024-01-01T00:00:00.000Z"


#
# PUBLIC
#
class TestDuckDB:
    """Parquet tables queried by DuckDB."""

    def test_timestamp_for_a_number_column(self, tmp_path: Path) -> None:
        response = _get(_mapper(tmp_path), "detections", {"start_time": TIMESTAMP})
        assert response.status_code == 400
        body = json.loads(response.content)
        assert body["error"] == "Invalid value for a query parameter"
        assert TIMESTAMP in body["detail"] and "parquet" not in body["detail"]

    def test_valid_number(self, tmp_path: Path) -> None:
        response = _get(_mapper(tmp_path), "detections", {"start_time": "3"})
        assert response.status_code == 200
        assert [row["id"] for row in json.loads(response.content)] == ["b"]

    def test_bad_table_data_is_still_a_server_error(self, tmp_path: Path) -> None:
        response = _get(_mapper(tmp_path), "bad_cast", {})
        assert response.status_code == 500
        assert json.loads(response.content) == {"error": "Database query error"}


class TestPostgres:
    """Native PostgreSQL routes (postgres_server fixture)."""

    def test_timestamp_for_a_number_column(self, tmp_path: Path,
                                           postgres_server: Dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(**postgres_server, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS value_check; "
                         "CREATE TABLE value_check (id text, start_time double precision); "
                         "INSERT INTO value_check VALUES ('a', 0), ('b', 3)")
        _write(tmp_path / "databases" / "config.yaml", {"database": {
            "connections": {"core": dict(postgres_server)},
            "vc": {"connection": "core", "table": "public.value_check"},
        }})
        _write(tmp_path / "databases" / "db.yaml", {"routes": [{
            "route": "rows", "sql": "SELECT * FROM [[vc]]",
            "query_params": [{"start_time": {"sql": "vc.start_time >= {{start_time}}"}}],
        }]})
        _write(tmp_path / "config.yaml", {"name": "x", "databases": ["db"]})
        mapper = RouteMapper(str(tmp_path / "config.yaml"))

        async def run() -> Any:
            await mapper.start()
            try:
                return await mapper.map_database_route("db", "rows", {"start_time": TIMESTAMP}, {})
            finally:
                await mapper.aclose()
        response = asyncio.run(run())
        assert response.status_code == 400
        assert TIMESTAMP in json.loads(response.content)["detail"]


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _mapper(root: Path) -> RouteMapper:
    duckdb.sql("COPY (SELECT * FROM (VALUES ('a', 0.0, 'x'), ('b', 3.0, 'y')) "
               "t(id, start_time, label)) "
               f"TO '{root / 'd.parquet'}' (FORMAT parquet)")
    _write(root / "databases" / "db.yaml", {
        "tables": {"d": str(root / "d.parquet")},
        "routes": [
            {"route": "detections", "sql": "SELECT * FROM [[d]]",
             "query_params": [{"start_time": {"sql": "d.start_time >= {{start_time}}"}}]},
            {"route": "bad_cast", "sql": "SELECT CAST(label AS DOUBLE) AS v FROM [[d]]"},
        ],
    })
    _write(root / "config.yaml", {"name": "x", "databases": ["db"]})
    return RouteMapper(str(root / "config.yaml"))


def _get(mapper: RouteMapper, path: str, params: Dict[str, str]) -> Any:
    return asyncio.run(mapper.map_database_route("db", path, params, {}))
