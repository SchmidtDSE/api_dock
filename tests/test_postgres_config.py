"""

Tests for PostgreSQL connections and tables in the config.

Covers ``database.connections`` validation and ``env:`` resolution, PostgreSQL
table definitions (``{connection, table}``, with ``connection`` from ``meta``),
choosing a route's engine (native PostgreSQL on one connection, DuckDB
otherwise, ``engine:`` checks), and the startup errors for each. No PostgreSQL
server or driver is needed.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from api_dock.database_config import load_shared_config, resolve_table_reference
from api_dock.postgres_config import (
    check_connection_config,
    check_postgres_table_name,
    connection_options,
    resolve_settings,
)
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import route_engine


#
# CONSTANTS
#
CONNECTIONS: Dict[str, Any] = {
    "core": {"host": "db.example.com", "dbname": "soundhub", "user": "reader",
             "password": "env:TEST_PG_PASSWORD", "sslmode": "verify-full"},
    "archive": {"host": "archive.example.com", "dbname": "old", "user": "reader"},
}

SHARED: Dict[str, Any] = {
    "connections": CONNECTIONS,
    "schema": {
        "core_v1": {
            "recordings": {"connection": "core", "table": "public.recordings"},
            "projects": {"connection": "core", "table": "public.projects"},
        },
        "archive_v1": {"recordings": {"connection": "archive", "table": "recordings"}},
        "files_v1": {"recordings": {"uri": "data/recordings.parquet"}},
    },
}


#
# PUBLIC
#
class TestConnectionConfig:
    """check_connection_config / resolve_settings."""

    def test_valid_entry(self) -> None:
        check_connection_config("core", {**CONNECTIONS["core"], "pool": {"max_size": 8},
                                         "statement_timeout_ms": 5000})

    @pytest.mark.parametrize("name, entry, message", [
        ("Core", {"host": "h"}, "lower case"),
        ("core", {}, "mapping"),
        ("core", {"host": "h", "options": "-c x=1"}, "can't be set"),
        ("core", {"host": "h", "password": "env:bad-name"}, "env:NAME"),
        ("core", {"host": True}, "text or an integer"),
        ("core", {"host": "h", "pool": {"max_size": 0}}, "positive integer"),
        ("core", {"host": "h", "pool": {"min_size": 5, "max_size": 2}}, "min_size"),
        ("core", {"host": "h", "pool": {"bogus": 1}}, "unknown key"),
        ("core", {"host": "h", "statement_timeout_ms": -1}, "positive integer"),
    ])
    def test_invalid_entry(self, name: str, entry: Dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            check_connection_config(name, entry)

    def test_resolve_settings_reads_env_and_defaults(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_PG_PASSWORD", "s3cret")
        settings = resolve_settings({**CONNECTIONS["core"], "pool": {"max_size": 8}})
        assert settings.fields["password"] == "s3cret"
        assert settings.fields["connect_timeout"] == "5"
        limits = (settings.max_size, settings.min_size, settings.statement_timeout_ms)
        assert limits == (8, 1, 10000)

    def test_missing_env_names_variable_not_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_PG_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="TEST_PG_PASSWORD, which is not set"):
            resolve_settings(CONNECTIONS["core"])

    @pytest.mark.parametrize("fields", [{"sslmode": "maybe"}, {"port": "99999"}, {"port": "x"}])
    def test_resolved_values_checked(self, fields: Dict[str, str]) -> None:
        with pytest.raises(ValueError):
            resolve_settings({"host": "h", **fields})

    def test_connection_options(self) -> None:
        assert connection_options(2500) == (
            "-c default_transaction_read_only=on -c statement_timeout=2500 -c TimeZone=UTC"
        )

    @pytest.mark.parametrize("table", ["recordings", "public.recordings"])
    def test_valid_table_names(self, table: str) -> None:
        check_postgres_table_name("t", table)

    @pytest.mark.parametrize("table", ["Public.Recordings", "a.b.c", "x-y", 3])
    def test_invalid_table_names(self, table: Any) -> None:
        with pytest.raises(ValueError):
            check_postgres_table_name("t", table)


class TestPostgresTables:
    """Table lookup for PostgreSQL tables."""

    def test_schema_table_has_connection(self) -> None:
        ref = resolve_table_reference("core_v1.recordings", {}, SHARED)
        assert (ref.connection, ref.uri, ref.is_postgres) == ("core", "public.recordings", True)

    def test_connection_from_meta(self) -> None:
        shared = {**SHARED, "meta": {"connection": "core"},
                  "schema": {"s": {"t": {"table": "public.t"}, "f": {"uri": "f.parquet"}}}}
        assert resolve_table_reference("s.t", {}, shared).connection == "core"
        file_ref = resolve_table_reference("s.f", {}, shared)
        assert file_ref.connection is None and "connection" not in file_ref.metadata

    def test_version_file_table(self) -> None:
        version = {"tables": {"r": {"connection": "core", "table": "public.recordings"}}}
        assert resolve_table_reference("r", version, SHARED).connection == "core"

    def test_file_tables_unchanged(self) -> None:
        ref = resolve_table_reference("files_v1.recordings", {}, SHARED)
        assert (ref.connection, ref.is_postgres) == (None, False)

    def test_connections_key_is_not_a_global_table(self) -> None:
        assert resolve_table_reference("connections", {}, SHARED) is None


class TestSharedConfigValidation:
    """load_shared_config checks connections and PostgreSQL tables."""

    @pytest.mark.parametrize("shared, message", [
        ({"connections": {"core": {}}}, "mapping"),
        ({"connections": CONNECTIONS, "schema": {"s": {"t": {"table": "x"}}}},
         "needs a 'connection'"),
        ({"connections": CONNECTIONS,
          "schema": {"s": {"t": {"connection": "nope", "table": "x"}}}},
         "unknown connection 'nope'"),
        ({"connections": CONNECTIONS,
          "schema": {"s": {"t": {"connection": "core", "table": "A.B"}}}}, "lower-case"),
        ({"connections": CONNECTIONS, "g": {"connection": "nope", "table": "x"}},
         "global tables"),
    ])
    def test_invalid(self, tmp_path: Path, shared: Dict[str, Any], message: str) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"database": shared})
        with pytest.raises(ValueError, match=message):
            load_shared_config(str(tmp_path))

    def test_valid(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"database": SHARED})
        load_shared_config(str(tmp_path))


class TestRouteEngine:
    """Which engine a route runs on."""

    GROUPS = {"all_recordings": ["core_v1", "archive_v1", "files_v1"]}

    def _engine(self, sql: Any, version: Dict[str, Any] = None, **route: Any) -> Any:
        route_config = {"route": "r", "sql": sql, **route}
        return route_engine(route_config, version or {"schema": "core_v1"}, SHARED, self.GROUPS)

    def test_all_tables_on_one_connection_is_native(self) -> None:
        sql = "SELECT * FROM [[recordings]] r JOIN [[core_v1.projects]] p ON r.p = p.id"
        assert self._engine(sql) == "core"

    def test_files_only_is_duckdb(self) -> None:
        assert self._engine("SELECT * FROM [[recordings]]", {"schema": "files_v1"}) is None

    def test_no_tables_is_duckdb(self) -> None:
        assert self._engine("SELECT 1") is None

    def test_mixed_file_and_postgres_is_duckdb(self) -> None:
        assert self._engine("SELECT * FROM [[recordings]] r, [[files_v1.recordings]] f") is None

    def test_two_connections_is_duckdb(self) -> None:
        assert self._engine("SELECT * FROM [[recordings]] r, [[archive_v1.recordings]] a") is None

    def test_union_is_duckdb(self) -> None:
        assert self._engine("SELECT * FROM [[all_recordings.recordings]] u") is None

    def test_query_params_and_branches_count(self) -> None:
        sql = [{"when": "f", "then": "SELECT * FROM [[files_v1.recordings]]"},
               {"else": "SELECT * FROM [[recordings]]"}]
        assert self._engine(sql) is None
        assert self._engine("SELECT * FROM [[recordings]]", query_params=[
            {"x": {"sql": "id IN (SELECT id FROM [[archive_v1.recordings]])"}},
        ]) is None

    def test_engine_duckdb_forces_duckdb(self) -> None:
        assert self._engine("SELECT * FROM [[recordings]]", engine="duckdb") is None

    def test_engine_postgres_asserts(self) -> None:
        assert self._engine("SELECT * FROM [[recordings]]", engine="postgres") == "core"
        with pytest.raises(ValueError, match="one PostgreSQL connection"):
            self._engine("SELECT * FROM [[all_recordings.recordings]] u", engine="postgres")

    def test_unknown_engine(self) -> None:
        with pytest.raises(ValueError, match="engine must be one of"):
            self._engine("SELECT 1", engine="sqlite")

    def test_named_query_tables_count(self) -> None:
        version = {"schema": "core_v1", "queries": {"q": "SELECT * FROM [[files_v1.recordings]]"}}
        assert self._engine("[[q]]", version) is None


class TestStartup:
    """Startup errors for PostgreSQL configs."""

    def _start(self, tmp_path: Path, routes: List[Dict[str, Any]],
               tables: Dict[str, Any] = None) -> RouteMapper:
        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["db"]})
        _write_yaml(config_dir / "databases" / "config.yaml", {"database": SHARED})
        _write_yaml(config_dir / "databases" / "db.yaml",
                    {"name": "db", "schema": "core_v1", "tables": tables or {}, "routes": routes})
        return RouteMapper(str(config_dir / "config.yaml"))

    def test_valid_postgres_routes_start(self, tmp_path: Path) -> None:
        self._start(tmp_path, [
            {"route": "recordings", "sql": "SELECT * FROM [[recordings]]", "engine": "postgres"},
            {"route": "mixed", "sql": "SELECT * FROM [[recordings]] r, [[files_v1.recordings]] f"},
        ])

    def test_engine_mismatch_stops_startup(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="route 'mixed'.*one PostgreSQL connection"):
            self._start(tmp_path, [{
                "route": "mixed", "engine": "postgres",
                "sql": "SELECT * FROM [[recordings]] r, [[files_v1.recordings]] f",
            }])

    def test_bad_version_table_stops_startup(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="tables: table 'x' uses unknown connection"):
            self._start(tmp_path, [{"route": "r", "sql": "SELECT 1"}],
                        tables={"x": {"connection": "nope", "table": "x"}})


#
# INTERNAL
#
def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write ``data`` as YAML to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
