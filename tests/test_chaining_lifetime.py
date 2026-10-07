"""

Config lifetime for resolvers and templated tables.

A database that uses ``resolvers:``, route ``resolve:`` or a templated table is
kept as checked at startup, with all its versions, as is its internal target.
Edits to those files take effect only in a new mapper, which runs the startup
checks again. A database read live from its files can't start to use these
settings: the request gets the restart-required error.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import copy
from pathlib import Path
from typing import Any, Dict

import pytest

from api_dock.route_mapper import RouteMapper
from tests.chaining_fixtures import (
    body,
    catalog,
    get,
    observations,
    readings_route,
    write_releases,
)
from tests.conftest import write_config, write_yaml


#
# CONSTANTS
#
READINGS_PATH: str = "2.0/projects/7/sites/42/readings"

RESTART_REQUIRED: Dict[str, str] = {"error": "Database configuration changed; restart required"}

DAILY_SQL: str = (
    "SELECT release_id, data_uri, schema_version, validator FROM [[current_release]] "
    "WHERE project = {{project}} AND dataset = 'daily'"
)


#
# FIXTURES
#
def _write(root: Path, catalog_config: Dict[str, Any],
           observations_config: Dict[str, Any]) -> str:
    """Write the releases, catalog 1.0, observations 2.0 and a live ``plain`` database.

    Args:
        root: Test folder.
        catalog_config: Catalog 1.0 config.
        observations_config: Observations 2.0 config.

    Returns:
        Path of config.yaml.
    """
    return write_config(root, {
        "catalog": {"versions": {"1.0": catalog_config}},
        "observations": {"versions": {"2.0": observations_config}},
        "plain": {"versions": {"1.0": _plain()}},
    }, expose=True)


def _plain(**changes: Any) -> Dict[str, Any]:
    """Build a DuckDB config without startup-only settings.

    Args:
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {
        "name": "plain",
        "routes": [{"route": "items/{{id}}", "sql": "SELECT {{id}} AS id"}],
    }
    config.update(changes)
    return config


def _edit(root: Path, database: str, version: str, config: Dict[str, Any]) -> None:
    """Replace one version file.

    Args:
        root: Test folder.
        database: Database name.
        version: Version.
        config: New config.
    """
    write_yaml(root / "databases" / database / f"{version}.yaml", config)


#
# PUBLIC
#
class TestSnapshots:
    """After startup, file edits have no effect until a new mapper is created."""

    @pytest.mark.anyio
    async def test_edits_have_no_effect(self, tmp_path: Path) -> None:
        """Edits to the target, the resolver, tables, headers and versions are ignored."""
        releases_csv = write_releases(tmp_path)
        config_path = _write(tmp_path, catalog(releases_csv), observations(tmp_path))
        mapper = RouteMapper(config_path)
        before = await get(mapper, "observations", READINGS_PATH)
        metadata = mapper.get_config_metadata()
        listings = [mapper.get_listing(spec) for spec in mapper.listing_specs]

        edited_route = readings_route(
            headers={"X-Edited": "yes"},
            query_params=[{"dataset": {"default": "hourly"}},
                          {"limit": {"sql_append": "LIMIT 0", "default": "0"}}],
        )
        edited = observations(tmp_path, routes=[edited_route])
        edited["tables"]["readings"]["allow"] = ["s3://elsewhere/"]
        edited["resolvers"]["current_release"]["via"] = "catalog/2.0/releases/current"
        _edit(tmp_path, "observations", "2.0", edited)
        _edit(tmp_path, "catalog", "1.0", catalog(
            releases_csv, internal=False,
            routes=[{"route": "releases/current", "sql": DAILY_SQL}],
        ))
        _edit(tmp_path, "catalog", "2.0", catalog(releases_csv))
        _edit(tmp_path, "observations", "3.0", observations(tmp_path))

        after = await get(mapper, "observations", READINGS_PATH)
        assert (after.status_code, body(after), after.headers) == (
            before.status_code, body(before), before.headers
        )
        assert mapper.get_config_metadata() == metadata
        assert [mapper.get_listing(spec) for spec in mapper.listing_specs] == listings
        assert any(listings)
        assert (await get(mapper, "catalog", "1.0/releases/current")).status_code == 404
        assert body(await get(mapper, "observations", "")) == {"versions": ["2.0"]}

    @pytest.mark.anyio
    async def test_deleted_route_still_served(self, tmp_path: Path) -> None:
        """A route deleted from the file is still served."""
        releases_csv = write_releases(tmp_path)
        mapper = RouteMapper(_write(tmp_path, catalog(releases_csv), observations(tmp_path)))
        _edit(tmp_path, "observations", "2.0", observations(tmp_path, routes=[]))
        _edit(tmp_path, "catalog", "1.0", catalog(releases_csv, routes=[]))
        assert (await get(mapper, "observations", READINGS_PATH)).status_code == 200

    @pytest.mark.anyio
    async def test_new_mapper_uses_valid_edits(self, tmp_path: Path) -> None:
        """A new mapper uses an edited target route."""
        releases_csv = write_releases(tmp_path)
        config_path = _write(tmp_path, catalog(releases_csv), observations(tmp_path))
        RouteMapper(config_path)
        _edit(tmp_path, "catalog", "1.0", catalog(
            releases_csv, routes=[{"route": "releases/current", "sql": DAILY_SQL}],
        ))
        response = await get(RouteMapper(config_path), "observations", READINGS_PATH)
        assert response.headers["X-Release-Id"] == "r-20"

    @pytest.mark.parametrize("edit", ["via", "internal", "allow"])
    def test_new_mapper_rejects_invalid_edits(self, tmp_path: Path, edit: str) -> None:
        """A new mapper runs the startup checks on the edited files."""
        releases_csv = write_releases(tmp_path)
        config_path = _write(tmp_path, catalog(releases_csv), observations(tmp_path))
        RouteMapper(config_path)
        if edit == "internal":
            _edit(tmp_path, "catalog", "1.0", catalog(releases_csv, internal=False))
        else:
            edited = observations(tmp_path)
            if edit == "via":
                edited["resolvers"]["current_release"]["via"] = "catalog/9.9/releases/current"
            else:
                edited["tables"]["readings"]["allow"] = []
            _edit(tmp_path, "observations", "2.0", edited)
        with pytest.raises(ValueError):
            RouteMapper(config_path)


class TestLiveConfigs:
    """A live DuckDB config reloads edits, but can't start to use the new settings."""

    @pytest.mark.anyio
    async def test_ordinary_edit_reloads(self, tmp_path: Path) -> None:
        """An edit without new settings takes effect at once."""
        releases_csv = write_releases(tmp_path)
        mapper = RouteMapper(_write(tmp_path, catalog(releases_csv), observations(tmp_path)))
        _edit(tmp_path, "plain", "1.0", _plain(routes=[
            {"route": "items/{{id}}", "sql": "SELECT {{id}} || '!' AS id"},
        ]))
        assert body(await get(mapper, "plain", "1.0/items/7")) == [{"id": "7!"}]

    @pytest.mark.parametrize("changes", [
        {"resolvers": {"res": {"via": "catalog/1.0/releases/current", "bind": ["release_id"]}}},
        {"resolvers": "malformed"},
        {"routes": [{"route": "items/{{id}}", "sql": "SELECT 1", "resolve": ["res"]}]},
        {"routes": [{"route": "items/{{id}}", "sql": "SELECT 1", "resolve": "malformed"}]},
        {"tables": {"t": {"uri": "{{res.u}}"}}},
        {"tables": {"t": "{{res.u}}"}},
        {"tables": {"t": {"uri": "/data/x.csv", "format": "csv"}}},
        {"tables": {"t": {"uri": "/data/x.csv", "files": 3}}},
        {"tables": {"t": {"uri": "/data/x.csv", "allow": "malformed"}}},
    ])
    @pytest.mark.anyio
    async def test_new_setting_needs_restart(
            self, tmp_path: Path, changes: Dict[str, Any]) -> None:
        """Introducing a new setting, even malformed, needs a restart."""
        releases_csv = write_releases(tmp_path)
        mapper = RouteMapper(_write(tmp_path, catalog(releases_csv), observations(tmp_path)))
        _edit(tmp_path, "plain", "1.0", _plain(**copy.deepcopy(changes)))
        response = await get(mapper, "plain", "1.0/items/7")
        assert (response.status_code, body(response)) == (500, RESTART_REQUIRED)
