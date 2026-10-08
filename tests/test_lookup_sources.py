"""

Tests for running lookups: SQL (Parquet, PostgreSQL, mixed) and HTTP

SQL lookups run on DuckDB over the shared config's tables, attaching
PostgreSQL read-only (``postgres_server`` fixture; skipped without
PostgreSQL/psycopg). HTTP lookups use the ``http_server`` fixture.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any, Dict, List, Tuple

import duckdb
import pytest
import yaml

from api_dock.lookups import (
    load_lookup_specs,
    LookupContext,
    LookupFailure,
    run_lookup,
)


#
# CONSTANTS
#
RUNS: List[Tuple[str, str, str]] = [("birdnet", "2.4", "r1"), ("perch", "8.0", "r2")]
PG_SETUP: str = """
DROP SCHEMA IF EXISTS lookup_catalog CASCADE;
CREATE SCHEMA lookup_catalog;
CREATE TABLE lookup_catalog.runs (model text, version text, run_id text, published boolean);
INSERT INTO lookup_catalog.runs VALUES
  ('birdnet', '2.4', 'r1', true), ('perch', '8.0', 'r2', true), ('owl', '1.0', 'r3', false);
"""


#
# PUBLIC
#
class TestSqlLookups:
    """SQL lookups over file tables."""

    def test_parquet(self, tmp_path: Path) -> None:
        _catalog(tmp_path)
        _shared(tmp_path, {"runs": {"uri": str(tmp_path / "runs.parquet")}},
                "SELECT model, version, run_id FROM [[runs]] ORDER BY model")
        assert _run(tmp_path) == [
            {"model": "birdnet", "version": "2.4", "run_id": "r1"},
            {"model": "perch", "version": "8.0", "run_id": "r2"},
        ]

    def test_schema_table_and_types(self, tmp_path: Path) -> None:
        _catalog(tmp_path)
        _shared(tmp_path, {"schema": {"cat": {"runs": str(tmp_path / "runs.parquet")}}},
                "SELECT COUNT(*) AS n, DATE '2026-01-02' AS d FROM [[cat.runs]]")
        assert _run(tmp_path) == [{"n": 2, "d": "2026-01-02"}]

    def test_missing_table_fails(self, tmp_path: Path) -> None:
        _shared(tmp_path, {}, "SELECT * FROM [[nope]]")
        with pytest.raises(ValueError, match="not found"):
            _run(tmp_path)

    def test_bad_sql_fails(self, tmp_path: Path) -> None:
        _catalog(tmp_path)
        _shared(tmp_path, {"runs": str(tmp_path / "runs.parquet")}, "SELEC model FROM [[runs]]")
        with pytest.raises(Exception):
            _run(tmp_path)


class TestPostgresLookups:
    """SQL lookups over PostgreSQL tables (attached read-only to DuckDB)."""

    @pytest.fixture
    def pg(self, postgres_server: Dict[str, str]) -> Dict[str, str]:
        import psycopg

        with psycopg.connect(**postgres_server, autocommit=True) as conn:
            conn.execute(PG_SETUP)
        return postgres_server

    def test_postgres_table(self, tmp_path: Path, pg: Dict[str, str]) -> None:
        _shared(tmp_path, {
            "connections": {"core": dict(pg)},
            "schema": {"cat": {"runs": {"connection": "core", "table": "lookup_catalog.runs"}}},
        }, "SELECT model, version FROM [[cat.runs]] WHERE published ORDER BY model")
        assert _run(tmp_path) == [
            {"model": "birdnet", "version": "2.4"}, {"model": "perch", "version": "8.0"},
        ]

    def test_postgres_joined_with_parquet(self, tmp_path: Path, pg: Dict[str, str]) -> None:
        _catalog(tmp_path)
        _shared(tmp_path, {
            "connections": {"core": dict(pg)},
            "pg_runs": {"connection": "core", "table": "lookup_catalog.runs"},
            "files": str(tmp_path / "runs.parquet"),
        }, "SELECT pg_runs.model, files.run_id FROM [[pg_runs]] JOIN [[files]] "
           "ON files.model = pg_runs.model ORDER BY pg_runs.model")
        assert _run(tmp_path) == [
            {"model": "birdnet", "run_id": "r1"}, {"model": "perch", "run_id": "r2"},
        ]


class TestHttpLookups:
    """HTTP lookups."""

    def test_url_and_rows_path(self, tmp_path: Path, http_server: Any) -> None:
        http_server.body = {"data": {"items": [{"version": "1.0"}, {"version": "2.0"}]}}
        _remote_lookups(tmp_path, {"x": {"url": http_server.url, "path": "deployments",
                                         "params": {"live": "1"}, "rows": "data.items"}})
        assert _run(tmp_path, "x") == [{"version": "1.0"}, {"version": "2.0"}]
        assert http_server.requests[-1]["path"] == "/deployments?live=1"

    def test_object_is_one_row(self, tmp_path: Path, http_server: Any) -> None:
        http_server.body = {"current": {"uri": "s3://b/x"}}
        _remote_lookups(tmp_path, {"x": {"url": http_server.url, "rows": "current"}})
        assert _run(tmp_path, "x") == [{"uri": "s3://b/x"}]

    def test_remote_with_cookies_and_env_header(
            self, tmp_path: Path, http_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOKUP_TEST_TOKEN", "t0k")
        monkeypatch.setenv("LOOKUP_TEST_COOKIE", "c00kie")
        http_server.body = [{"version": "1.0"}]
        _write(tmp_path / "remotes" / "core.yaml", {
            "name": "core", "url": http_server.url + "/api",
            "cookies": [{"key": "session", "value": "env:LOOKUP_TEST_COOKIE"}],
        })
        _remote_lookups(tmp_path, {"x": {"remote": "core", "path": "/runs",
                                         "headers": {"Authorization": "env:LOOKUP_TEST_TOKEN"}}})
        assert _run(tmp_path, "x", main={"remotes": ["core"]}) == [{"version": "1.0"}]
        request = http_server.requests[-1]
        assert request["path"] == "/api/runs"
        assert request["headers"]["Authorization"] == "t0k"
        assert request["headers"]["Cookie"] == "session=c00kie"

    @pytest.mark.parametrize("status, body, rows, message", [
        (500, {"error": "x"}, None, "HTTP 500"),
        (200, "not json", None, "didn't return JSON"),
        (200, {"a": 1, "b": 2}, "missing", "'missing' not found"),
        (200, [1, 2], None, "isn't a list of objects"),
    ])
    def test_errors(self, tmp_path: Path, http_server: Any, status: int, body: Any,
                    rows: Any, message: str) -> None:
        http_server.status, http_server.body = status, body
        lookup = {"url": http_server.url}
        if rows:
            lookup["rows"] = rows
        _remote_lookups(tmp_path, {"x": lookup})
        with pytest.raises(LookupFailure, match=message):
            _run(tmp_path, "x")

    def test_unset_header_env(self, tmp_path: Path, http_server: Any) -> None:
        _remote_lookups(tmp_path, {"x": {"url": http_server.url,
                                         "headers": {"X": "env:API_DOCK_TEST_UNSET_VAR"}}})
        with pytest.raises(LookupFailure, match="not set"):
            _run(tmp_path, "x")


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _catalog(root: Path) -> None:
    values = ", ".join(f"('{m}', '{v}', '{r}')" for m, v, r in RUNS)
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {values}) t(model, version, run_id)) "
               f"TO '{root / 'runs.parquet'}' (FORMAT parquet)")


def _shared(root: Path, database: Dict[str, Any], sql: str) -> None:
    _write(root / "databases" / "config.yaml",
           {"database": database, "lookups": {"runs": {"sql": sql}}})


def _remote_lookups(root: Path, lookups: Dict[str, Any]) -> None:
    _write(root / "remotes" / "config.yaml", {"lookups": lookups})


def _run(root: Path, name: str = "runs", main: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    specs = load_lookup_specs(str(root))
    return run_lookup(specs[name], LookupContext(str(root), main or {}))
