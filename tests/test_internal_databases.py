"""

Tests for internal databases.

A database config with ``internal: true`` can't be reached over HTTP or through
the public ``RouteMapper.map_database_route()``. It answers as a database that
does not exist, and it is left out of the metadata, the name lookups and the
catalog listings. All versions of a database must agree on the setting.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.route_mapper import RouteMapper
from tests.conftest import write_config, write_yaml


#
# CONSTANTS
#
CURRENT_ROUTE: Dict[str, Any] = {
    "route": "releases/current", "sql": "SELECT 'r-1' AS release_id",
}

# Path forms after the database name, for comparing internal and unknown names.
PATH_FORMS: List[str] = [
    "", "1.0", "1.0/", "latest", "latest/releases/current",
    "1.0/releases/current", "2.0/releases/current", "1.0/nope",
]


#
# FIXTURES
#
def catalog(**changes: Any) -> Dict[str, Any]:
    """Build an internal DuckDB database config.

    Args:
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {"name": "catalog", "internal": True, "routes": [CURRENT_ROUTE]}
    config.update(changes)
    return config


def observations() -> Dict[str, Any]:
    """Build a public DuckDB database config.

    Returns:
        Database config.
    """
    return {"name": "observations", "routes": [
        {"route": "readings", "sql": "SELECT 1 AS reading"},
    ]}


def _config_path(tmp_path: Path, expose: Any = True, **catalog_changes: Any) -> str:
    """Write a config with a versioned internal ``catalog`` and a public ``observations``.

    Args:
        tmp_path: Pytest temp directory.
        expose: The main config's ``expose`` setting.
        **catalog_changes: Top-level keys to change in catalog 1.0.

    Returns:
        Path of config.yaml.
    """
    databases = {
        "catalog": {"versions": {"1.0": catalog(**catalog_changes)}},
        "observations": observations(),
    }
    return write_config(tmp_path, databases, expose=expose)


#
# PUBLIC
#
class TestHttpRequests:
    """HTTP requests to an internal database get the unknown-name response."""

    @pytest.mark.parametrize("path", PATH_FORMS)
    def test_fastapi_matches_unknown_name(self, tmp_path: Path, path: str) -> None:
        """FastAPI gives the same status and body as for a name that does not exist."""
        client = TestClient(create_fastapi_app(_config_path(tmp_path)))
        internal = client.get(f"/catalog/{path}")
        unknown = client.get(f"/missing/{path}")
        assert internal.status_code == unknown.status_code == 404
        assert internal.text.replace("'catalog'", "'missing'") == unknown.text

    @pytest.mark.parametrize("path", PATH_FORMS)
    def test_flask_matches_unknown_name(self, tmp_path: Path, path: str) -> None:
        """Flask gives the same status and body as for a name that does not exist."""
        client = create_flask_app(_config_path(tmp_path)).test_client()
        internal = client.get(f"/catalog/{path}")
        unknown = client.get(f"/missing/{path}")
        assert internal.status_code == unknown.status_code == 404
        assert internal.get_data(as_text=True).replace("'catalog'", "'missing'") == \
            unknown.get_data(as_text=True)

    @pytest.mark.parametrize("path", ["1.0/releases/current", "latest/releases/current"])
    def test_body_is_remote_not_found(self, tmp_path: Path, path: str) -> None:
        """The body is the existing unknown-remote error in both adapters."""
        config_path = _config_path(tmp_path)
        expected = {"error": "Remote 'catalog' not found"}
        fastapi_response = TestClient(create_fastapi_app(config_path)).get(f"/catalog/{path}")
        flask_response = create_flask_app(config_path).test_client().get(f"/catalog/{path}")
        assert fastapi_response.json() == expected
        assert flask_response.get_json() == expected

    def test_authentication_is_not_reached(self, tmp_path: Path) -> None:
        """An internal database with authentication still answers 404, not 401."""
        authentication = {"key": "token", "value": "secret", "encrypted": False}
        client = TestClient(create_fastapi_app(
            _config_path(tmp_path, authentication=authentication)
        ))
        response = client.get("/catalog/1.0/releases/current")
        assert (response.status_code, response.json()) == (
            404, {"error": "Remote 'catalog' not found"}
        )

    def test_public_database_still_served(self, tmp_path: Path) -> None:
        """A public database next to an internal one is served as before."""
        client = TestClient(create_fastapi_app(_config_path(tmp_path)))
        response = client.get("/observations/readings")
        assert (response.status_code, response.json()) == (200, [{"reading": 1}])


class TestDirectCall:
    """The public map_database_route() treats an internal database as unknown."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("path", PATH_FORMS)
    async def test_matches_unknown_database(self, tmp_path: Path, path: str) -> None:
        """The response is the unknown-database 404."""
        mapper = RouteMapper(_config_path(tmp_path))
        response = await mapper.map_database_route(database_name="catalog", path=path)
        assert response.status_code == 404
        assert json.loads(response.content) == {"error": "Database 'catalog' not found"}


class TestPublicNames:
    """Metadata, name lookups and listings leave out internal databases."""

    def test_metadata_and_lookups(self, tmp_path: Path) -> None:
        """get_config_metadata(), get_database_names() and is_database_name() hide it."""
        mapper = RouteMapper(_config_path(tmp_path))
        assert mapper.get_config_metadata()["remotes"] == ["observations"]
        assert mapper.get_database_names() == ["observations"]
        assert mapper.is_database_name("catalog") is False
        assert mapper.is_database_name("observations") is True

    @pytest.mark.parametrize("route", ["/", "/databases", "/sources"])
    def test_default_listings(self, tmp_path: Path, route: str) -> None:
        """The root metadata and the default listings don't show it, in both adapters."""
        config_path = _config_path(tmp_path)
        fastapi_body = TestClient(create_fastapi_app(config_path)).get(route).json()
        flask_body = create_flask_app(config_path).test_client().get(route).get_json()
        for body in (fastapi_body, flask_body):
            assert "catalog" not in json.dumps(body)
            assert "observations" in json.dumps(body)

    def test_include_treats_internal_as_unknown(self, tmp_path: Path) -> None:
        """A custom listing that names it does not show it, and warns as for an unknown name."""
        expose = {"databases": {"route": "list/all", "include": ["catalog", "observations"]}}
        mapper = RouteMapper(_config_path(tmp_path, expose=expose))
        assert mapper.get_listing(mapper.listing_specs[0]) == [
            {"model": "observations", "version": None},
        ]
        assert mapper.listing_warnings == ["expose.databases: include names unknown 'catalog'"]

    def test_no_shadow_warning(self, tmp_path: Path) -> None:
        """A listing route named after an internal database does not warn about shadowing."""
        mapper = RouteMapper(_config_path(tmp_path, expose={"databases": "catalog"}))
        assert mapper.listing_warnings == []


class TestStartupCheck:
    """The internal: setting is checked at startup."""

    @pytest.mark.parametrize("value", ["true", 1, None, [True]])
    def test_non_boolean_stops_startup(self, tmp_path: Path, value: Any) -> None:
        """Only true or false are accepted; the error names the database and version."""
        with pytest.raises(ValueError) as error:
            RouteMapper(_config_path(tmp_path, internal=value))
        message = str(error.value)
        for expected in ["catalog", "1.0", "internal"]:
            assert expected in message

    def test_versions_must_agree(self, tmp_path: Path) -> None:
        """A version that differs from the others stops startup."""
        write_yaml(tmp_path / "databases" / "catalog" / "2.0.yaml", catalog(internal=False))
        path = _config_path(tmp_path)
        with pytest.raises(ValueError) as error:
            RouteMapper(path)
        message = str(error.value)
        for expected in ["catalog", "1.0", "2.0", "internal"]:
            assert expected in message

    def test_omitted_equals_false(self, tmp_path: Path) -> None:
        """internal: false and an omitted setting are the same value."""
        public = {k: v for k, v in catalog().items() if k != "internal"}
        write_yaml(tmp_path / "databases" / "catalog" / "2.0.yaml", public)
        mapper = RouteMapper(_config_path(tmp_path, internal=False))
        assert mapper.is_database_name("catalog")

    def test_not_inherited_from_main_config(self, tmp_path: Path) -> None:
        """internal: in the main config does not make a database internal."""
        path = write_config(tmp_path, {"observations": observations()})
        main = Path(path)
        main.write_text(main.read_text() + "internal: true\n")
        mapper = RouteMapper(path)
        assert mapper.is_database_name("observations")
