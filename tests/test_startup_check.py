"""

Tests for the database config check that runs when a RouteMapper is created.

Every database listed in the main config, and every version of a versioned
database, is loaded and each route is checked for shape, for variables in
quotes or comments, and for templates that end inside a comment. A failure
stops startup with an error naming the database, version,
route and reason. Config files are read from the main config's directory.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pytest
import yaml

from api_dock.config_discovery import find_config
from api_dock.database_config import validate_route_config
from api_dock.route_mapper import RouteMapper
from api_dock.types import PreparedRequest


#
# CONSTANTS
#
VALID_ROUTE: Dict[str, Any] = {
    "route": "items",
    "sql": "SELECT * FROM [[items]]",
    "query_params": [{"name": {"sql": "UPPER([[items]].name) = UPPER({{name}})"}}],
}


#
# PUBLIC
#
class TestValidateRouteConfig:
    """validate_route_config raises with a reason instead of returning False."""

    def test_valid_route_passes(self) -> None:
        """A well-formed route raises nothing."""
        assert validate_route_config(VALID_ROUTE) is None

    def test_multivalue_only_param_is_accepted(self) -> None:
        """multivalue_sql alone is a recognized param key."""
        route = {"route": "items", "query_params": [{"id": {"multivalue_sql": "id IN {{id}}"}}]}
        assert validate_route_config(route) is None

    def test_missing_route_key_reports_reason(self) -> None:
        """A route without route: is rejected with a reason naming the key."""
        with pytest.raises(ValueError, match="route"):
            validate_route_config({"sql": "SELECT 1"})

    def test_param_without_known_key_reports_param(self) -> None:
        """A param with no recognized key is rejected naming the param."""
        with pytest.raises(ValueError, match="bogus_param"):
            validate_route_config({"route": "items", "query_params": [{"bogus_param": {"x": 1}}]})


class TestStartupQuoteCheck:
    """A quoted variable in any bound template stops startup."""

    def test_error_names_database_route_location_and_fix(self, tmp_path: Path) -> None:
        """The error has the database, version, route, location and fixed template."""
        route = _route(query_params=[
            {"name": {"sql": "UPPER([[items]].name) LIKE UPPER('%{{name}}%')"}},
        ])
        config_path = _write_config(tmp_path, {"catalog": {"1.0": _database([route])}})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        message = str(error.value)
        for expected in ["catalog", "1.0", "items", "query_params.name.sql",
                         "UPPER([[items]].name) LIKE UPPER('%' || {{name}} || '%')"]:
            assert expected in message

    @pytest.mark.parametrize("route_kwargs, location", [
        ({"sql": "SELECT * FROM [[items]] WHERE a LIKE '{{a}}%'"}, "sql"),
        ({"query_params": [{"id": {
            "sql": "id = {{id}}", "multivalue_sql": "id IN 'x{{id}}'",
        }}]}, "query_params.id.multivalue_sql"),
        ({"query_params": [{"mode": {"conditional": {
            "on": {"sql": "flag = '{{mode}}!'"},
        }}}]}, "query_params.mode.conditional.on.sql"),
    ])
    def test_each_template_location_is_checked(
            self, tmp_path: Path, route_kwargs: Dict[str, Any], location: str) -> None:
        """Route sql, multivalue_sql and conditional sql are all checked."""
        config_path = _write_config(tmp_path, {"catalog": _database([_route(**route_kwargs)])})
        with pytest.raises(ValueError, match=location.replace(".", r"\.")):
            RouteMapper(config_path)

    def test_selector_branch_sql_is_checked(self, tmp_path: Path) -> None:
        """A conditional-selection branch's sql is checked."""
        route = _route(sql=[
            {"when": "a", "then": {"sql": "SELECT * FROM [[items]] WHERE a LIKE '{{a}}%'"}},
            {"else": "SELECT * FROM [[items]]"},
        ])
        config_path = _write_config(tmp_path, {"catalog": _database([route])})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        assert "WHERE a LIKE {{a}} || '%'" in str(error.value)

    def test_named_query_is_checked(self, tmp_path: Path) -> None:
        """A queries: entry is checked."""
        database = _database([_route(sql="[[by_a]]")])
        database["queries"] = {"by_a": "SELECT * FROM [[items]] WHERE a LIKE '%{{a}}'"}
        config_path = _write_config(tmp_path, {"catalog": database})
        with pytest.raises(ValueError, match=r"queries\.by_a"):
            RouteMapper(config_path)

    def test_top_level_query_param_names_route(self, tmp_path: Path) -> None:
        """A quoted top-level query_params entry is reported for a route it merges into."""
        database = _database([_route()])
        database["query_params"] = [{"name": {"sql": "name LIKE '%{{name}}%'"}}]
        config_path = _write_config(tmp_path, {"catalog": database})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        assert "items" in str(error.value)
        assert "name LIKE '%' || {{name}} || '%'" in str(error.value)

    def test_every_version_is_checked(self, tmp_path: Path) -> None:
        """An older version with a quoted variable stops startup."""
        bad = _route(sql="SELECT * FROM [[items]] WHERE a LIKE '%{{a}}%'")
        config_path = _write_config(tmp_path, {"catalog": {
            "1.0": _database([bad]), "2.0": _database([_route()]),
        }})
        with pytest.raises(ValueError, match=r"1\.0"):
            RouteMapper(config_path)

    def test_sql_append_is_not_checked(self, tmp_path: Path) -> None:
        """sql_append values are not bound, so their templates are not quote-checked."""
        route = _route(query_params=[{"sort": {"sql_append": "ORDER BY '{{sort}}'"}}])
        config_path = _write_config(tmp_path, {"catalog": _database([route])})
        assert RouteMapper(config_path).database_names == ["catalog"]

    def test_valid_config_loads(self, tmp_path: Path) -> None:
        """A config without quoted variables starts."""
        config_path = _write_config(tmp_path, {"catalog": _database([VALID_ROUTE])})
        assert RouteMapper(config_path).database_names == ["catalog"]


class TestStartupCommentCheck:
    """A variable in a comment, or a template ending in a comment, stops startup."""

    def test_commented_variable_names_database_route_and_location(
            self, tmp_path: Path) -> None:
        """The error names where the variable is and suggests no rewrite."""
        route = _route(query_params=[
            {"name": {"sql": "[[items]].name = {{name}} /* was '{{name}}' */"}},
        ])
        config_path = _write_config(tmp_path, {"catalog": {"1.0": _database([route])}})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        message = str(error.value)
        for expected in ["catalog", "1.0", "items", "query_params.name.sql", "comment"]:
            assert expected in message
        assert "Use:" not in message

    @pytest.mark.parametrize("route_kwargs, location", [
        ({"sql": "SELECT * FROM [[items]] -- all rows\n"}, "sql"),
        ({"query_params": [{"sort": {"sql_append": "ORDER BY id -- newest first"}}]},
         "query_params.sort.sql_append"),
        ({"sql": [{"else": {
            "sql": "SELECT * FROM [[items]]", "sql_append": "ORDER BY id -- by id",
        }}]}, "sql[0].else.sql_append"),
    ])
    def test_template_ending_in_comment_names_location(
            self, tmp_path: Path, route_kwargs: Dict[str, Any], location: str) -> None:
        """Route sql, sql_append and a branch's sql_append may not end in a comment."""
        config_path = _write_config(tmp_path, {"catalog": _database([_route(**route_kwargs)])})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        message = str(error.value)
        assert f"{location}:" in message
        assert "own line" in message

    def test_named_query_ending_in_comment(self, tmp_path: Path) -> None:
        """A queries: entry may not end in a comment."""
        database = _database([_route(sql="[[all_items]]")])
        database["queries"] = {"all_items": "SELECT * FROM [[items]] -- every row"}
        config_path = _write_config(tmp_path, {"catalog": database})
        with pytest.raises(ValueError, match=r"queries\.all_items"):
            RouteMapper(config_path)

    def test_commented_variable_in_sql_append_is_allowed(self, tmp_path: Path) -> None:
        """sql_append values are written into the SQL, so a commented one is harmless."""
        route = _route(query_params=[
            {"sort": {"sql_append": "/* by {{sort}} */ ORDER BY {{sort}}"}},
        ])
        config_path = _write_config(tmp_path, {"catalog": _database([route])})
        assert RouteMapper(config_path).database_names == ["catalog"]


class TestStartupShapeCheck:
    """A malformed route or missing database file stops startup."""

    def test_route_without_route_key(self, tmp_path: Path) -> None:
        """The error names the database and the reason."""
        config_path = _write_config(tmp_path, {"catalog": _database([{"sql": "SELECT 1"}])})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        assert "catalog" in str(error.value)
        assert "route" in str(error.value)

    def test_param_without_known_key(self, tmp_path: Path) -> None:
        """The error names the database, route and param."""
        route = _route(query_params=[{"bogus_param": {"x": 1}}])
        config_path = _write_config(tmp_path, {"catalog": _database([route])})
        with pytest.raises(ValueError) as error:
            RouteMapper(config_path)
        for expected in ["catalog", "items", "bogus_param"]:
            assert expected in str(error.value)

    def test_listed_database_without_file(self, tmp_path: Path) -> None:
        """A database listed in the main config with no file stops startup."""
        config_path = _write_config(tmp_path, {}, listed=["ghost_db"])
        with pytest.raises(ValueError, match="ghost_db"):
            RouteMapper(config_path)

    def test_unknown_version_schema_stops_startup(self, tmp_path: Path) -> None:
        """A version's `schema:` must name a schema in databases/config.yaml."""
        (tmp_path / "databases").mkdir()
        (tmp_path / "databases" / "config.yaml").write_text(
            "database:\n  schema:\n    real: {t: s3://b/t.parquet}\n"
        )
        (tmp_path / "databases" / "db.yaml").write_text(
            "schema: typo\nroutes:\n  - route: r\n    sql: SELECT 1\n"
        )
        (tmp_path / "config.yaml").write_text("name: x\ndatabases: [db]\n")
        with pytest.raises(ValueError, match="schema: 'typo' isn't a schema"):
            RouteMapper(str(tmp_path / "config.yaml"))

    def test_missing_main_config_stops_startup(self, tmp_path: Path) -> None:
        """An explicitly given main config that doesn't exist is an error (not an empty API)."""
        with pytest.raises(ValueError, match="Main config not found"):
            RouteMapper(str(tmp_path / "missing.yaml"))

    @pytest.mark.parametrize("text, message", [
        ("name: [unclosed", "isn't valid YAML"),
        ("- a\n- b\n", "must be a mapping"),
    ])
    def test_unreadable_main_config_stops_startup(
            self, tmp_path: Path, text: str, message: str) -> None:
        """A main config that isn't a YAML mapping stops startup with its path."""
        path = tmp_path / "config.yaml"
        path.write_text(text)
        with pytest.raises(ValueError, match=message):
            RouteMapper(str(path))


class TestConfigDirectory:
    """Remote and database files are read from the main config's directory."""

    @pytest.mark.anyio
    async def test_config_path_outside_working_directory(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Database and remote routes are served when run from another directory."""
        items = tmp_path / "items.parquet"
        duckdb.sql(f"COPY (SELECT 1 AS id, 'Alpha' AS name) TO '{items}' (FORMAT parquet)")
        database = {**_database([VALID_ROUTE]), "tables": {"items": str(items)}}
        config_path = _write_config(
            tmp_path / "project", {"catalog": {"1.0": database}},
            remotes={"svc_file": {"name": "svc", "url": "https://svc.test"}},
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        route_mapper = RouteMapper(config_path)
        result = await route_mapper.map_database_route(
            "catalog", "latest/items", {"name": "alpha"}, {}
        )
        assert result.status_code == 200
        assert json.loads(result.content) == [{"id": 1, "name": "Alpha"}]

        assert route_mapper.remote_names == ["svc"]
        prepared = await route_mapper.prepare_remote_request("svc", "things", "GET")
        assert isinstance(prepared, PreparedRequest)
        assert prepared.url.startswith("https://svc.test/things")

    @pytest.mark.anyio
    async def test_remote_restrictions_apply_outside_working_directory(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A remote's own restricted routes are blocked when its file is in a custom folder."""
        remote = {"name": "svc", "url": "https://svc.test", "restricted": ["admin"]}
        config_path = _write_config(tmp_path / "project", {}, remotes={"svc_file": remote})
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        route_mapper = RouteMapper(config_path)
        blocked = await route_mapper.prepare_remote_request("svc", "admin", "GET")
        assert blocked.status_code == 403
        allowed = await route_mapper.prepare_remote_request("svc", "things", "GET")
        assert isinstance(allowed, PreparedRequest)

    @pytest.mark.anyio
    async def test_bundled_config_serves_example_db(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no local config, the bundled example_db passes the check and is served."""
        monkeypatch.chdir(tmp_path)
        route_mapper = RouteMapper(find_config())
        result = await route_mapper.map_database_route("example_db", "", {}, {})
        assert result.status_code == 200
        assert "items" in json.loads(result.content)["routes"]


class TestStartupSharedConfig:
    """Versions are checked as requests see them: with shared routes and schemas."""

    SHARED: Dict[str, Any] = {
        "database": {"schema": {
            "a_1p0": {"items": "data/a.parquet", "revisions": "data/rev.parquet"},
            "b_1p0": {"items": "data/b.parquet"},
        }},
        "schema_groups": {"ab": ["a_1p0", "b_1p0"]},
        "slugs": [
            {"name": "a", "version": "1.0", "schema": "a_1p0"},
            {"name": "b", "version": "1.0", "schema": "b_1p0"},
        ],
    }

    def _start(self, tmp_path: Path, shared: Dict[str, Any]) -> RouteMapper:
        """Write a config whose databases all come from the shared file, then start."""
        config_path = _write_config(tmp_path, {}, listed=["a", "b"])
        _write_yaml(Path(config_path).parent / "databases" / "config.yaml", shared)
        return RouteMapper(config_path)

    def test_valid_shared_setup_starts(self, tmp_path: Path) -> None:
        """Schemas, unions, groups, source_columns and {{self.*}} pass the check."""
        self._start(tmp_path, {**self.SHARED, "routes": [
            {"route": "items/{{id}}", "sql": "SELECT * FROM [[items]] WHERE id = '{{id}}'"},
            {"route": "items/{{id}}/others", "source_columns": ["schema", "name"],
             "sql": "SELECT i.* FROM [[*.items]] i "
                    "WHERE NOT (i.schema_name = {{self.schema}} AND i.id = {{id}})"},
            {"route": "group", "sql": "SELECT * FROM [[ab!.items]] g"},
        ]})

    def test_shared_route_is_checked_for_each_version(self, tmp_path: Path) -> None:
        """A bad template in a shared route stops startup, naming database and version."""
        with pytest.raises(ValueError) as error:
            self._start(tmp_path, {**self.SHARED, "routes": [
                {"route": "items", "sql": "SELECT * FROM [[items]] WHERE name LIKE '%{{q}}%'"},
            ]})
        for expected in ["Database 'a'", "version '1.0'", "items", "'%' || {{q}} || '%'"]:
            assert expected in str(error.value)

    def test_unknown_table_stops_startup(self, tmp_path: Path) -> None:
        """A [[table]] that resolves for no source is reported at startup."""
        with pytest.raises(ValueError, match="Table 'nope' not found"):
            self._start(tmp_path, {**self.SHARED, "routes": [
                {"route": "x", "sql": "SELECT * FROM [[nope]]"},
            ]})

    def test_table_only_required_where_route_is_included(self, tmp_path: Path) -> None:
        """A route using a table only one version has is fine when included only there."""
        route = {"route": "revisions", "sql": "SELECT * FROM [[revisions]]"}
        self._start(tmp_path, {**self.SHARED, "routes": [{**route, "include": ["a/1.0"]}]})
        with pytest.raises(ValueError, match="Database 'b'.*revisions"):
            self._start(tmp_path / "again", {**self.SHARED, "routes": [route]})

    @pytest.mark.parametrize("sql, message", [
        ("SELECT [[*.items]].id FROM [[*.items]] i", "only be used after FROM/JOIN"),
        ("SELECT * FROM [[a_1p0!.items]] i", "only applies to"),
        ("SELECT * FROM [[nogroup.items]] i", "not found"),
    ])
    def test_bad_union_stops_startup(self, tmp_path: Path, sql: str, message: str) -> None:
        """Misused unions and unknown schema groups are reported at startup."""
        with pytest.raises(ValueError, match=message):
            self._start(tmp_path, {**self.SHARED, "routes": [{"route": "u", "sql": sql}]})

    def test_bad_source_columns_stops_startup(self, tmp_path: Path) -> None:
        """An unknown source_columns fact is reported at startup."""
        with pytest.raises(ValueError, match="unknown fact 'bogus'"):
            self._start(tmp_path, {**self.SHARED, "routes": [
                {"route": "u", "source_columns": ["bogus"], "sql": "SELECT * FROM [[*.items]] i"},
            ]})

    def test_bad_shared_file_stops_startup(self, tmp_path: Path) -> None:
        """An invalid shared config (here a group naming an unknown schema) stops startup."""
        with pytest.raises(ValueError, match="Shared database config"):
            self._start(tmp_path, {**self.SHARED, "schema_groups": {"g": ["missing"]}})


#
# INTERNAL
#
def _route(
        sql: Any = "SELECT * FROM [[items]]",
        query_params: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build an items route.

    Args:
        sql: Route sql (string or selector list).
        query_params: Route query_params, if any.

    Returns:
        Route configuration dictionary.
    """
    route: Dict[str, Any] = {"route": "items", "sql": sql}
    if query_params is not None:
        route["query_params"] = query_params
    return route


def _database(routes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a database config with an items table.

    Args:
        routes: Route configurations.

    Returns:
        Database configuration dictionary.
    """
    return {"tables": {"items": "data/items.parquet"}, "routes": routes}


def _write_config(
        root: Path,
        databases: Dict[str, Dict[str, Any]],
        remotes: Optional[Dict[str, Dict[str, Any]]] = None,
        listed: Optional[List[str]] = None) -> str:
    """Write a main config plus database and remote files under root/api_dock_config.

    A database value whose keys are all versions (e.g. ``"1.0"``) is written as
    a versioned directory; any other value is written as a single file.

    Args:
        root: Directory to write into.
        databases: Database name to config, or to a version-to-config mapping.
        remotes: Remote file name to remote config.
        listed: Database names for the main config; defaults to databases' keys.

    Returns:
        Path to the main config file.
    """
    remotes = remotes or {}
    config_dir = root / "api_dock_config"
    (config_dir / "databases").mkdir(parents=True)
    (config_dir / "remotes").mkdir()
    for name, database in databases.items():
        if all(key[0].isdigit() for key in database):
            (config_dir / "databases" / name).mkdir()
            for version, versioned in database.items():
                _write_yaml(config_dir / "databases" / name / f"{version}.yaml", versioned)
        else:
            _write_yaml(config_dir / "databases" / f"{name}.yaml", database)
    for filename, remote in remotes.items():
        _write_yaml(config_dir / "remotes" / f"{filename}.yaml", remote)
    main = {
        "name": "test",
        "databases": listed if listed is not None else list(databases),
        "remotes": list(remotes),
    }
    _write_yaml(config_dir / "config.yaml", main)
    return str(config_dir / "config.yaml")


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write data to path as YAML.

    Args:
        path: File to write.
        data: Data to serialize.
    """
    path.write_text(yaml.safe_dump(data, sort_keys=False))
