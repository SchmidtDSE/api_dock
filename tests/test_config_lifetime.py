"""

Tests for how long DuckDB configs with internal: or route headers: are used.

If any version of a database uses ``internal:`` (even ``internal: false``) or
route ``headers:``, all its versions are kept as checked at startup, as for a
database with a PostgreSQL version. Requests, listings and public visibility
use these configs until a new mapper is created. A database read live from its
files can't start to use these settings: such a config gets the
restart-required error.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import duckdb
import pytest

from api_dock.route_mapper import RouteMapper
from tests.conftest import write_config, write_yaml


#
# CONSTANTS
#
RESTART_REQUIRED: Dict[str, str] = {"error": "Database configuration changed; restart required"}


#
# FIXTURES
#
def shop(sql: str = "SELECT {{id}} AS id", **changes: Any) -> Dict[str, Any]:
    """Build a DuckDB ``shop`` config with an ``items/{{id}}`` route.

    Args:
        sql: The route's SQL.
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {"name": "shop", "routes": [{"route": "items/{{id}}", "sql": sql}]}
    config.update(changes)
    return config


def with_headers(headers: Any, sql: str = "SELECT {{id}} AS id", **changes: Any) -> Dict[str, Any]:
    """Build a ``shop`` config whose route has headers.

    Args:
        headers: The route's headers: value.
        sql: The route's SQL.
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config = shop(sql, **changes)
    config["routes"][0]["headers"] = headers
    return config


def _versioned(tmp_path: Path, config: Dict[str, Any]) -> str:
    """Write ``shop`` with version 1.0 and catalog listings.

    Args:
        tmp_path: Pytest temp directory.
        config: Config of version 1.0.

    Returns:
        Path of config.yaml.
    """
    return write_config(tmp_path, {"shop": {"versions": {"1.0": config}}}, expose=True)


async def _get(mapper: RouteMapper, path: str) -> Tuple[int, Any, Dict[str, str]]:
    """Send a request to ``shop`` and decode the response.

    Args:
        mapper: Route mapper.
        path: Path after the database name.

    Returns:
        Status code, decoded JSON body and headers.
    """
    response = await mapper.map_database_route(database_name="shop", path=path)
    return response.status_code, json.loads(response.content), response.headers


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
class TestSnapshot:
    """A database with internal: or headers: keeps its startup configs."""

    @pytest.mark.anyio
    async def test_internal_false_keeps_startup_config(self, tmp_path: Path) -> None:
        """Edits, deleted routes and new version files change nothing until restart."""
        mapper = RouteMapper(_versioned(tmp_path, shop(internal=False)))
        folder = tmp_path / "databases" / "shop"
        write_yaml(folder / "1.0.yaml", {"name": "shop", "internal": False, "routes": []})
        write_yaml(folder / "2.0.yaml", shop("SELECT 'new' AS id"))

        assert await _get(mapper, "1.0/items/1") == (200, [{"id": "1"}], {})
        assert await _get(mapper, "latest/items/1") == (200, [{"id": "1"}], {})
        assert await _get(mapper, "") == (200, {"versions": ["1.0"]}, {})
        assert (await _get(mapper, "2.0/items/1"))[0] == 404
        assert _listing(mapper) == [{"model": "shop", "version": "1.0"}]

    @pytest.mark.anyio
    async def test_header_table_and_append_edits_are_ignored(self, tmp_path: Path) -> None:
        """Edits to headers, tables and sql_append have no effect until restart."""
        parquet = tmp_path / "items.parquet"
        duckdb.sql(f"COPY (SELECT 1 AS id UNION ALL SELECT 2) TO '{parquet}' (FORMAT parquet)")
        sql = "SELECT [[items]].id FROM [[items]] WHERE [[items]].id >= {{id}}"
        config = with_headers({"X-Id": "{{id}}"}, sql, tables={"items": str(parquet)})
        config["routes"][0]["query_params"] = [
            {"order": {"sql_append": "ORDER BY id {{order}}", "default": "ASC"}},
        ]
        mapper = RouteMapper(_versioned(tmp_path, config))

        changed = with_headers({"X-Id": "changed"}, sql, tables={"items": "missing.parquet"})
        changed["routes"][0]["query_params"] = [
            {"order": {"sql_append": "ORDER BY id DESC LIMIT 0", "default": "ASC"}},
        ]
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", changed)

        assert await _get(mapper, "1.0/items/1") == (200, [{"id": 1}, {"id": 2}], {"X-Id": "1"})

    def test_internal_edit_does_not_change_visibility(self, tmp_path: Path) -> None:
        """Editing internal: changes nothing until restart, in either direction."""
        databases = {"shop": shop(internal=False), "hidden": shop(internal=True)}
        path = write_config(tmp_path, databases, expose=True)
        mapper = RouteMapper(path)
        write_yaml(tmp_path / "databases" / "shop.yaml", shop(internal=True))
        write_yaml(tmp_path / "databases" / "hidden.yaml", shop(internal=False))

        assert mapper.get_database_names() == ["shop"]
        assert mapper.get_config_metadata()["remotes"] == ["shop"]
        assert _listing(mapper) == [{"model": "shop", "version": None}]

    @pytest.mark.anyio
    async def test_new_mapper_reads_valid_edits(self, tmp_path: Path) -> None:
        """A new mapper uses the edited config."""
        path = _versioned(tmp_path, with_headers({"X-Id": "{{id}}"}))
        RouteMapper(path)
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", with_headers({"X-Id": "v2"}))
        assert await _get(RouteMapper(path), "1.0/items/1") == (200, [{"id": "1"}], {"X-Id": "v2"})

    def test_new_mapper_refuses_invalid_edits(self, tmp_path: Path) -> None:
        """A new mapper runs the startup checks on the edited config."""
        path = _versioned(tmp_path, with_headers({"X-Id": "{{id}}"}))
        RouteMapper(path)
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", with_headers({"Set-Cookie": "x"}))
        with pytest.raises(ValueError, match="Set-Cookie"):
            RouteMapper(path)


class TestLiveConfig:
    """A database read live can't start to use internal: or headers:."""

    @pytest.mark.anyio
    async def test_ordinary_edits_still_reload(self, tmp_path: Path) -> None:
        """An edit without the new settings is used on the next request."""
        mapper = RouteMapper(_versioned(tmp_path, shop()))
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", shop("SELECT 'new' AS id"))
        assert await _get(mapper, "1.0/items/1") == (200, [{"id": "new"}], {})

    @pytest.mark.anyio
    @pytest.mark.parametrize("config", [
        shop(internal=True),
        shop(internal=False),
        shop(internal="maybe"),
        with_headers({"X-Id": "{{id}}"}),
        with_headers("not a mapping"),
    ], ids=["internal-true", "internal-false", "internal-malformed",
            "headers", "headers-malformed"])
    async def test_new_setting_needs_restart(self, tmp_path: Path, config: Dict[str, Any]) -> None:
        """The request, the version list and latest get the restart-required error."""
        mapper = RouteMapper(_versioned(tmp_path, shop()))
        write_yaml(tmp_path / "databases" / "shop" / "1.0.yaml", config)

        assert await _get(mapper, "1.0/items/1") == (500, RESTART_REQUIRED, {})
        assert await _get(mapper, "latest/items/1") == (500, RESTART_REQUIRED, {})
        assert await _get(mapper, "") == (500, RESTART_REQUIRED, {})
        assert mapper.get_database_names() == ["shop"]
        assert mapper.get_config_metadata()["remotes"] == ["shop"]
        assert _listing(mapper) == [{"model": "shop", "version": "1.0"}]

    @pytest.mark.anyio
    async def test_new_version_with_setting_needs_restart(self, tmp_path: Path) -> None:
        """A new version file with a new setting is refused; older versions still work."""
        mapper = RouteMapper(_versioned(tmp_path, shop()))
        write_yaml(tmp_path / "databases" / "shop" / "2.0.yaml", shop(internal=True))

        assert await _get(mapper, "2.0/items/1") == (500, RESTART_REQUIRED, {})
        assert await _get(mapper, "latest/items/1") == (500, RESTART_REQUIRED, {})
        assert await _get(mapper, "1.0/items/1") == (200, [{"id": "1"}], {})
        assert _listing(mapper) == [
            {"model": "shop", "version": "1.0"}, {"model": "shop", "version": "2.0"},
        ]

    @pytest.mark.anyio
    async def test_unversioned(self, tmp_path: Path) -> None:
        """An unversioned live config that adds headers: needs a restart."""
        mapper = RouteMapper(write_config(tmp_path, {"shop": shop()}))
        write_yaml(tmp_path / "databases" / "shop.yaml", with_headers({"X-Id": "{{id}}"}))
        assert await _get(mapper, "items/1") == (500, RESTART_REQUIRED, {})
